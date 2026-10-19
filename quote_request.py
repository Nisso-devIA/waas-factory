"""
quote_request.py — Devis sur mesure (configurateur démo → validation Telegram → paiement).

Calqué sur demo_request.py, et réutilise ses helpers Telegram (telegram_api,
edit_message_text, answer_callback, empty_keyboard) pour rester cohérent.

Flux :
  1. POST /api/quote/{slug} → le serveur RECALCULE le prix (pricing.compute_quote),
     écrit un devis 'pending' dans _quotes/, et alerte l'admin sur Telegram avec
     [✅ Valider le devis | 🚫 Refuser].
  2. Callback 'quote:ok:{qid}' → fige le devis dans meta.json, crée le lien Stripe
     (palier + frais de création one-shot) via un builder injecté par webhook_server,
     et l'affiche dans la carte pour que l'admin le transmette au client
     (même principe que demo_request : pas d'envoi auto).
  3. Callback 'quote:no:{qid}' → 'Refusé', boutons retirés.

Règle d'or : le prix n'est JAMAIS transmis par le navigateur. pricing.compute_quote
est la seule source — un client qui bidouille le POST voit son prix recalculé.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import demo_request as dr  # réutilise telegram_api / edit_message_text / answer_callback / empty_keyboard
import pricing

log = logging.getLogger("quote_request")

# --------------------------------------------------------------------------- #
# Config (webhook_server peut réassigner SITES_DIR / QUOTES_DIR à l'import)
# --------------------------------------------------------------------------- #
SITES_DIR = Path(os.getenv("SITES_DIR", "/var/www/sites")).resolve()
QUOTES_DIR = Path(os.getenv("QUOTES_DIR") or (SITES_DIR / "_quotes")).resolve()

SAFE_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")
_QID_RE = re.compile(r"[A-Za-z0-9_-]{8,64}")
_CB_RE = re.compile(r"quote:(ok|no):([A-Za-z0-9_-]{8,64})")

# Sérialise les transitions d'état (pending → validating → validated/refused)
_claim_lock = threading.Lock()

# Signature du builder de checkout injecté par webhook_server :
#   build_checkout(slug: str, quote: dict) -> str   (renvoie l'URL Stripe Checkout)
BuildCheckout = Callable[[str, dict], str]


# --------------------------------------------------------------------------- #
# Persistance des devis (_quotes/{qid}.json) — mêmes garde-fous que demo_request
# --------------------------------------------------------------------------- #
def ensure_quotes_dir() -> Path:
    QUOTES_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(QUOTES_DIR, 0o700)
    except OSError:
        pass
    return QUOTES_DIR


def quote_path(qid: str) -> Path:
    if not _QID_RE.fullmatch(qid or ""):
        raise ValueError("quote id invalide")
    return ensure_quotes_dir() / f"{qid}.json"


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    ensure_quotes_dir()
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def load_quote(qid: str) -> dict[str, Any] | None:
    try:
        path = quote_path(qid)
    except ValueError:
        return None
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("Lecture devis %s échouée : %s", qid, e)
        return None


def save_quote(data: dict[str, Any]) -> Path:
    path = quote_path(data["id"])
    _atomic_write_json(path, data)
    return path


def mark_quote(qid: str, **updates: Any) -> dict[str, Any] | None:
    data = load_quote(qid)
    if not data:
        return None
    data.update(updates)
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    save_quote(data)
    return data


# --------------------------------------------------------------------------- #
# Accès au site (démo) + écriture du devis figé dans meta.json
# --------------------------------------------------------------------------- #
def site_dir_for(slug: str) -> Path | None:
    if not SAFE_SLUG_RE.fullmatch(slug or ""):
        return None
    d = (SITES_DIR / slug).resolve()
    if d.parent != SITES_DIR.resolve() or not d.is_dir():
        return None
    return d


def load_site_meta(site_dir: Path) -> dict[str, Any]:
    p = site_dir / "meta.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_site_quote(site_dir: Path, quote: dict[str, Any]) -> None:
    """Fige le devis validé dans meta['quote'] — autorité lue par /checkout."""
    p = site_dir / "meta.json"
    meta = load_site_meta(site_dir)
    meta["quote"] = quote
    tmp = p.with_name(f".{p.name}.tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


# --------------------------------------------------------------------------- #
# Création d'un devis (depuis POST /api/quote/{slug})
# --------------------------------------------------------------------------- #
def create_pending_quote(slug: str, raw_selection: Any) -> dict[str, Any]:
    """
    Recalcule le prix côté serveur et persiste un devis 'pending'.
    Lève ValueError si le site est inconnu, déjà actif, ou la sélection invalide.
    """
    site_dir = site_dir_for(slug)
    if site_dir is None:
        raise ValueError("Site introuvable.")
    if (site_dir / ".activated").exists():
        raise ValueError("Ce site est déjà actif.")

    quote = pricing.compute_quote(raw_selection)  # AUTORITÉ — ignore tout prix client
    meta = load_site_meta(site_dir)
    now = datetime.now(timezone.utc).isoformat()
    data: dict[str, Any] = {
        "id": secrets.token_urlsafe(12),
        "status": "pending",
        "created_at": now,
        "updated_at": now,
        "slug": slug,
        "business_name": meta.get("name") or slug,
        "selection": quote["selection"],
        "quote": quote,
    }
    save_quote(data)
    return data


# --------------------------------------------------------------------------- #
# Rendu des messages Telegram
# --------------------------------------------------------------------------- #
def format_quote_alert(data: dict[str, Any]) -> str:
    q = data["quote"]
    detail = "\n".join(f"   • {ln['label']} : {ln['amount']} €" for ln in q["lines"])
    t = q["tier"]
    return (
        "💶 Nouveau devis sur mesure\n"
        f"• Site : {data.get('business_name')} ({data.get('slug')})\n"
        f"• Niveau : {q['level']} (délai {q['delai']})\n"
        f"{detail}\n"
        f"• Création : {q['creation_eur']} € (acompte 40 % : {q['deposit_eur']} €)\n"
        f"• Abonnement : {t['name']} — {t['monthly_eur']} €/mois ({t['billing']})\n"
        f"• Id : {data.get('id')}"
    )


def format_validated_text(data: dict[str, Any], url: str) -> str:
    q = data["quote"]
    t = q["tier"]
    return (
        "✅ Devis validé — lien de paiement à transmettre au client\n"
        f"• Site : {data.get('business_name')} ({data.get('slug')})\n"
        f"• Création : {q['creation_eur']} € + {t['name']} {t['monthly_eur']} €/mois\n"
        f"• Lien : {url}"
    )


def format_refused_text(data: dict[str, Any]) -> str:
    return (
        "🚫 Devis refusé\n"
        f"• Site : {data.get('business_name')} ({data.get('slug')})\n"
        f"• Id : {data.get('id')}"
    )


def inline_keyboard_quote(qid: str) -> dict[str, Any]:
    ok, no = f"quote:ok:{qid}", f"quote:no:{qid}"
    return {
        "inline_keyboard": [[
            {"text": "✅ Valider le devis", "callback_data": ok},
            {"text": "🚫 Refuser", "callback_data": no},
        ]]
    }


def send_quote_alert(data: dict[str, Any]) -> bool:
    chat_id = dr.TELEGRAM_CHAT_ID
    if not dr.TELEGRAM_BOT_TOKEN or not chat_id:
        log.info("Telegram non configuré — alerte devis non envoyée (id=%s)", data.get("id"))
        return False
    resp = dr.telegram_api(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": format_quote_alert(data),
            "reply_markup": inline_keyboard_quote(data["id"]),
        },
    )
    if not resp:
        return False
    try:
        mid = (resp.get("result") or {}).get("message_id")
        if mid is not None:
            mark_quote(data["id"], telegram_chat_id=str(chat_id), telegram_message_id=int(mid))
    except Exception as e:  # noqa: BLE001
        log.warning("message_id devis non mémorisé : %s", e)
    return True


# --------------------------------------------------------------------------- #
# Validation / refus (idempotents via _claim_lock)
# --------------------------------------------------------------------------- #
def _claim_validating(qid: str) -> tuple[dict[str, Any] | None, str | None]:
    """pending → validating (atomique). Renvoie (data, None) ou (None, raison)."""
    with _claim_lock:
        data = load_quote(qid)
        if not data:
            return None, "introuvable"
        if (data.get("status") or "") != "pending":
            return None, data.get("status") or "inconnu"
        token = secrets.token_hex(8)
        return mark_quote(qid, status="validating", validation_token=token), None


def do_validate(
    data: dict[str, Any],
    *,
    build_checkout: BuildCheckout,
    chat_id: str | None = None,
    message_id: int | None = None,
) -> tuple[bool, str]:
    """Fige le devis, crée le lien Stripe, met à jour la carte Telegram. data est déjà 'validating'."""
    qid, slug = data["id"], data["slug"]
    site_dir = site_dir_for(slug)
    if site_dir is None:
        mark_quote(qid, status="failed", failure_reason="site introuvable", validation_token=None)
        return False, "Site introuvable."

    write_site_quote(site_dir, data["quote"])
    try:
        url = build_checkout(slug, data["quote"])
    except Exception as e:  # noqa: BLE001
        log.exception("Création du checkout devis échouée pour %s", slug)
        # On remet en 'pending' pour que l'admin puisse re-tenter
        mark_quote(qid, status="pending", validation_token=None)
        return False, f"Échec Stripe : {str(e)[:150]}"

    updated = mark_quote(
        qid,
        status="validated",
        checkout_url=url,
        validated_at=datetime.now(timezone.utc).isoformat(),
        validation_token=None,
    )
    cid = chat_id or data.get("telegram_chat_id") or dr.TELEGRAM_CHAT_ID
    mid = message_id if message_id is not None else data.get("telegram_message_id")
    if cid and mid:
        dr.edit_message_text(cid, int(mid), format_validated_text(updated or data, url), dr.empty_keyboard())
    return True, url


def refuse_quote(
    qid: str,
    *,
    chat_id: str | None = None,
    message_id: int | None = None,
) -> tuple[bool, str]:
    data = load_quote(qid)
    if not data:
        return False, "Devis introuvable."
    status = data.get("status")
    if status == "validated":
        return False, "Déjà validé."
    if status == "refused":
        return True, "Déjà refusé."
    updated = mark_quote(qid, status="refused", validation_token=None)
    cid = chat_id or data.get("telegram_chat_id") or dr.TELEGRAM_CHAT_ID
    mid = message_id if message_id is not None else data.get("telegram_message_id")
    text = format_refused_text(updated or data)
    if cid and mid:
        dr.edit_message_text(cid, int(mid), text, dr.empty_keyboard())
    return True, text


# --------------------------------------------------------------------------- #
# Routage des callbacks Telegram (appelé AVANT demo_request dans le webhook)
# --------------------------------------------------------------------------- #
def process_quote_callback(
    update: dict[str, Any],
    *,
    build_checkout: BuildCheckout,
) -> dict[str, Any] | None:
    """
    Gère les callbacks 'quote:*'. Renvoie None si ce n'est pas un callback devis
    (→ laisse demo_request.process_telegram_update s'en occuper).
    """
    cb = update.get("callback_query")
    if not cb:
        return None
    m = _CB_RE.fullmatch(str(cb.get("data") or ""))
    if not m:
        return None  # pas un callback devis

    cb_id = str(cb.get("id") or "")
    msg = cb.get("message") or {}
    chat_id = str((msg.get("chat") or {}).get("id") or "")
    try:
        mid = int(msg.get("message_id")) if msg.get("message_id") is not None else 0
    except (TypeError, ValueError):
        mid = 0

    # N'accepte que le chat admin configuré
    if dr.TELEGRAM_CHAT_ID and chat_id and chat_id != str(dr.TELEGRAM_CHAT_ID):
        dr.answer_callback(cb_id, "Non autorisé")
        return {"handled": False, "reason": "wrong_chat"}

    action, qid = m.group(1), m.group(2)

    if action == "no":
        ok, text = refuse_quote(qid, chat_id=chat_id, message_id=mid or None)
        dr.answer_callback(cb_id, "Refusé" if ok else "Erreur")
        return {"handled": True, "action": "no", "quote_id": qid, "ok": ok, "message": text}

    # action == "ok"
    claimed, reason = _claim_validating(qid)
    if not claimed:
        toast = {
            "validating": "Validation déjà en cours",
            "validated": "Déjà validé",
            "refused": "Déjà refusé",
            "failed": "Échec — recréer le devis",
            "introuvable": "Introuvable",
        }.get(reason or "", "Ignoré")
        dr.answer_callback(cb_id, toast)
        return {"handled": True, "action": "ok", "quote_id": qid, "ok": False, "reason": reason}

    dr.answer_callback(cb_id, "Validation…")
    ok, result = do_validate(claimed, build_checkout=build_checkout, chat_id=chat_id, message_id=mid)
    return {
        "handled": True,
        "action": "ok",
        "quote_id": qid,
        "ok": ok,
        "checkout_url": result if ok else None,
        "message": None if ok else result,
    }


# --------------------------------------------------------------------------- #
# Auto-test : python3 quote_request.py
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import tempfile

    base = Path(tempfile.mkdtemp(prefix="qtest_"))
    SITES_DIR = (base / "sites").resolve()
    QUOTES_DIR = (base / "_quotes").resolve()
    SITES_DIR.mkdir(parents=True)

    # Fausse démo
    site = SITES_DIR / "plomberie-martin"
    site.mkdir()
    (site / "meta.json").write_text(json.dumps({"name": "Plomberie Martin"}), encoding="utf-8")

    # 1) Création : vitrine (890) + 1 page sup (90) + RDV (250) = 1230 ; palier forcé croissance
    data = create_pending_quote("plomberie-martin", {"type": "vitrine", "pages": 6, "options": {"rdv": True}, "tier": "croissance"})
    assert data["status"] == "pending"
    assert data["quote"]["creation_eur"] == 1230, data["quote"]["creation_eur"]
    assert data["quote"]["tier"]["id"] == "croissance"
    qid = data["id"]

    # 2) Un callback NON-devis renvoie None (→ demo_request gère)
    assert process_quote_callback({"callback_query": {"data": "demo:gen:abc12345"}}, build_checkout=lambda s, q: "x") is None

    # 3) Validation via callback (Telegram non configuré → edit no-op)
    def fake_checkout(slug, quote):
        assert quote["creation_cents"] == 123000
        return "https://checkout.stripe.test/c/pay_123"

    upd_ok = {"callback_query": {"id": "1", "data": f"quote:ok:{qid}", "message": {"message_id": 7, "chat": {"id": "0"}}}}
    res = process_quote_callback(upd_ok, build_checkout=fake_checkout)
    assert res and res["ok"] is True, res
    after = load_quote(qid)
    assert after["status"] == "validated"
    assert after["checkout_url"] == "https://checkout.stripe.test/c/pay_123"
    assert load_site_meta(site).get("quote", {}).get("creation_eur") == 1230  # figé dans meta.json

    # 4) Re-valider un devis déjà validé → refusé (idempotence)
    res2 = process_quote_callback(upd_ok, build_checkout=fake_checkout)
    assert res2 and res2["ok"] is False and res2["reason"] == "validated", res2

    # 5) Refus d'un autre devis
    d2 = create_pending_quote("plomberie-martin", {"type": "landing"})
    ok, _ = refuse_quote(d2["id"])
    assert ok and load_quote(d2["id"])["status"] == "refused"

    # 6) Site déjà actif → refus de devis
    (site / ".activated").write_text("{}", encoding="utf-8")
    try:
        create_pending_quote("plomberie-martin", {"type": "vitrine"})
        raise AssertionError("aurait dû lever ValueError (site actif)")
    except ValueError:
        pass

    print("✅ quote_request.py — auto-tests OK")
