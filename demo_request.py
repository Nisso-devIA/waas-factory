"""
demo_request.py — Demandes de démo gratuite (formulaire vitrine → Telegram admin).

Flux :
  1. POST /api/demo-request → validation + anti-spam → JSON pending + alerte Telegram
  2. Callback Telegram « Générer la démo » → toast immédiat + editMessage « en cours »
     → génération en arrière-plan → edit succès / échec (timeout aligné scratch, ~10 min)
  3. Callback « Refuser » → edit « Refusée », boutons retirés

Pas d'e-mail / SMS automatique vers l'artisan : l'admin transmet le lien.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

log = logging.getLogger("demo_request")

# --------------------------------------------------------------------------- #
# Config (overridable via env; webhook_server may also assign at import time)
# --------------------------------------------------------------------------- #
SITES_DIR = Path(os.getenv("SITES_DIR", "/var/www/sites")).resolve()
DEMO_REQUESTS_DIR = Path(
    os.getenv("DEMO_REQUESTS_DIR") or (SITES_DIR / "_demo_requests")
).resolve()
TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
TELEGRAM_CHAT_ID = (
    os.getenv("TELEGRAM_CHAT_ID") or os.getenv("TELEGRAM_USER_ID") or ""
).strip()
TELEGRAM_WEBHOOK_SECRET = (os.getenv("TELEGRAM_WEBHOOK_SECRET") or "").strip()
RATE_LIMIT_WINDOW_S = int(os.getenv("DEMO_REQUEST_RATE_WINDOW_S", "900"))  # 15 min
RATE_LIMIT_MAX = int(os.getenv("DEMO_REQUEST_RATE_MAX", "3"))


def _resolve_generate_timeout_s() -> int:
    """
    Watchdog Telegram « délai dépassé ».

    Priorité :
      1. DEMO_GENERATE_TIMEOUT_S ou DEMO_GEN_TIMEOUT_SEC (override explicite)
      2. max(XAI_SCRATCH_PHASE2_TIMEOUT_S, XAI_SCRATCH_TIMEOUT_S) + marge (180 s)
      3. défaut 780 s (13 min) — couvre phase2≈660 s + marge

    Exemple VPS : phase2=660 → 660+180 = 840 s.
    """
    for key in ("DEMO_GENERATE_TIMEOUT_S", "DEMO_GEN_TIMEOUT_SEC"):
        raw = (os.getenv(key) or "").strip()
        if raw.isdigit() and int(raw) > 0:
            return int(raw)
    phase_max = 0
    for key in ("XAI_SCRATCH_PHASE2_TIMEOUT_S", "XAI_SCRATCH_TIMEOUT_S"):
        raw = (os.getenv(key) or "").strip()
        if raw.isdigit():
            phase_max = max(phase_max, int(raw))
    if phase_max > 0:
        margin = int((os.getenv("DEMO_GENERATE_TIMEOUT_MARGIN_S") or "180").strip() or "180")
        return phase_max + max(0, margin)
    return 780


# Max wait before marking génération as timed out (aligné scratch / phase2)
GENERATE_TIMEOUT_S = _resolve_generate_timeout_s()
# Hint admin affiché pendant la génération (pas le watchdog)
GENERATE_ETA_HINT = (os.getenv("DEMO_GENERATE_ETA_HINT") or "≈5–9 min").strip() or "≈5–9 min"
try:
    ADMIN_TZ = ZoneInfo(os.getenv("DEMO_ADMIN_TZ", "Europe/Paris"))
except Exception:  # noqa: BLE001 — tzdata manquant → UTC
    ADMIN_TZ = timezone.utc

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE = re.compile(r"^[\d\s.+()\-]{8,25}$")
# Honeypot field name (must stay empty)
HONEYPOT_FIELD = "fax"

_rate_lock = threading.Lock()
_rate_hits: dict[str, list[float]] = {}
# Serialize claim pending/failed/generated → generating (idempotence)
_gen_lock = threading.Lock()
# Active watchdog timers keyed by (request_id, generation_token)
_timeout_timers: dict[str, threading.Timer] = {}
_timeout_lock = threading.Lock()


def ensure_requests_dir() -> Path:
    DEMO_REQUESTS_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(DEMO_REQUESTS_DIR, 0o700)
    except OSError:
        pass
    return DEMO_REQUESTS_DIR


def request_path(request_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", request_id or ""):
        raise ValueError("request_id invalide")
    return ensure_requests_dir() / f"{request_id}.json"


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    ensure_requests_dir()
    tmp = path.with_name(f".{path.name}.tmp")
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    tmp.write_text(payload, encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def load_request(request_id: str) -> dict[str, Any] | None:
    path = request_path(request_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("Lecture demande %s échouée : %s", request_id, e)
        return None


def save_request(data: dict[str, Any]) -> Path:
    rid = data["id"]
    path = request_path(rid)
    atomic_write_json(path, data)
    return path


def client_ip_from_headers(
    headers: dict[str, str] | Any,
    fallback: str | None = None,
) -> str:
    """
    Délègue à demo_limits.resolve_client_ip :
    CF-Connecting-IP uniquement si peer ∈ Cloudflare (ou proxy local de confiance).
    """
    try:
        import demo_limits

        return demo_limits.resolve_client_ip(headers, fallback or "unknown")
    except Exception:  # noqa: BLE001
        fb = (fallback or "unknown").strip() or "unknown"
        return fb


def check_rate_limit(ip: str) -> bool:
    """Return True if allowed, False if rate-limited."""
    now = time.monotonic()
    key = ip or "unknown"
    with _rate_lock:
        hits = [t for t in _rate_hits.get(key, []) if now - t < RATE_LIMIT_WINDOW_S]
        if len(hits) >= RATE_LIMIT_MAX:
            _rate_hits[key] = hits
            return False
        hits.append(now)
        _rate_hits[key] = hits
        return True


def reset_rate_limits() -> None:
    with _rate_lock:
        _rate_hits.clear()


def validate_payload(raw: dict[str, Any]) -> tuple[dict[str, str] | None, str | None]:
    """
    Returns (clean_fields, error_message).
    Honeypot filled → treat as spam (caller returns 204 / silent ok).
    """
    if (raw.get(HONEYPOT_FIELD) or "").strip():
        return None, "honeypot"

    company = (raw.get("company") or raw.get("company_name") or "").strip()
    trade = (raw.get("trade") or raw.get("metier") or "").strip()
    city = (raw.get("city") or raw.get("ville") or "").strip()
    phone = (raw.get("phone") or raw.get("telephone") or "").strip()
    email = (raw.get("email") or "").strip().lower()
    website = (raw.get("website") or raw.get("current_website") or "").strip()
    consent = raw.get("rgpd_consent") or raw.get("consent")

    if consent is True or str(consent).lower() in ("1", "true", "on", "yes"):
        consent_ok = True
    else:
        consent_ok = False

    if not company or len(company) > 120:
        return None, "Nom de l'entreprise requis (max 120 caractères)."
    if not trade or len(trade) > 80:
        return None, "Métier requis (max 80 caractères)."
    if not city or len(city) > 80:
        return None, "Ville requise (max 80 caractères)."
    if not phone or not PHONE_RE.match(phone):
        return None, "Téléphone invalide."
    if not email or not EMAIL_RE.match(email) or len(email) > 120:
        return None, "E-mail invalide."
    if website and len(website) > 200:
        return None, "Site web trop long."
    if website and not re.match(r"^https?://", website, re.I):
        # Allow bare domains — normalize later
        if not re.match(r"^[a-z0-9.-]+\.[a-z]{2,}(/.*)?$", website, re.I):
            return None, "URL du site actuel invalide."
    if not consent_ok:
        return None, "Le consentement RGPD est obligatoire."

    clean = {
        "company": company,
        "trade": trade,
        "city": city,
        "phone": phone,
        "email": email,
        "website": website,
    }
    return clean, None


def create_pending_request(
    fields: dict[str, str],
    ip: str,
    *,
    ip_hash: str | None = None,
    prior_notes: list[str] | None = None,
) -> dict[str, Any]:
    rid = secrets.token_urlsafe(12)
    now = datetime.now(timezone.utc).isoformat()
    try:
        import demo_limits

        email_norm = demo_limits.normalize_email(fields["email"])
        hashed = ip_hash or demo_limits.hash_ip(ip)
    except Exception:  # noqa: BLE001
        email_norm = (fields["email"] or "").strip().lower()
        hashed = ip_hash or "unknown"

    data: dict[str, Any] = {
        "id": rid,
        "status": "pending",
        "created_at": now,
        "updated_at": now,
        # RGPD : pas d'IP en clair — HMAC seulement
        "ip_hash": hashed,
        "email": fields["email"],
        "email_norm": email_norm,
        "company": fields["company"],
        "trade": fields["trade"],
        "city": fields["city"],
        "phone": fields["phone"],
        "website": fields.get("website") or "",
        "demo_url": None,
        "slug": None,
        "source": None,
        "prior_notes": list(prior_notes or []),
    }
    save_request(data)
    return data


def mark_request(request_id: str, **updates: Any) -> dict[str, Any] | None:
    data = load_request(request_id)
    if not data:
        return None
    data.update(updates)
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    save_request(data)
    return data


def format_admin_alert(data: dict[str, Any]) -> str:
    website = data.get("website") or "(non renseigné)"
    lines = [
        "📩 Nouvelle demande de démo gratuite",
        f"• Entreprise : {data.get('company')}",
        f"• Métier : {data.get('trade')}",
        f"• Ville : {data.get('city')}",
        f"• Tél : {data.get('phone')}",
        f"• E-mail : {data.get('email')}",
        f"• Site actuel : {website}",
        f"• Id : {data.get('id')}",
        f"• Statut : {data.get('status')}",
    ]
    notes = data.get("prior_notes") or []
    if notes:
        lines.append("• Historique : " + " ; ".join(str(n) for n in notes))
    return "\n".join(lines)


def _cb_ok(data: str) -> bool:
    return len(data.encode("utf-8")) <= 64


def inline_keyboard_for(request_id: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {
                    "text": "Générer la démo",
                    "callback_data": f"demo:gen:{request_id}",
                },
                {
                    "text": "Refuser",
                    "callback_data": f"demo:ref:{request_id}",
                },
            ]
        ]
    }


def empty_keyboard() -> dict[str, Any]:
    return {"inline_keyboard": []}


def success_keyboard(request_id: str, slug: str) -> dict[str, Any]:
    row: list[dict[str, str]] = []
    if slug:
        ext = f"demo:ext:{slug}"
        if _cb_ok(ext):
            row.append({"text": "Prolonger 4 jours", "callback_data": ext})
    regen = f"demo:regen:{request_id}"
    if _cb_ok(regen):
        row.append({"text": "Régénérer", "callback_data": regen})
    return {"inline_keyboard": [row] if row else []}


def failure_keyboard(request_id: str) -> dict[str, Any]:
    retry = f"demo:retry:{request_id}"
    if not _cb_ok(retry):
        return empty_keyboard()
    return {"inline_keyboard": [[{"text": "Réessayer", "callback_data": retry}]]}


def telegram_api(method: str, payload: dict[str, Any], timeout: float = 12.0) -> dict[str, Any] | None:
    token = TELEGRAM_BOT_TOKEN
    if not token:
        return None
    url = f"https://api.telegram.org/bot{token}/{method}"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "WaaS-Bot/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read().decode("utf-8", "replace")
            data = json.loads(raw) if raw else {}
            if data.get("ok") is True:
                return data
            log.warning("Telegram %s ok!=true : %s", method, data.get("description") or raw[:200])
            return None
    except Exception as e:  # noqa: BLE001
        log.warning("Échec Telegram %s : %s", method, e)
        return None


def send_demo_alert(data: dict[str, Any]) -> bool:
    chat_id = TELEGRAM_CHAT_ID
    if not TELEGRAM_BOT_TOKEN or not chat_id:
        log.info("Telegram non configuré — alerte démo non envoyée (id=%s)", data.get("id"))
        return False
    payload = {
        "chat_id": chat_id,
        "text": format_admin_alert(data),
        "reply_markup": inline_keyboard_for(data["id"]),
    }
    resp = telegram_api("sendMessage", payload)
    if not resp:
        return False
    try:
        msg = (resp.get("result") or {}) if isinstance(resp, dict) else {}
        mid = msg.get("message_id")
        if mid is not None and data.get("id"):
            mark_request(data["id"], telegram_chat_id=str(chat_id), telegram_message_id=int(mid))
    except Exception as e:  # noqa: BLE001
        log.warning("Impossible de mémoriser message_id Telegram : %s", e)
    return True


def answer_callback(callback_query_id: str, text: str, *, show_alert: bool = False) -> None:
    if not callback_query_id:
        return
    telegram_api(
        "answerCallbackQuery",
        {
            "callback_query_id": callback_query_id,
            "text": text[:200],
            "show_alert": show_alert,
        },
    )


def edit_message_text(
    chat_id: str | int,
    message_id: int,
    text: str,
    reply_markup: dict[str, Any] | None = None,
) -> bool:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text[:4096],
        "reply_markup": reply_markup if reply_markup is not None else empty_keyboard(),
    }
    return telegram_api("editMessageText", payload) is not None


def _now_local_hhmm(dt: datetime | None = None) -> str:
    when = dt or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(ADMIN_TZ).strftime("%H:%M")


def _short_error(exc: BaseException | str, limit: int = 120) -> str:
    text = str(exc).strip() or "erreur inconnue"
    text = re.sub(r"\s+", " ", text)
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def format_generating_text(data: dict[str, Any], started_hhmm: str) -> str:
    base = format_admin_alert({**data, "status": "generating"})
    # ETA claire pour que l'admin ne croie pas que c'est bloqué (scratch ~4–7 min)
    return (
        f"{base}\n\n"
        f"⏳ Génération en cours… ({GENERATE_ETA_HINT}, démarrée à {started_hhmm})"
    )


def content_source_label(content_source: str | None) -> str:
    """Human label for Telegram: Grok vs secours/template (never jargon)."""
    src = (content_source or "").strip().casefold()
    if not src:
        return "inconnu"
    if "template-fallback" in src or src.startswith("template"):
        return "secours (template)"
    if "scratch" in src or src == "xai" or src.endswith(":xai"):
        return "Grok"
    if src in ("curated", "fallback"):
        return "secours (template)"
    return content_source or "inconnu"


def format_success_text(
    data: dict[str, Any],
    url: str,
    expires_fr: str,
    content_source: str | None = None,
) -> str:
    expiry_line = f"\n⏱ Expire le {expires_fr}" if expires_fr else ""
    src = content_source if content_source is not None else data.get("content_source")
    source_line = f"\n• Source : {content_source_label(src if isinstance(src, str) else None)}"
    return (
        f"✅ Démo prête\n"
        f"• Entreprise : {data.get('company')}\n"
        f"• Id : {data.get('id')}\n"
        f"• Lien : {url}{expiry_line}{source_line}"
    )


def format_failure_text(reason: str) -> str:
    return f"❌ Échec : {_short_error(reason)}"


def format_timeout_text() -> str:
    return "❌ Échec : délai dépassé"


def format_refused_text(data: dict[str, Any]) -> str:
    return (
        f"🚫 Refusée\n"
        f"• Entreprise : {data.get('company')}\n"
        f"• Id : {data.get('id')}"
    )


def generate_demo_for_request(data: dict[str, Any]) -> dict[str, Any]:
    """Call lead_sites; returns result dict with preview_url / slug / source."""
    import lead_sites

    out = Path(os.getenv("SITES_DIR", str(SITES_DIR))).resolve()
    result = lead_sites.generate_demo_site(
        name=data["company"],
        trade=data["trade"],
        city=data["city"],
        phone=data["phone"],
        email=data.get("email") or "",
        website=data.get("website") or None,
        out_dir=out,
        try_places=True,
    )
    return result


def _cancel_timeout(request_id: str, token: str) -> None:
    key = f"{request_id}:{token}"
    with _timeout_lock:
        t = _timeout_timers.pop(key, None)
    if t is not None:
        t.cancel()


def _schedule_timeout(
    request_id: str,
    token: str,
    chat_id: str,
    message_id: int,
    timeout_s: float | None = None,
) -> None:
    delay = float(GENERATE_TIMEOUT_S if timeout_s is None else timeout_s)

    def _fire() -> None:
        with _timeout_lock:
            _timeout_timers.pop(f"{request_id}:{token}", None)
        data = load_request(request_id)
        if not data:
            return
        if data.get("status") != "generating" or data.get("generation_token") != token:
            return
        mark_request(
            request_id,
            status="failed",
            failure_reason="délai dépassé",
            generation_token=None,
        )
        if chat_id and message_id:
            edit_message_text(
                chat_id,
                message_id,
                format_timeout_text(),
                failure_keyboard(request_id),
            )

    timer = threading.Timer(delay, _fire)
    timer.daemon = True
    with _timeout_lock:
        old = _timeout_timers.pop(f"{request_id}:{token}", None)
        _timeout_timers[f"{request_id}:{token}"] = timer
    if old is not None:
        old.cancel()
    timer.start()


def claim_generation(
    request_id: str,
    *,
    allow_from: tuple[str, ...],
) -> tuple[dict[str, Any] | None, str | None]:
    """
    Atomically transition allow_from → generating.
    Returns (updated_data, None) on success, or (None, reason).
    """
    with _gen_lock:
        data = load_request(request_id)
        if not data:
            return None, "introuvable"
        status = data.get("status") or ""
        if status not in allow_from:
            return None, status or "inconnu"
        token = secrets.token_hex(8)
        started = datetime.now(timezone.utc)
        updated = mark_request(
            request_id,
            status="generating",
            generation_started_at=started.isoformat(),
            generation_token=token,
            failure_reason=None,
        )
        return updated, None


def _resolve_expiry_fr(result: dict[str, Any]) -> tuple[Any, str]:
    expires_at = result.get("expires_at")
    expires_fr = ""
    try:
        import demo_ttl

        if not expires_at and result.get("slug"):
            meta = demo_ttl.load_meta(demo_ttl.sites_dir() / result["slug"])
            expires_at = meta.get("expires_at")
        expires_fr = demo_ttl.format_expiry_long_fr(demo_ttl.parse_iso(expires_at))
    except Exception:  # noqa: BLE001
        expires_fr = ""
    return expires_at, expires_fr


def run_generation_job(
    request_id: str,
    token: str,
    chat_id: str,
    message_id: int,
) -> dict[str, Any]:
    """
    Background worker: generate demo then edit the Telegram message.
    Idempotent vs timeout / superseded token.
    """
    data = load_request(request_id)
    if not data:
        return {"ok": False, "reason": "introuvable"}
    if data.get("status") != "generating" or data.get("generation_token") != token:
        return {"ok": False, "reason": "stale"}

    try:
        result = generate_demo_for_request(data)
    except Exception as e:  # noqa: BLE001
        log.exception("Génération démo échouée pour %s", request_id)
        return _finish_failure(request_id, token, chat_id, message_id, _short_error(e))

    # Re-check before writing success (timeout may have won)
    current = load_request(request_id)
    if not current or current.get("status") != "generating" or current.get("generation_token") != token:
        return {"ok": False, "reason": "stale"}

    expires_at, expires_fr = _resolve_expiry_fr(result)
    url = result.get("preview_url") or "(url manquante)"
    slug = result.get("slug") or ""
    content_source = result.get("content_source") or ""
    if content_source and (
        "template-fallback" in str(content_source).casefold()
        or str(content_source).casefold().startswith("template")
    ):
        log.warning(
            "demo %s served via secours/template (content_source=%s)",
            request_id,
            content_source,
        )
    updated = mark_request(
        request_id,
        status="generated",
        demo_url=result.get("preview_url"),
        slug=slug or None,
        source=result.get("source"),
        place_id=result.get("place_id"),
        demo_expires_at=expires_at,
        content_source=content_source or None,
        generation_token=None,
        failure_reason=None,
    )
    _cancel_timeout(request_id, token)
    text = format_success_text(
        updated or data, url, expires_fr, content_source=content_source or None
    )
    if chat_id and message_id:
        edit_message_text(chat_id, message_id, text, success_keyboard(request_id, slug))
    return {"ok": True, "url": url, "slug": slug, "content_source": content_source}


def _finish_failure(
    request_id: str,
    token: str,
    chat_id: str,
    message_id: int,
    reason: str,
) -> dict[str, Any]:
    current = load_request(request_id)
    if not current or current.get("status") != "generating" or current.get("generation_token") != token:
        return {"ok": False, "reason": "stale"}
    mark_request(
        request_id,
        status="failed",
        failure_reason=reason,
        generation_token=None,
    )
    _cancel_timeout(request_id, token)
    if chat_id and message_id:
        edit_message_text(
            chat_id,
            message_id,
            format_failure_text(reason),
            failure_keyboard(request_id),
        )
    return {"ok": False, "reason": reason}


def start_generation(
    request_id: str,
    *,
    chat_id: str,
    message_id: int,
    allow_from: tuple[str, ...] = ("pending",),
    run_async: bool = True,
    timeout_s: float | None = None,
) -> dict[str, Any]:
    """
    Claim → edit « en cours » → run generation (thread by default).
    """
    claimed, reason = claim_generation(request_id, allow_from=allow_from)
    if not claimed:
        return {"started": False, "reason": reason or "blocked"}

    token = str(claimed.get("generation_token") or "")
    started_iso = claimed.get("generation_started_at")
    try:
        started_dt = datetime.fromisoformat(started_iso) if started_iso else datetime.now(timezone.utc)
    except ValueError:
        started_dt = datetime.now(timezone.utc)
    hhmm = _now_local_hhmm(started_dt)
    text = format_generating_text(claimed, hhmm)
    if chat_id and message_id:
        edit_message_text(chat_id, message_id, text, empty_keyboard())
        mark_request(
            request_id,
            telegram_chat_id=str(chat_id),
            telegram_message_id=int(message_id),
        )

    _schedule_timeout(request_id, token, str(chat_id), int(message_id or 0), timeout_s=timeout_s)

    if run_async:
        threading.Thread(
            target=run_generation_job,
            args=(request_id, token, str(chat_id), int(message_id or 0)),
            daemon=True,
            name=f"demo-gen-{request_id[:8]}",
        ).start()
        return {"started": True, "token": token, "async": True}

    job = run_generation_job(request_id, token, str(chat_id), int(message_id or 0))
    return {"started": True, "token": token, "async": False, "job": job}


def handle_extend(slug: str, days: int | None = None) -> tuple[bool, str]:
    try:
        import demo_ttl

        result = demo_ttl.extend_demo(slug, days=days)
    except FileNotFoundError:
        return False, f"Démo introuvable : {slug}"
    except ValueError as e:
        return False, str(e)
    except Exception as e:  # noqa: BLE001
        log.exception("Prolongation échouée pour %s", slug)
        return False, f"Échec prolongation : {e}"

    if result.get("status") == "activated_no_expiry":
        return True, f"✅ {slug} est activé (payé) — pas d'expiration."
    fr = result.get("expires_fr") or result.get("expires_at") or "?"
    return True, f"✅ {slug} prolongé jusqu'au {fr}"


def handle_refuse(
    request_id: str,
    *,
    chat_id: str | None = None,
    message_id: int | None = None,
) -> tuple[bool, str]:
    data = load_request(request_id)
    if not data:
        return False, "Demande introuvable."
    if data.get("status") == "generated":
        return False, "Déjà générée — refus ignoré."
    if data.get("status") == "generating":
        return False, "Génération en cours — refus ignoré."
    if data.get("status") == "refused":
        return True, "Déjà refusée."
    updated = mark_request(request_id, status="refused", generation_token=None)
    text = format_refused_text(updated or data)
    cid = chat_id or data.get("telegram_chat_id") or TELEGRAM_CHAT_ID
    mid = message_id if message_id is not None else data.get("telegram_message_id")
    if cid and mid:
        edit_message_text(cid, int(mid), text, empty_keyboard())
    return True, text


def process_telegram_update(update: dict[str, Any]) -> dict[str, Any]:
    """
    Handle Telegram Bot API Update (callback_query for demo buttons).
    Génération : toast immédiat + edit « en cours » + job async (ne bloque pas le webhook).
    """
    cb = update.get("callback_query")
    if not cb:
        return {"handled": False, "reason": "no_callback"}

    cb_id = str(cb.get("id") or "")
    raw_data = str(cb.get("data") or "")
    msg = cb.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id") or "")
    message_id = msg.get("message_id")
    try:
        message_id_int = int(message_id) if message_id is not None else 0
    except (TypeError, ValueError):
        message_id_int = 0

    # Only accept callbacks from the configured admin chat
    if TELEGRAM_CHAT_ID and chat_id and chat_id != str(TELEGRAM_CHAT_ID):
        answer_callback(cb_id, "Non autorisé")
        return {"handled": False, "reason": "wrong_chat"}

    m_ext = re.fullmatch(r"demo:ext:([a-z0-9][a-z0-9-]{0,80})", raw_data)
    if m_ext:
        slug = m_ext.group(1)
        ok, text = handle_extend(slug)
        answer_callback(cb_id, "OK" if ok else "Erreur")
        if TELEGRAM_CHAT_ID:
            telegram_api("sendMessage", {"chat_id": TELEGRAM_CHAT_ID, "text": text})
        return {"handled": True, "action": "ext", "slug": slug, "ok": ok, "message": text}

    m = re.fullmatch(
        r"demo:(gen|ref|retry|regen):([A-Za-z0-9_-]{8,64})",
        raw_data,
    )
    if not m:
        answer_callback(cb_id, "Action inconnue")
        return {"handled": False, "reason": "bad_callback"}

    action, rid = m.group(1), m.group(2)

    if action == "ref":
        ok, text = handle_refuse(rid, chat_id=chat_id, message_id=message_id_int or None)
        answer_callback(cb_id, "Refusée" if ok else "Erreur")
        return {"handled": True, "action": "ref", "request_id": rid, "ok": ok, "message": text}

    # gen / retry / regen
    if action == "gen":
        allow_from = ("pending",)
    elif action == "retry":
        allow_from = ("failed",)
    else:  # regen
        allow_from = ("generated",)

    # Claim local (ms) puis toast unique — answerCallbackQuery une seule fois
    claimed, claim_reason = claim_generation(rid, allow_from=allow_from)
    if not claimed:
        reason = claim_reason or "blocked"
        if reason == "generating":
            toast = "Déjà en cours"
        elif reason == "generated":
            toast = "Déjà générée"
        elif reason == "refused":
            toast = "Déjà refusée"
        elif reason == "failed":
            toast = "Échec — utilisez Réessayer"
        elif reason == "introuvable":
            toast = "Introuvable"
        elif reason == "pending":
            toast = "Encore en attente"
        else:
            toast = "Ignoré"
        answer_callback(cb_id, toast)
        return {
            "handled": True,
            "action": action,
            "request_id": rid,
            "ok": False,
            "started": False,
            "reason": reason,
        }

    # US1 — toast immédiat dès que le clic est accepté (avant génération lourde)
    answer_callback(cb_id, "Génération lancée")

    token = str(claimed.get("generation_token") or "")
    started_iso = claimed.get("generation_started_at")
    try:
        started_dt = (
            datetime.fromisoformat(started_iso) if started_iso else datetime.now(timezone.utc)
        )
    except ValueError:
        started_dt = datetime.now(timezone.utc)
    hhmm = _now_local_hhmm(started_dt)
    text = format_generating_text(claimed, hhmm)
    cid = chat_id or str(TELEGRAM_CHAT_ID or "")
    if cid and message_id_int:
        edit_message_text(cid, message_id_int, text, empty_keyboard())
        mark_request(
            rid,
            telegram_chat_id=str(cid),
            telegram_message_id=int(message_id_int),
        )

    _schedule_timeout(rid, token, cid, message_id_int)
    threading.Thread(
        target=run_generation_job,
        args=(rid, token, cid, message_id_int),
        daemon=True,
        name=f"demo-gen-{rid[:8]}",
    ).start()

    return {
        "handled": True,
        "action": action,
        "request_id": rid,
        "ok": True,
        "started": True,
        "token": token,
    }


def verify_telegram_secret(header_value: str | None) -> bool:
    """
    Telegram setWebhook secret_token → header X-Telegram-Bot-Api-Secret-Token.
    If TELEGRAM_WEBHOOK_SECRET is empty, reject in production-minded default
    (tests can set the secret). Empty secret → False.
    """
    expected = TELEGRAM_WEBHOOK_SECRET
    if not expected:
        return False
    got = (header_value or "").strip()
    if not got or len(got) != len(expected):
        return False
    return secrets.compare_digest(got, expected)


def webhook_setup_hint() -> str:
    """Documented setup command (no secrets)."""
    return (
        "curl -sS -X POST \"https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/setWebhook\" "
        "-H 'Content-Type: application/json' "
        "-d '{\"url\":\"https://visibleartisan.fr/webhook/telegram\","
        "\"secret_token\":\"$TELEGRAM_WEBHOOK_SECRET\","
        "\"allowed_updates\":[\"callback_query\"]}'"
    )
