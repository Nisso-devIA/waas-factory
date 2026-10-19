#!/usr/bin/env python3
"""
pricing.py — Moteur de devis FAISANT AUTORITÉ (côté serveur).

Règle d'or : le prix d'un devis n'est JAMAIS transmis par le navigateur.
Le front (devis.html) sert à l'affichage ; ici on recalcule à partir des seuls
choix du client (type, pages, options, palier) et on valide tout. Les montants
ci-dessous sont la source de vérité serveur — ils doivent rester alignés avec
le bloc CONFIG du configurateur front.

Usage côté webhook_server.py :

    from pricing import compute_quote
    quote = compute_quote(selection)        # selection = dict venant du POST du devis
    # -> quote["tier"]["stripe_env"]  : var d'env qui contient le price_id Stripe du palier
    # -> quote["creation_cents"]      : frais de création en centimes (line item one-shot)
    # -> quote["deposit_cents"]       : acompte 40% en centimes (si on facture l'acompte)

Tous les montants de la grille sont en EUROS entiers. Modifier ICI = changer les prix.
"""
from __future__ import annotations

import math
from typing import Any

# --------------------------------------------------------------------------- #
# GRILLE TARIFAIRE (source de vérité) — à ajuster librement
# --------------------------------------------------------------------------- #
PAGE_EXTRA_EUR = 90          # € par page au-delà de celles incluses
DEPOSIT_PCT = 0.40           # acompte à la commande
ANNUAL_FREE_MONTHS = 2       # mois offerts si paiement annuel
CURRENCY = "eur"

# Frais de création (one-shot). min_tier = palier d'abonnement minimum conseillé.
TYPES: dict[str, dict[str, Any]] = {
    "landing":  {"name": "Mini-site / Landing", "price": 490,  "pages_incl": 1,  "score": 1, "min_tier": 1},
    "vitrine":  {"name": "Site vitrine",        "price": 890,  "pages_incl": 5,  "score": 2, "min_tier": 2},
    "vitrinep": {"name": "Site vitrine +",      "price": 1490, "pages_incl": 10, "score": 3, "min_tier": 2},
    "ecom":     {"name": "Boutique en ligne",   "price": 2490, "pages_incl": 8,  "score": 5, "min_tier": 2},
}

# Options. qty="pages" => prix × nb de pages ; qty="lang" => prix × nb de langues.
OPTIONS: dict[str, dict[str, Any]] = {
    "rdv":     {"name": "Prise de rendez-vous en ligne",       "price": 250, "score": 2, "min_tier": 2},
    "blog":    {"name": "Blog / actualités",                   "price": 200, "score": 2, "min_tier": 2},
    "galerie": {"name": "Galerie / portfolio réalisations",    "price": 150, "score": 1, "min_tier": 1},
    "form":    {"name": "Formulaire avancé (devis, upload)",   "price": 120, "score": 1, "min_tier": 1},
    "avis":    {"name": "Avis Google intégrés",                "price": 90,  "score": 1, "min_tier": 2},
    "multi":   {"name": "Multilingue",                         "price": 300, "score": 2, "min_tier": 2, "qty": "lang"},
    "espace":  {"name": "Espace client / connexion",           "price": 400, "score": 3, "min_tier": 3},
    "redac":   {"name": "Rédaction des contenus",              "price": 60,  "score": 1, "min_tier": 1, "qty": "pages"},
    "logo":    {"name": "Logo & identité visuelle",            "price": 350, "score": 1, "min_tier": 1},
    "photos":  {"name": "Visuels / photos premium",            "price": 120, "score": 1, "min_tier": 1},
}

# Paliers d'abonnement mensuel. stripe_env = nom de la var d'env qui porte le price_id Stripe.
# NB : "visibilite" (49€) correspond à l'offre actuelle -> réutiliser le STRIPE_PRICE_ID existant.
TIERS: list[dict[str, Any]] = [
    {"id": "essentiel",  "name": "Essentiel",  "price": 29, "stripe_env": "STRIPE_PRICE_ESSENTIEL"},
    {"id": "visibilite", "name": "Visibilité", "price": 49, "stripe_env": "STRIPE_PRICE_VISIBILITE"},
    {"id": "croissance", "name": "Croissance", "price": 99, "stripe_env": "STRIPE_PRICE_CROISSANCE"},
]

# Niveau de complexité à partir du score cumulé (seuils croissants).
LEVELS: list[tuple[int, str, str]] = [
    (3,  "Simple",   "4 à 7 jours"),
    (7,  "Standard", "1 à 2 semaines"),
    (12, "Avancé",   "2 à 4 semaines"),
    (10**9, "Premium", "4 à 6 semaines"),
]

PAGES_MIN, PAGES_MAX = 1, 40
LANGS_MIN, LANGS_MAX = 2, 5


# --------------------------------------------------------------------------- #
# Validation / normalisation des entrées (ne jamais faire confiance au client)
# --------------------------------------------------------------------------- #
class QuoteError(ValueError):
    """Données de devis invalides."""


def _clamp_int(value: Any, lo: int, hi: int, default: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _tier_by_id(tier_id: Any) -> dict[str, Any] | None:
    return next((t for t in TIERS if t["id"] == tier_id), None)


def sanitize_selection(raw: Any) -> dict[str, Any]:
    """Transforme un payload client arbitraire en sélection propre et bornée."""
    if not isinstance(raw, dict):
        raise QuoteError("Sélection invalide.")

    type_id = raw.get("type") or raw.get("type_id")
    if type_id not in TYPES:
        raise QuoteError(f"Type de projet inconnu : {type_id!r}")

    pages_incl = TYPES[type_id]["pages_incl"]
    pages = _clamp_int(raw.get("pages"), PAGES_MIN, PAGES_MAX, pages_incl)
    langs = _clamp_int(raw.get("langs"), LANGS_MIN, LANGS_MAX, 2)

    raw_opts = raw.get("options") or {}
    if not isinstance(raw_opts, dict):
        raw_opts = {}
    options = {oid: bool(raw_opts.get(oid)) for oid in OPTIONS if raw_opts.get(oid)}

    tier_id = raw.get("tier") or raw.get("tier_id")
    if tier_id is not None and _tier_by_id(tier_id) is None:
        tier_id = None  # ignore un palier inconnu -> on prendra le recommandé

    billing = "annuel" if str(raw.get("billing", "mensuel")).lower() == "annuel" else "mensuel"

    return {
        "type": type_id,
        "pages": pages,
        "langs": langs,
        "options": options,
        "tier": tier_id,
        "billing": billing,
    }


# --------------------------------------------------------------------------- #
# Calcul du devis
# --------------------------------------------------------------------------- #
def _level_for(score: int) -> tuple[str, str]:
    for threshold, label, delai in LEVELS:
        if score <= threshold:
            return label, delai
    return LEVELS[-1][1], LEVELS[-1][2]


def _monthly_price(tier: dict[str, Any], billing: str) -> int:
    if billing == "annuel":
        return round(tier["price"] * (12 - ANNUAL_FREE_MONTHS) / 12)
    return tier["price"]


def compute_quote(raw_selection: Any) -> dict[str, Any]:
    """
    Calcule un devis complet à partir d'une sélection (validée en interne).
    Retourne un dict sérialisable JSON, avec les montants en € et en centimes.
    """
    sel = sanitize_selection(raw_selection)
    t = TYPES[sel["type"]]
    pages, langs = sel["pages"], sel["langs"]

    lines: list[dict[str, Any]] = [{"label": t["name"], "detail": "formule de base", "amount": t["price"]}]
    creation = t["price"]
    score = t["score"]
    min_tier = t["min_tier"]

    extra = max(0, pages - t["pages_incl"])
    if extra:
        cost = extra * PAGE_EXTRA_EUR
        lines.append({
            "label": f"{extra} page{'s' if extra > 1 else ''} supplémentaire{'s' if extra > 1 else ''}",
            "detail": f"{PAGE_EXTRA_EUR}€ / page",
            "amount": cost,
        })
        creation += cost
        score += math.ceil(extra / 3)

    for oid, meta in OPTIONS.items():
        if not sel["options"].get(oid):
            continue
        qty = meta.get("qty")
        if qty == "pages":
            cost, label, detail = meta["price"] * pages, meta["name"], f"{meta['price']}€ × {pages} pages"
        elif qty == "lang":
            cost = meta["price"] * langs
            label, detail = f"Multilingue · {langs} langue{'s' if langs > 1 else ''}", f"{meta['price']}€ / langue"
        else:
            cost, label, detail = meta["price"], meta["name"], ""
        lines.append({"label": label, "detail": detail, "amount": cost})
        creation += cost
        score += meta["score"]
        min_tier = max(min_tier, meta["min_tier"])

    level_label, delai = _level_for(score)

    recommended = TIERS[min(min_tier, len(TIERS)) - 1]
    chosen = _tier_by_id(sel["tier"]) or recommended
    monthly = _monthly_price(chosen, sel["billing"])

    creation_cents = int(round(creation * 100))
    deposit_eur = round(creation * DEPOSIT_PCT)
    deposit_cents = int(round(creation * DEPOSIT_PCT * 100))

    return {
        "selection": sel,
        "lines": lines,
        "creation_eur": creation,
        "creation_cents": creation_cents,
        "deposit_eur": deposit_eur,
        "deposit_cents": deposit_cents,
        "score": score,
        "level": level_label,
        "delai": delai,
        "recommended_tier_id": recommended["id"],
        "tier": {
            "id": chosen["id"],
            "name": chosen["name"],
            "price_eur": chosen["price"],
            "monthly_eur": monthly,
            "billing": sel["billing"],
            "stripe_env": chosen["stripe_env"],
        },
    }


# --------------------------------------------------------------------------- #
# Auto-test rapide : python3 pricing.py
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import json

    sample = {"type": "vitrine", "pages": 5, "options": {"rdv": True, "avis": True}, "billing": "mensuel"}
    q = compute_quote(sample)
    print(json.dumps(q, ensure_ascii=False, indent=2))

    # Vérifs : 890 (vitrine) + 250 (rdv) + 90 (avis) = 1230€ ; score 2+2+1=5 -> Standard ;
    # min_tier 2 -> Visibilité (49€) ; acompte 40% = 492€.
    assert q["creation_eur"] == 1230, q["creation_eur"]
    assert q["creation_cents"] == 123000
    assert q["deposit_eur"] == 492
    assert q["level"] == "Standard", q["level"]
    assert q["tier"]["id"] == "visibilite", q["tier"]
    assert q["recommended_tier_id"] == "visibilite"

    # Un client qui tente d'injecter un prix est ignoré : seuls ses CHOIX comptent.
    hostile = {"type": "vitrine", "pages": 5, "creation_cents": 1, "price": 0, "tier": "pirate"}
    hq = compute_quote(hostile)
    assert hq["creation_eur"] == 890, hq["creation_eur"]          # recalculé, pas le "1" injecté
    assert hq["tier"]["id"] == "visibilite"                        # palier pirate ignoré -> recommandé

    print("\n✅ pricing.py — auto-tests OK")
