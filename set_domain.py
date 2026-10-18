#!/usr/bin/env python3
"""
set_domain.py — Associe ou remplace le nom de domaine .fr d'un client actif.
Met à jour atomiquement domains.json, .activated et le lien symbolique Caddy.
"""
from __future__ import annotations

import json
import sys

import webhook_server as ws


def set_client_domain(slug: str, raw_domain: str) -> int:
    site_dir = ws.SITES_DIR / slug
    if not ws.SAFE_SLUG_RE.match(slug) or not site_dir.is_dir():
        print(f"Erreur : dossier client introuvable pour le slug '{slug}' dans {ws.SITES_DIR}", file=sys.stderr)
        return 1

    act_file = site_dir / ".activated"
    if not act_file.exists():
        print(f"Erreur : le site '{slug}' n'est pas encore activé (.activated absent).", file=sys.stderr)
        return 1

    domain = ws.normalize_domain(raw_domain)
    if not domain:
        print(f"Erreur : nom de domaine invalide '{raw_domain}'.", file=sys.stderr)
        return 1

    domains = ws.load_domains_map()

    owner = domains.get(domain)
    if owner and owner != slug:
        print(f"Erreur : le domaine '{domain}' est déjà attribué au client '{owner}'.", file=sys.stderr)
        return 1

    for old_dom, s in list(domains.items()):
        if s == slug and old_dom != domain:
            domains.pop(old_dom, None)
            old_link = ws.SITES_DIR / old_dom
            if old_link.is_symlink():
                old_link.unlink(missing_ok=True)
            print(f"[NETTOYAGE] Ancien domaine retiré : {old_dom}")

    domains[domain] = slug
    ws.save_domains_map(domains)

    symlink_path = ws.SITES_DIR / domain
    if not symlink_path.exists() and not symlink_path.is_symlink():
        symlink_path.symlink_to(site_dir.name)

    act_data = json.loads(act_file.read_text(encoding="utf-8"))
    act_data["desired_domain"] = domain
    ws.atomic_write_text(act_file, json.dumps(act_data, ensure_ascii=False, indent=2))

    print(f"[OK] {domain} (et www.{domain}) -> {slug} configuré et autorisé pour Caddy SSL.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python set_domain.py <slug> <domaine.fr>", file=sys.stderr)
        sys.exit(2)
    sys.exit(set_client_domain(sys.argv[1], sys.argv[2]))
