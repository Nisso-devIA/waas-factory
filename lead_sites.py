#!/usr/bin/env python3
"""
lead_sites.py — Prospection d'artisans bien notés SANS site web + génération
d'un aperçu de site (landing page Tailwind) par prospect, avec bandeau
d'activation Stripe (abonnement 49 €/mois).
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import logging
import os
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger("lead_sites")

SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
FIELD_MASK = ",".join(
    [
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.nationalPhoneNumber",
        "places.internationalPhoneNumber",
        "places.websiteUri",
        "places.rating",
        "places.userRatingCount",
        "places.reviews",
        "places.regularOpeningHours.weekdayDescriptions",
        "places.googleMapsUri",
        "nextPageToken",
    ]
)
MAX_PAGES = 3

DEFAULT_TOWNS = [
    "Cergy", "Pontoise", "Osny", "Argenteuil", "Sarcelles", "Garges-lès-Gonesse",
    "Franconville", "Ermont", "Bezons", "Herblay-sur-Seine", "Goussainville",
    "Saint-Ouen-l'Aumône", "Taverny", "Montmorency", "Gonesse", "Villiers-le-Bel",
]
DEFAULT_TRADE = "plombier chauffagiste"

DIRECTORY_AND_SOCIAL_DOMAINS = (
    "facebook.com", "fb.com", "instagram.com", "pagesjaunes.fr",
    "societe.com", "pappers.fr", "linkedin.com", "mappy.com", "yelp.fr",
)

# 1. Formes juridiques insensibles à la casse + "& fils" / "& cie" (sans \b devant &)
COMPANY_INSENSITIVE_RE = re.compile(
    r"\b(?:sarl|sas|sasu|eurl|snc|scop|ets|[ée]tablissements|groupe)\b|&\s*(?:fils|cie)\b",
    re.IGNORECASE,
)
# 2. "SA" ou "S.A." en majuscules avec (?!\w) après le point final pour capter "Dupont S.A."
COMPANY_SENSITIVE_SA_RE = re.compile(r"\b(?:SA\b|S\.A\.(?!\w))")

# Contenu métier + templates → trade_profiles / demo_templates
from trade_profiles import (  # noqa: E402
    format_fr_phone,
    normalize_inputs,
    resolve_profile,
    resolve_services,
    title_case_fr,
)
from demo_templates import render_page  # noqa: E402
from site_plan import resolve_site_plan  # noqa: E402
from scratch_site import (  # noqa: E402
    demo_mode,
    generate_scratch_html,
    metrics_public_dict,
)

# Last generation metrics (scratch or template) — never contains secrets
_LAST_RENDER_METRICS: dict = {}


def last_render_metrics() -> dict:
    return dict(_LAST_RENDER_METRICS)


@dataclass
class Config:
    api_key: str | None = field(default_factory=lambda: os.getenv("GOOGLE_PLACES_API_KEY"))
    preview_base: str = field(
        default_factory=lambda: os.getenv("PREVIEW_BASE_DOMAIN", "https://demo.example.fr").rstrip("/")
    )
    api_base: str | None = field(
        default_factory=lambda: (os.getenv("API_BASE_URL") or "").rstrip("/") or None
    )
    stripe_secret: str | None = field(default_factory=lambda: os.getenv("STRIPE_SECRET_KEY"))
    stripe_price: str | None = field(default_factory=lambda: os.getenv("STRIPE_PRICE_ID"))
    payment_link: str | None = field(default_factory=lambda: os.getenv("STRIPE_PAYMENT_LINK"))
    success_url: str = field(
        default_factory=lambda: os.getenv(
            "STRIPE_SUCCESS_URL", "https://example.fr/merci?session_id={CHECKOUT_SESSION_ID}"
        )
    )
    cancel_url: str = field(default_factory=lambda: os.getenv("STRIPE_CANCEL_URL", "https://example.fr/"))
    price_label: str = field(default_factory=lambda: os.getenv("OFFER_PRICE_LABEL", "49€/mois"))


def atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    """Écriture atomique via fichier temporaire dans le même répertoire."""
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(content, encoding=encoding)
    tmp.replace(path)


def make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=5,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST"}),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


class PlacesError(RuntimeError):
    pass


def search_text(session: requests.Session, api_key: str, query: str, page_token: str | None = None) -> dict:
    body = {"textQuery": query, "languageCode": "fr", "regionCode": "FR", "pageSize": 20}
    if page_token:
        body["pageToken"] = page_token
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": FIELD_MASK,
    }
    try:
        r = session.post(SEARCH_URL, json=body, headers=headers, timeout=(5, 20))
    except requests.RequestException as e:
        raise PlacesError(f"Erreur réseau pour '{query}': {e}") from e
    if r.status_code != 200:
        try:
            msg = r.json().get("error", {}).get("message", r.text)
        except ValueError:
            msg = r.text
        raise PlacesError(f"Places API {r.status_code} pour '{query}': {msg[:300]}")
    return r.json()


def fetch_places(session: requests.Session, api_key: str, query: str) -> list[dict]:
    places, token = [], None
    for page in range(MAX_PAGES):
        data = search_text(session, api_key, query, token)
        batch = data.get("places", [])
        places.extend(batch)
        log.debug("'%s' page %d: %d résultats", query, page + 1, len(batch))
        token = data.get("nextPageToken")
        if not token:
            break
    return places


def load_mock(path: Path) -> dict[str, list[dict]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if "places" in data:
        return {"*": data["places"]}
    return {q: v.get("places", []) for q, v in data.items()}


def has_real_website(uri: str | None, strict_no_website: bool = False) -> bool:
    if not uri:
        return False
    if strict_no_website:
        return True
    host = urlparse(uri).netloc.lower().removeprefix("www.")
    return not any(host == d or host.endswith("." + d) for d in DIRECTORY_AND_SOCIAL_DOMAINS)


def qualifies(p: dict, min_rating: float, min_reviews: int, strict_no_website: bool = False) -> bool:
    if has_real_website(p.get("websiteUri"), strict_no_website):
        return False
    if not (p.get("internationalPhoneNumber") or p.get("nationalPhoneNumber")):
        return False
    return float(p.get("rating") or 0) >= min_rating and int(p.get("userRatingCount") or 0) >= min_reviews


def collect_leads(
    queries: list[str],
    min_rating: float,
    min_reviews: int,
    cfg: Config,
    mock: dict | None,
    strict_no_website: bool = False,
) -> list[dict]:
    session = None if mock else make_session()
    seen: dict[str, dict] = {}
    for q in queries:
        try:
            if mock is not None:
                results = mock.get(q, mock.get("*", []))
            else:
                results = fetch_places(session, cfg.api_key, q)
        except PlacesError as e:
            log.error("%s", e)
            continue
        kept = 0
        for p in results:
            pid = p.get("id")
            if not pid or pid in seen:
                continue
            if qualifies(p, min_rating, min_reviews, strict_no_website):
                seen[pid] = p
                kept += 1
        log.info("'%s' : %d résultats, %d nouveaux prospects qualifiés", q, len(results), kept)
    return list(seen.values())


def esc(v) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def safe_json_ld(data: dict) -> str:
    return (
        json.dumps(data, ensure_ascii=False)
        .replace("</", "<\\/")
        .replace("<!--", "<\\!--")
    )


def display_name(p: dict) -> str:
    dn = p.get("displayName")
    return (dn.get("text") if isinstance(dn, dict) else dn) or "Entreprise"


def tel_href(p: dict) -> str:
    intl = p.get("internationalPhoneNumber")
    if intl:
        digits = re.sub(r"\D", "", intl)
        return "+" + digits if digits else ""
    nat = re.sub(r"\D", "", p.get("nationalPhoneNumber") or "")
    if nat.startswith("0") and len(nat) == 10:
        return "+33" + nat[1:]
    return nat


def is_french_mobile(e164_phone: str) -> bool:
    return bool(re.match(r"^\+33[67]\d{8}$", e164_phone))


def is_company_name(name: str) -> bool:
    """Détecte les sociétés (SARL, SAS, & Fils, SA/S.A. en majuscules) sans faux positif sur 'Sa'."""
    return bool(COMPANY_INSENSITIVE_RE.search(name) or COMPANY_SENSITIVE_SA_RE.search(name))


def slugify(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:60].strip("-") or "entreprise"


def load_existing_slugs(out_dir: Path) -> tuple[dict[str, str], set[str]]:
    """
    Relit les dossiers existants dans `out_dir` en ignorant les liens symboliques
    (ex. domaines .fr pointant vers un slug) pour associer chaque `place_id` à son vrai slug.
    """
    pid_to_slug: dict[str, str] = {}
    used: set[str] = set()
    if not out_dir.exists():
        return pid_to_slug, used

    for child in out_dir.iterdir():
        if child.is_symlink() or not child.is_dir():
            continue
        used.add(child.name)
        meta_path = child / "meta.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                pid = meta.get("place_id")
                if pid:
                    pid_to_slug[pid] = child.name
            except (OSError, ValueError) as e:
                log.warning("Impossible de lire %s : %s", meta_path, e)

    return pid_to_slug, used


def resolve_slug(name: str, pid: str, pid_to_slug: dict[str, str], used: set[str]) -> str:
    if pid in pid_to_slug:
        return pid_to_slug[pid]
    base = slugify(name)
    slug = base
    if slug in used:
        slug = f"{base}-{hashlib.sha1(pid.encode()).hexdigest()[:6]}"
    n = 2
    while slug in used:
        slug = f"{base}-{n}"
        n += 1
    used.add(slug)
    pid_to_slug[pid] = slug
    return slug


def review_text(r: dict) -> str:
    for k in ("text", "originalText"):
        v = r.get(k)
        if isinstance(v, dict) and v.get("text"):
            return v["text"]
    return ""


def pick_reviews(p: dict, n: int = 3) -> list[dict]:
    revs = [r for r in p.get("reviews", []) or [] if review_text(r).strip()]
    good = [r for r in revs if (r.get("rating") or 0) >= 4]
    rest = [r for r in revs if (r.get("rating") or 0) < 4]
    return (good + rest)[:n]


def truncate(text: str, limit: int = 220) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + "…"


def stars_html(rating) -> str:
    try:
        r = max(0, min(5, int(round(float(rating)))))
    except (TypeError, ValueError):
        r = 0
    return (
        f'<span class="text-amber-400" aria-hidden="true">{"★" * r}</span>'
        f'<span class="text-slate-300" aria-hidden="true">{"★" * (5 - r)}</span>'
        f'<span class="sr-only">{r} sur 5</span>'
    )


def fr_number(x) -> str:
    try:
        return f"{float(x):.1f}".replace(".", ",")
    except (TypeError, ValueError):
        return "–"


def is_authentic_google_place_id(place_id: str | None) -> bool:
    """
    True for real Google Places ids. Rejects form_* synthetics and test fixtures
    (e.g. ChIJ_test_osny_001 from mock.json).
    """
    pid = (place_id or "").strip()
    if not pid or pid.startswith("form_"):
        return False
    if "_test_" in pid.lower():
        return False
    if pid.startswith("places/"):
        pid = pid[len("places/") :]
    if pid.startswith("ChIJ") and len(pid) >= 20:
        return True
    # New Places API opaque ids
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{27,}", pid))


def has_google_places_rating(p: dict) -> bool:
    """Rating badge only when both rating and review count come from real Places data."""
    if not is_authentic_google_place_id(p.get("id")):
        return False
    try:
        if p.get("rating") is None or p.get("userRatingCount") is None:
            return False
        rating = float(p["rating"])
        count = int(p["userRatingCount"])
    except (TypeError, ValueError):
        return False
    return 0 < rating <= 5 and count >= 1


def add_query_param(url: str, key: str, value: str) -> str:
    parts = urlparse(url)
    q = dict(parse_qsl(parts.query))
    q[key] = value
    return urlunparse(parts._replace(query=urlencode(q)))


def checkout_url(p: dict, slug: str, cfg: Config, dry_run: bool, force_session: bool = False) -> str:
    if cfg.api_base:
        return f"{cfg.api_base}/checkout/{slug}"

    if cfg.payment_link and not force_session:
        return add_query_param(cfg.payment_link, "client_reference_id", slug)

    pid, name = p["id"], display_name(p)
    if cfg.stripe_secret and cfg.stripe_price and not dry_run:
        try:
            import stripe
            stripe.api_key = cfg.stripe_secret
            meta = {"place_id": pid, "business_name": name[:450], "slug": slug}
            sess = stripe.checkout.Session.create(
                mode="subscription",
                line_items=[{"price": cfg.stripe_price, "quantity": 1}],
                client_reference_id=slug,
                metadata=meta,
                subscription_data={"metadata": meta},
                success_url=cfg.success_url,
                cancel_url=cfg.cancel_url,
                locale="fr",
            )
            return sess.url
        except Exception as e:  # noqa: BLE001
            log.error("Stripe : échec Checkout Session pour %s (%s)", name, e)

    if cfg.payment_link:
        return add_query_param(cfg.payment_link, "client_reference_id", slug)
    return ""


def place_from_form(
    name: str,
    city: str,
    phone: str,
    *,
    email: str = "",
    website: str | None = None,
    place_id: str | None = None,
) -> dict:
    """Synthetic Places-like dict when Google Places is unavailable or unmatched."""
    pid = place_id or f"form_{hashlib.sha256(f'{name}|{city}|{phone}'.encode()).hexdigest()[:16]}"
    place: dict = {
        "id": pid,
        "displayName": {"text": name},
        "formattedAddress": city,
        "nationalPhoneNumber": phone,
    }
    if email:
        place["email"] = email
    if website:
        place["websiteUri"] = website
    return place


def try_fetch_place_for_artisan(
    session: requests.Session,
    api_key: str,
    name: str,
    trade: str,
    city: str,
) -> dict | None:
    """Best-effort Places lookup; returns first qualifying place or None."""
    query = f"{trade} {name} {city}".strip()
    try:
        places = fetch_places(session, api_key, query)
    except PlacesError as e:
        log.warning("Places lookup échoué (%s) : %s", query, e)
        return None
    if not places:
        return None
    # Prefer exact-ish name match; else first result
    name_low = name.casefold()
    for p in places:
        if name_low in display_name(p).casefold():
            return p
    return places[0]


def generate_demo_site(
    *,
    name: str,
    trade: str,
    city: str,
    phone: str,
    email: str = "",
    website: str | None = None,
    out_dir: Path | None = None,
    try_places: bool = True,
    dry_run: bool = False,
) -> dict:
    """
    Generate one artisan demo site from form (or Places-enriched) data.
    Returns {slug, preview_url, place_id, source, checkout_url}.
    Does not send outreach SMS/email.
    """
    name, city, phone = normalize_inputs(name, city, phone)
    trade = (trade or "").strip()
    cfg = Config()
    out = Path(out_dir or os.getenv("SITES_DIR", "./dist")).resolve()
    source = "form"
    place: dict | None = None

    if try_places and cfg.api_key:
        session = make_session()
        place = try_fetch_place_for_artisan(session, cfg.api_key, name, trade, city)
        if place:
            source = "places"
            # Overlay form contact if Places missing phone
            if not place.get("nationalPhoneNumber") and not place.get("internationalPhoneNumber"):
                place["nationalPhoneNumber"] = phone
            # Prefer cleaned display name / city when Places only has raw form leftovers
            dn = display_name(place)
            if dn and dn == dn.lower():
                place["displayName"] = {"text": name}

    if place is None:
        place = place_from_form(name, city, phone, email=email, website=website)
        source = "form"

    pid = place.get("id") or f"form_{slugify(name)}"
    pid_to_slug, used = load_existing_slugs(out)
    slug = resolve_slug(display_name(place), pid, pid_to_slug, used)
    preview = f"{cfg.preview_base}/{slug}/"
    checkout = checkout_url(place, slug, cfg, dry_run=dry_run)

    if not dry_run:
        out.mkdir(parents=True, exist_ok=True)
        site_dir = out / slug
        site_dir.mkdir(parents=True, exist_ok=True)
        rendered = render_preview(place, checkout, cfg, trade, slug=slug)
        atomic_write_text(site_dir / "index.html", rendered)
        atomic_write_text(site_dir / "index.preview.html", rendered)
        metrics = last_render_metrics()
        meta = {
            "place_id": pid,
            "slug": slug,
            "name": display_name(place),
            "trade": trade,
            "phone": phone,
            "email": email,
            "address": place.get("formattedAddress", city),
            "preview_url": preview,
            "checkout_url": checkout,
            "source": source,
            "from_demo_request": True,
            "content_source": metrics.get("content_source") or "unknown",
            "demo_mode": metrics.get("mode") or demo_mode(),
            "gen_metrics": {
                k: metrics[k]
                for k in (
                    "duration_s",
                    "prompt_tokens",
                    "completion_tokens",
                    "total_tokens",
                    "attempts",
                    "model",
                    "fallback",
                    "fallback_reason",
                )
                if k in metrics
            },
        }
        try:
            import demo_ttl

            meta = demo_ttl.stamp_ttl_fields(meta)
        except Exception as e:  # noqa: BLE001
            log.warning("TTL meta non appliqué : %s", e)
        atomic_write_text(site_dir / "meta.json", json.dumps(meta, ensure_ascii=False, indent=2))
        expires_at = meta.get("expires_at")
    else:
        expires_at = None

    metrics_out = last_render_metrics() if not dry_run else {}
    return {
        "slug": slug,
        "preview_url": preview,
        "place_id": pid,
        "source": source,
        "checkout_url": checkout,
        "expires_at": expires_at,
        "content_source": metrics_out.get("content_source") or "unknown",
    }


def render_preview(
    p: dict,
    checkout: str,
    cfg: Config,
    trade: str,
    *,
    slug: str | None = None,
    allow_llm: bool = True,
) -> str:
    global _LAST_RENDER_METRICS
    _LAST_RENDER_METRICS = {}
    raw_name = display_name(p)
    name = title_case_fr(raw_name) if raw_name == raw_name.lower() else raw_name
    # Prefer cleaned national display; fall back to formatted FR phone
    raw_phone = p.get("nationalPhoneNumber") or p.get("internationalPhoneNumber") or ""
    phone_display = format_fr_phone(raw_phone) or raw_phone
    tel = tel_href(p)
    maps = p.get("googleMapsUri") or ""
    address = p.get("formattedAddress", "")
    if address and address == address.lower():
        address = title_case_fr(address)

    # City line for zone: last segment of address or full address
    city_line = address
    if "," in address:
        # Prefer city token (often last segment before country)
        parts = [x.strip() for x in address.split(",") if x.strip()]
        city_line = parts[-2] if len(parts) >= 2 and parts[-1].casefold() in ("france",) else parts[-1]
        # Strip leading postal code
        city_line = re.sub(r"^\d{5}\s+", "", city_line).strip() or city_line

    site_slug = slug or slugify(name)
    mode = demo_mode()

    try:
        import demo_ttl

        expiry_note = demo_ttl.banner_expiry_suffix()
    except Exception:  # noqa: BLE001
        expiry_note = ""

    if checkout:
        banner = (
            "<!-- BANDEAU_DEMO_START -->"
            '<div class="demo-banner">'
            f"Aperçu généré pour <strong>{esc(name)}</strong> — "
            f'<a href="{esc(checkout)}">'
            f"Activer ce site sur mon domaine .fr ({esc(cfg.price_label)}) →</a>"
            f"{esc(expiry_note)}</div>"
            "<!-- BANDEAU_DEMO_END -->"
        )
    else:
        banner = (
            "<!-- BANDEAU_DEMO_START -->"
            f'<div class="demo-banner">Aperçu généré pour <strong>{esc(name)}</strong>'
            f"{esc(expiry_note)}</div>"
            "<!-- BANDEAU_DEMO_END -->"
        )

    # --- Scratch path: unique full HTML+CSS via Grok ---
    if mode == "scratch" and allow_llm:
        schema_scratch: dict = {
            "@context": "https://schema.org",
            "@type": "LocalBusiness",
            "name": name,
            "address": address,
            "telephone": tel or phone_display,
            "description": f"{name} — {(trade or '').strip()} à {city_line or address}",
        }
        meta_desc = f"{name} — {(trade or 'professionnel').strip()} à {city_line or address}."
        scratch = generate_scratch_html(
            name=name,
            trade=trade,
            city=city_line or address or "votre ville",
            phone_display=phone_display,
            tel=tel,
            address=address,
            checkout=checkout,
            price_label=cfg.price_label,
            banner_html=banner,
            meta_description=meta_desc,
            json_ld=safe_json_ld(schema_scratch),
            allow_llm=allow_llm,
            slug=site_slug,
        )
        _LAST_RENDER_METRICS = metrics_public_dict(scratch.metrics)
        _LAST_RENDER_METRICS["slug"] = site_slug
        if scratch.metrics.ok and scratch.html:
            # Marker for regenerate_demo / meta
            if "data-content-source=" not in scratch.html:
                scratch_html = re.sub(
                    r"(?i)<body\b([^>]*)>",
                    r'<body\1 data-content-source="scratch-xai" data-demo-mode="scratch">',
                    scratch.html,
                    count=1,
                )
            else:
                scratch_html = scratch.html
            return scratch_html
        # else fall through to template generator (last resort)
        log.warning(
            "scratch failed → template last resort slug=%s reason=%s duration=%.1fs",
            site_slug,
            scratch.metrics.fallback_reason,
            scratch.metrics.duration_s,
        )

    # Template path (default for DEMO_MODE=template, or scratch fallback)
    scratch_fallback = bool(_LAST_RENDER_METRICS.get("fallback")) if mode == "scratch" else False
    # Prefer Grok for copy when allow_llm; curated bouche profiles if xAI fails
    profile = resolve_profile(trade, name=name, city=city_line, allow_llm=allow_llm)
    plan = resolve_site_plan(
        slug=site_slug,
        name=name,
        city=city_line or address or "votre ville",
        trade=trade,
        allow_llm=allow_llm,
        profile=profile,
    )
    trade_label = plan.trade_label
    prev = dict(_LAST_RENDER_METRICS) if scratch_fallback else {}
    _LAST_RENDER_METRICS = {
        "mode": mode,
        "ok": True,
        "fallback": scratch_fallback,
        "fallback_reason": prev.get("fallback_reason", ""),
        "model": prev.get("model", ""),
        "duration_s": prev.get("duration_s", 0),
        "prompt_tokens": prev.get("prompt_tokens", 0),
        "completion_tokens": prev.get("completion_tokens", 0),
        "total_tokens": prev.get("total_tokens", 0),
        "attempts": prev.get("attempts", 0),
        "content_source": (
            f"template-fallback:{plan.content_source}" if scratch_fallback else plan.content_source
        ),
        "slug": site_slug,
    }

    # Google avis / note : uniquement si place_id + rating + count issus de Places réel
    # (pas form_*, pas fixtures mock ChIJ_test_* — ex. Morel mock.json 4,8/42).
    google_proof = has_google_places_rating(p)
    cards = []
    if is_authentic_google_place_id(p.get("id")):
        for r in pick_reviews(p):
            author = (r.get("authorAttribution") or {}).get("displayName") or "Client Google"
            author_uri = (r.get("authorAttribution") or {}).get("uri") or ""
            when = r.get("relativePublishTimeDescription") or ""
            author_html = (
                f'<a href="{esc(author_uri)}" target="_blank" rel="noopener nofollow">{esc(author)}</a>'
                if author_uri
                else esc(author)
            )
            cards.append(
                '<figure class="demo-review">'
                f'<div class="demo-stars">{stars_html(r.get("rating"))}</div>'
                f'<blockquote>« {esc(truncate(review_text(r)))} »</blockquote>'
                f'<figcaption><span>— {author_html}</span>'
                + (f' <span class="demo-muted">· {esc(when)}</span>' if when else "")
                + "</figcaption></figure>"
            )
    if cards and google_proof:
        attribution = (
            f'<a href="{esc(maps)}" target="_blank" rel="noopener nofollow">Avis issus de Google</a>'
            if maps
            else "Avis issus de Google"
        )
        reviews_section = (
            '<section class="demo-section" id="avis">'
            '<div class="demo-wrap">'
            '<header class="demo-section-head"><h2>Ce que disent nos clients</h2></header>'
            f'<div class="demo-grid-3">{"".join(cards)}</div>'
            f'<p class="demo-reviews-attr">{attribution} · '
            f"note moyenne {esc(fr_number(p.get('rating')))}/5 sur {esc(p.get('userRatingCount', 0))} avis</p>"
            "</div></section>"
        )
    else:
        reviews_section = ""

    weekdays = (p.get("regularOpeningHours") or {}).get("weekdayDescriptions") or []
    if weekdays:
        hours_items = "".join(f"<li>{esc(day)}</li>" for day in weekdays)
        hours_section = (
            '<section class="demo-section demo-section-alt" id="horaires">'
            '<div class="demo-wrap demo-hours">'
            "<h2>Horaires</h2>"
            f"<ul>{hours_items}</ul>"
            "</div></section>"
        )
    else:
        hours_section = ""

    schema_data: dict = {
        "@context": "https://schema.org",
        "@type": "LocalBusiness",
        "name": name,
        "address": address,
        "telephone": tel or phone_display,
        "description": plan.hero_subtitle or plan.positioning,
    }
    raw_rating = p.get("rating")
    raw_count = p.get("userRatingCount")
    if google_proof:
        schema_data["aggregateRating"] = {
            "@type": "AggregateRating",
            "ratingValue": float(raw_rating),
            "reviewCount": int(raw_count),
        }
        rating_badge = (
            f'<span class="demo-badge">⭐ {esc(fr_number(raw_rating))}/5 sur Google · '
            f"{esc(raw_count)} avis</span>"
        )
        meta_description = (
            plan.meta_description
            or (
                f"{name} ({trade_label}) – {address}. "
                f"Noté {fr_number(raw_rating)}/5 sur Google ({raw_count} avis)."
            )
        )
    else:
        rating_badge = '<span class="demo-badge">Aperçu ArtisanVisible</span>'
        meta_description = (
            plan.meta_description
            or f"{name} ({trade_label}) – {address}. {plan.hero_subtitle or plan.positioning}"
        )

    html_out = render_page(
        plan=plan,
        address=address,
        phone_display=phone_display,
        tel=tel,
        maps=maps,
        banner=banner,
        rating_badge=rating_badge,
        reviews_section=reviews_section,
        hours_section=hours_section,
        meta_description=meta_description,
        json_ld=safe_json_ld(schema_data),
        esc=esc,
    )
    # Stamp variety attrs even on template / scratch-fallback paths
    try:
        from design_variety import inject_preset_css, pick_design_preset, stamp_preset_attrs

        preset = pick_design_preset(site_slug, trade)
        html_out = stamp_preset_attrs(inject_preset_css(html_out, preset), preset)
    except Exception:  # noqa: BLE001
        log.warning("variety stamp skipped for slug=%s", site_slug)
    # B-28: flag template-fallback explicitly + scrub blank grey/marble placeholders
    src_label = _LAST_RENDER_METRICS.get("content_source") or "template"
    if "data-content-source=" in html_out:
        html_out = re.sub(
            r'data-content-source="[^"]*"',
            f'data-content-source="{src_label}"',
            html_out,
            count=1,
        )
    else:
        html_out = re.sub(
            r"(?i)<body\b([^>]*)>",
            rf'<body\1 data-content-source="{src_label}" data-demo-mode="{mode}">',
            html_out,
            count=1,
        )
    html_out = re.sub(
        r'(?is)<(?:div|figure)\b[^>]*class=["\'][^"\']*\b(?:demo-hero-media-neutral|demo-gallery-placeholder)[^"\']*["\'][^>]*>.*?</(?:div|figure)\s*>',
        "",
        html_out,
    )
    html_out = re.sub(
        r'(?is)<div\b[^>]*class=["\'][^"\']*\bdemo-neutral-visual\b[^"\']*["\'][^>]*/?>',
        "",
        html_out,
    )
    try:
        from photo_banks import pick_trade_images
        from scratch_site import _ensure_hero_image

        catalog = [
            {"path": f"/demo-assets/images/{n}", "subject": n}
            for n in pick_trade_images(trade, site_slug, limit=3)
        ]
        if catalog:
            html_out = _ensure_hero_image(html_out, catalog)
    except Exception:  # noqa: BLE001
        pass
    return html_out


CSV_FIELDNAMES = [
    "slug",
    "name",
    "trade",
    "is_company_hint",
    "phone",
    "phone_e164",
    "is_mobile",
    "outreach_ok",
    "address",
    "rating",
    "reviews",
    "existing_social_url",
    "maps_url",
    "preview_url",
    "checkout_url",
    "outreach_message",
    "place_id",
]


def write_merged_csv(csv_path: Path, new_rows: list[dict]) -> int:
    merged: dict[str, dict] = {}
    if csv_path.exists():
        try:
            with csv_path.open("r", newline="", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f, delimiter=";")
                for row in reader:
                    pid = row.get("place_id")
                    if pid:
                        merged[pid] = row
        except OSError as e:
            log.warning("Lecture de l'ancien CSV impossible (%s), recréation.", e)

    for row in new_rows:
        merged[row["place_id"]] = row

    tmp_csv = csv_path.with_name(f".{csv_path.name}.tmp")
    with tmp_csv.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES, delimiter=";", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(merged.values())
    tmp_csv.replace(csv_path)

    return len(merged)


def build_queries(args: argparse.Namespace) -> list[str]:
    if args.queries:
        return args.queries
    return [f"{args.trade} {t} Val-d'Oise" for t in DEFAULT_TOWNS]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Prospects sans site web -> aperçus de sites + leads.csv")
    ap.add_argument("--queries", nargs="+", help='Requêtes complètes (ex. "plombier Cergy"). Défaut : métier x villes du 95')
    ap.add_argument("--trade", default=DEFAULT_TRADE, help=f"Métier si --queries absent (défaut : {DEFAULT_TRADE})")
    ap.add_argument("--min-rating", type=float, default=4.2)
    ap.add_argument("--min-reviews", type=int, default=15)
    ap.add_argument("--strict-no-website", action="store_true", help="Exclure aussi les artisans ayant une page Facebook/PagesJaunes")
    ap.add_argument("--create-stripe-sessions", action="store_true", help="Forcer la création immédiate de sessions Stripe (expirent en 24h)")
    ap.add_argument("--out", type=Path, default=Path(os.getenv("SITES_DIR", "./dist")))
    ap.add_argument("--dry-run", action="store_true", help="Liste les prospects sans écrire de fichiers ni appeler Stripe")
    ap.add_argument("--mock", type=Path, help="Fixture JSON à la place de l'API Google (tests)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    cfg = Config()

    mock = load_mock(args.mock) if args.mock else None
    if mock is None and not cfg.api_key:
        log.error("GOOGLE_PLACES_API_KEY manquant (ou utilisez --mock).")
        return 2

    queries = build_queries(args)
    leads = collect_leads(queries, args.min_rating, args.min_reviews, cfg, mock, args.strict_no_website)
    log.info("%d prospects qualifiés sur cette exécution", len(leads))
    if not leads:
        return 0

    if not args.dry_run:
        args.out.mkdir(parents=True, exist_ok=True)

    pid_to_slug, used = load_existing_slugs(args.out)
    rows: list[dict] = []

    for p in leads:
        name = display_name(p)
        slug = resolve_slug(name, p["id"], pid_to_slug, used)
        site_dir = args.out / slug

        if not args.dry_run and (site_dir / ".activated").exists():
            log.info("Client déjà actif ignoré : %s (%s)", name, slug)
            continue

        preview = f"{cfg.preview_base}/{slug}/"
        checkout = checkout_url(p, slug, cfg, args.dry_run, args.create_stripe_sessions)
        phone_display = p.get("nationalPhoneNumber") or p.get("internationalPhoneNumber") or ""
        e164 = tel_href(p)
        mobile = is_french_mobile(e164)
        company_hint = is_company_name(name)

        # Conformité CNIL : un mobile sans indice de société (EI probable) ne reçoit pas de SMS automatisé
        outreach_ok = not (mobile and not company_hint)
        outreach_msg = (
            f"Bonjour, j'ai vu que {name} avait d'excellents avis Google ({fr_number(p.get('rating'))}/5) "
            f"mais pas de site internet. J'ai préparé un aperçu gratuit avec vos avis ici : {preview} "
            f"- Dites-moi si vous souhaitez l'activer !"
            if outreach_ok
            else ""
        )

        if not args.dry_run:
            site_dir.mkdir(parents=True, exist_ok=True)
            rendered_html = render_preview(p, checkout, cfg, args.trade, slug=slug)
            atomic_write_text(site_dir / "index.html", rendered_html)
            atomic_write_text(site_dir / "index.preview.html", rendered_html)

            meta_payload = {
                "place_id": p["id"],
                "slug": slug,
                "name": name,
                "trade": args.trade,
                "is_company_hint": company_hint,
                "phone": e164 or phone_display,
                "address": p.get("formattedAddress", ""),
                "rating": p.get("rating"),
                "reviews_count": p.get("userRatingCount"),
                "preview_url": preview,
                "checkout_url": checkout,
                "price_label": cfg.price_label,
            }
            try:
                import demo_ttl

                meta_payload = demo_ttl.stamp_ttl_fields(meta_payload)
            except Exception as e:  # noqa: BLE001
                log.warning("TTL meta non appliqué : %s", e)
            atomic_write_text(
                site_dir / "meta.json",
                json.dumps(meta_payload, ensure_ascii=False, indent=2),
            )

        print(
            f"[{'DRY' if args.dry_run else 'SITE'}] {name} "
            f"({phone_display} | Mobile={mobile} | Société={company_hint} | OutreachOK={outreach_ok}) -> {preview}"
        )
        rows.append(
            {
                "slug": slug,
                "name": name,
                "trade": args.trade,
                "is_company_hint": "1" if company_hint else "0",
                "phone": phone_display,
                "phone_e164": e164,
                "is_mobile": "1" if mobile else "0",
                "outreach_ok": "1" if outreach_ok else "0",
                "address": p.get("formattedAddress", ""),
                "rating": fr_number(p.get("rating")) if p.get("rating") is not None else "",
                "reviews": p.get("userRatingCount", ""),
                "existing_social_url": p.get("websiteUri", ""),
                "maps_url": p.get("googleMapsUri", ""),
                "preview_url": preview,
                "checkout_url": checkout,
                "outreach_message": outreach_msg,
                "place_id": p["id"],
            }
        )

    if not args.dry_run and rows:
        csv_path = args.out / "leads.csv"
        total_rows = write_merged_csv(csv_path, rows)
        log.info("Fichier CSV mis à jour : %s (%d prospects au total)", csv_path, total_rows)

    return 0


if __name__ == "__main__":
    sys.exit(main())
