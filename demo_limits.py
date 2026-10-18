"""
demo_limits.py — Anti-abus POST /api/demo-request (e-mail + IP).

- Normalisation e-mail (lowercase, trim, strip +tag, ignore dots gmail)
- Au plus 1 demande / e-mail si pending, generated non expirée, ou refused < 30 j
- Blocage domaines jetables (liste locale)
- IP : CF-Connecting-IP seulement si peer ∈ plages Cloudflare (ou proxy local de confiance)
- Limites IP : 2 / 24h et 3 / 30j (env)
- IP stockée en HMAC-SHA256 (sel env) ; purge > 30 j
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("demo_limits")

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
EMAIL_REFUSED_COOLDOWN_DAYS = int(os.getenv("DEMO_EMAIL_REFUSED_DAYS", "30"))
IP_LIMIT_24H = int(os.getenv("DEMO_IP_LIMIT_24H", "2"))
IP_LIMIT_30D = int(os.getenv("DEMO_IP_LIMIT_30D", "3"))
IP_RETENTION_DAYS = int(os.getenv("DEMO_IP_RETENTION_DAYS", "30"))
IP_HASH_SALT = (os.getenv("DEMO_IP_HASH_SALT") or os.getenv("DEMO_REQUEST_IP_SALT") or "").strip()

# Message UX unique (pas de détail des règles)
REJECT_MESSAGE_FR = (
    "Vous avez déjà demandé une démo. "
    "Si besoin, réessayez plus tard ou contactez-nous via les mentions légales."
)

# Domaines jetables courants (liste courte locale — pas de dépendance réseau)
DISPOSABLE_DOMAINS = frozenset(
    {
        "mailinator.com",
        "guerrillamail.com",
        "guerrillamail.org",
        "sharklasers.com",
        "grr.la",
        "yopmail.com",
        "yopmail.fr",
        "tempmail.com",
        "temp-mail.org",
        "throwaway.email",
        "trashmail.com",
        "trashmail.me",
        "10minutemail.com",
        "10minutemail.net",
        "discard.email",
        "dispostable.com",
        "maildrop.cc",
        "getnada.com",
        "nada.ltd",
        "emailondeck.com",
        "fakeinbox.com",
        "mailnesia.com",
        "moakt.com",
        "tempail.com",
        "tmpmail.org",
        "tmpmail.net",
        "mailcatch.com",
        "spamgourmet.com",
        "mintemail.com",
        "mytemp.email",
        "mailnull.com",
        "spam4.me",
        "guerrillamailblock.com",
        "pokemail.net",
        "spam.la",
        "bccto.me",
        "anonbox.net",
        "discardmail.com",
        "mailforspam.com",
        "jetable.org",
        "jetable.com",
        "tempinbox.com",
        "correo.temporal.org",
    }
)

# Plages Cloudflare (IPv4 + IPv6) — alignées sur conf Nginx real_ip VPS
# Source publique Cloudflare ; mise à jour manuelle occasionnelle OK
CLOUDFLARE_CIDRS = (
    "173.245.48.0/20",
    "103.21.244.0/22",
    "103.22.200.0/22",
    "103.31.4.0/22",
    "141.101.64.0/18",
    "108.162.192.0/18",
    "190.93.240.0/20",
    "188.114.96.0/20",
    "197.234.240.0/22",
    "198.41.128.0/17",
    "162.158.0.0/15",
    "104.16.0.0/13",
    "104.24.0.0/14",
    "172.64.0.0/13",
    "131.0.72.0/22",
    "2400:cb00::/32",
    "2606:4700::/32",
    "2803:f800::/32",
    "2405:b500::/32",
    "2405:8100::/32",
    "2a06:98c0::/29",
    "2c0f:f248::/32",
)

# Proxies locaux Nginx → Caddy → uvicorn (peer vu par FastAPI)
LOCAL_TRUSTED_CIDRS = (
    "127.0.0.0/8",
    "::1/128",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
)

_CF_NETS = [ipaddress.ip_network(c) for c in CLOUDFLARE_CIDRS]
_LOCAL_NETS = [ipaddress.ip_network(c) for c in LOCAL_TRUSTED_CIDRS]
_TRUSTED_PEER_NETS = _CF_NETS + _LOCAL_NETS

_lock = threading.Lock()
_block_counter = 0  # compteur optionnel (process-local)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(value: str | None) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def normalize_email(email: str) -> str:
    """
    lowercase, trim, strip +tag ; pour gmail/googlemail ignore les points locaux.
    """
    raw = (email or "").strip().lower()
    if "@" not in raw:
        return raw
    local, _, domain = raw.partition("@")
    domain = domain.strip()
    # Strip +tag
    if "+" in local:
        local = local.split("+", 1)[0]
    if domain in ("gmail.com", "googlemail.com"):
        local = local.replace(".", "")
        domain = "gmail.com"  # canonical
    return f"{local}@{domain}"


def is_disposable_email(email: str) -> bool:
    norm = normalize_email(email)
    if "@" not in norm:
        return False
    domain = norm.rsplit("@", 1)[-1]
    # Sous-domaine d'un jetable connu
    parts = domain.split(".")
    for i in range(len(parts) - 1):
        cand = ".".join(parts[i:])
        if cand in DISPOSABLE_DOMAINS:
            return True
    return domain in DISPOSABLE_DOMAINS


def ip_in_networks(ip_str: str, networks: list) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str.split("%")[0])
    except ValueError:
        return False
    return any(ip in net for net in networks)


def is_cloudflare_ip(ip_str: str) -> bool:
    return ip_in_networks(ip_str, _CF_NETS)


def is_trusted_peer(ip_str: str) -> bool:
    """Peer Cloudflare OU proxy local (Nginx/Caddy loopback)."""
    return ip_in_networks(ip_str, _TRUSTED_PEER_NETS)


def resolve_client_ip(
    headers: Any,
    peer: str | None,
) -> str:
    """
    Trust CF-Connecting-IP only if peer is Cloudflare (or local trusted proxy
    that already applied CF real_ip). Otherwise return peer (anti-spoof).
    """
    peer_ip = (peer or "").strip() or "unknown"

    def _h(name: str) -> str:
        if hasattr(headers, "get"):
            v = headers.get(name) or headers.get(name.lower()) or ""
        else:
            v = ""
        return str(v).strip()

    if not is_trusted_peer(peer_ip):
        # Peer non fiable : ignorer les en-têtes (spoof)
        return peer_ip.split("%")[0]

    cf = _h("CF-Connecting-IP") or _h("cf-connecting-ip")
    if cf:
        cand = cf.split("%")[0].split(",")[0].strip()
        if re_fullmatch_ip(cand):
            return cand

    # Derrière Nginx local : X-Real-IP souvent déjà le visiteur (real_ip CF)
    xri = _h("X-Real-IP") or _h("x-real-ip")
    if xri:
        cand = xri.split("%")[0].strip()
        if re_fullmatch_ip(cand):
            return cand

    xff = _h("X-Forwarded-For") or _h("x-forwarded-for")
    if xff:
        first = xff.split(",")[0].strip().split("%")[0]
        if re_fullmatch_ip(first):
            return first

    return peer_ip.split("%")[0]


def re_fullmatch_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def hash_ip(ip: str, salt: str | None = None) -> str:
    """HMAC-SHA256 hex ; sel requis en prod (sinon hash préfixé 'dev:' pour tests)."""
    use_salt = (salt if salt is not None else IP_HASH_SALT) or "dev-insecure-salt"
    digest = hmac.new(
        use_salt.encode("utf-8"),
        (ip or "unknown").encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return digest


def requests_dir() -> Path:
    # Prefer live demo_request module config (tests / webhook_server reassign it)
    try:
        import demo_request as _dr

        if getattr(_dr, "DEMO_REQUESTS_DIR", None):
            return Path(_dr.DEMO_REQUESTS_DIR).resolve()
    except Exception:  # noqa: BLE001
        pass
    sites = Path(os.getenv("SITES_DIR", "/var/www/sites")).resolve()
    return Path(os.getenv("DEMO_REQUESTS_DIR") or (sites / "_demo_requests")).resolve()


def ip_log_path() -> Path:
    return requests_dir() / "_ip_hits.jsonl"


def ensure_requests_dir() -> Path:
    d = requests_dir()
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def list_request_records() -> list[dict[str, Any]]:
    d = requests_dir()
    if not d.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in d.glob("*.json"):
        if path.name.startswith("_") or path.name.startswith("."):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                out.append(data)
        except (OSError, json.JSONDecodeError):
            continue
    return out


def _demo_still_active(rec: dict[str, Any]) -> bool:
    """generated & not expired — best-effort via meta or expires_at on request."""
    if rec.get("status") != "generated":
        return False
    # Prefer site meta if slug known
    slug = rec.get("slug")
    if slug:
        try:
            import demo_ttl

            site = demo_ttl.sites_dir() / slug
            if site.is_dir():
                if demo_ttl.is_activated(site):
                    return True
                meta = demo_ttl.load_meta(site)
                return not demo_ttl.is_expired(meta, site)
        except Exception:  # noqa: BLE001
            pass
    exp = parse_iso(rec.get("demo_expires_at") or rec.get("expires_at"))
    if exp is not None:
        return now_utc() < exp
    # Pas d'info d'expiry → considérer encore active (prudence anti-abus)
    return True


def email_blocked_reason(email: str, records: list[dict[str, Any]] | None = None) -> str | None:
    """
    Retourne un code interne si bloqué, sinon None.
    Codes : disposable | pending | generated | refused_cooldown
    """
    if is_disposable_email(email):
        return "disposable"
    norm = normalize_email(email)
    recs = records if records is not None else list_request_records()
    cooldown = timedelta(days=max(1, EMAIL_REFUSED_COOLDOWN_DAYS))
    now = now_utc()

    for rec in recs:
        rec_email = normalize_email(str(rec.get("email") or ""))
        if rec_email != norm:
            continue
        status = rec.get("status") or ""
        if status == "pending":
            return "pending"
        if status == "generated" and _demo_still_active(rec):
            return "generated"
        if status == "refused":
            when = parse_iso(rec.get("updated_at") or rec.get("created_at"))
            if when and (now - when) < cooldown:
                return "refused_cooldown"
    return None


def append_ip_hit(ip_hash: str, *, at: datetime | None = None) -> None:
    ensure_requests_dir()
    path = ip_log_path()
    entry = {"ip_hash": ip_hash, "at": (at or now_utc()).isoformat()}
    with _lock:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def load_ip_hits() -> list[dict[str, Any]]:
    path = ip_log_path()
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                if isinstance(row, dict) and row.get("ip_hash"):
                    out.append(row)
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return out


def purge_ip_hits(*, retention_days: int | None = None) -> int:
    """Réécrit le journal IP en écartant les entrées > retention. Retourne nb purgées."""
    days = retention_days if retention_days is not None else IP_RETENTION_DAYS
    cutoff = now_utc() - timedelta(days=max(1, days))
    hits = load_ip_hits()
    kept: list[dict[str, Any]] = []
    purged = 0
    for row in hits:
        when = parse_iso(row.get("at"))
        if when is None or when < cutoff:
            purged += 1
            continue
        kept.append(row)
    path = ip_log_path()
    ensure_requests_dir()
    tmp = path.with_name("._ip_hits.jsonl.tmp")
    with _lock:
        with tmp.open("w", encoding="utf-8") as f:
            for row in kept:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        tmp.replace(path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    # Purge aussi ip_hash absents / vieilles demandes (> retention) — best effort
    return purged


def purge_old_request_ip_fields(*, retention_days: int | None = None) -> int:
    """Efface ip_hash des JSON de demandes plus vieux que retention (RGPD)."""
    days = retention_days if retention_days is not None else IP_RETENTION_DAYS
    cutoff = now_utc() - timedelta(days=max(1, days))
    n = 0
    for rec in list_request_records():
        when = parse_iso(rec.get("created_at"))
        if when is None or when >= cutoff:
            continue
        if "ip_hash" not in rec and "ip" not in rec:
            continue
        rid = rec.get("id")
        if not rid:
            continue
        path = requests_dir() / f"{rid}.json"
        if not path.is_file():
            continue
        rec.pop("ip_hash", None)
        rec.pop("ip", None)  # legacy plaintext
        rec["ip_purged_at"] = now_utc().isoformat()
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
        n += 1
    return n


def ip_blocked_reason(ip_hash: str, hits: list[dict[str, Any]] | None = None) -> str | None:
    rows = hits if hits is not None else load_ip_hits()
    now = now_utc()
    day = timedelta(hours=24)
    month = timedelta(days=30)
    n24 = 0
    n30 = 0
    for row in rows:
        if row.get("ip_hash") != ip_hash:
            continue
        when = parse_iso(row.get("at"))
        if when is None:
            continue
        age = now - when
        if age <= month:
            n30 += 1
        if age <= day:
            n24 += 1
    if n24 >= max(1, IP_LIMIT_24H):
        return "ip_24h"
    if n30 >= max(1, IP_LIMIT_30D):
        return "ip_30d"
    return None


def prior_request_notes(email: str, ip_hash: str) -> list[str]:
    """Notes admin pour l'alerte Telegram (historique)."""
    notes: list[str] = []
    norm = normalize_email(email)
    email_hits = 0
    for rec in list_request_records():
        if normalize_email(str(rec.get("email") or "")) == norm:
            email_hits += 1
    if email_hits > 0:
        notes.append(f"e-mail déjà vu ({email_hits} demande(s) en base)")
    ip_hits = sum(1 for r in load_ip_hits() if r.get("ip_hash") == ip_hash)
    if ip_hits > 0:
        notes.append(f"IP déjà vue ({ip_hits} hit(s) journal)")
    return notes


def check_and_register(
    email: str,
    ip: str,
    *,
    salt: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Vérifie limites e-mail + IP. Si OK et pas dry_run, enregistre le hit IP.
    Retourne {allowed, reason, ip_hash, message, prior_notes}.
    """
    global _block_counter
    # Purge opportuniste (légère)
    try:
        purge_ip_hits()
    except Exception as e:  # noqa: BLE001
        log.warning("purge_ip_hits: %s", e)

    ip_h = hash_ip(ip, salt=salt)
    reason = email_blocked_reason(email)
    if reason is None:
        reason = ip_blocked_reason(ip_h)

    if reason:
        with _lock:
            _block_counter += 1
            count = _block_counter
        return {
            "allowed": False,
            "reason": reason,
            "ip_hash": ip_h,
            "message": REJECT_MESSAGE_FR,
            "block_count": count,
            "prior_notes": [],
        }

    notes = prior_request_notes(email, ip_h)
    if not dry_run:
        append_ip_hit(ip_h)
    return {
        "allowed": True,
        "reason": None,
        "ip_hash": ip_h,
        "message": None,
        "block_count": 0,
        "prior_notes": notes,
    }


def reset_email(email: str) -> int:
    """
    Admin : retire le blocage e-mail en marquant les demandes concernées
    status=reset_by_admin (conserve l'historique).
    """
    norm = normalize_email(email)
    n = 0
    for rec in list_request_records():
        if normalize_email(str(rec.get("email") or "")) != norm:
            continue
        if rec.get("status") in ("pending", "refused", "generated"):
            rec["status_before_reset"] = rec.get("status")
            rec["status"] = "reset_by_admin"
            rec["updated_at"] = now_utc().isoformat()
            path = requests_dir() / f"{rec['id']}.json"
            path.write_text(json.dumps(rec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            n += 1
    return n


def reset_ip(ip_or_hash: str) -> int:
    """Admin : retire les hits IP du journal (IP claire ou hash)."""
    raw = (ip_or_hash or "").strip()
    if not raw:
        return 0
    target = raw if re.fullmatch(r"[0-9a-f]{64}", raw) else hash_ip(raw)
    hits = load_ip_hits()
    kept = [h for h in hits if h.get("ip_hash") != target]
    removed = len(hits) - len(kept)
    if removed == 0:
        return 0
    path = ip_log_path()
    ensure_requests_dir()
    tmp = path.with_name("._ip_hits.jsonl.tmp")
    with _lock:
        with tmp.open("w", encoding="utf-8") as f:
            for row in kept:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        tmp.replace(path)
    return removed


def get_block_counter() -> int:
    with _lock:
        return _block_counter
