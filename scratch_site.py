#!/usr/bin/env python3
"""
Scratch demo sites — Grok generates unique full HTML+CSS per business.

Hard post-processing (never trust the model alone):
  - strip JS / external resources / iframes / forms
  - inject banner, sticky call, gallery markers, real tel:
  - forbid invented reviews / ratings / years / labels / prices
  - fallback to the template generator on failure
"""
from __future__ import annotations

import html as html_lib
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from design_variety import (
    inject_preset_css,
    pick_design_preset,
    stamp_preset_attrs,
)
from photo_banks import (
    canonicalize_trade,
    file_content_hash,
    pick_trade_images,
    resolve_trade_images,
    subject_of,
)
from trade_profiles import (
    DEFAULT_XAI_MODEL,
    XaiUnknownModelError,
    curated_or_fallback,
    match_profile_key,
    resolved_xai_model,
)

log = logging.getLogger("scratch_site")

XAI_API_URL = "https://api.x.ai/v1/chat/completions"
# Wall-clock budget per xAI call (streaming uses this as overall deadline).
# Phase 2 HTML often needs 3–6 min on grok-4.7 — keep generous defaults.
SCRATCH_TIMEOUT_S = int(os.getenv("XAI_SCRATCH_TIMEOUT_S") or os.getenv("XAI_TIMEOUT_S") or "600")
SCRATCH_PHASE1_TIMEOUT_S = int(os.getenv("XAI_SCRATCH_PHASE1_TIMEOUT_S") or "150")
SCRATCH_PHASE2_TIMEOUT_S = int(
    os.getenv("XAI_SCRATCH_PHASE2_TIMEOUT_S") or str(SCRATCH_TIMEOUT_S)
)
# Idle gap between SSE chunks (grok-4.7 can pause mid-HTML, parfois >200s).
# Too low → TimeoutError while overall budget still remains (B-21 root cause).
# 300s : tolère les longues pauses de grok-4.7 pour réduire les secours (sortie inchangée).
SCRATCH_STREAM_IDLE_TIMEOUT_S = int(os.getenv("XAI_SCRATCH_STREAM_IDLE_TIMEOUT_S") or "300")
SCRATCH_MAX_ATTEMPTS = int(os.getenv("XAI_SCRATCH_MAX_ATTEMPTS") or "1")  # HTTP tries per call
SCRATCH_RETRY_BACKOFF_S = (2.0, 4.0)
SCRATCH_PHASE2_MAX_TOKENS = int(os.getenv("XAI_SCRATCH_PHASE2_MAX_TOKENS") or "2800")
SCRATCH_PHASE2_RETRY_MAX_TOKENS = int(os.getenv("XAI_SCRATCH_PHASE2_RETRY_MAX_TOKENS") or "2200")
# Cap phase-1 CSS so phase-2 prompt stays compact (oversized CSS slows HTML gen).
SCRATCH_CSS_MAX_CHARS = int(os.getenv("XAI_SCRATCH_CSS_MAX_CHARS") or "3200")
# Whole-generation wall budget (~11 min target). Plus de marge pour survivre
# aux pauses de grok-4.7 SANS grossir la sortie (reste 2800 tokens) → moins de secours.
SCRATCH_TOTAL_BUDGET_S = int(os.getenv("XAI_SCRATCH_TOTAL_BUDGET_S") or "660")
DEMO_ASSET_V = os.getenv("DEMO_ASSET_V") or "20261018w"

# Short headings that are OK even if 1–2 words
_ALLOWED_SHORT_TITLES = frozenset(
    {
        "contact",
        "contacts",
        "faq",
        "services",
        "service",
        "zone",
        "réalisations",
        "realisations",
        "à propos",
        "a propos",
        "tarifs",
        "horaires",
        "accès",
        "acces",
        "équipe",
        "equipe",
        "méthode",
        "methode",
        "dépannage",
        "depannage",
        "urgence",
        "chauffage",
        "plomberie",
        "électricité",
        "electricite",
        "jardin",
        "coaching",
        "téléphone",
        "telephone",
        "adresse",
        "email",
        "e-mail",
        "interventions",
        "intervention",
        "prestations",
        "prestation",
        "galerie",
        "photos",
        "avis",
        "parcours",
        "processus",
        "étapes",
        "etapes",
        "pourquoi nous",
        "qui sommes-nous",
    }
)

# Decorative / poetic 1–2 word titles that confuse clients
_ABSTRACT_TITLE_RE = re.compile(
    r"(?i)^\s*(le\s+|la\s+|les\s+|l['’])?"
    r"(bruit|nuit|cuivre|net|mousse|main|four|fil|air|g[eé]o|phi|"
    r"clart[eé]|cacao|tenue|grilles|appel|s[eé]ance|r[eé]p[eé]tition|"
    r"atelier|geste|gestes|lumi[eè]re lente|cartes?)\s*"
    r"([./·|].*)?\s*$"
)

_INTERNAL_COPY_RES = [
    re.compile(r"(?i)remplacez\s+par\s+vos"),
    re.compile(r"(?i)espace\s+client\s*/\s*upload"),
    re.compile(r"(?i)photo\s+[àa]\s+venir\s*[·\-|].*remplac"),
    re.compile(r"(?i)exemples?\s+de\s+finitions\s*[—\-–].*remplacez"),
    re.compile(r"(?i)ces\s+emplacements\s+se\s+(rempliront|remplacent)"),
    re.compile(r"(?i)client\s+upload"),
    re.compile(r"(?i)m[eé]tier\s+photo\s*-\s*ajouter"),
]

# Prompt-leak / robotic meta phrasing — never ship to clients
_META_COPY_RES = [
    re.compile(r"(?i)le\s+m[eé]tier\s+est\b"),
    re.compile(r"(?i)le\s+lieu\s+est\b"),
    re.compile(r"(?i)l['’]adresse\s+indiqu[eée]e?\s+est\b"),
    re.compile(r"(?i)le\s+contact\s+est\b"),
    re.compile(r"(?i)trois\s+r[eé]ponses?\s+courtes"),
    re.compile(r"(?i)le\s+point\s+de\s+d[eé]part\s+est\b"),
    re.compile(r"(?i)r[eé]alis[eé]e?s?\s+sur\s+le\s+terrain"),
    re.compile(r"(?i)chantiers?\s+r[eé]els?\b"),
    re.compile(r"(?i)nos\s+propres\s+(chantiers|r[eé]alisations)"),
    re.compile(r"(?i)la\s+photo\s+montre\b"),
    re.compile(r"(?i)les?\s+photos?\s+montrent\b"),
    re.compile(r"(?i)montre\s+bien\s+ce\s+que\s+c['’]est"),
]

# Tags allowed in the final document (plus html/head/body/meta/title/link/style)
ALLOWED_TAGS = frozenset(
    {
        "html",
        "head",
        "body",
        "meta",
        "title",
        "link",
        "style",
        "div",
        "span",
        "section",
        "article",
        "header",
        "footer",
        "main",
        "nav",
        "aside",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "p",
        "a",
        "ul",
        "ol",
        "li",
        "figure",
        "figcaption",
        "img",
        "picture",
        "source",
        "strong",
        "em",
        "b",
        "i",
        "small",
        "br",
        "hr",
        "blockquote",
        "cite",
        "time",
        "address",
        "dl",
        "dt",
        "dd",
        "table",
        "thead",
        "tbody",
        "tr",
        "th",
        "td",
        "button",
        "label",
        "details",
        "summary",
        "svg",
        "path",
        "circle",
        "rect",
        "line",
        "polyline",
        "polygon",
        "g",
        "defs",
        "use",
        "symbol",
        "title",
        "desc",
    }
)

FORBIDDEN_CLAIM_RES = [
    re.compile(r"\b\d{1,4}\s*avis\b", re.I),
    re.compile(r"[★☆⭐]{2,}|\b\d([.,]\d)?\s*/\s*5\b"),
    re.compile(r"\bdepuis\s+(19|20)\d{2}\b", re.I),
    re.compile(r"\b(certifi[ée]e?|labellis[ée]e?|agr[ée]{1,2}e?|qualibat|rge\b)\b", re.I),
    re.compile(r"\b\d+\s*(ans?\s+d['’]exp[eé]rience|années?\s+d['’]exp[eé]rience)\b", re.I),
    re.compile(r"\b(à\s+partir\s+de|dès|des)\s*\d+\s*€|\b\d+\s*€\s*/\s*(h|heure|mois|jour)\b", re.I),
    re.compile(r"\bgoogle\s*(avis|rating|note)\b", re.I),
]

PHOTO_BLURBS: dict[str, str] = {
    "coach": "séance coaching / sport / salle",
    "plombier": "plomberie / sanitaire / tuyauterie",
    "electricien": "électricité / tableau / éclairage",
    "électricien": "électricité / tableau / éclairage",
    "paysagiste": "exemple d'ambiance jardin (illustration stock, pas un chantier client)",
    "coiffeur": "salon / coupe / coiffure",
    "peintre": "peinture / chantier intérieur",
    "menuisier": "bois / atelier / menuiserie",
    "photographe": "photo / studio / prise de vue",
    "local": "chantier / atelier local",
}


@dataclass
class ScratchMetrics:
    mode: str = "scratch"
    ok: bool = False
    fallback: bool = False
    fallback_reason: str = ""
    model: str = ""
    duration_s: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    attempts: int = 0
    content_source: str = "scratch"


@dataclass
class ScratchResult:
    html: str
    metrics: ScratchMetrics
    used_images: list[str] = field(default_factory=list)


def demo_mode() -> str:
    raw = (os.getenv("DEMO_MODE") or "scratch").strip().lower()
    if raw in ("template", "tpl", "curated", "library"):
        return "template"
    return "scratch"


def _trade_photo_catalog(
    trade: str, slug: str = "", *, limit: int | None = None
) -> list[dict[str, str]]:
    """Métier-safe photo list via photo_banks (never cross-family); mélangée par slug."""
    profile = curated_or_fallback(trade)
    key = match_profile_key(trade) or profile.key
    pool = resolve_trade_images(trade) or resolve_trade_images(key) or resolve_trade_images(profile.key)
    n = int(limit) if limit else 5
    files = pick_trade_images(trade, slug, limit=n) if pool else []
    if not files and pool:
        files = pool[:n]
    prefix = (files[0].split("-")[0] if files else "").casefold()
    blurb = PHOTO_BLURBS.get(prefix) or PHOTO_BLURBS.get(key.casefold()) or "ambiance métier"
    out = []
    for i, fname in enumerate(files, 1):
        subj = subject_of(fname)
        desc = f"{subj} — {blurb}" if subj else f"{blurb} — photo {i}/{len(files)}"
        out.append(
            {
                "path": f"/demo-assets/images/{fname}?v={DEMO_ASSET_V}",
                "file": fname,
                "subject": subj,
                "desc": desc,
            }
        )
    return out


def _structure_brief(trade: str, slug: str = "", *, preset_brief: str = "") -> str:
    """Force layout variety by métier + slug preset — avoid same 3-card clone."""
    if preset_brief:
        slug_l = (slug or "").casefold()
        extra = ""
        key = (
            match_profile_key(trade)
            or canonicalize_trade(trade)
            or curated_or_fallback(trade).key
            or ""
        ).casefold()
        if key == "pâtisserie" and "galerie" in slug_l:
            extra = (
                " PRIORITÉ SLUG: première section après hero = photos produits "
                "(pas de FAQ above the fold)."
            )
        if key == "paysagiste" and ("avant" in slug_l or "apres" in slug_l or "après" in slug_l):
            extra = (
                " PRIORITÉ SLUG: galerie d'exemples d'ambiance (légendes « Exemple — … »)."
            )
        return (
            preset_brief
            + extra
            + " Titres/légendes DOIVENT matcher le sujet de chaque photo. "
            "INTERDIT ligne de grille incomplète (5 cartes en 3 col)."
        )
    # Fallback if preset unavailable
    key = (
        match_profile_key(trade)
        or canonicalize_trade(trade)
        or curated_or_fallback(trade).key
        or ""
    ).casefold()
    seed = sum(ord(c) for c in (slug or trade or "x")) % 3
    variants = (
        "Varie: commence par contact/urgence, puis services en liste, puis zone.",
        "Varie: hero éditorial, puis FAQ courte, puis services (2 ou 4 blocs, pas 3), puis zone.",
        "Varie: hero split, une longue section métier, puis zone, contact en premier plan.",
    )
    base = {
        "paysagiste": (
            "STRUCTURE (paysagiste): hero → galerie exemples ambiance → services LISTE "
            "→ zone → contact → FAQ. Photos = illustrations, pas chantiers réels."
        ),
        "électricien": (
            "STRUCTURE (électricien): hero+tél → interventions LISTE/2 col → photo → zone → FAQ → contact."
        ),
        "coach sportif": (
            "STRUCTURE (coach): hero → formats liste/2 blocs → séance → zone → FAQ → contact."
        ),
    }.get(key)
    return (
        (base or variants[seed])
        + " INTERDIT de toujours produire: hero → 3 cartes → « Comment se déroule » → Zone → FAQ."
    )


def _section_body_text(inner: str) -> str:
    """Visible text in a section, headings stripped (for emptiness checks)."""
    cleaned = re.sub(r"(?is)<!--.*?-->", "", inner or "")
    cleaned = re.sub(r"(?is)<h[1-6]\b[^>]*>.*?</h[1-6]\s*>", " ", cleaned)
    cleaned = re.sub(r"(?is)<[^>]+>", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _strip_empty_sections(html: str) -> str:
    """Drop empty / heading-only <section> shells (FAQ bar with no questions, etc.)."""

    def _repl(m: re.Match[str]) -> str:
        full = m.group(0)
        attrs = m.group(1) or ""
        inner = m.group(2) or ""
        if re.search(r"(?i)id=[\"']realisations|class=[\"'][^\"']*av-realisations", attrs):
            return full  # handled by gallery inject
        if re.search(r"(?i)id=[\"']contact", attrs):
            return full  # repaired by _ensure_contact_block
        # Hero-ish: keep
        if re.search(r"(?i)id=[\"']hero|class=[\"'][^\"']*\bhero\b", attrs):
            return full
        body = _section_body_text(inner)
        # Truly empty
        if len(re.sub(r"\s+", "", body)) < 8:
            return ""
        # FAQ / accordion with heading but no Q&A payload
        is_faq = bool(
            re.search(r"(?i)id=[\"']faq|class=[\"'][^\"']*\bfaq\b", attrs)
            or re.search(r"(?i)questions?|comptoir|souvent", inner[:400])
        )
        has_qa = bool(
            re.search(r"(?is)<details\b", inner)
            or re.search(r"(?is)<(dt|dd)\b", inner)
            or len(re.findall(r"(?is)<(li|p|article)\b", inner)) >= 2
        )
        if is_faq and not has_qa and len(body.split()) < 18:
            return ""
        # Heading-only (or heading + decoration): no real content
        if len(body.split()) < 6 and not re.search(r"(?is)<(img|figure|ul|ol|details)\b", inner):
            return ""
        return full

    return re.sub(r"(?is)<section(\b[^>]*)>(.*?)</section\s*>", _repl, html)


def _repair_truncated_list_items(html: str) -> str:
    """Drop <li> cut mid-sentence (Grok truncation) — e.g. « Revenir entre » alone."""

    def _li(m: re.Match[str]) -> str:
        full = m.group(0)
        inner = m.group(1) or ""
        text = re.sub(r"(?is)<[^>]+>", " ", inner)
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            return ""
        # Truncated: very short, or ends mid-phrase without punctuation
        words = text.split()
        if len(words) <= 2 and not re.search(r"[.!?…:]$", text):
            return ""
        if len(text) < 28 and not re.search(r"[.!?…]$", text) and not re.search(
            r"(?is)<(p|img|ul|ol)\b", inner
        ):
            # incomplete like "Revenir entre"
            return ""
        return full

    return re.sub(r"(?is)<li\b[^>]*>(.*?)</li\s*>", _li, html)


def _ensure_contact_block(
    html: str,
    *,
    name: str,
    phone_display: str,
    tel: str,
    city: str,
) -> str:
    """If #contact is missing, empty, or unstyled, inject a styled contact card."""
    esc = html_lib.escape
    need = False
    m = re.search(r'(?is)<section\b[^>]*id=["\']contact["\'][^>]*>(.*?)</section\s*>', html)
    if not m:
        need = True
    else:
        inner = m.group(1)
        inner_txt = re.sub(r"(?is)<[^>]+>", " ", inner)
        unstyled = "av-contact-card" not in inner
        thin = len(re.sub(r"\s+", "", inner_txt)) < 24 or not re.search(
            r"(?i)tel:|appeler|\d{2}\s*\d{2}", inner
        )
        if thin or unstyled:
            need = True
            html = re.sub(
                r'(?is)<section\b[^>]*id=["\']contact["\'][^>]*>.*?</section\s*>',
                "",
                html,
                count=1,
            )
    if not need:
        return html
    phone_line = (
        f'<a class="btn cta" href="tel:{esc(tel, quote=True)}">Appeler {esc(phone_display)}</a>'
        if tel
        else ""
    )
    city_line = f"<p class=\"av-contact-city\">{esc(city)}</p>" if city else ""
    block = (
        f'<section id="contact" class="av-contact">\n'
        f'<div class="av-contact-card">\n'
        f"<h2>Contacter {esc(name)}</h2>\n"
        f"{city_line}\n"
        f"{phone_line}\n"
        f"</div>\n</section>\n"
    )
    if "REALISATIONS_START" in html:
        html = html.replace("<!-- REALISATIONS_START -->", block + "<!-- REALISATIONS_START -->", 1)
    elif re.search(r"(?i)<footer\b", html):
        html = re.sub(r"(?i)<footer\b", block + "<footer", html, count=1)
    else:
        html = re.sub(r"(?i)</body>", block + "</body>", html, count=1)
    return html


def _stream_content_usable(content: str) -> bool:
    """True if a truncated stream still has enough HTML to salvage."""
    text = (content or "").strip()
    if len(text) < 400:
        return False
    low = text.casefold()
    has_body = "<body" in low or "<section" in low or "<main" in low
    has_signal = "<h1" in low or "<header" in low or "tel:" in low
    return has_body and has_signal


def _finalize_stream_usage(content: str, usage: dict[str, int]) -> dict[str, int]:
    if usage.get("total_tokens"):
        return usage
    approx = max(1, len(content) // 4)
    return {
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "completion_tokens": approx,
        "total_tokens": int(usage.get("prompt_tokens") or 0) + approx,
    }


def _xai_chat_stream(
    api_key: str,
    *,
    model: str,
    messages: list[dict[str, str]],
    temperature: float,
    max_tokens: int,
    overall_timeout_s: float,
) -> tuple[str, dict[str, int]]:
    """
    Streaming chat completions — avoids one hard read-timeout on large HTML.
    Per-chunk socket timeout; overall wall-clock deadline.
    On idle/overall timeout, salvage partial HTML when usable (B-21).
    """
    body = {
        "model": model,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": True,
        "messages": messages,
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        XAI_API_URL,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "Accept": "text/event-stream",
        },
        method="POST",
    )
    # Idle gap: grok-4.7 often pauses >120s mid-HTML / long TTFT on phase1.
    # Must track the call budget (not overall/2) — overall/2 made phase1 idle≈90s
    # and killed gens with 0 tokens before any SSE data (B-21 round2).
    idle_cap = float(SCRATCH_STREAM_IDLE_TIMEOUT_S)
    chunk_timeout = min(idle_cap, max(90.0, float(overall_timeout_s) - 5.0))
    deadline = time.monotonic() + overall_timeout_s
    parts: list[str] = []
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    timed_out = False
    timeout_kind = ""

    def _salvage_or_raise() -> tuple[str, dict[str, int]]:
        content = "".join(parts).strip()
        if _stream_content_usable(content):
            log.warning(
                "scratch stream %s — salvaging partial HTML (%d chars)",
                timeout_kind or "timeout",
                len(content),
            )
            return content, _finalize_stream_usage(content, usage)
        raise TimeoutError(f"scratch stream {timeout_kind or 'timeout'}")

    try:
        with urllib.request.urlopen(req, timeout=chunk_timeout) as resp:
            while True:
                if time.monotonic() > deadline:
                    timed_out = True
                    timeout_kind = "overall deadline"
                    break
                try:
                    line = resp.readline()
                except TimeoutError:
                    timed_out = True
                    timeout_kind = "idle gap"
                    break
                except OSError as e:
                    # socket.timeout / Broken pipe mid-stream
                    if "timed out" in str(e).lower() or isinstance(e, TimeoutError):
                        timed_out = True
                        timeout_kind = "socket idle"
                        break
                    raise
                if not line:
                    break
                try:
                    decoded = line.decode("utf-8", errors="replace").strip()
                except Exception:
                    continue
                if not decoded:
                    continue
                if decoded.startswith(":"):  # SSE comment / keepalive
                    continue
                if not decoded.startswith("data:"):
                    continue
                payload = decoded[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                choices = obj.get("choices") or []
                if choices:
                    delta = (choices[0].get("delta") or {}).get("content")
                    if isinstance(delta, str) and delta:
                        parts.append(delta)
                u = obj.get("usage")
                if isinstance(u, dict):
                    usage = {
                        "prompt_tokens": int(u.get("prompt_tokens") or usage["prompt_tokens"]),
                        "completion_tokens": int(
                            u.get("completion_tokens") or usage["completion_tokens"]
                        ),
                        "total_tokens": int(u.get("total_tokens") or usage["total_tokens"]),
                    }
    except TimeoutError:
        timed_out = True
        timeout_kind = timeout_kind or "connect/read"
        return _salvage_or_raise()
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
            timed_out = True
            timeout_kind = "urlopen"
            return _salvage_or_raise()
        raise

    if timed_out:
        return _salvage_or_raise()

    content = "".join(parts).strip()
    if not content:
        raise ValueError("empty stream content")
    return content, _finalize_stream_usage(content, usage)


def _xai_chat(
    api_key: str,
    *,
    model: str,
    messages: list[dict[str, str]],
    temperature: float = 0.75,
    max_tokens: int | None = None,
    stream: bool = True,
    timeout_s: float | None = None,
) -> tuple[str, dict[str, int]]:
    mt = int(
        max_tokens
        if max_tokens is not None
        else (os.getenv("XAI_SCRATCH_MAX_TOKENS") or "3200")
    )
    call_timeout = float(timeout_s if timeout_s is not None else SCRATCH_TIMEOUT_S)
    last_err: Exception | None = None
    for attempt in range(1, SCRATCH_MAX_ATTEMPTS + 1):
        try:
            if stream:
                return _xai_chat_stream(
                    api_key,
                    model=model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=mt,
                    overall_timeout_s=call_timeout,
                )
            body = {
                "model": model,
                "temperature": temperature,
                "max_tokens": mt,
                "messages": messages,
            }
            data = json.dumps(body).encode("utf-8")
            req = urllib.request.Request(
                XAI_API_URL,
                data=data,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=call_timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            content = payload["choices"][0]["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise ValueError("empty content")
            usage = payload.get("usage") or {}
            tokens = {
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
                "total_tokens": int(usage.get("total_tokens") or 0),
            }
            return content, tokens
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
            # Some gateways reject stream_options — retry once without it via non-stream
            if e.code in (400, 422) and stream and "stream" in low:
                log.warning("scratch stream rejected (HTTP %s) — fallback non-stream", e.code)
                stream = False
                last_err = e
                continue
            last_err = e
            log.warning("scratch xAI HTTP %s tentative %d/%d", e.code, attempt, SCRATCH_MAX_ATTEMPTS)
            if e.code in (401, 403, 404):
                break
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError, ValueError, KeyError) as e:
            last_err = e
            log.warning(
                "scratch xAI %s tentative %d/%d",
                type(e).__name__,
                attempt,
                SCRATCH_MAX_ATTEMPTS,
            )
        if attempt < SCRATCH_MAX_ATTEMPTS:
            time.sleep(SCRATCH_RETRY_BACKOFF_S[min(attempt - 1, len(SCRATCH_RETRY_BACKOFF_S) - 1)])
    assert last_err is not None
    raise last_err


def _extract_html(raw: str) -> str:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:html|HTML)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    # Prefer full document
    m = re.search(r"(?is)<!DOCTYPE\s+html\b.*</html\s*>", text)
    if m:
        return m.group(0)
    m = re.search(r"(?is)<html\b.*</html\s*>", text)
    if m:
        return m.group(0)
    return text


def _strip_dangerous(html: str) -> str:
    # Remove scripts / noscript / iframes / forms / objects / embeds / templates
    for tag in (
        "script",
        "noscript",
        "iframe",
        "form",
        "object",
        "embed",
        "template",
        "canvas",
        "video",
        "audio",
        "base",
    ):
        html = re.sub(rf"(?is)<{tag}\b[^>]*>.*?</{tag}\s*>", "", html)
        html = re.sub(rf"(?is)<{tag}\b[^>]*/?>", "", html)
    # Event handler attributes
    html = re.sub(r"(?i)\s+on[a-z]+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", "", html)
    # javascript: urls
    html = re.sub(r"(?i)(href|src)\s*=\s*([\"'])\s*javascript:[^\"']*\2", r'\1="#"', html)
    # External stylesheets / preloads (keep only /demo-assets/ or relative empty)
    def _link_ok(m: re.Match[str]) -> str:
        tag = m.group(0)
        href = re.search(r'(?i)href\s*=\s*([\"\'])(.*?)\1', tag)
        if not href:
            return ""
        url = href.group(2).strip()
        if url.startswith("/demo-assets/") or url.startswith("data:"):
            return tag
        return ""

    html = re.sub(r"(?is)<link\b[^>]*>", _link_ok, html)
    # External images → drop src if not local
    def _img_fix(m: re.Match[str]) -> str:
        tag = m.group(0)
        src_m = re.search(r'(?i)src\s*=\s*([\"\'])(.*?)\1', tag)
        if not src_m:
            return ""
        src = src_m.group(2).strip()
        if src.startswith("/demo-assets/") or src.startswith("data:image/"):
            # ensure cache-bust on demo-assets images
            if src.startswith("/demo-assets/images/") and "?v=" not in src:
                src2 = src + f"?v={DEMO_ASSET_V}"
                return tag.replace(src_m.group(0), f'src="{src2}"')
            return tag
        return ""

    html = re.sub(r"(?is)<img\b[^>]*/?>", _img_fix, html)
    # Remove @import url(http...) from style blocks
    def _clean_style(m: re.Match[str]) -> str:
        css = m.group(1)
        css = re.sub(r"(?is)@import\b[^;]+;", "", css)
        css = re.sub(r"(?is)url\(\s*['\"]?https?://[^)]+\)", "url()", css)
        return f"<style>{css}</style>"

    html = re.sub(r"(?is)<style\b[^>]*>(.*?)</style\s*>", _clean_style, html)
    return html


def _visible_text_sample(html: str) -> str:
    """Strip platform chrome + scripts/styles for copy checks."""
    sample = re.sub(
        r"(?is)<!-- BANDEAU_DEMO_START -->.*?<!-- BANDEAU_DEMO_END -->",
        " ",
        html,
    )
    sample = re.sub(
        r"(?is)<!-- REALISATIONS_START -->.*?<!-- REALISATIONS_END -->",
        " ",
        sample,
    )
    sample = re.sub(r'(?is)<a\b[^>]*class="[^"]*demo-sticky-call[^"]*"[^>]*>.*?</a>', " ", sample)
    sample = re.sub(r"(?is)<style\b[^>]*>.*?</style\s*>", " ", sample)
    sample = re.sub(r"(?is)<script\b[^>]*>.*?</script\s*>", " ", sample)
    sample = re.sub(r"(?is)<[^>]+>", " ", sample)
    return re.sub(r"\s+", " ", sample).strip()


def _forbidden_claims(html: str) -> list[str]:
    hits = []
    sample = _visible_text_sample(html)
    for rx in FORBIDDEN_CLAIM_RES:
        if rx.search(sample):
            hits.append(rx.pattern)
    return hits


def _image_filenames(html: str) -> list[str]:
    names: list[str] = []
    for m in re.finditer(r"""(?is)<img\b[^>]*\bsrc\s*=\s*["']([^"']+)["']""", html):
        src = m.group(1)
        mm = re.search(r"/([a-z0-9-]+\.jpe?g)", src, re.I)
        if mm:
            names.append(mm.group(1).casefold())
    return names


# Near-identical stock variants (visual lookalikes) — merged with content-hash aliases
_NEAR_DUP_GROUPS: tuple[frozenset[str], ...] = (
    frozenset(
        {
            "paysagiste-01.jpg",
            "paysagiste-04.jpg",
            "paysagiste-06.jpg",
            "hero-paysagiste.jpg",
        }
    ),
    # Trowel / outils desk family — includes byte-aliases of 03 (e.g. 45)
    frozenset(
        {
            "paysagiste-02.jpg",
            "paysagiste-03.jpg",
            "paysagiste-05.jpg",
            "paysagiste-45.jpg",
        }
    ),
    frozenset({"paysagiste-09.jpg", "paysagiste-13.jpg"}),
    frozenset({"electricien-06.jpg", "electricien-08.jpg"}),
    frozenset(
        {"electricien-01.jpg", "electricien-02.jpg", "hero-electricien.jpg"}
    ),
    frozenset({"plombier-01.jpg", "hero-plombier.jpg", "hero-plombier-v2.jpg"}),
    frozenset({"coach-01.jpg", "hero-coach.jpg"}),
)

_HASH_TO_NAMES: dict[str, set[str]] | None = None


def _basename_image(src_or_name: str) -> str:
    raw = (src_or_name or "").split("?")[0].strip()
    mm = re.search(r"/([a-z0-9_.-]+\.jpe?g)$", raw, re.I)
    if mm:
        return mm.group(1).casefold()
    return raw.split("/")[-1].casefold()


def _content_hash_index() -> dict[str, set[str]]:
    """Map content MD5 → set of filenames that share those bytes."""
    global _HASH_TO_NAMES
    if _HASH_TO_NAMES is not None:
        return _HASH_TO_NAMES
    from pathlib import Path
    import hashlib

    idx: dict[str, set[str]] = {}
    for cand in (
        Path(__file__).resolve().parent / "static" / "demo-assets" / "images",
        Path("/opt/waas-factory/static/demo-assets/images"),
        Path("/var/www/sites/_vitrine/demo-assets/images"),
    ):
        if not cand.is_dir():
            continue
        for p in cand.glob("*.jpg"):
            try:
                h = hashlib.md5(p.read_bytes()).hexdigest()
            except OSError:
                continue
            idx.setdefault(h, set()).add(p.name.casefold())
        break  # first existing dir is enough
    _HASH_TO_NAMES = idx
    return idx


def _near_dup_key(name: str) -> str:
    n = _basename_image(name)
    for g in _NEAR_DUP_GROUPS:
        if n in g:
            return "near:" + sorted(g)[0]
    return n


def _image_identity_key(src_or_name: str) -> str:
    """
    Stable image identity for anti-dupe.
    Prefer content hash (real bytes), then near-dup visual group, then filename.
    Never rely on bank entry IDs alone — aliases like paysagiste-45≡03 must collide.
    """
    n = _basename_image(src_or_name)
    h = file_content_hash(n)
    idx = _content_hash_index()
    aliases = set()
    if h and h in idx:
        aliases |= idx[h]
    aliases.add(n)
    # If any alias is in a near-dup group, use that group key
    near_keys = {_near_dup_key(a) for a in aliases}
    near_pref = sorted(k for k in near_keys if k.startswith("near:"))
    if near_pref:
        return near_pref[0]
    if h:
        return f"content:{h}"
    return f"file:{n}"


def _dedupe_page_images(html: str) -> str:
    """Keep first <img> per content-hash / near-dup identity; drop later duplicates."""
    seen: set[str] = set()

    def _repl(m: re.Match[str]) -> str:
        tag = m.group(0)
        mm = re.search(r"""\bsrc\s*=\s*["']([^"']+)["']""", tag, re.I)
        if not mm:
            return tag
        key = _image_identity_key(mm.group(1))
        if key in seen:
            return ""
        seen.add(key)
        return tag

    return re.sub(r"(?is)<img\b[^>]*/?>", _repl, html)


def _abstract_heading_errors(html: str) -> list[str]:
    """Reject 1–2 word poetic/decorative section titles."""
    errs: list[str] = []
    # Ignore injected gallery block
    sample = re.sub(
        r"(?is)<!-- REALISATIONS_START -->.*?<!-- REALISATIONS_END -->",
        " ",
        html,
    )
    for m in re.finditer(r"(?is)<h([2-4])\b[^>]*>(.*?)</h\1>", sample):
        raw = re.sub(r"(?is)<[^>]+>", " ", m.group(2))
        text = re.sub(r"\s+", " ", raw).strip()
        if not text:
            continue
        words = [w for w in re.split(r"\s+", text) if w]
        key = text.casefold().strip(" .·|/—–-")
        if key in _ALLOWED_SHORT_TITLES:
            continue
        # Only flag decorative/poetic titles — not plain nouns like "Téléphone"
        if len(words) <= 2 and _ABSTRACT_TITLE_RE.match(text):
            errs.append(f"abstract title: {text[:60]}")
        elif len(words) == 1 and len(key) <= 6 and key not in _ALLOWED_SHORT_TITLES:
            # Ultra-short single tokens (Nuit, Net, Fil…) without allowlist
            if not key.isdigit() and _ABSTRACT_TITLE_RE.match(text):
                errs.append(f"abstract title: {text[:60]}")
    return errs[:6]


def _visible_blob(html: str) -> str:
    sample = _visible_text_sample(html)
    gallery = ""
    gm = re.search(
        r"(?is)<!-- REALISATIONS_START -->(.*)<!-- REALISATIONS_END -->",
        html,
    )
    if gm:
        gallery = re.sub(r"(?is)<[^>]+>", " ", gm.group(1))
    return f"{sample} {gallery}"


def _internal_copy_errors(html: str) -> list[str]:
    """Dev/placeholder strings must not appear in visible chrome."""
    blob = _visible_blob(html)
    hits = []
    for rx in _INTERNAL_COPY_RES:
        if rx.search(blob):
            hits.append(rx.pattern)
    return hits


def _meta_copy_errors(html: str) -> list[str]:
    """Reject robotic prompt-leak / meta phrasing and false « own works » claims."""
    blob = _visible_blob(html)
    hits = []
    for rx in _META_COPY_RES:
        if rx.search(blob):
            hits.append(rx.pattern)
    return hits


def _ensure_hero_image(html: str, catalog: list[dict[str, str]]) -> str:
    """
    If the hero/header has no <img>, inject the first catalog photo so we never
    ship a blank gray hero box (Chauffe QA).
    """
    if not catalog:
        return html
    # Find first hero-ish block
    hero_re = re.compile(
        r"(?is)(<(?:header|section|div)\b[^>]*(?:id=[\"']hero[\"']|"
        r"class=[\"'][^\"']*\b(?:hero|demo-hero|hero-wrap|hero-grid)\b[^\"']*[\"'])[^>]*>)"
        r"(.*?)(</(?:header|section|div)\s*>)",
        re.I,
    )
    m = hero_re.search(html or "")
    if not m:
        # No marked hero — try first <header>
        m = re.search(
            r"(?is)(<header\b[^>]*>)(.*?)(</header\s*>)",
            html or "",
        )
    if not m:
        return html
    inner = m.group(2) or ""
    if re.search(r"(?is)<img\b", inner):
        return html
    # Skip if a CSS background-image already carries a demo-assets photo
    if re.search(r"(?i)demo-assets/images/", inner) and re.search(
        r"(?i)background(?:-image)?\s*:", inner
    ):
        return html
    item = catalog[0]
    src = html_lib.escape(item.get("path") or "", quote=True)
    alt = html_lib.escape(
        (item.get("subject") or item.get("desc") or "Illustration métier")[:120],
        quote=True,
    )
    # Prefer a media layer (never a floating card inside the nav/hero-bar).
    # type-led / editorial: band AFTER copy (avoids mid-hero floating over absolute photo).
    structure = ""
    sm = re.search(r'data-av-hero-structure=["\']([^"\']+)["\']', html or "")
    if sm:
        structure = sm.group(1).strip().casefold()
    if structure in ("type-led-band", "centered-editorial"):
        media = (
            f'<div class="av-hero-photo-band" aria-hidden="true">'
            f'<img src="{src}" alt="{alt}" width="1600" height="1067" '
            f'style="width:100%;height:min(40vh,26rem);object-fit:cover;display:block">'
            f"</div>"
        )
        rebuilt = m.group(1) + inner + media + m.group(3)
    else:
        media = (
            f'<div class="demo-hero-media av-hero-photo" aria-hidden="true">'
            f'<img src="{src}" alt="{alt}" width="1600" height="1067" '
            f'style="width:100%;height:100%;object-fit:cover;display:block">'
            f'<div class="demo-hero-veil"></div></div>'
        )
        # Keep nav first then media+content — not media inside nav.
        rebuilt = m.group(1) + media + inner + m.group(3)
    return html[: m.start()] + rebuilt + html[m.end() :]


def _ensure_above_fold_chrome(
    html: str,
    *,
    name: str,
    city: str,
    phone_display: str,
    tel: str,
    trade_label: str = "",
) -> str:
    """
    B-28 QA: type-led / centered-editorial / magazine must show
    name + one-line pitch + call CTA above the fold (not photo-only / empty band).
    """
    if not html:
        return html
    sm = re.search(r'data-av-hero-structure=["\']([^"\']+)["\']', html)
    structure = (sm.group(1) if sm else "").casefold()
    if structure not in (
        "type-led-band",
        "centered-editorial",
        "magazine-asymmetric",
    ):
        return html
    hero_re = re.compile(
        r"(?is)(<(?:header|section)\b[^>]*(?:id=[\"']hero[\"']|"
        r"class=[\"'][^\"']*\b(?:hero|demo-hero)\b[^\"']*[\"'])[^>]*>)"
        r"(.*?)(</(?:header|section)\s*>)",
    )
    m = hero_re.search(html) or re.search(
        r"(?is)(<header\b[^>]*>)(.*?)(</header\s*>)", html
    )
    if not m:
        return html
    open_t, inner, close_t = m.group(1), m.group(2) or "", m.group(3)
    has_h1 = bool(re.search(r"(?is)<h1\b", inner))
    has_p = bool(re.search(r"(?is)<p\b", inner))
    has_cta = bool(
        re.search(r"(?is)(href=[\"']tel:|class=[\"'][^\"']*\b(cta|btn)\b)", inner)
    )
    if has_h1 and has_p and has_cta:
        return html
    safe_name = html_lib.escape(name or "Artisan")
    safe_city = html_lib.escape(city or "")
    label = html_lib.escape(trade_label or "Artisan")
    pitch = (
        f"{label} à {safe_city}. Un interlocuteur joignable, un devis clair, "
        f"un travail soigné."
        if safe_city
        else f"{label}. Un interlocuteur joignable, un devis clair."
    )
    tel_href = html_lib.escape(tel or "", quote=True)
    phone_txt = html_lib.escape(phone_display or tel or "Nous appeler")
    bits: list[str] = []
    if not has_h1:
        bits.append(f"<h1>{safe_name}</h1>")
    if not has_p:
        bits.append(f"<p>{pitch}</p>")
    if not has_cta and (tel or phone_display):
        bits.append(
            f'<a class="cta" href="tel:{tel_href}">Appeler — {phone_txt}</a>'
        )
    if not bits:
        return html
    # Insert chrome before first media/figure so photo stays below
    media_m = re.search(
        r"(?is)(<(?:div|figure)\b[^>]*class=[\"'][^\"']*\b(?:demo-hero-media|av-hero-photo|"
        r"av-hero-photo-band|hero-photo|demo-hero-editorial-media)\b)",
        inner,
    )
    chrome = "\n".join(bits) + "\n"
    if media_m:
        new_inner = inner[: media_m.start()] + chrome + inner[media_m.start() :]
    else:
        new_inner = chrome + inner
    return html[: m.start()] + open_t + new_inner + close_t + html[m.end() :]


def _scrub_meta_copy(html: str) -> str:
    """
    Drop sentences that leak prompt/meta phrasing so a near-good scratch page
    is kept instead of falling back to the template (B-21 Pulse-mode failures).
    """
    if not html or not _meta_copy_errors(html):
        return html

    def _clean_text(chunk: str) -> str:
        # Split on sentence-ish boundaries; drop any piece matching a meta leak
        parts = re.split(r"(?<=[.!?…])\s+", chunk)
        kept: list[str] = []
        for p in parts:
            if not p.strip():
                continue
            if any(rx.search(p) for rx in _META_COPY_RES):
                continue
            kept.append(p.strip())
        return " ".join(kept)

    def _repl_text(m: re.Match[str]) -> str:
        before, text, after = m.group(1), m.group(2), m.group(3)
        if not text.strip() or "<" in text:
            return m.group(0)
        cleaned = _clean_text(text)
        if cleaned == text.strip():
            return m.group(0)
        return f"{before}{cleaned}{after}"

    # Text nodes between tags (skip style/script via prior strip)
    out = re.sub(r"(>)([^<]+)(<)", _repl_text, html)
    if _meta_copy_errors(out):
        # Fallback: drop whole paragraphs/list items that still leak
        def _blank_el(m: re.Match[str]) -> str:
            inner = m.group(3) or ""
            if any(rx.search(inner) for rx in _META_COPY_RES):
                return ""
            return m.group(0)

        out = re.sub(r"(?is)<(p|li|figcaption|span)(\b[^>]*)>(.*?)</\1\s*>", _blank_el, out)
    return out


def _validate_html(html: str, *, check_claims: bool = True) -> list[str]:
    errs: list[str] = []
    low = html.casefold()
    if "<html" not in low or "</html>" not in low:
        errs.append("missing html root")
    if "<body" not in low or "</body>" not in low:
        errs.append("missing body")
    if "<style" not in low and "demo.css" not in low:
        errs.append("missing css")
    if "<h1" not in low:
        errs.append("missing h1")
    if "viewport" not in low:
        errs.append("missing viewport meta")
    # Responsive cue
    if "@media" not in low and "clamp(" not in low and "max-width" not in low:
        errs.append("weak responsive css")
    # Enough content
    text = _visible_text_sample(html)
    if len(text) < 280:
        errs.append("too little copy")
    if check_claims:
        claims = _forbidden_claims(html)
        if claims:
            errs.append("forbidden claims: " + "; ".join(claims[:3]))
    # Scripts other than JSON-LD
    for m in re.finditer(r"(?is)<script\b([^>]*)>", html):
        attrs = m.group(1).casefold()
        if "ld+json" not in attrs:
            errs.append("script remaining")
            break
    if re.search(r"(?i)<iframe\b", html):
        errs.append("iframe remaining")
    # Duplicate images — identity by content hash / near-dup / URL basename (not bank IDs)
    names = _image_filenames(html)
    from collections import Counter

    dups = [n for n, c in Counter(names).items() if c > 1]
    if dups:
        errs.append("duplicate images: " + ", ".join(dups[:4]))
    id_keys = [_image_identity_key(n) for n in names]
    id_dups = [k for k, c in Counter(id_keys).items() if c > 1]
    if id_dups:
        errs.append("duplicate image identity: " + ", ".join(id_dups[:4]))
    # Tone: no abstract micro-titles
    errs.extend(_abstract_heading_errors(html))
    # No internal/dev placeholder copy
    internal = _internal_copy_errors(html)
    if internal:
        errs.append("internal copy visible: " + "; ".join(internal[:2]))
    meta = _meta_copy_errors(html)
    if meta:
        errs.append("meta/prompt-leak copy: " + "; ".join(meta[:3]))
    # No rendered section without real content (except contact/réalisations handled elsewhere)
    for m in re.finditer(r"(?is)<section(\b[^>]*)>(.*?)</section\s*>", html):
        attrs, inner = m.group(1) or "", m.group(2) or ""
        if re.search(
            r"(?i)id=[\"'](contact|realisations|hero)|class=[\"'][^\"']*\b(hero|av-contact|av-realisations)\b",
            attrs,
        ):
            continue
        body = _section_body_text(inner)
        if len(body.split()) < 6 and not re.search(r"(?is)<(img|figure|ul|ol|details)\b", inner):
            errs.append("empty section rendered")
            break
    # B-28: reject blank grey / marble / neutral placeholder tiles (Madeleine)
    # Inspect markup only (strip <style>/<script>) — our CSS also names these classes to hide them.
    _markup = re.sub(r"(?is)<style\b[^>]*>.*?</style\s*>", " ", html or "")
    _markup = re.sub(r"(?is)<script\b[^>]*>.*?</script\s*>", " ", _markup)
    if re.search(
        r"(?i)<[^>]+class=[\"'][^\"']*\b(?:demo-hero-media-neutral|demo-neutral-visual|"
        r"demo-gallery-placeholder)\b|"
        r"placeholder[-_ ]?(tile|card|img|image)|marble[-_ ]?placeholder",
        _markup,
    ):
        errs.append("empty/neutral placeholder tile")
    # Empty figures / cards without a real <img src=...>
    for m in re.finditer(
        r"(?is)<(?:figure|article|div)\b[^>]*class=[\"'][^\"']*\b(?:card|tile|produit|gallery|vitrine)[^\"']*[\"'][^>]*>(.*?)</(?:figure|article|div)\s*>",
        html or "",
    ):
        inner = m.group(1) or ""
        has_img = re.search(r"(?is)<img\b[^>]*\bsrc=[\"'](?!data:)[^\"']+[\"']", inner)
        has_text = len(_section_body_text(inner).split()) >= 3
        if not has_img and not has_text and len(inner.strip()) < 40:
            errs.append("empty placeholder card/tile")
            break
    return errs


def _inject_devis(doc: str, slug: str) -> str:
    """Injecte le configurateur de devis (POST /api/quote/{slug}) sur les sites scratch.

    Le chemin template pose déjà la section via render_page ; en scratch, _strip_dangerous
    et le filtre ld+json retirent <form>/<script>. On ré-injecte donc la section APRÈS tous
    ces nettoyages (et après la validation), on garantit <body data-plan-slug> (lu par
    devis.js) et on mappe les couleurs --demo-* du template sur les --av-* du site.
    """
    if not slug or 'id="devis"' in doc.casefold():
        return doc
    try:
        from demo_templates import DEVIS_SECTION_TEMPLATE
    except Exception:  # noqa: BLE001
        return doc
    # devis.js lit le slug via document.body.dataset.planSlug
    if "data-plan-slug" not in doc:
        slug_attr = f' data-plan-slug="{html_lib.escape(slug, quote=True)}"'
        doc = re.sub(r"(?i)<body\b", lambda m: m.group(0) + slug_attr, doc, count=1)
    # Le template devis utilise --demo-* ; en scratch on expose --av-* → on les relie
    bridge = (
        '<style id="av-devis-bridge">.va-devis{'
        "--demo-accent:var(--av-accent);--demo-ink:var(--av-ink);"
        "--demo-paper:var(--av-paper);--demo-display:var(--av-font-display);}</style>"
    )
    section = bridge + DEVIS_SECTION_TEMPLATE
    anchor = re.search(r'(?i)<section\b[^>]*\bid=["\']contact["\']', doc)
    if anchor:
        i = anchor.start()
        return doc[:i] + section + "\n" + doc[i:]
    m = re.search(r"(?i)</body\s*>", doc)
    if m:
        i = m.start()
        return doc[:i] + section + "\n" + doc[i:]
    return doc + section


def _inject_platform(
    html: str,
    *,
    name: str,
    phone_display: str,
    tel: str,
    checkout: str,
    price_label: str,
    banner_html: str,
    gallery_caption: str,
    meta_description: str,
    json_ld: str,
    catalog: list[dict[str, str]],
) -> str:
    esc = html_lib.escape
    # Ensure charset + viewport + robots
    if re.search(r"(?i)<meta[^>]+charset=", html) is None:
        html = re.sub(
            r"(?i)<head([^>]*)>",
            r'<head\1>\n  <meta charset="UTF-8">',
            html,
            count=1,
        )
    if "viewport" not in html.casefold():
        html = re.sub(
            r"(?i)<head([^>]*)>",
            r'<head\1>\n  <meta name="viewport" content="width=device-width, initial-scale=1.0">',
            html,
            count=1,
        )
    if "noindex" not in html.casefold():
        html = re.sub(
            r"(?i)<head([^>]*)>",
            r'<head\1>\n  <meta name="robots" content="noindex, nofollow">',
            html,
            count=1,
        )
    # Meta description
    if re.search(r'(?i)<meta[^>]+name=["\']description["\']', html) is None:
        html = re.sub(
            r"(?i)</head>",
            f'  <meta name="description" content="{esc(meta_description, quote=True)}">\n</head>',
            html,
            count=1,
        )
    # JSON-LD (same pattern as template demos; kept by post-filter)
    if "application/ld+json" not in html.casefold() and json_ld:
        html = re.sub(
            r"(?i)</head>",
            f'  <script type="application/ld+json">{json_ld}</script>\n</head>',
            html,
            count=1,
        )

    # Platform chrome CSS (banner + sticky) — local classes, CSP-safe inline style block
    # Also neutralize bare `header{position:absolute}` leaking onto gallery / sections.
    chrome_css = """
<style id="av-chrome">
.demo-banner{position:sticky;top:0;z-index:60;background:#312e81;color:#fff;text-align:center;
padding:.75rem 1rem;font-size:.875rem;font-weight:500;font-family:system-ui,sans-serif}
.demo-banner a{color:#fff;font-weight:700;text-decoration:underline}
.demo-sticky-call{position:fixed;left:1rem;right:1rem;bottom:1rem;z-index:50;display:flex;
align-items:center;justify-content:center;gap:.5rem;padding:.9rem 1rem;border-radius:999px;
background:#0f1c1a;color:#fff;text-decoration:none;font-family:system-ui,sans-serif;
box-shadow:0 10px 30px rgb(0 0 0 / 25%)}
.demo-sticky-call span{opacity:.85;font-size:.8rem}
.demo-sticky-call strong{font-size:1rem}
@media(min-width:800px){.demo-sticky-call{display:none}}
body{margin:0;padding-bottom:5.5rem}
/* Collapse empty hero bands (>200px void) */
.hero,.demo-hero,header.hero,.hero-wrap,.hero-inner,.hero-grid,.hero > .wrap{
min-height:0!important;padding-top:.75rem!important;padding-bottom:1.25rem!important;
margin-top:0!important;gap:.65rem!important}
.hero::before,.hero::after,.demo-hero::before{display:none!important;content:none!important;height:0!important}
/* Kill empty band / absolute headers outside the hero */
.av-realisations,.av-realisations-head,section:not(.hero) > header,section.band > header{
position:static!important;inset:auto!important;display:block!important;
padding:0 0 .75rem!important;letter-spacing:normal!important;text-transform:none!important;
color:inherit!important;background:transparent!important}
.av-realisations{margin:2.5rem auto;padding:0 1.25rem;max-width:72rem}
.av-realisations-head h2{font-size:clamp(1.35rem,2.5vw,1.75rem);margin:0 0 .35rem}
.av-realisations-head p{margin:0;opacity:.85;font-size:.95rem}
/* Empty client gallery: elegant cue — no empty + boxes */
.av-realisations-empty{margin:2.75rem auto;padding:0 1.25rem;max-width:40rem}
.av-realisations-cue{margin:0;padding:1.35rem 1.5rem;border-radius:1rem;
border:1px dashed rgb(0 0 0 / 14%);background:linear-gradient(160deg,#f7f5f1,#eef2f6);
color:#3a3f45;font-family:system-ui,sans-serif;font-size:1rem;line-height:1.45;
text-align:center;letter-spacing:.01em}
.demo-gallery-placeholder{display:grid;gap:.85rem;margin-top:1rem}
@media(min-width:640px){.demo-gallery-placeholder{grid-template-columns:repeat(3,1fr)}}
.demo-gallery-slot{margin:0;border-radius:1rem;overflow:hidden;position:relative;
border:1px solid rgb(0 0 0 / 8%);background:#f0f2f5}
.demo-gallery-empty{height:12rem;background:linear-gradient(135deg,#e8f0f7,#f7f5f1)}
.demo-gallery-empty::after{content:"+";display:grid;place-items:center;height:100%;
font-size:2rem;opacity:.4}
.demo-gallery-slot figcaption{padding:.65rem .75rem;font-size:.85rem;opacity:.8}
/* Desktop CTAs must not stretch full width */
@media(min-width:800px){
a.cta,a.btn,.cta,.btn,.demo-btn,a[class*="cta"],a[class*="btn"]{
width:auto!important;max-width:20rem;display:inline-flex!important;justify-content:center}
.hero .cta,.hero .btn,.hero a.cta{margin-right:.5rem}
}
/* Equal card heights when product/service grids are used */
.grid-3,.cards,.card-grid,[class*="grid-3"]{align-items:stretch}
.grid-3 > article,.grid-3 > .card,article.card,.cards > article{
display:flex;flex-direction:column;height:100%;min-height:100%}
.grid-3 img,article.card img{width:100%;height:12rem;object-fit:cover;display:block}
/* Contact block — dark brand card; CTA = light chip + dark brand text (WCAG AA ≥4.5:1).
   Avoid leaf/ink on pine (Rennes green-on-green ~2:1). Paper-on-pine/cocoa/navy ≥10:1. */
.av-contact{padding:2.75rem 1.25rem;margin:0 auto;max-width:72rem}
.av-contact-card{display:grid;gap:1rem;justify-items:start;max-width:28rem;
padding:1.75rem 1.85rem;border-radius:1.15rem;
background:var(--pine,var(--cocoa,var(--cacao,var(--navy,var(--ink,#0f1c1a)))));
color:var(--paper,var(--bg,#f4f1ea));
box-shadow:0 12px 36px color-mix(in srgb, var(--ink,#0f1c1a) 22%, transparent)}
.av-contact-card h2{margin:0;font-size:clamp(1.25rem,2.2vw,1.55rem);font-weight:650;
letter-spacing:-.01em;color:var(--paper,var(--bg,#fff));font-family:var(--serif,Georgia,serif)}
.av-contact-city{margin:0;opacity:.85;font-size:.95rem;font-family:var(--sans,system-ui,sans-serif)}
.av-contact-card a.btn,.av-contact-card a.cta{
display:inline-flex!important;align-items:center;justify-content:center;
padding:.85rem 1.25rem;border-radius:.75rem;
background:var(--paper,var(--bg,#f4f1ea))!important;
color:var(--pine,var(--cocoa,var(--cacao,var(--navy,var(--ink,#0f1c1a)))))!important;
text-decoration:none;font-weight:650;font-family:var(--sans,system-ui,sans-serif);font-size:1rem;
border:0;width:auto!important;max-width:100%}
.av-contact-card a.btn:hover,.av-contact-card a.cta:hover{filter:brightness(.97)}
/* Odd gallery rows: center orphan in 2-col .split (e.g. Rennes 5th photo) */
@media(min-width:640px){
.split:has(> :only-child) > *{
grid-column:1/-1;justify-self:center;width:100%;max-width:calc((100% - 2rem)/2)}
}
/* Incomplete 3-col rows (5 cards → orphan pair): center trailing items */
@media(min-width:720px){
.grid-3 > :nth-child(3n+1):nth-last-child(2),
.grid-3 > :nth-child(3n+1):nth-last-child(2) ~ *,
.cards > :nth-child(3n+1):nth-last-child(2),
.cards > :nth-child(3n+1):nth-last-child(2) ~ *,
[class*="grid-3"] > :nth-child(3n+1):nth-last-child(2),
[class*="grid-3"] > :nth-child(3n+1):nth-last-child(2) ~ *,
.vitrine-grid > :nth-child(3n+1):nth-last-child(2),
.vitrine-grid > :nth-child(3n+1):nth-last-child(2) ~ *{
justify-self:center}
.grid-3:has(> :nth-child(5):last-child),
.cards:has(> :nth-child(5):last-child),
.vitrine-grid:has(> :nth-child(5):last-child){
grid-template-columns:repeat(6,1fr)}
.grid-3:has(> :nth-child(5):last-child) > *,
.cards:has(> :nth-child(5):last-child) > *,
.vitrine-grid:has(> :nth-child(5):last-child) > *{grid-column:span 2}
}
section.band:empty,section:empty{display:none!important}
</style>
""".strip()
    if 'id="av-chrome"' not in html:
        html = re.sub(r"(?i)</head>", chrome_css + "\n</head>", html, count=1)
    else:
        # Refresh chrome CSS on regen
        html = re.sub(
            r'(?is)<style id="av-chrome">.*?</style>',
            chrome_css,
            html,
            count=1,
        )

    # Banner after <body>
    if "BANDEAU_DEMO_START" not in html:
        html = re.sub(
            r"(?i)(<body\b[^>]*>)",
            r"\1\n  " + banner_html,
            html,
            count=1,
        )

    # Force tel: links to real number when we have one
    if tel:
        html = re.sub(
            r'(?i)href\s*=\s*([\"\'])tel:[^\"\']*\1',
            f'href="tel:{esc(tel, quote=True)}"',
            html,
        )

    # Client réalisations: elegant cue until photos exist (no empty « + » boxes).
    # Use <div> not <header> — Grok often styles bare header{position:absolute}.
    cue = "Vos photos apparaîtront ici."
    gallery = (
        "<!-- REALISATIONS_START -->\n"
        '<section id="realisations" class="av-realisations av-realisations-empty" '
        'aria-label="Galerie client">\n'
        f'<p class="av-realisations-cue">{esc(cue)}</p>\n'
        "</section>\n"
        "<!-- REALISATIONS_END -->\n"
    )
    if "REALISATIONS_START" in html:
        # Replace unfinished empty-slot galleries with the elegant cue
        html = re.sub(
            r"(?is)<!-- REALISATIONS_START -->.*?<!-- REALISATIONS_END -->",
            gallery.strip(),
            html,
            count=1,
        )
    else:
        if re.search(r"(?i)<footer\b", html):
            html = re.sub(r"(?i)<footer\b", gallery + "<footer", html, count=1)
        else:
            html = re.sub(r"(?i)</body>", gallery + "</body>", html, count=1)
    _ = (catalog, gallery_caption)  # caption reserved when client photos land

    # Sticky mobile call
    if tel and "demo-sticky-call" not in html:
        sticky = (
            f'<a class="demo-sticky-call" href="tel:{esc(tel, quote=True)}" '
            f'aria-label="Appeler {esc(phone_display, quote=True)}">'
            f"<span>Appeler</span><strong>{esc(phone_display)}</strong></a>"
        )
        html = re.sub(r"(?i)</body>", sticky + "\n</body>", html, count=1)

    # Checkout CTA presence: if banner missing link and checkout set, already in banner_html
    _ = (checkout, price_label, name)
    return html


def _build_prompt(
    *,
    name: str,
    trade: str,
    city: str,
    phone_display: str,
    address: str,
    catalog: list[dict[str, str]],
    error_feedback: str = "",
) -> tuple[str, str]:
    profile = curated_or_fallback(trade)
    photos_txt = "\n".join(f"- {c['path']} — {c['desc']}" for c in catalog) or "- (aucune photo locale)"
    system = (
        "Tu es directeur artistique d'une agence web haut de gamme française. "
        "Tu produis UN document HTML5 complet et unique pour une vitrine artisan locale. "
        "Réponds UNIQUEMENT avec le HTML (commence par <!DOCTYPE html>), sans markdown.\n\n"
        "Direction artistique: chaque site doit avoir une identité visuelle DISTINCTE "
        "(typographie, couleurs, rythme éditorial, composition) adaptée au métier et au nom. "
        "Layouts éditoriaux variés (pas une grille générique à 6 cartes). "
        "Professionnel, lisible, chaleureux, adapté mobile.\n\n"
        "CSS: uniquement dans <style> dans le <head>. Polices: system-ui / Georgia / "
        "ou @font-face vers /demo-assets/fonts/*.woff2 uniquement "
        "(ibm-plex-sans-latin-wght-normal.woff2, fraunces-latin-wght-normal.woff2).\n"
        "Images: UNIQUEMENT les chemins fournis (src locaux /demo-assets/...). "
        "Pas d'URL externes, pas de CDN, pas de Google Fonts.\n\n"
        "INTERDIT: JavaScript, script, iframe, form, object, onclick/on*, "
        "avis inventés, notes ★, nombres d'avis, années d'expérience, certifications, "
        "labels, tarifs/prix, badges Google fictifs.\n"
        "INCLUS: header/hero fort avec le NOM de l'entreprise en signal principal, "
        "1 accroche courte, 1 phrase d'appui, CTA (liens #contact et tel: si téléphone), "
        "sections métier concrètes, zone d'intervention (ville réelle seulement), "
        "FAQ courte, contact, footer. "
        "Accessible: contrastes corrects, texte ≥16px body, responsive (media queries)."
    )
    user = (
        f"Entreprise: {name}\n"
        f"Métier: {trade} (libellé: {profile.label})\n"
        f"Ville: {city}\n"
        f"Adresse: {address or city}\n"
        f"Téléphone affichable: {phone_display or '(non fourni)'}\n"
        f"Photos locales disponibles:\n{photos_txt}\n\n"
        "Crée une vitrine unique, haut de gamme, très différente d'un template générique. HTML compact (< ~500 lignes), CSS efficace, 4–6 sections max, 2–4 images max."
    )
    if error_feedback:
        user += (
            "\n\nCORRECTION OBLIGATOIRE — la version précédente a échoué la validation:\n"
            f"{error_feedback}\n"
            "Corrige ces points et renvoie un HTML complet valide."
        )
    return system, user


def _parse_design_json(raw: str) -> dict[str, str]:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json|css)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    data: dict[str, Any] | None = None
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            data = parsed
    except json.JSONDecodeError:
        # Extract first JSON object if model wrapped extra prose
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            try:
                parsed = json.loads(m.group(0))
                if isinstance(parsed, dict):
                    data = parsed
            except json.JSONDecodeError:
                data = None
    if data is None:
        # Treat bare CSS as design payload
        if "{" in text and re.search(r"[a-zA-Z-]{2,}\s*:", text):
            return {"mood": "css-raw", "layout": "editorial", "css": _cap_css(text)}
        raise ValueError("design not object")
    css = str(data.get("css") or data.get("stylesheet") or "").strip()
    if len(css) < 40:
        raise ValueError("css too short")
    css = _cap_css(css)
    return {
        "mood": str(data.get("mood") or data.get("direction") or "")[:200],
        "layout": str(data.get("layout") or "")[:200],
        "css": css,
    }


def _cap_css(css: str, max_chars: int | None = None) -> str:
    """Trim oversized phase-1 CSS so phase-2 prompts stay fast."""
    limit = int(max_chars if max_chars is not None else SCRATCH_CSS_MAX_CHARS)
    text = (css or "").strip()
    if len(text) <= limit:
        return text
    # Prefer cutting on a rule boundary near the limit
    cut = text[:limit]
    brace = cut.rfind("}")
    if brace >= limit // 2:
        cut = cut[: brace + 1]
    log.info("scratch CSS capped %d → %d chars", len(text), len(cut))
    return cut


def _close_truncated_html(doc: str) -> str:
    """Salvage stream-truncated HTML so validate/inject can proceed."""
    low = doc.casefold()
    body_i = low.find("<body")
    head_close = low.find("</head>")
    # Model often opens <body> inside <head> when truncated — close head first
    if body_i > 0 and "<head" in low and (head_close < 0 or head_close > body_i):
        doc = doc[:body_i] + "</head>\n" + doc[body_i:]
        low = doc.casefold()
        body_i = low.find("<body")
    if "<body" in low and "</body>" not in low:
        for tag in ("section", "div", "article", "main", "header", "footer"):
            opens = len(re.findall(rf"(?i)<{tag}\b", doc))
            closes = len(re.findall(rf"(?i)</{tag}\s*>", doc))
            if opens > closes:
                doc += f"</{tag}>" * (opens - closes)
        doc += "\n</body>"
        low = doc.casefold()
    if "<html" in low and "</html>" not in low:
        if "</body>" not in low:
            doc += "\n</body>"
        doc += "\n</html>"
    return doc


def _assemble_document(css: str, body_inner: str, *, title: str, meta_description: str) -> str:
    """Wrap Grok body fragment or full doc into a complete HTML document."""
    esc = html_lib.escape
    frag = (body_inner or "").strip()
    # If model returned a full document, prefer extracting body + keep its styles merged
    full = _extract_html(frag)
    if "<html" in full.casefold():
        full = _close_truncated_html(full)
        # Inject/replace our design CSS as primary
        if re.search(r"(?is)<style\b", full):
            full = re.sub(
                r"(?is)(<head\b[^>]*>)",
                rf'\1\n<style id="av-scratch-css">{css}</style>',
                full,
                count=1,
            )
        else:
            if "</head>" in full.casefold():
                full = re.sub(
                    r"(?i)</head>",
                    f'<style id="av-scratch-css">{css}</style>\n</head>',
                    full,
                    count=1,
                )
            else:
                full = re.sub(
                    r"(?is)(<head\b[^>]*>)",
                    rf'\1\n<style id="av-scratch-css">{css}</style>',
                    full,
                    count=1,
                )
        return _close_truncated_html(full)
    # Body fragment only
    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="robots" content="noindex, nofollow">
  <title>{esc(title)}</title>
  <meta name="description" content="{esc(meta_description, quote=True)}">
  <style id="av-scratch-css">{css}</style>
</head>
<body>
{frag}
</body>
</html>
"""


def generate_scratch_html(
    *,
    name: str,
    trade: str,
    city: str,
    phone_display: str,
    tel: str,
    address: str,
    checkout: str,
    price_label: str,
    banner_html: str,
    meta_description: str,
    json_ld: str,
    allow_llm: bool = True,
    slug: str = "",
) -> ScratchResult:
    """
    Generate a unique HTML page via Grok (2 shorter calls), sanitize, inject chrome.
    On failure returns empty html with metrics.fallback=True (caller uses template).
    """
    metrics = ScratchMetrics(mode="scratch", model=resolved_xai_model())
    t0 = time.monotonic()
    api_key = (os.getenv("XAI_API_KEY") or "").strip()
    preset = pick_design_preset(slug or name, trade)
    catalog = _trade_photo_catalog(trade, slug=slug, limit=preset.photo_limit)
    gallery_caption = curated_or_fallback(trade).gallery_caption
    profile = curated_or_fallback(trade)

    if not allow_llm or not api_key:
        metrics.fallback = True
        metrics.fallback_reason = "no_llm_or_key"
        metrics.duration_s = round(time.monotonic() - t0, 2)
        return ScratchResult(html="", metrics=metrics)

    tokens_acc = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    last_errs: list[str] = []
    model = metrics.model or resolved_xai_model()
    # Cap photo list — long catalogs inflate phase-2 prompt / latency
    photos_txt = (
        "\n".join(f"- {c['path']} — {c['desc']}" for c in catalog[:5]) or "- (aucune)"
    )

    def _add_tokens(tokens: dict[str, int]) -> None:
        for k in tokens_acc:
            tokens_acc[k] += tokens.get(k, 0)

    def _remaining_budget() -> float:
        return max(0.0, float(SCRATCH_TOTAL_BUDGET_S) - (time.monotonic() - t0))

    def _phase2_timeout(preferred: float, *, reserve: float = 0.0) -> float:
        """Cap call timeout; `reserve` keeps wall time for compact/non-stream retry (B-28)."""
        rem = _remaining_budget()
        usable = max(0.0, rem - 15.0 - float(reserve))
        return max(45.0, min(float(preferred), usable)) if usable >= 45.0 else 0.0

    def _postprocess(raw_html: str, css: str) -> tuple[str, list[str]]:
        doc = _assemble_document(
            css,
            raw_html,
            title=f"{name} – {profile.label}",
            meta_description=meta_description,
        )
        doc = _strip_dangerous(doc)
        doc = _scrub_meta_copy(doc)
        doc = _dedupe_page_images(doc)
        claim_errs = [
            e
            for e in _validate_html(doc, check_claims=True)
            if e.startswith(
                ("forbidden", "abstract", "duplicate", "internal", "meta/prompt-leak", "near-")
            )
        ]
        doc = _inject_platform(
            doc,
            name=name,
            phone_display=phone_display,
            tel=tel,
            checkout=checkout,
            price_label=price_label,
            banner_html=banner_html,
            gallery_caption=gallery_caption,
            meta_description=meta_description,
            json_ld=json_ld,
            catalog=catalog,
        )
        doc = inject_preset_css(doc, preset)
        doc = stamp_preset_attrs(doc, preset)
        doc = _ensure_hero_image(doc, catalog)
        doc = _ensure_above_fold_chrome(
            doc,
            name=name,
            city=city,
            phone_display=phone_display,
            tel=tel,
            trade_label=profile.label,
        )
        # B-28: strip blank grey / marble / neutral placeholder tiles
        doc = re.sub(
            r'(?is)<div\b[^>]*class=["\'][^"\']*\b(?:demo-hero-media-neutral|demo-gallery-placeholder)[^"\']*["\'][^>]*>.*?</div\s*>',
            "",
            doc,
        )
        doc = re.sub(
            r'(?is)<div\b[^>]*class=["\'][^"\']*\bdemo-neutral-visual\b[^"\']*["\'][^>]*/?>',
            "",
            doc,
        )
        # Hero inject / chrome may reintroduce img dups — collapse again
        doc = _dedupe_page_images(doc)

        def _keep_ld(m: re.Match[str]) -> str:
            return m.group(0) if "application/ld+json" in m.group(0).casefold() else ""

        doc = re.sub(r"(?is)<script\b[^>]*>.*?</script\s*>", _keep_ld, doc)
        doc = _strip_empty_sections(doc)
        doc = _repair_truncated_list_items(doc)
        doc = _ensure_contact_block(
            doc,
            name=name,
            phone_display=phone_display,
            tel=tel,
            city=city,
        )
        errs = _validate_html(doc, check_claims=False) + claim_errs
        # Duplicates are mechanically fixable via _dedupe — never hard-fail scratch
        if not any(
            e.startswith("duplicate") for e in _validate_html(doc, check_claims=False)
        ):
            errs = [e for e in errs if not e.startswith("duplicate")]
        # Configurateur de devis — après strip/validation pour garder <form>/<script>
        doc = _inject_devis(doc, slug)
        return doc, errs

    try:
        # --- Phase 1: art direction + CSS (short) ---
        metrics.attempts = 1
        design_system = (
            "Directeur artistique web FR. Réponds UNIQUEMENT en JSON valide avec clés: "
            "mood (string), layout (string courte), css (string CSS complète pour une "
            "vitrine artisan unique — variables, typo, hero, sections, mobile @media). "
            "Polices: @font-face /demo-assets/fonts/"
            "ibm-plex-sans-latin-wght-normal.woff2 et fraunces-latin-wght-normal.woff2 "
            "ou Georgia/system-ui selon le preset. "
            "Pas d'URL externes. Pas de JS. CSS ≤90 lignes et ≤2800 caractères. "
            "INTERDIT: styler le sélecteur bare `header{position:absolute;inset:...}` "
            "(casse les blocs plus bas). Utilise `.av-topnav` / `.hero-bar` full-width EN HAUT. "
            "INTERDIT nav flottante à mi-hero à côté d'une photo (bandeau vide). "
            "INTERDIT tuiles placeholder gris/marble/neutre sans <img> réelle. "
            "Pas de grand vide au-dessus du hero (padding-top body/hero ≤ 2rem). "
            "Boutons CTA: width auto, max-width 20rem sur desktop. "
            "OBLIGATOIRE: respecter EXACTEMENT les couleurs/typo/HERO STRUCTURE du PRESET "
            "(ne pas converger vers brun/crème si le preset est autre)."
        )
        design_user = (
            f"Entreprise: {name}\nMétier: {trade} ({profile.label})\nVille: {city}\n"
            f"Slug: {slug or '(n/a)'}\n"
            f"{preset.prompt_block()}\n"
            "DA premium DISTINCTE selon le preset — lisible, WCAG AA, pro."
        )
        # Phase 1: one automatic retry on timeout/empty (Volt failure mode: 0 tokens @ ~90s)
        design: dict[str, str] | None = None
        p1_last_err: Exception | None = None
        for p1_try in (1, 2):
            p1_timeout = min(
                float(SCRATCH_PHASE1_TIMEOUT_S),
                max(60.0, _remaining_budget() - 120.0),
            )
            if p1_timeout < 60.0:
                break
            try:
                # Phase 1 needs complete JSON — non-stream avoids idle-gap TimeoutError
                # that burned Sucré/Fuite into template-fallback (B-28).
                raw1, tok1 = _xai_chat(
                    api_key,
                    model=model,
                    messages=[
                        {"role": "system", "content": design_system},
                        {"role": "user", "content": design_user},
                    ],
                    temperature=0.75 if p1_try == 1 else 0.55,
                    max_tokens=1100 if p1_try == 1 else 900,
                    stream=False,
                    timeout_s=p1_timeout,
                )
                _add_tokens(tok1)
                design = _parse_design_json(raw1)
                log.info(
                    "scratch phase1 OK try=%d mood=%r css_len=%d remaining=%.0fs",
                    p1_try,
                    design["mood"][:60],
                    len(design["css"]),
                    _remaining_budget(),
                )
                break
            except (TimeoutError, urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as e:
                p1_last_err = e
                log.warning(
                    "scratch phase1 try %d/2 failed (%s) remaining=%.0fs",
                    p1_try,
                    type(e).__name__,
                    _remaining_budget(),
                )
                time.sleep(1.5)
        if design is None:
            raise p1_last_err or TimeoutError("scratch phase1 failed")

        # --- Phase 2: HTML body using that CSS ---
        struct = _structure_brief(trade, slug=slug, preset_brief=preset.structure_brief)
        html_system = (
            "HTML vitrine artisan FR unique. Réponds UNIQUEMENT HTML5 complet "
            "ou <body>…</body> — pas de markdown. "
            "Utilise le CSS fourni dans <style> (ne le réécris pas). "
            "Images: chemins listés seulement, chaque fichier AU PLUS UNE FOIS; "
            "sujet photo = titre/légende (jamais croissant sous « Gâteaux »). "
            "Photos « exemple »/illustration: légendes honnêtes, jamais chantiers réels. "
            "TON chaleureux, naturel. INTERDIT méta/prompt-leak "
            "(« Le métier est… », « Le lieu est… », « réalisé sur le terrain »…), "
            "JS, iframe, form, avis/notes/années/tarifs inventés, jargon opaque. "
            "Hero NOM + CTA tel:/#contact, sections utiles, FAQ, contact complet "
            "(tél+ville+tel:), footer. Mobile-first ≥16px. COMPACT. "
            "Respecte le layout preset (pas de grille 3 col incomplète). "
            "Varie la structure — pas toujours hero→3 cartes→déroulé→zone→FAQ."
        )
        html_system_compact = (
            "HTML vitrine artisan FR. UNIQUEMENT HTML5 ou <body>. "
            "CSS fourni tel quel. Photos listées 1× max, légendes = sujet. "
            "Ton naturel. Pas de JS/avis inventés/jargon. "
            "Hero+tél, 4–5 sections, FAQ, contact, footer. COMPACT <2200 tokens."
        )

        def _html_user(css: str, *, compact: bool = False) -> str:
            css_slice = css[:2200] if compact else css[:SCRATCH_CSS_MAX_CHARS]
            struct_part = (struct[:280] + "…") if compact and len(struct) > 280 else struct
            return (
                f"Entreprise: {name}\nMétier: {trade}\nVille: {city}\n"
                f"Adresse: {address or city}\n"
                f"Tél: {phone_display or '(non fourni)'}\n"
                f"Direction: {design['mood'][:80]} | {design['layout'][:80]}\n"
                f"{struct_part}\n"
                f"Photos:\n{photos_txt}\n\n"
                f"CSS:\n```css\n{css_slice}\n```\n"
                "HTML maintenant — chaleureux, structure distincte, compact."
            )

        html_user = _html_user(design["css"], compact=False)
        raw2 = ""
        phase2_err: Exception | None = None
        # Reserve ≥120s so stream idle timeout cannot consume the compact retry window.
        p2_timeout = _phase2_timeout(float(SCRATCH_PHASE2_TIMEOUT_S), reserve=130.0)
        if p2_timeout < 45.0:
            raise TimeoutError("scratch budget exhausted before phase2")

        metrics.attempts = 2
        try:
            raw2, tok2 = _xai_chat(
                api_key,
                model=model,
                messages=[
                    {"role": "system", "content": html_system},
                    {"role": "user", "content": html_user},
                ],
                temperature=0.65,
                max_tokens=SCRATCH_PHASE2_MAX_TOKENS,
                stream=True,
                timeout_s=p2_timeout,
            )
            _add_tokens(tok2)
            log.info(
                "scratch phase2 OK chars=%d remaining=%.0fs",
                len(raw2),
                _remaining_budget(),
            )
        except (TimeoutError, urllib.error.URLError, OSError, ValueError) as e:
            phase2_err = e
            log.warning(
                "scratch phase2 failed (%s) — smart compact retry if budget allows",
                type(e).__name__,
            )

        # Smart retry: shorter prompt + fewer tokens (root-cause path for idle timeouts)
        if phase2_err is not None or (raw2 and not _stream_content_usable(raw2)):
            retry_timeout = _phase2_timeout(min(240.0, float(SCRATCH_PHASE2_TIMEOUT_S)))
            if retry_timeout >= 60.0:
                metrics.attempts = 3
                compact_css = _cap_css(design["css"], max_chars=min(2200, SCRATCH_CSS_MAX_CHARS))
                design["css"] = compact_css
                html_user = _html_user(compact_css, compact=True)
                log.info(
                    "scratch phase2 compact retry timeout=%.0fs max_tokens=%d",
                    retry_timeout,
                    SCRATCH_PHASE2_RETRY_MAX_TOKENS,
                )
                try:
                    # Non-stream compact retry — stream idle gaps caused Fuite→template (B-28).
                    raw2, tok2 = _xai_chat(
                        api_key,
                        model=model,
                        messages=[
                            {"role": "system", "content": html_system_compact},
                            {"role": "user", "content": html_user},
                        ],
                        temperature=0.55,
                        max_tokens=SCRATCH_PHASE2_RETRY_MAX_TOKENS,
                        stream=False,
                        timeout_s=retry_timeout,
                    )
                    _add_tokens(tok2)
                    phase2_err = None
                except (TimeoutError, urllib.error.URLError, OSError, ValueError) as e:
                    log.warning("scratch phase2 compact retry failed (%s)", type(e).__name__)
                    if phase2_err is not None and not _stream_content_usable(raw2):
                        raise e
            elif phase2_err is not None:
                raise phase2_err

        if not _stream_content_usable(raw2):
            raise phase2_err or ValueError("phase2 empty html")

        html, errs = _postprocess(raw2, design["css"])

        def _hard_validation_errs(items: list[str]) -> list[str]:
            # Duplicates / near-dups: soft — dedupe already ran; keep scratch HTML
            return [
                e
                for e in items
                if e.startswith(("forbidden", "abstract", "meta/prompt-leak"))
            ]

        # One corrective HTML-only retry if validation fails and budget remains
        if errs:
            last_errs = errs
            fix_timeout = _phase2_timeout(min(180.0, float(SCRATCH_PHASE2_TIMEOUT_S)))
            if fix_timeout >= 60.0:
                log.warning(
                    "scratch validation fail, corrective retry (budget=%.0fs): %s",
                    fix_timeout,
                    "; ".join(errs[:6]),
                )
                metrics.attempts = max(metrics.attempts, 4)
                fix_user = (
                    html_user
                    + "\n\nCORRECTION: échec validation: "
                    + "; ".join(errs[:8])
                    + "\nRenvoie un HTML complet corrigé, compact."
                )
                try:
                    raw3, tok3 = _xai_chat(
                        api_key,
                        model=model,
                        messages=[
                            {"role": "system", "content": html_system_compact},
                            {"role": "user", "content": fix_user},
                        ],
                        temperature=0.4,
                        max_tokens=SCRATCH_PHASE2_RETRY_MAX_TOKENS,
                        stream=True,
                        timeout_s=fix_timeout,
                    )
                    _add_tokens(tok3)
                    html, errs = _postprocess(raw3, design["css"])
                except (TimeoutError, urllib.error.URLError, OSError, ValueError) as e:
                    # Keep pre-corrective HTML rather than burning the whole gen (Atelier mode)
                    log.warning(
                        "scratch corrective aborted (%s) — keeping prior HTML soft_ok=%s",
                        type(e).__name__,
                        not bool(_hard_validation_errs(last_errs)),
                    )
                    errs = last_errs
            else:
                log.warning(
                    "scratch validation fail, skip corrective (budget=%.0fs): %s",
                    _remaining_budget(),
                    "; ".join(errs[:6]),
                )

        hard = _hard_validation_errs(errs)
        accept = (not errs) or (bool(html) and not hard and _stream_content_usable(html))
        if accept:
            if errs:
                log.warning(
                    "scratch accepting soft validation warnings: %s",
                    "; ".join(errs[:6]),
                )
            metrics.ok = True
            metrics.content_source = "scratch-xai"
            metrics.prompt_tokens = tokens_acc["prompt_tokens"]
            metrics.completion_tokens = tokens_acc["completion_tokens"]
            metrics.total_tokens = tokens_acc["total_tokens"]
            metrics.duration_s = round(time.monotonic() - t0, 2)
            used = re.findall(
                r"/demo-assets/images/([A-Za-z0-9_.-]+\.(?:jpg|jpeg|png|webp))", html
            )
            log.info(
                "scratch OK name=%r duration=%.1fs tokens=%s attempts=%d",
                name,
                metrics.duration_s,
                metrics.total_tokens,
                metrics.attempts,
            )
            return ScratchResult(html=html, metrics=metrics, used_images=sorted(set(used)))

        last_errs = hard or errs
    except XaiUnknownModelError as e:
        last_errs = [f"unknown_model:{e.model}"]
        metrics.fallback_reason = last_errs[0]
    except Exception as e:  # noqa: BLE001
        last_errs = [type(e).__name__]
        log.warning("scratch generation error: %s", type(e).__name__)

    metrics.ok = False
    metrics.fallback = True
    metrics.fallback_reason = "; ".join(last_errs) or "unknown"
    metrics.prompt_tokens = tokens_acc["prompt_tokens"]
    metrics.completion_tokens = tokens_acc["completion_tokens"]
    metrics.total_tokens = tokens_acc["total_tokens"]
    metrics.duration_s = round(time.monotonic() - t0, 2)
    metrics.content_source = "template-fallback"
    # Explicit last-resort log — operators must see WHY template fired
    log.warning(
        "scratch FALLBACK (last resort) after %.1fs tokens=%s attempts=%d reason=%s "
        "— using template generator",
        metrics.duration_s,
        metrics.total_tokens,
        metrics.attempts,
        metrics.fallback_reason,
    )
    return ScratchResult(html="", metrics=metrics)


def metrics_public_dict(m: ScratchMetrics) -> dict[str, Any]:
    """Safe metrics for meta.json — never includes API key."""
    d = asdict(m)
    return d
