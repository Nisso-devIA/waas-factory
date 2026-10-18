#!/usr/bin/env python3
"""
ovh_domain.py — Vérification de disponibilité, achat plafonné et configuration DNS (.fr)
via l'API OVH, couplé à set_domain.py.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Any

import cloudflare_domain
import set_domain
import webhook_server as ws

DEFAULT_MAX_PRICE_HT = 15.0
OVH_PARKING_IPS = {"213.186.33.5"}


def get_ovh_client() -> Any:
    import ovh

    app_key = os.getenv("OVH_APPLICATION_KEY") or os.getenv("OVH_APP_KEY")
    app_secret = os.getenv("OVH_APPLICATION_SECRET") or os.getenv("OVH_APP_SECRET")
    consumer_key = os.getenv("OVH_CONSUMER_KEY")
    if not (app_key and app_secret and consumer_key):
        raise RuntimeError(
            "Clés OVH manquantes (OVH_APPLICATION_KEY, OVH_APPLICATION_SECRET, OVH_CONSUMER_KEY)."
        )
    return ovh.Client(
        endpoint=os.getenv("OVH_ENDPOINT", "ovh-eu"),
        application_key=app_key,
        application_secret=app_secret,
        consumer_key=consumer_key,
    )


def generate_variants(domain: str, dept: str = "95") -> list[str]:
    base = domain.removesuffix(".fr")
    return [
        domain,
        f"{base}-{dept}.fr",
        f"{base}-pro.fr",
        f"{base}-artisan.fr",
    ]


def extract_offer_price_ht(offer: dict) -> float | None:
    prices = offer.get("prices") or []
    for preferred_label in ("PRICE", "TOTAL"):
        for p in prices:
            if p.get("label") == preferred_label:
                val = (p.get("price") or {}).get("value")
                if val is not None:
                    return float(val)
    for p in prices:
        val = (p.get("price") or {}).get("value")
        if val is not None:
            return float(val)
    return None


def select_valid_offer(offers: list[dict], max_price: float = DEFAULT_MAX_PRICE_HT) -> tuple[dict, float] | None:
    for offer in offers:
        if not offer.get("orderable") or offer.get("action") != "create":
            continue
        if offer.get("pricingMode") != "default":
            continue
        price = extract_offer_price_ht(offer)
        if price is None or price <= 0 or price > max_price:
            continue
        return offer, price
    return None


def check_domain_offer(
    client: Any,
    subsidiary: str,
    domain: str,
    max_price: float = DEFAULT_MAX_PRICE_HT,
) -> tuple[dict, float] | None:
    cart = client.post("/order/cart", ovhSubsidiary=subsidiary)
    cart_id = cart["cartId"]
    try:
        offers = client.get(f"/order/cart/{cart_id}/domain", domain=domain)
        return select_valid_offer(offers, max_price=max_price)
    finally:
        try:
            client.delete(f"/order/cart/{cart_id}")
        except Exception:  # noqa: BLE001
            pass


def buy_exact_domain(
    client: Any,
    subsidiary: str,
    domain: str,
    max_price: float = DEFAULT_MAX_PRICE_HT,
) -> tuple[int, float]:
    cart = client.post("/order/cart", ovhSubsidiary=subsidiary)
    cart_id = cart["cartId"]
    checked_out = False
    try:
        client.post(f"/order/cart/{cart_id}/assign")
        offers = client.get(f"/order/cart/{cart_id}/domain", domain=domain)
        matched = select_valid_offer(offers, max_price=max_price)
        if not matched:
            raise RuntimeError(
                f"Le domaine '{domain}' est indisponible, non-standard ou dépasse le plafond de {max_price:.2f} € HT."
            )

        offer, price = matched
        client.post(
            f"/order/cart/{cart_id}/domain",
            domain=domain,
            duration="P1Y",
            planCode=offer.get("planCode", "fr"),
            pricingMode=offer["pricingMode"],
            quantity=1,
        )
        order = client.post(
            f"/order/cart/{cart_id}/checkout",
            autoPayWithPreferredPaymentMethod=True,
        )
        checked_out = True
        return int(order["orderId"]), price
    finally:
        if not checked_out:
            try:
                client.delete(f"/order/cart/{cart_id}")
            except Exception as e:  # noqa: BLE001
                print(f"Avertissement : suppression du panier {cart_id} impossible : {e}", file=sys.stderr)


def wait_for_dns_zone(client: Any, domain: str, timeout_sec: int = 180) -> bool:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        zones = client.get("/domain/zone")
        if domain in zones:
            return True
        time.sleep(10)
    return False


def configure_dns_zone(client: Any, domain: str, vps_ip: str) -> None:
    seen_ids: set[int] = set()
    for sub_query in ("", "www"):
        record_ids = client.get(f"/domain/zone/{domain}/record", subDomain=sub_query)
        for rec_id in record_ids:
            if rec_id in seen_ids:
                continue
            seen_ids.add(rec_id)

            rec = client.get(f"/domain/zone/{domain}/record/{rec_id}")
            if (rec.get("subDomain") or "") not in ("", "www"):
                continue

            rec_type = rec.get("fieldType")
            target = str(rec.get("target") or "")
            is_ovh_redirect_txt = rec_type == "TXT" and (
                target.startswith("1|") or target.startswith("l|")
            )
            if rec_type in ("A", "AAAA", "CNAME") or target in OVH_PARKING_IPS or is_ovh_redirect_txt:
                client.delete(f"/domain/zone/{domain}/record/{rec_id}")

    for sub in ("", "www"):
        client.post(
            f"/domain/zone/{domain}/record",
            fieldType="A",
            subDomain=sub,
            target=vps_ip,
            ttl=3600,
        )

    client.post(f"/domain/zone/{domain}/refresh")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Vérification, achat strict plafonné et DNS OVH pour un client WaaS")
    ap.add_argument("slug", help="Slug du client (ex: plomberie-chauffage-morel-fils)")
    ap.add_argument("domain", help="Nom de domaine .fr souhaité")
    ap.add_argument("--buy", action="store_true", help="Achète strictement le domaine exact passé en argument")
    ap.add_argument("--dns-only", action="store_true", help="Configure uniquement la zone DNS et Caddy (sans achat)")
    ap.add_argument("--max-price", type=float, default=DEFAULT_MAX_PRICE_HT, help=f"Plafond HT en € (défaut : {DEFAULT_MAX_PRICE_HT} €)")
    ap.add_argument("--vps-ip", default=os.getenv("VPS_PUBLIC_IP", ""), help="IP publique du VPS")
    ap.add_argument("--subsidiary", default=os.getenv("OVH_SUBSIDIARY", "FR"))
    args = ap.parse_args(argv)

    domain = ws.normalize_domain(args.domain)
    if not domain or not domain.endswith(".fr"):
        print(f"Erreur : '{args.domain}' n'est pas un domaine .fr valide.", file=sys.stderr)
        return 2

    site_dir = ws.SITES_DIR / args.slug
    if not (site_dir / ".activated").exists():
        print(f"Erreur : le client '{args.slug}' n'a pas de fichier .activated.", file=sys.stderr)
        return 1

    if (args.buy or args.dns_only) and not args.vps_ip:
        print("Erreur : VPS_PUBLIC_IP ou --vps-ip est requis pour --buy et --dns-only.", file=sys.stderr)
        return 2

    client = get_ovh_client()

    if not args.buy and not args.dns_only:
        for cand in generate_variants(domain):
            matched = check_domain_offer(client, args.subsidiary, cand, max_price=args.max_price)
            if matched:
                _, price = matched
                print(f"[DISPONIBLE] {cand} est libre au tarif standard ({price:.2f} € HT <= {args.max_price:.2f} € HT).")
                print(f"👉 Pour l'acheter : venv/bin/python ovh_domain.py {args.slug} {cand} --buy")
                return 0
            print(f"[INDISPONIBLE] {cand} est déjà pris, non-standard ou dépasse {args.max_price:.2f} € HT.")
        print("Aucune variante disponible automatiquement.", file=sys.stderr)
        return 1

    if args.buy:
        try:
            order_id, price = buy_exact_domain(client, args.subsidiary, domain, max_price=args.max_price)
        except Exception as e:  # noqa: BLE001
            print(f"[REFUS D'ACHAT] Impossible d'acheter '{domain}' : {e}", file=sys.stderr)
            print("Lancez d'abord le script sans --buy pour voir les variantes disponibles.", file=sys.stderr)
            return 1

        print(f"[COMMANDE VALIDÉE] Domaine '{domain}' acheté ({price:.2f} € HT, Order #{order_id}).")
        print(
            "ℹ️  Titularité : pensez à déclarer l'artisan comme titulaire du domaine "
            "dans l'espace client OVH une fois le domaine créé."
        )

        rc = set_domain.set_client_domain(args.slug, domain)
        if rc != 0:
            print("Avertissement : échec de set_client_domain après achat.", file=sys.stderr)

    print(f"Attente de la disponibilité de la zone DNS '{domain}' chez OVH...")
    if not wait_for_dns_zone(client, domain, timeout_sec=180):
        print(
            f"⚠️ La zone DNS '{domain}' n'est pas encore active chez OVH (délai AFNIC).\n"
            f"Le domaine est déjà enregistré dans .activated. Relancez simplement d'ici quelques minutes :\n"
            f"  venv/bin/python ovh_domain.py {args.slug} {domain} --dns-only",
            file=sys.stderr,
        )
        return 3

    configure_dns_zone(client, domain, args.vps_ip)
    if set_domain.set_client_domain(args.slug, domain) != 0:
        print(f"Erreur : la zone DNS de {domain} est configurée, mais l'activation locale Caddy a échoué.", file=sys.stderr)
        return 1

    # Post-DNS client domain → Cloudflare (idempotent; skip if no token)
    cloudflare_domain.try_move_client_domain_to_cloudflare(
        client, domain, vps_ip=args.vps_ip
    )

    print(f"[TERMINÉ] Zone DNS de {domain} pointée vers {args.vps_ip} et Caddy prêt pour le HTTPS.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
