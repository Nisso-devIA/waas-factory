#!/usr/bin/env python3
"""
cloudflare_domain.py — Après achat/DNS OVH d'un domaine client, crée (ou réutilise)
une zone Cloudflare, synchronise A/MX/SPF, passe en Full (strict), désactive DNSSEC
OVH si besoin, puis bascule les NS OVH vers Cloudflare.

Idempotent. Sans CLOUDFLARE_API_TOKEN : skip avec avertissement (ne fait pas échouer
le pipeline).
"""
from __future__ import annotations

import os
import sys
import time
from typing import Any

import requests

CF_API_BASE = "https://api.cloudflare.com/client/v4"
DEFAULT_ACCOUNT_ID = "2dc6d88394ebd5dfb147ddf563d7e2eb"
DEFAULT_VPS_IP = "57.128.177.132"

# Absolute no-touch zones (CF zone / DNS / settings / OVH NS for these names).
# Include Unicode, ASCII fold, and IDNA/punycode so comparisons catch all forms.
PROTECTED_ZONES: frozenset[str] = frozenset(
    {
        "miina-pâtisserie.fr",
        "miina-patisserie.fr",
        "xn--miina-ptisserie-9jb.fr",  # IDNA of miina-pâtisserie.fr
    }
)

# Minimum API token permissions (dashboard → Custom token):
#   Zone → Zone → Edit
#   Zone → DNS → Edit
#   Zone → Zone Settings → Edit
# Account resources: include account DEFAULT_ACCOUNT_ID
# Zone resources: All zones (required to create new client zones)
TOKEN_PERMISSIONS_DOC = (
    "Zone:Zone:Edit, Zone:DNS:Edit, Zone:Zone Settings:Edit "
    f"(account {DEFAULT_ACCOUNT_ID}, All zones)"
)


class CloudflareAPIError(RuntimeError):
    """Erreur API Cloudflare (hors skip token manquant)."""


class ProtectedZoneError(CloudflareAPIError):
    """Refus d'écriture sur une zone/domaine protégé (ex. miina-pâtisserie.fr)."""


def domain_identity_keys(domain: str) -> frozenset[str]:
    """Clés de comparaison : lowercase + formes IDNA/punycode (et décodage inverse)."""
    raw = (domain or "").strip().rstrip(".").lower()
    if not raw:
        return frozenset()
    keys: set[str] = {raw}
    try:
        keys.add(raw.encode("idna").decode("ascii").lower())
    except (UnicodeError, UnicodeDecodeError):
        pass
    try:
        keys.add(raw.encode("ascii").decode("idna").lower())
    except (UnicodeError, UnicodeDecodeError, UnicodeEncodeError):
        pass
    return frozenset(keys)


def _protected_identity_keys() -> frozenset[str]:
    keys: set[str] = set()
    for label in PROTECTED_ZONES:
        keys |= domain_identity_keys(label)
    return frozenset(keys)


def is_protected_zone(domain: str) -> bool:
    """True si domain (apex ou hostname) correspond à une zone protégée (IDNA-aware)."""
    name = (domain or "").strip().rstrip(".").lower()
    if not name:
        return False
    protected = _protected_identity_keys()
    if domain_identity_keys(name) & protected:
        return True
    parts = name.split(".")
    for i in range(1, len(parts)):
        suffix = ".".join(parts[i:])
        if domain_identity_keys(suffix) & protected:
            return True
    return False


def assert_domain_writable(domain: str, *, action: str = "write") -> None:
    """Refuse toute mutation si le domaine/hostname est protégé."""
    if is_protected_zone(domain):
        raise ProtectedZoneError(
            f"Refus {action} : domaine/zone protégé(e) "
            f"(miina-pâtisserie.fr / punycode) — cible={domain!r}"
        )


def cloudflare_configured() -> bool:
    return bool((os.getenv("CLOUDFLARE_API_TOKEN") or "").strip())


def get_account_id() -> str:
    return (os.getenv("CLOUDFLARE_ACCOUNT_ID") or DEFAULT_ACCOUNT_ID).strip()


def get_vps_ip(override: str | None = None) -> str:
    return (override or os.getenv("VPS_PUBLIC_IP") or DEFAULT_VPS_IP).strip()


def _auth_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _cf_request(
    method: str,
    path: str,
    *,
    token: str,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    session: requests.Session | None = None,
    timeout: float = 30.0,
) -> Any:
    url = f"{CF_API_BASE}{path}"
    http = session or requests
    resp = http.request(
        method,
        url,
        headers=_auth_headers(token),
        params=params,
        json=json_body,
        timeout=timeout,
    )
    try:
        payload = resp.json()
    except ValueError as e:
        raise CloudflareAPIError(
            f"Cloudflare {method} {path}: réponse non-JSON (HTTP {resp.status_code})"
        ) from e
    if not payload.get("success", False):
        errors = payload.get("errors") or []
        msg = "; ".join(
            f"{e.get('code', '?')}: {e.get('message', e)}" for e in errors
        ) or f"HTTP {resp.status_code}"
        raise CloudflareAPIError(f"Cloudflare {method} {path}: {msg}")
    return payload.get("result")


def find_zone(
    domain: str,
    token: str,
    account_id: str,
    session: requests.Session | None = None,
) -> dict[str, Any] | None:
    result = _cf_request(
        "GET",
        "/zones",
        token=token,
        params={"name": domain, "account.id": account_id, "per_page": 5},
        session=session,
    )
    if not result:
        return None
    for zone in result:
        if (zone.get("name") or "").lower() == domain.lower():
            return zone
    return result[0] if result else None


def create_zone(
    domain: str,
    token: str,
    account_id: str,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    assert_domain_writable(domain, action="create_zone")
    return _cf_request(
        "POST",
        "/zones",
        token=token,
        json_body={"name": domain, "account": {"id": account_id}, "type": "full"},
        session=session,
    )


def ensure_zone(
    domain: str,
    token: str,
    account_id: str,
    session: requests.Session | None = None,
) -> tuple[dict[str, Any], bool]:
    """Retourne (zone, created). Réutilise la zone existante si présente."""
    # Refuse even reuse/sync of protected zones (no DNS/SSL/NS writes later).
    assert_domain_writable(domain, action="ensure_zone")
    existing = find_zone(domain, token, account_id, session=session)
    if existing:
        return existing, False
    return create_zone(domain, token, account_id, session=session), True


def list_dns_records(
    zone_id: str,
    token: str,
    *,
    name: str | None = None,
    type_: str | None = None,
    session: requests.Session | None = None,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"per_page": 100}
    if name is not None:
        params["name"] = name
    if type_ is not None:
        params["type"] = type_
    result = _cf_request(
        "GET",
        f"/zones/{zone_id}/dns_records",
        token=token,
        params=params,
        session=session,
    )
    return list(result or [])


def upsert_a_record(
    zone_id: str,
    token: str,
    *,
    name: str,
    ipv4: str,
    proxied: bool = True,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    assert_domain_writable(name, action="upsert_a_record")
    existing = list_dns_records(
        zone_id, token, name=name, type_="A", session=session
    )
    body = {
        "type": "A",
        "name": name,
        "content": ipv4,
        "proxied": proxied,
        "ttl": 1 if proxied else 3600,
    }
    if existing:
        rec_id = existing[0]["id"]
        return _cf_request(
            "PUT",
            f"/zones/{zone_id}/dns_records/{rec_id}",
            token=token,
            json_body=body,
            session=session,
        )
    return _cf_request(
        "POST",
        f"/zones/{zone_id}/dns_records",
        token=token,
        json_body=body,
        session=session,
    )


def ensure_dns_only_record(
    zone_id: str,
    token: str,
    *,
    type_: str,
    name: str,
    content: str,
    priority: int | None = None,
    session: requests.Session | None = None,
) -> dict[str, Any] | None:
    """Crée un enregistrement DNS-only s'il n'existe pas déjà (même type/name/content)."""
    assert_domain_writable(name, action="ensure_dns_only_record")
    existing = list_dns_records(
        zone_id, token, name=name, type_=type_, session=session
    )
    norm = content.strip().strip('"').rstrip(".").lower()
    for rec in existing:
        rec_content = str(rec.get("content") or "").strip().strip('"').rstrip(".").lower()
        if rec_content == norm:
            if priority is None or int(rec.get("priority") or 0) == priority:
                return rec
    body: dict[str, Any] = {
        "type": type_,
        "name": name,
        "content": content.strip().strip('"'),
        "proxied": False,
        "ttl": 3600,
    }
    if priority is not None:
        body["priority"] = priority
    return _cf_request(
        "POST",
        f"/zones/{zone_id}/dns_records",
        token=token,
        json_body=body,
        session=session,
    )


def set_ssl_full_strict(
    zone_id: str,
    token: str,
    session: requests.Session | None = None,
    *,
    domain: str | None = None,
) -> Any:
    if domain is not None:
        assert_domain_writable(domain, action="set_ssl_full_strict")
    return _cf_request(
        "PATCH",
        f"/zones/{zone_id}/settings/ssl",
        token=token,
        json_body={"value": "strict"},
        session=session,
    )


def parse_ovh_mx_target(target: str) -> tuple[int, str] | None:
    """OVH MX target: '10 mx1.mail.ovh.net.' → (10, 'mx1.mail.ovh.net')."""
    parts = str(target or "").strip().split(None, 1)
    if len(parts) != 2:
        return None
    try:
        priority = int(parts[0])
    except ValueError:
        return None
    host = parts[1].rstrip(".")
    if not host:
        return None
    return priority, host


def list_ovh_mail_records(ovh_client: Any, domain: str) -> list[dict[str, Any]]:
    """MX + TXT SPF depuis la zone OVH (pour copie DNS-only vers Cloudflare)."""
    out: list[dict[str, Any]] = []
    try:
        record_ids = ovh_client.get(f"/domain/zone/{domain}/record")
    except Exception as e:  # noqa: BLE001
        print(f"Avertissement Cloudflare : lecture zone OVH impossible : {e}", file=sys.stderr)
        return out

    for rec_id in record_ids:
        try:
            rec = ovh_client.get(f"/domain/zone/{domain}/record/{rec_id}")
        except Exception:  # noqa: BLE001
            continue
        field = rec.get("fieldType")
        sub = rec.get("subDomain") or ""
        target = str(rec.get("target") or "")
        name = domain if not sub else f"{sub}.{domain}"

        if field == "MX":
            parsed = parse_ovh_mx_target(target)
            if parsed:
                priority, host = parsed
                out.append(
                    {
                        "type": "MX",
                        "name": name,
                        "content": host,
                        "priority": priority,
                    }
                )
        elif field == "TXT" and target.lower().lstrip('"').startswith("v=spf1"):
            out.append(
                {
                    "type": "TXT",
                    "name": name,
                    "content": target.strip().strip('"'),
                    "priority": None,
                }
            )
    return out


def disable_ovh_dnssec(ovh_client: Any, domain: str, *, timeout_sec: int = 180) -> None:
    """Désactive DNSSEC OVH et attend que dnssecState soit disabled (DS vide)."""
    assert_domain_writable(domain, action="disable_ovh_dnssec")
    try:
        info = ovh_client.get(f"/domain/{domain}")
    except Exception as e:  # noqa: BLE001
        print(f"Avertissement Cloudflare : lecture DNSSEC OVH : {e}", file=sys.stderr)
        return

    state = (info.get("dnssecState") or "").lower()
    ds = info.get("dsRecord") or []
    if state == "disabled" and not ds:
        print(f"[Cloudflare] DNSSEC OVH déjà désactivé pour {domain}.")
        return

    if state in ("enabled", "enableinprogress"):
        try:
            ovh_client.delete(f"/domain/zone/{domain}/dnssec")
            print(f"[Cloudflare] DNSSEC OVH : désactivation demandée pour {domain}.")
        except Exception as e:  # noqa: BLE001
            # Zone API may already be disabling
            print(f"Avertissement Cloudflare : DELETE dnssec : {e}", file=sys.stderr)

    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            info = ovh_client.get(f"/domain/{domain}")
        except Exception:  # noqa: BLE001
            time.sleep(5)
            continue
        state = (info.get("dnssecState") or "").lower()
        ds = info.get("dsRecord") or []
        if state == "disabled" and not ds:
            print(f"[Cloudflare] DNSSEC OVH désactivé pour {domain}.")
            return
        time.sleep(5)
    print(
        f"Avertissement Cloudflare : DNSSEC OVH pas encore fully disabled pour {domain} "
        f"(état={info.get('dnssecState')!r}) — bascule NS quand même risquée.",
        file=sys.stderr,
    )


def switch_ovh_nameservers(
    ovh_client: Any,
    domain: str,
    nameservers: list[str],
) -> None:
    """Bascule OVH en NS externes Cloudflare (host objects)."""
    assert_domain_writable(domain, action="switch_ovh_nameservers")
    hosts = [{"host": ns.rstrip(".")} for ns in nameservers if ns]
    if len(hosts) < 2:
        raise CloudflareAPIError(
            f"Moins de 2 nameservers Cloudflare pour {domain}: {nameservers!r}"
        )
    ovh_client.post(
        f"/domain/{domain}/nameServers/update",
        nameServers=hosts,
    )
    print(
        f"[Cloudflare] NS OVH → {', '.join(h['host'] for h in hosts)} (externe)."
    )


def sync_zone_records(
    zone: dict[str, Any],
    token: str,
    domain: str,
    vps_ip: str,
    mail_records: list[dict[str, Any]],
    session: requests.Session | None = None,
) -> None:
    assert_domain_writable(domain, action="sync_zone_records")
    zone_id = zone["id"]
    upsert_a_record(
        zone_id, token, name=domain, ipv4=vps_ip, proxied=True, session=session
    )
    upsert_a_record(
        zone_id,
        token,
        name=f"www.{domain}",
        ipv4=vps_ip,
        proxied=True,
        session=session,
    )
    for mail in mail_records:
        ensure_dns_only_record(
            zone_id,
            token,
            type_=mail["type"],
            name=mail["name"],
            content=mail["content"],
            priority=mail.get("priority"),
            session=session,
        )
    try:
        set_ssl_full_strict(zone_id, token, session=session, domain=domain)
        print(f"[Cloudflare] SSL mode Full (strict) pour {domain}.")
    except ProtectedZoneError:
        raise
    except CloudflareAPIError as e:
        print(f"Avertissement Cloudflare : SSL strict non appliqué : {e}", file=sys.stderr)


def move_client_domain_to_cloudflare(
    ovh_client: Any,
    domain: str,
    *,
    vps_ip: str | None = None,
    session: requests.Session | None = None,
    dnssec_timeout_sec: int = 180,
    skip_ns_switch: bool = False,
) -> dict[str, Any]:
    """
    Pipeline idempotent post-DNS OVH → Cloudflare.

    Retourne un dict de statut. Si token absent : skipped=True, sans exception.
    """
    token = (os.getenv("CLOUDFLARE_API_TOKEN") or "").strip()
    if not token:
        print(
            "Avertissement Cloudflare : CLOUDFLARE_API_TOKEN absent — "
            "étape Cloudflare ignorée (pipeline OVH OK).",
            file=sys.stderr,
        )
        return {"skipped": True, "reason": "missing_token", "domain": domain}

    # Hard stop before any CF/OVH mutation on protected zones (miina, …).
    assert_domain_writable(domain, action="move_client_domain_to_cloudflare")

    account_id = get_account_id()
    ip = get_vps_ip(vps_ip)
    own_session = session is None
    http = session or requests.Session()

    try:
        zone, created = ensure_zone(domain, token, account_id, session=http)
        zone_id = zone["id"]
        nameservers = list(zone.get("name_servers") or zone.get("nameservers") or [])
        if not nameservers:
            # Refresh zone details for NS
            zone = _cf_request("GET", f"/zones/{zone_id}", token=token, session=http)
            nameservers = list(zone.get("name_servers") or [])

        action = "créée" if created else "réutilisée"
        print(
            f"[Cloudflare] Zone {action} pour {domain} "
            f"(id={zone_id}, ns={', '.join(nameservers) or 'n/a'})."
        )

        mail_records = list_ovh_mail_records(ovh_client, domain)
        sync_zone_records(zone, token, domain, ip, mail_records, session=http)

        disable_ovh_dnssec(ovh_client, domain, timeout_sec=dnssec_timeout_sec)

        if skip_ns_switch:
            print("[Cloudflare] Bascule NS OVH ignorée (skip_ns_switch).")
        else:
            switch_ovh_nameservers(ovh_client, domain, nameservers)

        return {
            "skipped": False,
            "domain": domain,
            "zone_id": zone_id,
            "created": created,
            "nameservers": nameservers,
            "vps_ip": ip,
            "mail_records_copied": len(mail_records),
        }
    finally:
        if own_session:
            http.close()


def try_move_client_domain_to_cloudflare(
    ovh_client: Any,
    domain: str,
    *,
    vps_ip: str | None = None,
) -> dict[str, Any]:
    """Wrapper non-fatal pour ovh_domain : catch + warning, ne remonte pas d'exception."""
    try:
        return move_client_domain_to_cloudflare(
            ovh_client, domain, vps_ip=vps_ip
        )
    except ProtectedZoneError as e:
        print(
            f"Avertissement Cloudflare : zone protégée, aucune écriture — {e}",
            file=sys.stderr,
        )
        return {
            "skipped": True,
            "reason": "protected_zone",
            "domain": domain,
            "error": str(e),
        }
    except Exception as e:  # noqa: BLE001
        print(
            f"Avertissement Cloudflare : échec migration {domain} : {e} "
            f"(domaine OVH / Caddy inchangés).",
            file=sys.stderr,
        )
        return {"skipped": True, "reason": "error", "domain": domain, "error": str(e)}
