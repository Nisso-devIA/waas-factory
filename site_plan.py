#!/usr/bin/env python3
"""
Plans de site uniques pour démos ArtisanVisible.

Chaque slug tire un plan déterministe (palette, hero, ordre de sections).
Grok peut enrichir le plan ; sinon fallback curaté encore varié — jamais
la même grille 6 cartes icônes pour tous.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from typing import Any

from photo_banks import (
    FAMILY_ALIMENTATION,
    TRADE_IMAGES,
    resolve_family,
    resolve_trade_images,
)
from trade_profiles import (
    DEFAULT_XAI_MODEL,
    TradeProfile,
    XaiUnknownModelError,
    _clean_text,
    curated_or_fallback,
    resolved_xai_model,
)

log = logging.getLogger("site_plan")

XAI_API_URL = "https://api.x.ai/v1/chat/completions"
# Plan generation: 60–90s, 2 retries with backoff (timeouts were falling back to curated)
XAI_PLAN_TIMEOUT_S = int(os.getenv("XAI_PLAN_TIMEOUT_S") or os.getenv("XAI_TIMEOUT_S") or "90")
XAI_MAX_ATTEMPTS = int(os.getenv("XAI_MAX_ATTEMPTS") or "3")  # 1 try + 2 retries
XAI_RETRY_BACKOFF_S = (2.0, 4.0, 8.0)

# --------------------------------------------------------------------------- #
# Local design presets (no CDN)
# --------------------------------------------------------------------------- #

# Keep local map in sync with design_variety.PALETTES (template path).
from design_variety import PALETTES as _VARIETY_PALETTES  # noqa: E402

PALETTES: dict[str, dict[str, str]] = {
    pid: {k: v for k, v in cols.items() if k in ("accent", "accent_soft", "ink", "paper")}
    for pid, cols in _VARIETY_PALETTES.items()
}
# Legacy alias still referenced by older CSS
if "graphite" not in PALETTES:
    PALETTES["graphite"] = {
        "accent": "#3D5A80",
        "accent_soft": "#E8EEF5",
        "ink": "#15202B",
        "paper": "#F7F8FA",
    }

FONT_PAIRS = ("fraunces-plex", "plex-only", "fraunces-soft")
HERO_VARIANTS = ("overlay", "split", "editorial", "minimal")
NAV_VARIANTS = ("none", "minimal", "links")

# Section kinds available in the library
SECTION_KINDS = (
    "flagship_split",
    "services_numbered",
    "services_accordion",
    "process_steps",
    "story_values",
    "audience",
    "zone_cities",
    "faq",
    "gallery",
    "final_cta",
)

# TRADE_IMAGES imported from photo_banks (familles + bouche). No global default.


def slug_rng(slug: str) -> random.Random:
    digest = hashlib.sha256((slug or "entreprise").encode("utf-8")).hexdigest()
    return random.Random(int(digest[:16], 16))


def pick_palette(slug: str, trade_key: str) -> str:
    from design_variety import TRADE_PALETTE_POOLS, pick_design_preset

    # Prefer design_variety pools (same seed family as scratch) for métier variety
    pool = TRADE_PALETTE_POOLS.get(trade_key)
    if pool:
        # Align with scratch preset when possible
        try:
            return pick_design_preset(slug, trade_key).palette_id
        except Exception:
            return slug_rng(slug + ":palette").choice([p for p in pool if p in PALETTES] or list(PALETTES))
    keys = [k for k in PALETTES.keys()]
    bias = {
        "coiffeur": ["slate-rose", "wine-cream", "berry-mist"],
        "esthéticienne": ["slate-rose", "wine-cream", "berry-mist"],
        "photographe": ["navy-mist", "ink-brass", "graphite"],
        "traiteur": ["evergreen", "forest-sand", "sage-porcelain"],
        "restaurant": ["navy-mist", "ink-brass", "ink-apricot"],
        "boucherie": ["slate-coral", "wine-cream", "cocoa-cream"],
    }
    pool2 = bias.get(trade_key) or keys
    pool2 = [p for p in pool2 if p in PALETTES] or keys
    return slug_rng(slug + ":palette").choice(pool2)


def pick_section_sequence(slug: str) -> list[str]:
    """Deterministic unique section order — always includes gallery + contact CTA + services form."""
    rng = slug_rng(slug + ":sections")
    # Always one services presentation (never the old 6-icon grid as sole option)
    services_kind = rng.choice(["services_numbered", "services_accordion", "flagship_split"])
    mid = ["process_steps", "story_values", "audience", "zone_cities"]
    rng.shuffle(mid)
    # Pick 2–3 mid sections
    n_mid = 2 + (rng.randint(0, 1))
    chosen_mid = mid[:n_mid]
    # Alternate rhythm: services early or after story
    if rng.random() < 0.5:
        seq = [services_kind] + chosen_mid
    else:
        seq = [chosen_mid[0], services_kind] + chosen_mid[1:]
    # FAQ optional but usually present
    if rng.random() < 0.85:
        seq.append("faq")
    seq.append("gallery")
    seq.append("final_cta")
    # Deduplicate while preserving order
    seen: set[str] = set()
    out: list[str] = []
    for k in seq:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


@dataclass
class SitePlan:
    slug: str
    name: str
    trade_label: str
    city: str
    hero_variant: str
    nav_variant: str
    palette_id: str
    font_pair: str
    accent: str
    accent_soft: str
    ink: str
    paper: str
    hero_image: str
    section_image: str
    positioning: str
    voice: str
    hero_title: str
    hero_subtitle: str
    cta_primary: str
    cta_secondary: str
    meta_description: str
    gallery_caption: str
    section_order: list[str]
    # Rich copy blocks
    services: list[dict[str, str]] = field(default_factory=list)  # title, lead, detail, for_whom
    flagship: dict[str, str] = field(default_factory=dict)  # title, body, aside
    process_steps: list[dict[str, str]] = field(default_factory=list)  # title, body
    story: dict[str, str] = field(default_factory=dict)  # title, body
    values: list[dict[str, str]] = field(default_factory=list)
    audience: list[str] = field(default_factory=list)
    zone_blurb: str = ""
    zone_cities: list[str] = field(default_factory=list)
    faq: list[dict[str, str]] = field(default_factory=list)  # q, a
    gallery_images: list[str] = field(default_factory=list)
    story_image: str = ""
    content_source: str = "curated"


def _trade_images(profile: TradeProfile) -> list[str]:
    """Dedicated bank → family pool → [] (neutral, never cross-métier)."""
    for candidate in (profile.key, profile.label or ""):
        imgs = resolve_trade_images(candidate)
        if imgs:
            return imgs
    return []


def _pick_distinct_images(
    imgs: list[str], rng: random.Random
) -> tuple[str, str, str, list[str]]:
    """Hero + section + story + up to 3 gallery slots — all distinct, no page repeats."""
    bank = [i for i in imgs if i]
    if not bank:
        return "", "", "", []
    order = list(bank)
    rng.shuffle(order)
    hero = order[0]
    section = order[1] if len(order) > 1 else order[0]
    story = order[2] if len(order) > 2 else section
    used = {hero, section, story}
    gallery = [x for x in order if x not in used][:3]
    return hero, section, story, gallery


def _is_bouche(profile: TradeProfile) -> bool:
    return resolve_family(profile.key) == FAMILY_ALIMENTATION or resolve_family(profile.label) == FAMILY_ALIMENTATION


def _developed_services(profile: TradeProfile, name: str, city: str, rng: random.Random) -> list[dict[str, str]]:
    """3–5 concrete prestations (not a padded 6-icon boilerplate)."""
    base = list(profile.services)[: rng.choice([3, 4, 5])]
    bouche = _is_bouche(profile)
    out: list[dict[str, str]] = []
    # Distinct detail lines — never repeat the same sentence across cards
    if bouche:
        details = [
            f"Chez {name}, chaque création est préparée avec soin pour les gourmands de {city}.",
            f"On part de votre envie (goûts, nombre de parts, date) pour une proposition claire.",
            f"Commande anticipée et retrait en boutique — idéal pour vos occasions à {city}.",
            f"Des produits frais, dressés pour l'occasion, prêts à régaler vos convives.",
            f"Un conseil simple sur le choix, la conservation et le service à table.",
        ]
        for_whom_tpl = "Pour les particuliers et professionnels de {city} qui cherchent un {label} de confiance."
    else:
        details = [
            f"Chez {name}, on clarifie le besoin, on explique les étapes, puis on réalise avec soin à {city}.",
            f"Un échange préalable pour caler le périmètre, le planning et le budget.",
            f"Réalisation soignée pour les clients de {city}, avec un point de suivi à la fin.",
            f"Des options concrètes, sans jargon, pour savoir exactement quoi attendre.",
            f"Un interlocuteur joignable pendant toute la prestation.",
        ]
        for_whom_tpl = "Idéal si vous cherchez un {label} de confiance à {city}."
    rng.shuffle(details)
    for i, (title, desc, _icon) in enumerate(base):
        out.append(
            {
                "title": title,
                "lead": desc,
                "detail": details[i % len(details)],
                "for_whom": for_whom_tpl.format(city=city, label=profile.label.lower()),
            }
        )
    return out


def build_curated_plan(
    *,
    slug: str,
    name: str,
    city: str,
    trade: str,
    profile: TradeProfile | None = None,
) -> SitePlan:
    profile = profile or curated_or_fallback(trade)
    rng = slug_rng(slug)
    palette_id = pick_palette(slug, profile.key)
    colors = PALETTES[palette_id]
    imgs = _trade_images(profile)
    hero_img, section_img, story_img, gallery_imgs = _pick_distinct_images(imgs, rng)
    # Prefer profile hero when it still exists in the bank
    if profile.hero_image and profile.hero_image in imgs:
        hero_img = profile.hero_image
        rest = [i for i in imgs if i != hero_img]
        rng.shuffle(rest)
        section_img = rest[0] if rest else hero_img
        story_img = rest[1] if len(rest) > 1 else section_img
        used = {hero_img, section_img, story_img}
        gallery_imgs = [i for i in rest if i not in used][:3]
    order = pick_section_sequence(slug)
    # B-28: hero structure from design_variety (unique per same-trade demos)
    try:
        from design_variety import HERO_STRUCTURES, pick_design_preset

        _preset = pick_design_preset(slug, trade)
        hero_meta = HERO_STRUCTURES.get(_preset.hero_structure_id) or {}
        hero_v = str(hero_meta.get("template_hero") or rng.choice(HERO_VARIANTS))
        if hero_v not in HERO_VARIANTS:
            hero_v = rng.choice(HERO_VARIANTS)
    except Exception:
        hero_v = rng.choice(HERO_VARIANTS)
    nav_v = "links"  # B-28: proper top bar — never mid-hero floating nav
    font = rng.choice(FONT_PAIRS)

    services = _developed_services(profile, name, city, rng)
    bouche = _is_bouche(profile)
    if bouche:
        flagship = {
            "title": services[0]["title"] if services else profile.label,
            "body": (
                f"{name} régale les gourmands de {city} avec une approche artisanale et accueillante. "
                f"{profile.hook}"
            ),
            "aside": f"Commande & conseil — {profile.label.lower()} à {city}.",
        }
        steps = [
            {"title": "Votre envie", "body": f"Vous décrivez l'occasion, les goûts et le nombre de parts ; {name} vous guide."},
            {"title": "Proposition", "body": "Une création adaptée (saveurs, format, date de retrait), sans jargon."},
            {"title": "Préparation & retrait", "body": f"Préparation soignée et retrait en boutique à {city} (ou livraison selon offre)."},
        ]
        audience = [
            f"Familles de {city} pour un gâteau d'anniversaire ou une fête",
            f"Entreprises et associations qui commandent un plateau sucré",
            f"Clients qui cherchent un {profile.label.lower()} de proximité",
        ]
    else:
        flagship = {
            "title": services[0]["title"] if services else profile.label,
            "body": (
                f"{name} accompagne les habitants de {city} avec une approche claire et humaine. "
                f"{profile.hook} On prend le temps d'expliquer ce qui sera fait, avant de démarrer."
            ),
            "aside": f"Un interlocuteur dédié — {profile.label.lower()} à {city}.",
        }
        steps = [
            {"title": "Échange", "body": f"Vous décrivez votre besoin ; {name} vous répond avec des options concrètes."},
            {"title": "Proposition", "body": "Un déroulé clair, sans jargon, pour savoir exactement quoi attendre."},
            {"title": "Réalisation", "body": f"Travail soigné à {city}, avec un point de suivi à la fin."},
        ]
        audience = [
            f"Particuliers à {city} qui veulent un professionnel joignable",
            f"Personnes qui préfèrent un chiffrage clair avant de s'engager",
            f"Clients qui cherchent un {profile.label.lower()} de proximité",
        ]
    story = {
        "title": f"L'approche {name}",
        "body": (
            f"Basé à {city}, {name} mise sur la proximité et la transparence. "
            f"{profile.why_us[0][1] if profile.why_us else 'Un accompagnement simple, orienté résultat.'}"
        ),
    }
    values = [{"title": t, "body": d} for t, d in profile.why_us[:3]]
    cities = [city]
    if city and city.casefold() not in ("paris",):
        pass
    else:
        # Soft nearby hints without inventing fake suburbs as facts — keep generic
        cities = [city, "communes voisines"]

    faq = [{"q": q, "a": a} for q, a in profile.faq[:4]]
    title, subtitle = profile.hero_title, profile.hero_subtitle or profile.hook
    if not title:
        title = f"{profile.label} à {city}"
        # avoid dup with subtitle
        if subtitle and subtitle.casefold().startswith(title.casefold()):
            pass

    return SitePlan(
        slug=slug,
        name=name,
        trade_label=profile.label,
        city=city,
        hero_variant=hero_v,
        nav_variant=nav_v,
        palette_id=palette_id,
        font_pair=font,
        accent=colors["accent"],
        accent_soft=colors["accent_soft"],
        ink=colors["ink"],
        paper=colors["paper"],
        hero_image=hero_img,
        section_image=section_img,
        story_image=story_img,
        positioning=f"{profile.label} de proximité à {city}",
        voice="simple, chaleureux, concret",
        hero_title=title[:120],
        hero_subtitle=(subtitle or profile.hook)[:220],
        cta_primary=profile.cta_primary,
        cta_secondary=profile.cta_secondary,
        meta_description=(profile.meta_description or f"{name} — {profile.label} à {city}.")[:160],
        gallery_caption=profile.gallery_caption,
        section_order=order,
        services=services,
        flagship=flagship,
        process_steps=steps,
        story=story,
        values=values,
        audience=audience,
        zone_blurb=profile.zone_blurb,
        zone_cities=cities,
        faq=faq,
        gallery_images=gallery_imgs,
        content_source=profile.content_source if profile.content_source != "xai" else "curated",
    )


def _merge_xai_plan(base: SitePlan, data: dict[str, Any]) -> SitePlan:
    """Overlay Grok JSON onto a curated structural base (palette/order already set)."""
    hero_title = _clean_text(data.get("hero_title"), max_len=100) or base.hero_title
    hero_subtitle = _clean_text(data.get("hero_subtitle"), max_len=220) or base.hero_subtitle
    if hero_subtitle.casefold().startswith(hero_title.casefold()):
        rest = hero_subtitle[len(hero_title) :].lstrip(" \t:-–—.,")
        hero_subtitle = rest or hero_subtitle

    services: list[dict[str, str]] = []
    for it in data.get("services") or []:
        if not isinstance(it, dict):
            continue
        title = _clean_text(it.get("title"), max_len=80)
        if not title:
            continue
        services.append(
            {
                "title": title,
                "lead": _clean_text(it.get("lead") or it.get("desc"), max_len=180),
                "detail": _clean_text(it.get("detail") or it.get("body"), max_len=360),
                "for_whom": _clean_text(it.get("for_whom"), max_len=160),
            }
        )
    if not services:
        services = base.services
    else:
        services = services[:5]

    def steps(items: Any) -> list[dict[str, str]]:
        out = []
        for it in items or []:
            if isinstance(it, dict):
                t = _clean_text(it.get("title"), max_len=80)
                b = _clean_text(it.get("body") or it.get("desc"), max_len=240)
                if t and b:
                    out.append({"title": t, "body": b})
        return out or base.process_steps

    faq = []
    for it in data.get("faq") or []:
        if isinstance(it, dict):
            q = _clean_text(it.get("q"), max_len=120)
            a = _clean_text(it.get("a"), max_len=280)
            if q and a:
                faq.append({"q": q, "a": a})
    if not faq:
        faq = base.faq

    audience = [
        _clean_text(x, max_len=120)
        for x in (data.get("audience") or [])
        if _clean_text(x, max_len=120)
    ] or base.audience

    # Optional section order from Grok — sanitize to known kinds
    order = base.section_order
    raw_order = data.get("section_order")
    if isinstance(raw_order, list) and raw_order:
        cleaned = [str(x) for x in raw_order if str(x) in SECTION_KINDS]
        # Keep at least one services presentation from slug seed if Grok omitted all
        if not any(k.startswith("services_") or k == "flagship_split" for k in cleaned):
            for k in base.section_order:
                if k.startswith("services_") or k == "flagship_split":
                    cleaned.insert(0, k)
                    break
        if "gallery" not in cleaned:
            cleaned.append("gallery")
        if "final_cta" not in cleaned:
            cleaned.append("final_cta")
        if cleaned:
            order = cleaned

    # Visual identity stays slug-deterministic (Grok must not collapse every site to the same hero)
    hero_v = base.hero_variant
    nav_v = base.nav_variant

    flag = data.get("flagship") if isinstance(data.get("flagship"), dict) else {}
    story = data.get("story") if isinstance(data.get("story"), dict) else {}
    values_raw = data.get("values") if isinstance(data.get("values"), list) else []
    values = []
    for it in values_raw:
        if isinstance(it, dict):
            t = _clean_text(it.get("title"), max_len=80)
            b = _clean_text(it.get("body") or it.get("desc"), max_len=200)
            if t and b:
                values.append({"title": t, "body": b})
    if not values:
        values = base.values

    return replace(
        base,
        hero_variant=hero_v,
        nav_variant=nav_v,
        positioning=_clean_text(data.get("positioning"), max_len=160) or base.positioning,
        voice=_clean_text(data.get("voice"), max_len=80) or base.voice,
        hero_title=hero_title,
        hero_subtitle=hero_subtitle,
        cta_primary=_clean_text(data.get("cta_primary"), max_len=60) or base.cta_primary,
        cta_secondary=_clean_text(data.get("cta_secondary"), max_len=60) or base.cta_secondary,
        meta_description=_clean_text(data.get("meta_description"), max_len=160) or base.meta_description,
        gallery_caption=_clean_text(data.get("gallery_caption"), max_len=160) or base.gallery_caption,
        section_order=order,
        services=services,
        flagship={
            "title": _clean_text(flag.get("title"), max_len=100) or base.flagship.get("title", ""),
            "body": _clean_text(flag.get("body"), max_len=420) or base.flagship.get("body", ""),
            "aside": _clean_text(flag.get("aside"), max_len=160) or base.flagship.get("aside", ""),
        },
        process_steps=steps(data.get("process_steps")),
        story={
            "title": _clean_text(story.get("title"), max_len=100) or base.story.get("title", ""),
            "body": _clean_text(story.get("body"), max_len=420) or base.story.get("body", ""),
        },
        values=values,
        audience=audience,
        zone_blurb=_clean_text(data.get("zone_blurb"), max_len=280) or base.zone_blurb,
        # Do not invent neighbourhoods / suburbs — keep the real city (+ soft "communes voisines")
        zone_cities=_safe_zone_cities(
            data.get("zone_cities"),
            city=base.city,
            fallback=base.zone_cities,
        ),
        faq=faq,
        content_source="xai",
    )


def _safe_zone_cities(raw: Any, *, city: str, fallback: list[str]) -> list[str]:
    city_l = (city or "").casefold().strip()
    allowed_soft = {"communes voisines", "environs", "agglomération", "agglomeration"}
    out: list[str] = []
    if city:
        out.append(city)
    for c in raw or []:
        t = _clean_text(c, max_len=40)
        if not t:
            continue
        tl = t.casefold()
        if tl == city_l:
            continue
        if tl in allowed_soft or (city_l and city_l in tl):
            out.append(t)
        # else: drop invented districts / towns
    if len(out) == 1 and city:
        out.append("communes voisines")
    return out[:6] or list(fallback)


def _xai_plan_request(api_key: str, body: dict[str, Any]) -> dict[str, Any]:
    model = str(body.get("model") or "")
    data = json.dumps(body).encode("utf-8")
    last_err: Exception | None = None
    for attempt in range(1, XAI_MAX_ATTEMPTS + 1):
        req = urllib.request.Request(
            XAI_API_URL,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=XAI_PLAN_TIMEOUT_S) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                err_body = e.read().decode("utf-8", errors="replace")
            except OSError:
                err_body = ""
            low = err_body.lower()
            if "model" in low and any(
                m in low for m in ("not found", "does not exist", "unknown", "invalid model")
            ):
                raise XaiUnknownModelError(model, http_code=e.code) from e
            last_err = e
            log.warning(
                "xAI site plan échec tentative %d/%d : HTTPError %s",
                attempt,
                XAI_MAX_ATTEMPTS,
                e.code,
            )
            if e.code in (401, 403, 404):
                break
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
            last_err = e
            log.warning(
                "xAI site plan échec tentative %d/%d : %s",
                attempt,
                XAI_MAX_ATTEMPTS,
                type(e).__name__,
            )
        if attempt < XAI_MAX_ATTEMPTS:
            delay = XAI_RETRY_BACKOFF_S[min(attempt - 1, len(XAI_RETRY_BACKOFF_S) - 1)]
            time.sleep(delay)
    assert last_err is not None
    raise last_err


def _parse_xai_json_content(content: Any) -> dict[str, Any]:
    if not isinstance(content, str):
        raise ValueError("empty")
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
    data = json.loads(content)
    if not isinstance(data, dict):
        raise ValueError("not object")
    return data


def _xai_chat_json(
    api_key: str,
    *,
    model: str,
    system: str,
    user: str,
    temperature: float = 0.45,
) -> dict[str, Any]:
    body = {
        "model": model,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    payload = _xai_plan_request(api_key, body)
    return _parse_xai_json_content(payload["choices"][0]["message"]["content"])


def xai_enrich_plan(base: SitePlan, *, trade: str) -> SitePlan | None:
    """
    Enrich via Grok in two shorter calls (more reliable than one huge JSON):
      1) hero + services + flagship + CTAs
      2) story / values / audience / zone / faq
    Retries with backoff; returns None only if call 1 fails entirely.
    """
    api_key = (os.getenv("XAI_API_KEY") or "").strip()
    if not api_key:
        return None
    model = resolved_xai_model()
    rules = (
        "Français simple. INTERDIT: inventer avis, notes, chiffres, années d'expérience, "
        "certifications, labels, tarifs/prix. Réponds UNIQUEMENT en JSON valide."
    )
    ctx = (
        f"Slug: {base.slug}\nNom: {base.name}\nMétier: {trade}\nVille: {base.city}\n"
        f"Palette: {base.palette_id}\n"
    )
    try:
        part1 = _xai_chat_json(
            api_key,
            model=model,
            system=(
                "Tu es directeur artistique d'une petite agence web française. "
                "JSON clés: positioning, voice, hero_title, hero_subtitle, cta_primary, "
                "cta_secondary, meta_description, gallery_caption, "
                "services (3–5 {title, lead, detail, for_whom}), "
                "flagship {title, body, aside}, process_steps (3 {title, body}). "
                + rules
            ),
            user=ctx + "Rédige le hero, les prestations et le process — concrets, distinctifs.",
        )
        plan = _merge_xai_plan(base, part1)
        try:
            part2 = _xai_chat_json(
                api_key,
                model=model,
                system=(
                    "Tu complètes une vitrine artisan française déjà structurée. "
                    "JSON clés: story {title, body}, values (3 {title, body}), "
                    "audience (3 strings), zone_blurb, zone_cities (ville réelle + "
                    "'communes voisines' seulement — n'invente pas de quartiers), "
                    "faq (3–4 {q,a}). " + rules
                ),
                user=(
                    ctx
                    + f"Positionnement: {plan.positioning}\n"
                    + "Complète histoire, valeurs, audience, zone et FAQ."
                ),
            )
            plan = _merge_xai_plan(plan, part2)
        except Exception as e:  # noqa: BLE001
            log.warning(
                "xAI plan phase 2 partielle pour %r : %s — conservation phase 1",
                base.slug,
                type(e).__name__,
            )
        return plan
    except XaiUnknownModelError as e:
        log.warning(
            "WARNING: modèle xAI inconnu / refusé %r (HTTP %s) — plan curaté varié",
            e.model,
            e.http_code,
        )
        return None
    except Exception as e:  # noqa: BLE001
        log.warning("xAI site plan indisponible pour %r : %s", base.slug, type(e).__name__)
        return None


def resolve_site_plan(
    *,
    slug: str,
    name: str,
    city: str,
    trade: str,
    allow_llm: bool = True,
    profile: TradeProfile | None = None,
) -> SitePlan:
    profile = profile or curated_or_fallback(trade)
    base = build_curated_plan(slug=slug, name=name, city=city, trade=trade, profile=profile)
    if allow_llm:
        enriched = xai_enrich_plan(base, trade=trade)
        if enriched:
            return enriched
    return base
