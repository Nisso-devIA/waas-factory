#!/usr/bin/env python3
"""
webhook_server.py — Serveur FastAPI compagnon de lead_sites.py :
  - GET  /checkout/{slug}  : Crée une session Stripe Checkout avec saisie du domaine .fr
  - POST /webhook/stripe   : Gère l'activation (paid / no_payment_required / async_payment_succeeded),
                             l'annulation des doubles abonnements, le délai de grâce sur impayé,
                             la protection contre les conflits de domaines et la remise en démo
  - GET  /api/check-domain : Autorise Caddy On-Demand TLS si le domaine est actif
  - POST /api/demo-request : Formulaire vitrine « démo gratuite » → JSON pending + alerte Telegram
  - POST /webhook/telegram : Callbacks Telegram (secret_token) Générer / Refuser
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import secrets
import threading
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import stripe
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import demo_request
import demo_templates
import photo_upload
import pricing
import quote_request

log = logging.getLogger("webhook_server")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

app = FastAPI(title="WaaS Checkout & Webhook Server")

SITES_DIR = Path(os.getenv("SITES_DIR", "/var/www/sites")).resolve()
stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_PRICE_ID = os.getenv("STRIPE_PRICE_ID", "")
# Prix récurrents par palier (devis). Visibilité réutilise STRIPE_PRICE_ID (offre 49€) si non défini.
STRIPE_PRICE_ESSENTIEL = os.getenv("STRIPE_PRICE_ESSENTIEL", "").strip()
STRIPE_PRICE_VISIBILITE = os.getenv("STRIPE_PRICE_VISIBILITE", "").strip()
STRIPE_PRICE_CROISSANCE = os.getenv("STRIPE_PRICE_CROISSANCE", "").strip()
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_REQUIRE_TOS = os.getenv("STRIPE_REQUIRE_TOS", "").lower() in ("1", "true", "yes")
SUCCESS_URL = os.getenv("STRIPE_SUCCESS_URL", "https://demo.example.fr/merci")
CANCEL_URL = os.getenv("STRIPE_CANCEL_URL", "https://demo.example.fr/")
NOTIFY_WEBHOOK_URL = os.getenv("NOTIFY_WEBHOOK_URL", "").strip()
PUBLIC_BASE_URL = (
    os.getenv("PUBLIC_BASE_URL")
    or os.getenv("PREVIEW_BASE_DOMAIN")
    or "https://visibleartisan.fr"
).rstrip("/")
# Keep photo_upload base in sync with server env
photo_upload.PUBLIC_BASE_URL = PUBLIC_BASE_URL
# CallMeBot WhatsApp (same var names as /home/ubuntu/whatsapp-bot/.env + Sentinelle)
CALLMEBOT_PHONE = re.sub(
    r"\D",
    "",
    os.getenv("WHATSAPP_PHONE") or os.getenv("OWNER_NUMBER") or os.getenv("CALLMEBOT_PHONE") or "",
)
CALLMEBOT_APIKEY = (
    os.getenv("WHATSAPP_APIKEY") or os.getenv("CALLMEBOT_APIKEY") or ""
).strip()
CALLMEBOT_URL = "https://api.callmebot.com/whatsapp.php"
# Telegram — dedicated ArtisanVisible bot (TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID)
TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
TELEGRAM_CHAT_ID = (
    os.getenv("TELEGRAM_CHAT_ID") or os.getenv("TELEGRAM_USER_ID") or ""
).strip()
TELEGRAM_WEBHOOK_SECRET = (os.getenv("TELEGRAM_WEBHOOK_SECRET") or "").strip()

# Keep demo_request module config in sync
demo_request.SITES_DIR = SITES_DIR
demo_request.DEMO_REQUESTS_DIR = Path(
    os.getenv("DEMO_REQUESTS_DIR") or (SITES_DIR / "_demo_requests")
).resolve()
demo_request.TELEGRAM_BOT_TOKEN = TELEGRAM_BOT_TOKEN
demo_request.TELEGRAM_CHAT_ID = TELEGRAM_CHAT_ID
demo_request.TELEGRAM_WEBHOOK_SECRET = TELEGRAM_WEBHOOK_SECRET

# Keep quote_request module config in sync
quote_request.SITES_DIR = SITES_DIR
quote_request.QUOTES_DIR = Path(
    os.getenv("QUOTES_DIR") or (SITES_DIR / "_quotes")
).resolve()


SAFE_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")
SAFE_DOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z]{2,10})+$")
BANNER_RE = re.compile(r"<!-- BANDEAU_DEMO_START -->.*?<!-- BANDEAU_DEMO_END -->\s*", re.DOTALL)

SUSPENDED_STATUSES = {"unpaid", "canceled", "incomplete_expired"}
VALID_CHECKOUT_PAYMENT_STATUSES = {"paid", "no_payment_required"}


def atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(content, encoding=encoding)
    tmp.replace(path)


# --------------------------------------------------------------------------- #
# Notification Admin Non-Bloquante (Thread Daemon)
# --------------------------------------------------------------------------- #
def format_admin_notification(
    slug: str,
    registered_domain: str | None,
    desired_domain: str | None,
    conflict_owner: str | None,
    details: dict,
    plan_label: str | None = None,
) -> str:
    phone = details.get("phone") or "Non renseigné"
    email = details.get("email") or "Non renseigné"
    name = details.get("name") or slug

    if registered_domain:
        domain_line = f"`{registered_domain}`"
    elif desired_domain and conflict_owner:
        domain_line = (
            f"⚠️ `{desired_domain}` (déjà attribué à `{conflict_owner}` "
            f"— choisir une variante avec `set_domain.py`)"
        )
    else:
        domain_line = "`Aucun (à choisir avec set_domain.py)`"

    return (
        f"🎉 **Nouveau client abonné ({plan_label or '49€/mois'}) !**\n"
        f"• **Artisan :** `{name}` (`{slug}`)\n"
        f"• **Téléphone :** `{phone}` | **Email :** `{email}`\n"
        f"• **Domaine .fr demandé :** {domain_line}"
    )


def normalize_callmebot_phone(raw: str) -> str:
    """Normalize FR-style numbers for CallMeBot (digits only, international)."""
    phone = re.sub(r"\D", "", raw or "")
    if phone.startswith("00"):
        phone = phone[2:]
    if phone.startswith("0") and len(phone) == 10:
        phone = "33" + phone[1:]
    return phone


def callmebot_body_indicates_success(body: str) -> bool:
    """
    CallMeBot often returns HTTP 200 with an HTML error body
    (e.g. "0 messages left" / "Message not sent"). Treat as success only
    when the body clearly indicates the message was accepted.
    """
    low = (body or "").lower()
    if "message not sent" in low:
        return False
    if re.search(r"\b0\b\s*</?b>\s*messages?\s+left", low) or "0 messages left" in low:
        return False
    if "messages left" in low and re.search(r">\s*0\s*<", low):
        return False
    if "message queued" in low or "message sent" in low or "successfully queued" in low:
        return True
    # Ambiguous 200 bodies without success markers → not success
    return False


def send_callmebot_whatsapp(text: str, timeout: float = 8.0) -> int | None:
    """
    GET https://api.callmebot.com/whatsapp.php (same as Sentinelle).
    Returns HTTP status on success, or None if not configured / network error /
    HTTP 200 with an error body (quota, etc.).
    """
    phone = normalize_callmebot_phone(CALLMEBOT_PHONE)
    apikey = CALLMEBOT_APIKEY
    if not phone or not apikey:
        return None
    q = urllib.parse.urlencode({"phone": phone, "text": text, "apikey": apikey})
    url = f"{CALLMEBOT_URL}?{q}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as res:
            status = int(getattr(res, "status", None) or res.getcode())
            body = res.read().decode("utf-8", "replace")
            # Never log full phone/apikey — strip query for diagnostics
            body_snip = re.sub(r"\+?\d{8,}", "…PHONE…", body)[:240]
            if status == 200 and callmebot_body_indicates_success(body):
                log.info("CallMeBot WhatsApp HTTP %s (body OK)", status)
                return status
            log.warning(
                "CallMeBot WhatsApp non envoyé (HTTP %s) : %s",
                status,
                body_snip.replace("\n", " "),
            )
            return None
    except Exception as e:  # noqa: BLE001
        log.warning("Échec CallMeBot WhatsApp : %s", e)
        return None


def _send_discord_webhook(msg: str) -> None:
    if not NOTIFY_WEBHOOK_URL:
        return
    # allowed_mentions:{parse:[]} → un nom client type "@everyone" ne peut pas pinguer le canal admin.
    payload = json.dumps({"content": msg, "text": msg, "allowed_mentions": {"parse": []}}).encode("utf-8")
    req = urllib.request.Request(
        NOTIFY_WEBHOOK_URL,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "WaaS-Bot/1.0"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:  # noqa: BLE001
        log.warning("Échec de l'envoi de la notification Discord : %s", e)


def send_telegram_message(text: str, timeout: float = 8.0) -> bool:
    """
    POST api.telegram.org/bot<TOKEN>/sendMessage.
    Success only if JSON ok:true. Returns False if unconfigured / error.
    """
    token = TELEGRAM_BOT_TOKEN
    chat_id = TELEGRAM_CHAT_ID
    if not token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "WaaS-Bot/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            body = res.read().decode("utf-8", "replace")
            data = json.loads(body) if body else {}
            if data.get("ok") is True:
                log.info("Telegram sendMessage ok=true")
                return True
            # Never log the token; redact chat ids that look numeric-long
            err = data.get("description") or body[:200]
            log.warning("Telegram non envoyé (ok!=true) : %s", err)
            return False
    except Exception as e:  # noqa: BLE001
        log.warning("Échec Telegram : %s", e)
        return False


def _send_admin_notification(
    slug: str,
    registered_domain: str | None,
    desired_domain: str | None,
    conflict_owner: str | None,
    details: dict,
    plan_label: str | None = None,
) -> None:
    msg = format_admin_notification(slug, registered_domain, desired_domain, conflict_owner, details, plan_label)
    _send_discord_webhook(msg)
    # Plain-ish text for WhatsApp / Telegram (drop heavy markdown stars)
    plain = (
        msg.replace("**", "")
        .replace("`", "")
        .replace("🎉 ", "")
    )
    send_callmebot_whatsapp(plain)
    send_telegram_message(plain)


def notify_admin_async(
    slug: str,
    registered_domain: str | None,
    desired_domain: str | None,
    conflict_owner: str | None,
    details: dict,
    plan_label: str | None = None,
) -> None:
    has_discord = bool(NOTIFY_WEBHOOK_URL)
    has_wa = bool(CALLMEBOT_PHONE and CALLMEBOT_APIKEY)
    has_tg = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
    if not (has_discord or has_wa or has_tg):
        return
    threading.Thread(
        target=_send_admin_notification,
        args=(slug, registered_domain, desired_domain, conflict_owner, details, plan_label),
        daemon=True,
    ).start()


def _plan_label_from_session(session_obj: dict) -> str | None:
    """Libellé du plan pour la notif d'activation, depuis les metadata Stripe (palier + création)."""
    meta = session_obj.get("metadata") or {}
    tier_id = meta.get("tier")
    if not tier_id:
        return None  # ancien flux 49€ → libellé par défaut
    tier = next((t for t in pricing.TIERS if t["id"] == tier_id), None)
    parts = []
    if tier:
        parts.append(f"{tier['name']} {tier['price']}€/mois")
    try:
        cents = int(meta.get("creation_cents") or 0)
        if cents > 0:
            parts.append(f"création {cents // 100}€")
    except (TypeError, ValueError):
        pass
    return " + ".join(parts) or None


# --------------------------------------------------------------------------- #
# Gestion du registre de domaines personnalisés (domains.json)
# --------------------------------------------------------------------------- #
def domains_map_path() -> Path:
    return SITES_DIR / "domains.json"


def load_domains_map() -> dict[str, str]:
    path = domains_map_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_domains_map(mapping: dict[str, str]) -> None:
    atomic_write_text(domains_map_path(), json.dumps(mapping, ensure_ascii=False, indent=2))


def normalize_domain(raw: str | None) -> str | None:
    if not raw:
        return None
    d = raw.strip().lower()
    d = re.sub(r"^https?://", "", d)
    d = d.split("/")[0].strip().removeprefix("www.")
    if "." not in d and SAFE_SLUG_RE.match(d):
        d = f"{d}.fr"
    return d if SAFE_DOMAIN_RE.match(d) else None


def extract_desired_domain(session_obj: dict) -> str | None:
    for field in session_obj.get("custom_fields") or []:
        if field.get("key") == "desired_domain":
            val = (field.get("text") or {}).get("value")
            norm = normalize_domain(val)
            if norm:
                return norm
    return normalize_domain((session_obj.get("metadata") or {}).get("desired_domain"))


# --------------------------------------------------------------------------- #
# Helpers Fichiers & Vérification de l'Abonnement Actif
# --------------------------------------------------------------------------- #
def resolve_site_dir(slug: str) -> Path:
    if not slug or not SAFE_SLUG_RE.match(slug):
        raise HTTPException(status_code=400, detail="Identifiant de site invalide.")
    target = (SITES_DIR / slug).resolve()
    if target.parent != SITES_DIR or not target.is_dir():
        raise HTTPException(status_code=404, detail="Site introuvable.")
    return target


def is_active_subscription(site_dir: Path, sub_id: str | None) -> bool:
    act = site_dir / ".activated"
    if not act.exists() or not sub_id:
        return False
    try:
        return json.loads(act.read_text(encoding="utf-8")).get("stripe_subscription_id") == sub_id
    except (OSError, ValueError):
        return False


def find_slug_by_subscription(sub_id: str | None) -> str | None:
    if not sub_id or not SITES_DIR.exists():
        return None
    for child in SITES_DIR.iterdir():
        if child.is_symlink() or not child.is_dir():
            continue
        act_file = child / ".activated"
        if act_file.exists():
            try:
                data = json.loads(act_file.read_text(encoding="utf-8"))
                if data.get("stripe_subscription_id") == sub_id:
                    return child.name
            except (OSError, ValueError):
                continue
    return None


def extract_invoice_subscription_id(obj: dict) -> str | None:
    return obj.get("subscription") or (
        ((obj.get("parent") or {}).get("subscription_details") or {}).get("subscription")
    )


def build_fallback_banner(site_dir: Path) -> str:
    meta_path = site_dir / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    name = html.escape(meta.get("name", site_dir.name), quote=True)
    checkout = html.escape(meta.get("checkout_url", f"/checkout/{site_dir.name}"), quote=True)
    price_label = html.escape(meta.get("price_label", "49€/mois"), quote=True)
    return (
        "<!-- BANDEAU_DEMO_START -->"
        '<div class="bg-indigo-600 text-white text-center py-3 px-4 text-sm font-medium sticky top-0 z-50 shadow-md">'
        f"👀 Aperçu généré pour <strong>{name}</strong> — "
        f'<a href="{checkout}" class="underline font-bold ml-1 whitespace-nowrap">'
        f"Activer ce site sur mon domaine .fr ({price_label}) →</a></div>"
        "<!-- BANDEAU_DEMO_END -->"
    )


# --------------------------------------------------------------------------- #
# Activation & Suspension Atomiques
# --------------------------------------------------------------------------- #
def activate_site_files(site_dir: Path, session_obj: dict) -> str:
    slug = site_dir.name
    new_sub_id = session_obj.get("subscription")
    act_file = site_dir / ".activated"

    # 1. Idempotence + protection contre les doubles abonnements
    if act_file.exists():
        try:
            existing = json.loads(act_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing = {}
        existing_sub_id = existing.get("stripe_subscription_id")
        # (a) Déjà activé avec CE MÊME abonnement → événement Stripe redélivré : idempotent,
        #     aucun effet de bord répété (pas de re-notification, pas de timestamp réécrit).
        if existing_sub_id and new_sub_id and existing_sub_id == new_sub_id:
            log.info("Site %s déjà activé pour l'abonnement %s — événement redélivré ignoré", slug, new_sub_id)
            return "already_active"
        # (b) Abonnement DIFFÉRENT → vrai doublon : on garde l'existant et on annule le nouveau.
        if existing_sub_id and new_sub_id and existing_sub_id != new_sub_id:
            log.warning(
                "Double abonnement détecté pour %s : conservation de %s, annulation du doublon %s",
                slug, existing_sub_id, new_sub_id,
            )
            if not stripe.api_key:
                # Impossible d'annuler → ne PAS répondre 200 : laisser Stripe redélivrer.
                raise HTTPException(status_code=503, detail="Annulation du doublon impossible (Stripe non configuré)")
            try:
                stripe.Subscription.cancel(new_sub_id)
            except Exception as e:  # noqa: BLE001
                log.error("Échec de l'annulation automatique du doublon %s : %s", new_sub_id, e)
                # Erreur transitoire : renvoyer non-2xx pour que Stripe réessaie,
                # sinon l'abonnement fantôme reste actif et facture le client dans le vide.
                raise HTTPException(
                    status_code=503, detail="Échec annulation doublon — nouvelle tentative requise"
                ) from e
            return "duplicate_cancelled"

    # 2. Sauvegarde atomique de l'aperçu démo et nettoyage de index.html
    index_path = site_dir / "index.html"
    preview_backup = site_dir / "index.preview.html"
    if index_path.exists():
        html_text = index_path.read_text(encoding="utf-8")
        if "<!-- BANDEAU_DEMO_START -->" in html_text and not preview_backup.exists():
            atomic_write_text(preview_backup, html_text)

        html_text = BANNER_RE.sub("", html_text)
        html_text = html_text.replace(
            '<meta name="robots" content="noindex, nofollow">',
            '<meta name="robots" content="index, follow">',
        )
        # Options achetées : retire le configurateur de devis (vente) + injecte les
        # sections dues (RDV…) d'après meta['quote']['selection']['options'].
        try:
            _m = json.loads((site_dir / "meta.json").read_text(encoding="utf-8"))
            _opts = ((_m.get("quote") or {}).get("selection") or {}).get("options") or {}
        except (OSError, ValueError):
            _opts = {}
        html_text = demo_templates.apply_purchased_options(html_text, slug, _opts)
        atomic_write_text(index_path, html_text)

    # 3. Enregistrement du domaine personnalisé (sans écraser celui d'un autre client)
    desired_domain = extract_desired_domain(session_obj)
    registered_domain: str | None = None
    conflict_owner: str | None = None
    if desired_domain:
        domains = load_domains_map()
        owner = domains.get(desired_domain)
        if owner and owner != slug:
            conflict_owner = owner
            log.warning("Le domaine %s demandé par %s appartient déjà à %s", desired_domain, slug, owner)
        else:
            domains[desired_domain] = slug
            save_domains_map(domains)
            registered_domain = desired_domain
            domain_symlink = SITES_DIR / desired_domain
            if not domain_symlink.exists() and not domain_symlink.is_symlink():
                try:
                    domain_symlink.symlink_to(site_dir.name)
                except OSError as e:
                    log.warning("Création du lien symbolique %s impossible : %s", desired_domain, e)

    # 4. Écriture atomique de .activated (+ conserve / crée le token upload photos)
    customer_details = session_obj.get("customer_details") or {}
    existing_upload_token = None
    if act_file.exists():
        try:
            existing_upload_token = json.loads(act_file.read_text(encoding="utf-8")).get("upload_token")
        except (OSError, ValueError):
            existing_upload_token = None

    activation_info = {
        "activated_at": datetime.now(timezone.utc).isoformat(),
        "slug": slug,
        "stripe_session_id": session_obj.get("id", ""),
        "stripe_customer_id": session_obj.get("customer"),
        "stripe_subscription_id": new_sub_id,
        "desired_domain": registered_domain,
        "requested_domain_raw": desired_domain,
        "domain_conflict_with": conflict_owner,
        "tos_accepted": (session_obj.get("consent") or {}).get("terms_of_service"),
        "customer_details": customer_details,
    }
    if existing_upload_token:
        activation_info["upload_token"] = existing_upload_token
    atomic_write_text(act_file, json.dumps(activation_info, ensure_ascii=False, indent=2))

    try:
        photo_upload.ensure_upload_token(
            site_dir,
            SITES_DIR,
            business_name=(customer_details.get("name") if isinstance(customer_details, dict) else None),
        )
    except Exception as e:  # noqa: BLE001
        log.warning("Token upload photos non créé pour %s : %s", slug, e)

    # 5. TTL : site payé = jamais d'expiration
    try:
        import demo_ttl

        demo_ttl.clear_expiry_on_activation(site_dir)
    except Exception as e:  # noqa: BLE001
        log.warning("clear_expiry_on_activation échoué pour %s : %s", slug, e)

    log.info("Site activé : %s (subscription=%s, domain=%s)", slug, new_sub_id, registered_domain)

    # 6. Notification asynchrone non-bloquante
    notify_admin_async(
        slug, registered_domain, desired_domain, conflict_owner, customer_details,
        _plan_label_from_session(session_obj),
    )
    return "activated"


def deactivate_site_files(site_dir: Path, reason: str) -> None:
    slug = site_dir.name
    act_file = site_dir / ".activated"
    index_path = site_dir / "index.html"
    preview_backup = site_dir / "index.preview.html"

    if preview_backup.exists():
        atomic_write_text(index_path, preview_backup.read_text(encoding="utf-8"))
    elif index_path.exists():
        html_text = index_path.read_text(encoding="utf-8")
        html_text = html_text.replace(
            '<meta name="robots" content="index, follow">',
            '<meta name="robots" content="noindex, nofollow">',
        )
        if "<!-- BANDEAU_DEMO_START -->" not in html_text:
            banner = build_fallback_banner(site_dir)
            html_text = re.sub(r"(<body[^>]*>)", r"\1\n  " + banner, html_text, count=1)
        atomic_write_text(index_path, html_text)

    domains = load_domains_map()
    removed_domains = [d for d, s in list(domains.items()) if s == slug]
    for d in removed_domains:
        domains.pop(d, None)
        symlink_path = SITES_DIR / d
        if symlink_path.is_symlink():
            symlink_path.unlink(missing_ok=True)
    if removed_domains:
        save_domains_map(domains)

    if act_file.exists():
        act_file.unlink()

    log.info("Site remis en mode démo : %s (raison=%s)", slug, reason)


# --------------------------------------------------------------------------- #
# Endpoints FastAPI
# --------------------------------------------------------------------------- #
def _resolve_tier_price_id(tier: dict) -> str:
    """price_id Stripe du palier. Visibilité réutilise STRIPE_PRICE_ID (49€) si non défini."""
    mapping = {
        "STRIPE_PRICE_ESSENTIEL": STRIPE_PRICE_ESSENTIEL,
        "STRIPE_PRICE_VISIBILITE": STRIPE_PRICE_VISIBILITE or STRIPE_PRICE_ID,
        "STRIPE_PRICE_CROISSANCE": STRIPE_PRICE_CROISSANCE,
    }
    return mapping.get((tier or {}).get("stripe_env", ""), "")


def _build_checkout_session(
    slug: str,
    site_dir: Path,
    meta: dict,
    *,
    quote: dict | None = None,
    customer: str | None = None,
):
    """
    Construit la session Stripe Checkout (mode subscription) et la renvoie.
    - Avec un devis (meta['quote'] ou `quote`) : prix du palier + frais de création one-shot.
    - Sinon : offre 49€ legacy (STRIPE_PRICE_ID).
    """
    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Configuration Stripe incomplète sur le serveur.")

    if quote is None and isinstance(meta.get("quote"), dict):
        quote = meta["quote"]

    business_name = meta.get("name", slug)[:450]
    place_id = meta.get("place_id", "")
    preview_url = meta.get("preview_url", CANCEL_URL)
    stripe_meta = {"slug": slug, "place_id": place_id, "business_name": business_name}

    if quote:
        tier = quote.get("tier") or {}
        price_id = _resolve_tier_price_id(tier)
        if not price_id:
            raise HTTPException(
                status_code=500,
                detail=f"Prix Stripe manquant pour le palier « {tier.get('id')} ».",
            )
        line_items: list[dict] = [{"price": price_id, "quantity": 1}]
        creation_cents = int(quote.get("creation_cents") or 0)
        if creation_cents > 0:
            # Frais de création one-shot (montant variable) : price_data sans 'recurring'
            # => facturé une seule fois, sur la 1re facture de l'abonnement (panier mixte Stripe).
            line_items.append(
                {
                    "price_data": {
                        "currency": "eur",
                        "product_data": {"name": f"Création du site — {quote.get('level', 'sur mesure')}"},
                        "unit_amount": creation_cents,
                    },
                    "quantity": 1,
                }
            )
        stripe_meta["tier"] = tier.get("id", "")
        stripe_meta["creation_cents"] = str(creation_cents)
    else:
        if not STRIPE_PRICE_ID:
            raise HTTPException(status_code=500, detail="Configuration Stripe incomplète sur le serveur.")
        line_items = [{"price": STRIPE_PRICE_ID, "quantity": 1}]

    checkout_kwargs: dict = {
        "mode": "subscription",
        "line_items": line_items,
        "client_reference_id": slug,
        "metadata": stripe_meta,
        "subscription_data": {"metadata": stripe_meta},
        "phone_number_collection": {"enabled": True},
        "custom_fields": [
            {
                "key": "desired_domain",
                "label": {
                    "type": "custom",
                    "custom": "Nom de domaine .fr souhaité (ex: martin-plomberie.fr)",
                },
                "type": "text",
                "optional": True,
            }
        ],
        "success_url": f"{SUCCESS_URL}?slug={slug}&session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": preview_url,
        "locale": "fr",
    }
    if STRIPE_REQUIRE_TOS:
        checkout_kwargs["consent_collection"] = {"terms_of_service": "required"}
    if customer and (stripe.api_key or "").startswith("sk_test_"):
        checkout_kwargs["customer"] = customer

    try:
        return stripe.checkout.Session.create(**checkout_kwargs)
    except stripe.StripeError as e:
        log.error("Erreur Stripe pour %s : %s", slug, e)
        raise HTTPException(status_code=502, detail="Impossible d'initialiser le paiement Stripe.") from e


def build_quote_checkout_url(slug: str, quote: dict) -> str:
    """Builder injecté dans quote_request : crée la session Checkout d'un devis validé, renvoie son URL."""
    site_dir = resolve_site_dir(slug)
    meta_path = site_dir / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    sess = _build_checkout_session(slug, site_dir, meta, quote=quote)
    return sess.url


@app.get("/checkout/{slug}")
def create_checkout_session(slug: str, customer: str | None = None):
    site_dir = resolve_site_dir(slug)

    if (site_dir / ".activated").exists():
        meta_path = site_dir / "meta.json"
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if meta.get("preview_url"):
                return RedirectResponse(url=meta["preview_url"], status_code=303)
        return HTMLResponse("Ce site est déjà actif.", status_code=200)

    meta_path = site_dir / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}

    # Démo expirée : refuser l'activation
    try:
        import demo_ttl

        if demo_ttl.is_expired(meta, site_dir):
            return HTMLResponse(
                demo_ttl.expired_page_html(),
                status_code=410,
                headers={"X-Robots-Tag": "noindex, nofollow"},
            )
    except Exception as e:  # noqa: BLE001
        log.warning("Contrôle TTL checkout échoué pour %s : %s", slug, e)

    sess = _build_checkout_session(slug, site_dir, meta, customer=customer)
    return RedirectResponse(url=sess.url, status_code=303)


@app.post("/webhook/stripe")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    if not STRIPE_WEBHOOK_SECRET:
        log.error("STRIPE_WEBHOOK_SECRET non configuré.")
        raise HTTPException(status_code=500, detail="Webhook secret non configuré.")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Payload invalide") from e
    except stripe.SignatureVerificationError as e:
        raise HTTPException(status_code=400, detail="Signature Stripe invalide") from e

    event_type = event.get("type")
    obj = event["data"]["object"]

    if event_type in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        if (
            event_type == "checkout.session.completed"
            and obj.get("payment_status") not in VALID_CHECKOUT_PAYMENT_STATUSES
        ):
            log.info(
                "Session %s complétée mais payment_status=%s : attente de async_payment_succeeded",
                obj.get("id"), obj.get("payment_status"),
            )
            return {"status": "pending_payment"}

        slug = obj.get("client_reference_id") or (obj.get("metadata") or {}).get("slug")
        if slug and SAFE_SLUG_RE.match(slug):
            site_dir = SITES_DIR / slug
            if site_dir.is_dir():
                result = activate_site_files(site_dir, obj)
                return {"status": result, "slug": slug}

    elif event_type in ("customer.subscription.deleted", "customer.subscription.updated"):
        sub_id = obj.get("id")
        sub_status = obj.get("status", "")
        if event_type == "customer.subscription.deleted" or sub_status in SUSPENDED_STATUSES:
            slug = (obj.get("metadata") or {}).get("slug") or find_slug_by_subscription(sub_id)
            if slug and SAFE_SLUG_RE.match(slug):
                site_dir = SITES_DIR / slug
                if site_dir.is_dir():
                    if not is_active_subscription(site_dir, sub_id):
                        log.info(
                            "Événement %s ignoré pour %s : l'abonnement %s n'est pas l'abonnement actif",
                            event_type, slug, sub_id,
                        )
                        return {"status": "ignored_non_active_subscription", "slug": slug}
                    deactivate_site_files(site_dir, reason=f"{event_type}:{sub_status}")
                    return {"status": "deactivated", "slug": slug}

    elif event_type == "invoice.payment_failed":
        sub_id = extract_invoice_subscription_id(obj)
        parent_sub_meta = (((obj.get("parent") or {}).get("subscription_details") or {}).get("metadata") or {})
        slug = (
            (obj.get("metadata") or {}).get("slug")
            or parent_sub_meta.get("slug")
            or (obj.get("subscription_details", {}).get("metadata") or {}).get("slug")
            or find_slug_by_subscription(sub_id)
        )
        if slug and SAFE_SLUG_RE.match(slug):
            site_dir = SITES_DIR / slug
            if site_dir.is_dir() and is_active_subscription(site_dir, sub_id):
                attempt = obj.get("attempt_count", 1)
                log.warning(
                    "Échec de paiement facture (tentative %s) pour %s (sub=%s) : maintien du site pendant les relances.",
                    attempt, slug, sub_id,
                )
                return {"status": "payment_failed_grace_period", "slug": slug}

    elif event_type == "checkout.session.async_payment_failed":
        sub_id = obj.get("subscription")
        slug = obj.get("client_reference_id") or (obj.get("metadata") or {}).get("slug") or find_slug_by_subscription(sub_id)
        if slug and SAFE_SLUG_RE.match(slug):
            site_dir = SITES_DIR / slug
            if site_dir.is_dir() and is_active_subscription(site_dir, sub_id):
                deactivate_site_files(site_dir, reason=event_type)
                return {"status": "deactivated", "slug": slug}

    return {"status": "ok"}


@app.get("/api/check-domain")
def check_domain(domain: str):
    clean = normalize_domain(domain)
    if not clean:
        raise HTTPException(status_code=403, detail="Format de domaine invalide")

    domains = load_domains_map()
    slug = domains.get(clean)
    if slug and SAFE_SLUG_RE.match(slug):
        site_dir = SITES_DIR / slug
        if site_dir.is_dir() and (site_dir / ".activated").exists():
            return {"status": "allowed", "slug": slug}

    raise HTTPException(status_code=403, detail="Domaine non autorisé ou abonnement inactif")


@app.get("/api/upload-info")
def upload_info(slug: str, session_id: str = ""):
    """
    For /merci/?slug=&session_id= — returns private upload URL only when
    session_id matches the activated Stripe checkout session (or is empty
    only in tests when STRIPE is unset — production always requires match).
    """
    if not SAFE_SLUG_RE.match(slug):
        raise HTTPException(status_code=400, detail="Slug invalide")
    site_dir = SITES_DIR / slug
    if not site_dir.is_dir() or not (site_dir / ".activated").exists():
        raise HTTPException(status_code=404, detail="Site inconnu ou inactif")
    try:
        act = json.loads((site_dir / ".activated").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise HTTPException(status_code=404, detail="Activation illisible") from e

    expected_session = act.get("stripe_session_id") or ""
    if not session_id or not expected_session or not secrets_compare(session_id, expected_session):
        raise HTTPException(status_code=403, detail="Session non autorisée")

    try:
        token = photo_upload.ensure_upload_token(site_dir, SITES_DIR)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail="Token upload indisponible") from e

    meta = {}
    meta_path = site_dir / "meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
    name = meta.get("name") or (act.get("customer_details") or {}).get("name") or slug
    return {
        "slug": slug,
        "name": name,
        "upload_url": photo_upload.upload_url(slug, token),
        "site_url": photo_upload.site_public_url(slug),
    }


def secrets_compare(a: str, b: str) -> bool:
    if not a or not b:
        return False
    return secrets.compare_digest(str(a), str(b))


@app.get("/upload/{slug}/{token}", response_class=HTMLResponse)
def upload_page(slug: str, token: str, request: Request):
    if not SAFE_SLUG_RE.match(slug):
        raise HTTPException(status_code=404, detail="Introuvable")
    site_dir = (SITES_DIR / slug).resolve()
    try:
        site_dir.relative_to(SITES_DIR.resolve())
    except ValueError as e:
        raise HTTPException(status_code=404, detail="Introuvable") from e
    try:
        act = photo_upload.validate_slug_token(site_dir, slug, token)
    except PermissionError:
        raise HTTPException(status_code=404, detail="Lien invalide ou expiré") from None

    client = request.client.host if request.client else "unknown"
    if not photo_upload.check_rate_limit(f"get:{client}:{slug}"):
        raise HTTPException(status_code=429, detail="Trop de requêtes — réessayez dans une minute")

    meta = {}
    meta_path = site_dir / "meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
    name = meta.get("name") or (act.get("customer_details") or {}).get("name") or slug
    return photo_upload.render_upload_page(name, photo_upload.site_public_url(slug))


@app.post("/upload/{slug}/{token}")
async def upload_photos(
    slug: str,
    token: str,
    request: Request,
    photos: list[UploadFile] = File(default=[]),
    captions: list[str] = Form(default=[]),
):
    if not SAFE_SLUG_RE.match(slug):
        raise HTTPException(status_code=404, detail="Introuvable")
    site_dir = (SITES_DIR / slug).resolve()
    try:
        site_dir.relative_to(SITES_DIR.resolve())
    except ValueError as e:
        raise HTTPException(status_code=404, detail="Introuvable") from e
    try:
        act = photo_upload.validate_slug_token(site_dir, slug, token)
    except PermissionError:
        raise HTTPException(status_code=404, detail="Lien invalide ou expiré") from None

    client = request.client.host if request.client else "unknown"
    if not photo_upload.check_rate_limit(f"post:{client}:{slug}"):
        raise HTTPException(status_code=429, detail="Trop de requêtes — réessayez dans une minute")

    if not photos:
        raise HTTPException(status_code=400, detail="Aucune photo envoyée")

    items: list[tuple[bytes, str | None, str | None, str]] = []
    for i, up in enumerate(photos):
        raw = await up.read()
        cap = captions[i] if i < len(captions) else ""
        items.append((raw, up.content_type, up.filename, cap))

    try:
        created = photo_upload.save_uploaded_photos(site_dir, items)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        log.exception("Upload photos échoué pour %s", slug)
        raise HTTPException(status_code=500, detail="Échec du traitement des photos") from e

    meta = {}
    meta_path = site_dir / "meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
    name = meta.get("name") or (act.get("customer_details") or {}).get("name") or slug
    alert = photo_upload.format_photo_upload_alert(slug, name, len(created))
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        threading.Thread(target=send_telegram_message, args=(alert,), daemon=True).start()

    return JSONResponse(
        {
            "status": "ok",
            "count": len(created),
            "photos": [{"id": p["id"], "caption": p.get("caption", "")} for p in created],
            "site_url": photo_upload.site_public_url(slug),
        }
    )


@app.post("/upload/{slug}/{token}/delete/{photo_id}")
def upload_soft_delete(slug: str, token: str, photo_id: str, request: Request):
    if not SAFE_SLUG_RE.match(slug):
        raise HTTPException(status_code=404, detail="Introuvable")
    site_dir = (SITES_DIR / slug).resolve()
    try:
        site_dir.relative_to(SITES_DIR.resolve())
    except ValueError as e:
        raise HTTPException(status_code=404, detail="Introuvable") from e
    try:
        photo_upload.validate_slug_token(site_dir, slug, token)
    except PermissionError:
        raise HTTPException(status_code=404, detail="Lien invalide ou expiré") from None

    client = request.client.host if request.client else "unknown"
    if not photo_upload.check_rate_limit(f"del:{client}:{slug}"):
        raise HTTPException(status_code=429, detail="Trop de requêtes")

    try:
        ok = photo_upload.soft_delete_photo(site_dir, photo_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if not ok:
        raise HTTPException(status_code=404, detail="Photo introuvable")
    return {"status": "soft_deleted", "id": photo_id}


# --------------------------------------------------------------------------- #
# Demande de démo gratuite (vitrine → pending → Telegram admin)
# --------------------------------------------------------------------------- #
@app.get("/api/demo-expired")
@app.get("/api/demo-expired/{slug}")
def api_demo_expired(slug: str = ""):
    """Page 410 pour démos archivées (Caddy rewrite quand .expired présent)."""
    import demo_ttl

    return HTMLResponse(
        demo_ttl.expired_page_html(),
        status_code=410,
        headers={
            "X-Robots-Tag": "noindex, nofollow",
            "Cache-Control": "no-store",
        },
    )


@app.post("/api/demo-request")
async def api_demo_request(request: Request):
    """
    Public form endpoint. Persists JSON (status=pending), rate-limits by email+IP,
    rejects honeypot spam silently, alerts Telegram with inline buttons.
    """
    import demo_limits

    peer = request.client.host if request.client else "unknown"
    ip = demo_limits.resolve_client_ip(request.headers, peer)

    try:
        raw = await request.json()
    except Exception:  # noqa: BLE001
        # Also accept form-urlencoded / multipart
        try:
            form = await request.form()
            raw = dict(form)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=400, detail="Corps JSON ou formulaire requis") from e

    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="Payload invalide")

    fields, err = demo_request.validate_payload(raw)
    if err == "honeypot":
        # Silent success for bots
        return JSONResponse({"status": "ok"}, status_code=200)
    if err or fields is None:
        raise HTTPException(status_code=400, detail=err or "Données invalides")

    # Burst court (filet) + limites e-mail / IP persistantes
    if not demo_request.check_rate_limit(ip):
        raise HTTPException(status_code=429, detail=demo_limits.REJECT_MESSAGE_FR)

    gate = demo_limits.check_and_register(fields["email"], ip)
    if not gate["allowed"]:
        # Pas d'alerte Telegram pour les refus
        raise HTTPException(status_code=429, detail=gate["message"] or demo_limits.REJECT_MESSAGE_FR)

    data = demo_request.create_pending_request(
        fields, ip, ip_hash=gate["ip_hash"], prior_notes=gate.get("prior_notes") or []
    )
    threading.Thread(
        target=demo_request.send_demo_alert,
        args=(data,),
        daemon=True,
    ).start()

    return JSONResponse(
        {
            "status": "ok",
            "id": data["id"],
            "message": "Demande enregistrée. Nous vous recontactons rapidement.",
        },
        status_code=201,
    )


@app.post("/api/quote/{slug}")
async def api_quote(slug: str, request: Request):
    """
    L'artisan envoie sa config de devis depuis la démo. Le prix est RECALCULÉ serveur
    (pricing.py), un devis 'pending' est créé, et l'admin reçoit la carte Telegram
    [Valider | Refuser]. Le client ne reçoit le lien de paiement qu'après validation.
    """
    if not SAFE_SLUG_RE.match(slug):
        raise HTTPException(status_code=400, detail="Identifiant de site invalide.")

    import demo_limits

    peer = request.client.host if request.client else "unknown"
    ip = demo_limits.resolve_client_ip(request.headers, peer)  # vraie IP client derrière Caddy
    if not demo_request.check_rate_limit(f"quote:{ip}"):
        raise HTTPException(status_code=429, detail="Trop de demandes — réessayez dans un instant.")

    try:
        raw = await request.json()
    except Exception:  # noqa: BLE001
        try:
            form = await request.form()
            raw = dict(form)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=400, detail="Corps JSON requis") from e
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="Payload invalide")

    try:
        data = quote_request.create_pending_quote(slug, raw)
    except pricing.QuoteError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

    threading.Thread(target=quote_request.send_quote_alert, args=(data,), daemon=True).start()
    return JSONResponse(
        {
            "status": "ok",
            "id": data["id"],
            "message": "Votre devis a été envoyé. Vous recevrez le lien de paiement après validation.",
        },
        status_code=201,
    )


def send_rdv_alert(slug: str, site_dir: Path, fields: dict) -> bool:
    """Notifie l'admin sur Telegram d'une demande de RDV d'un client (site activé).
    Pas d'infra email pour l'instant : l'admin relaie à l'artisan, dont les
    coordonnées sont incluses pour faciliter le relais."""
    if not demo_request.TELEGRAM_BOT_TOKEN or not demo_request.TELEGRAM_CHAT_ID:
        log.info("Telegram non configuré — demande de RDV non envoyée (slug=%s)", slug)
        return False
    try:
        meta = json.loads((site_dir / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    business = meta.get("name") or slug
    artisan = " / ".join(x for x in (meta.get("phone") or "", meta.get("email") or "") if x)
    lines = [
        "📅 Demande de rendez-vous (client d'un site activé)",
        f"• Site : {business} ({slug})",
        f"• Client : {fields.get('name')} — {fields.get('phone')}",
    ]
    if fields.get("slot"):
        lines.append(f"• Créneau souhaité : {fields.get('slot')}")
    if fields.get("message"):
        lines.append(f"• Message : {fields.get('message')}")
    if artisan:
        lines.append(f"→ À transmettre à l'artisan : {artisan}")
    resp = demo_request.telegram_api(
        "sendMessage", {"chat_id": demo_request.TELEGRAM_CHAT_ID, "text": "\n".join(lines)}
    )
    return bool(resp)


@app.post("/api/rdv/{slug}")
async def api_rdv(slug: str, request: Request):
    """Un client de l'artisan demande un RDV depuis le site activé. On notifie l'admin
    sur Telegram (qui relaie à l'artisan — pas d'infra email pour l'instant)."""
    if not SAFE_SLUG_RE.match(slug):
        raise HTTPException(status_code=400, detail="Identifiant de site invalide.")

    import demo_limits

    peer = request.client.host if request.client else "unknown"
    ip = demo_limits.resolve_client_ip(request.headers, peer)
    if not demo_request.check_rate_limit(f"rdv:{ip}"):
        raise HTTPException(status_code=429, detail="Trop de demandes — réessayez dans un instant.")

    try:
        raw = await request.json()
    except Exception:  # noqa: BLE001
        try:
            raw = dict(await request.form())
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=400, detail="Corps JSON requis") from e
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="Payload invalide")

    site_dir = SITES_DIR / slug
    if not (site_dir / "index.html").exists():
        raise HTTPException(status_code=404, detail="Site introuvable.")

    def _clean(v: object, n: int) -> str:
        return " ".join(str(v or "").split())[:n]

    name = _clean(raw.get("name"), 120)
    phone = _clean(raw.get("phone"), 40)
    slot = _clean(raw.get("slot"), 160)
    message = _clean(raw.get("message"), 1000)
    if not name or not phone:
        raise HTTPException(status_code=400, detail="Nom et téléphone requis.")

    threading.Thread(
        target=send_rdv_alert,
        args=(slug, site_dir, {"name": name, "phone": phone, "slot": slot, "message": message}),
        daemon=True,
    ).start()
    return JSONResponse(
        {"status": "ok", "message": "Votre demande de rendez-vous a bien été envoyée."},
        status_code=201,
    )


@app.post("/webhook/telegram")
async def telegram_webhook(request: Request):
    """
    Telegram Bot API webhook (preferred over polling).
    Requires header X-Telegram-Bot-Api-Secret-Token matching TELEGRAM_WEBHOOK_SECRET.
    Caddy already proxies /webhook/* → FastAPI on :8000.
    """
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if not demo_request.verify_telegram_secret(secret):
        raise HTTPException(status_code=403, detail="Forbidden")

    try:
        update = await request.json()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="JSON invalide") from e

    if not isinstance(update, dict):
        raise HTTPException(status_code=400, detail="Update invalide")

    # Devis d'abord (callbacks quote:*), puis démo (demo:*)
    quote_result = quote_request.process_quote_callback(update, build_checkout=build_quote_checkout_url)
    if quote_result is not None:
        return {"ok": True, "result": quote_result}

    result = demo_request.process_telegram_update(update)
    return {"ok": True, "result": result}
