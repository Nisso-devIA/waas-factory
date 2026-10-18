#!/usr/bin/env python3
"""
Banques photo démos — familles de métiers, jamais de cross-famille.

Règles:
- Si un métier a une banque dédiée → l’utiliser.
- Sinon → pool de sa famille (alimentation, bâtiment, beauté, sport, services).
- Sinon → liste vide (visuel neutre CSS, aucune photo) — JAMAIS une autre famille.
- `local-*.jpg` = pool bâtiment uniquement (chantier / plans) — pas un fallback global.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Iterable

# --------------------------------------------------------------------------- #
# Familles
# --------------------------------------------------------------------------- #

FAMILY_ALIMENTATION = "alimentation"  # métiers de bouche
FAMILY_BATIMENT = "batiment"
FAMILY_BEAUTE = "beaute"
FAMILY_SPORT = "sport"
FAMILY_SERVICES = "services"

FAMILIES = (
    FAMILY_ALIMENTATION,
    FAMILY_BATIMENT,
    FAMILY_BEAUTE,
    FAMILY_SPORT,
    FAMILY_SERVICES,
)

# Métier canonique → famille
TRADE_FAMILY: dict[str, str] = {
    # Bouche
    "pâtisserie": FAMILY_ALIMENTATION,
    "patisserie": FAMILY_ALIMENTATION,
    "pâtissier": FAMILY_ALIMENTATION,
    "patissier": FAMILY_ALIMENTATION,
    "boulangerie": FAMILY_ALIMENTATION,
    "boulanger": FAMILY_ALIMENTATION,
    "traiteur": FAMILY_ALIMENTATION,
    "boucherie": FAMILY_ALIMENTATION,
    "boucher": FAMILY_ALIMENTATION,
    "restaurant": FAMILY_ALIMENTATION,
    "restaurateur": FAMILY_ALIMENTATION,
    "cuisine": FAMILY_ALIMENTATION,
    "chef": FAMILY_ALIMENTATION,
    # Bâtiment / habitat
    "plombier": FAMILY_BATIMENT,
    "plombier chauffagiste": FAMILY_BATIMENT,
    "chauffagiste": FAMILY_BATIMENT,
    "électricien": FAMILY_BATIMENT,
    "electricien": FAMILY_BATIMENT,
    "couvreur": FAMILY_BATIMENT,
    "serrurier": FAMILY_BATIMENT,
    "menuisier": FAMILY_BATIMENT,
    "peintre": FAMILY_BATIMENT,
    "paysagiste": FAMILY_BATIMENT,
    "garagiste": FAMILY_BATIMENT,
    "carreleur": FAMILY_BATIMENT,
    "maçon": FAMILY_BATIMENT,
    "macon": FAMILY_BATIMENT,
    "architecte": FAMILY_BATIMENT,
    # Beauté
    "coiffeur": FAMILY_BEAUTE,
    "coiffeuse": FAMILY_BEAUTE,
    "esthéticienne": FAMILY_BEAUTE,
    "estheticienne": FAMILY_BEAUTE,
    "barbier": FAMILY_BEAUTE,
    # Sport
    "coach sportif": FAMILY_SPORT,
    "coach": FAMILY_SPORT,
    "personal trainer": FAMILY_SPORT,
    # Services
    "photographe": FAMILY_SERVICES,
}

# Alias labels → métier canonique (banque dédiée)
TRADE_ALIASES: dict[str, str] = {
    "coach": "coach sportif",
    "personal trainer": "coach sportif",
    "plombier chauffagiste": "plombier",
    "chauffagiste": "plombier",
    "electricien": "électricien",
    "patisserie": "pâtisserie",
    "patissier": "pâtisserie",
    "pâtissier": "pâtisserie",
    "boulanger": "boulangerie",
    "boucher": "boucherie",
    "restaurateur": "restaurant",
    "coiffeuse": "coiffeur",
    "estheticienne": "esthéticienne",
}

# Banques dédiées (préfixe fichier = métier / famille). Never mix across families.
TRADE_IMAGES: dict[str, list[str]] = {
    "coach sportif": [
        "coach-01.jpg",
        "coach-02.jpg",
        "coach-03.jpg",
        "coach-04.jpg",
        "coach-05.jpg",
        "coach-06.jpg",
        "coach-07.jpg",
        "coach-08.jpg",
    ],
    # Plomberie only — pas de nettoyage/volets, chantier générique, archi, électro
    "plombier": [
        "plombier-01.jpg",  # tuyauterie / gaz
        "plombier-02.jpg",  # robinetterie baignoire
        "plombier-04.jpg",  # salle de bain moderne
        "plombier-09.jpg",  # évier / jet d'eau
        "plombier-10.jpg",  # soudure / atelier
        "plombier-11.jpg",  # sdb contemporaine
        "plombier-12.jpg",  # robinet baignoire chrome
    ],
    # Residential electrician — 01≈02 same worker; no smart-lock / DIY
    "électricien": [
        "electricien-01.jpg",
        "electricien-06.jpg",  # pendant light
    ],
    "coiffeur": [
        "coiffeur-01.jpg",
        "coiffeur-02.jpg",
        "coiffeur-03.jpg",
        "coiffeur-04.jpg",
        "coiffeur-05.jpg",
        "coiffeur-06.jpg",
    ],
    # Garden / végétal only — never architecture-dominant (houses, interiors).
    # Building-heavy files (08,11,16,17…) stay on disk for old pages but are excluded here.
    "paysagiste": [
        "paysagiste-07.jpg",  # allée fleurie
        "paysagiste-09.jpg",  # potager / carré potager
        "paysagiste-10.jpg",  # jardin créatif
        "paysagiste-02.jpg",  # outils / plantation (trowel family)
        # NOT 45: byte-identical to 03, near-dup of 02 — same trowel desk
        "paysagiste-28.jpg",  # plants en godets
        "paysagiste-36.jpg",  # terre / mains
        "paysagiste-40.jpg",  # feuillage
        "paysagiste-43.jpg",  # potager surélevé
        "paysagiste-47.jpg",  # massif feuillu
    ],
    "peintre": [
        "peintre-01.jpg",
        "peintre-02.jpg",
        "peintre-03.jpg",
        "peintre-04.jpg",
        "peintre-05.jpg",
        "peintre-06.jpg",
    ],
    "menuisier": [
        "menuisier-01.jpg",
        "menuisier-02.jpg",
        "menuisier-03.jpg",
        "menuisier-04.jpg",
        "menuisier-05.jpg",
        "menuisier-06.jpg",
    ],
    "photographe": [
        "photographe-01.jpg",
        "photographe-02.jpg",
        "photographe-03.jpg",
        "photographe-04.jpg",
        "photographe-05.jpg",
        "photographe-06.jpg",
    ],
    # Métiers de bouche — sous-banques distinctes (pas de pain pour pâtisserie)
    # Subjects tagged in IMAGE_SUBJECTS — never mismatch caption↔photo
    # Banque élargie (≥12) pour que deux slugs tirent des séquences distinctes
    "pâtisserie": [
        "patisserie-01.jpg",  # viennoiserie — croissants
        "patisserie-tarte.jpg",  # tarte aux fruits
        "patisserie-02.jpg",  # gâteau festif
        "patisserie-03.jpg",  # gâteau chocolat
        "patisserie-04.jpg",  # cupcake
        # patisserie-05 = glace/wafer caramel — exclu (pas pâtisserie)
        "patisserie-06.jpg",  # cupcakes fraise
        "patisserie-07.jpg",  # macarons
        "patisserie-08.jpg",  # verrines fraise
        "patisserie-09.jpg",  # brownies chocolat
        "patisserie-10.jpg",  # gâteau d'anniversaire
        "patisserie-11.jpg",  # crêpes crème fraises
        "patisserie-12.jpg",  # tarte citron meringuée
        "patisserie-13.jpg",  # viennoiserie spirale sucre perlé
        "patisserie-14.jpg",  # cupcakes cookies
    ],
    "boulangerie": [
        "bouche-02.jpg",  # pain tranché
        "bouche-06.jpg",  # fournil / baguettes
        "bouche-01.jpg",  # viennoiserie (overlap OK même famille)
        "patisserie-01.jpg",
        "bouche-08.jpg",
        "bouche-05.jpg",
    ],
    "traiteur": [
        "bouche-07.jpg",
        "bouche-08.jpg",
        "bouche-01.jpg",
        "patisserie-03.jpg",
        "bouche-05.jpg",
        "patisserie-02.jpg",
    ],
    "boucherie": [
        "bouche-08.jpg",
        "bouche-07.jpg",
        "bouche-06.jpg",
        "bouche-02.jpg",
        "bouche-01.jpg",
        "bouche-05.jpg",
    ],
    "restaurant": [
        "bouche-07.jpg",
        "bouche-08.jpg",
        "bouche-05.jpg",
        "patisserie-05.jpg",
        "bouche-01.jpg",
        "patisserie-03.jpg",
    ],
}

# Pools famille (partagés, jamais cross-famille)
FAMILY_IMAGES: dict[str, list[str]] = {
    FAMILY_ALIMENTATION: [
        "bouche-01.jpg",
        "bouche-02.jpg",
        "bouche-03.jpg",
        "bouche-04.jpg",
        "bouche-05.jpg",
        "bouche-06.jpg",
        "bouche-07.jpg",
        "bouche-08.jpg",
        "patisserie-01.jpg",
        "patisserie-02.jpg",
        "patisserie-03.jpg",
        "patisserie-04.jpg",
        # patisserie-05 exclu (glace/wafer — pas pâtisserie)
        "patisserie-06.jpg",
        "patisserie-07.jpg",
        "patisserie-08.jpg",
        "patisserie-09.jpg",
        "patisserie-10.jpg",
        "patisserie-11.jpg",
        "patisserie-12.jpg",
        "patisserie-13.jpg",
        "patisserie-14.jpg",
        "patisserie-tarte.jpg",
    ],
    # Ancien « default » local-* = chantier / plans — bâtiment uniquement
    FAMILY_BATIMENT: [
        "local-01.jpg",
        "local-02.jpg",
        "local-03.jpg",
        "local-04.jpg",
        "local-05.jpg",
        "local-06.jpg",
    ],
    FAMILY_BEAUTE: [
        "coiffeur-01.jpg",
        "coiffeur-02.jpg",
        "coiffeur-03.jpg",
        "coiffeur-04.jpg",
        "coiffeur-05.jpg",
        "coiffeur-06.jpg",
    ],
    FAMILY_SPORT: [
        "coach-01.jpg",
        "coach-02.jpg",
        "coach-03.jpg",
        "coach-04.jpg",
        "coach-05.jpg",
        "coach-06.jpg",
        "coach-07.jpg",
        "coach-08.jpg",
    ],
    FAMILY_SERVICES: [
        "photographe-01.jpg",
        "photographe-02.jpg",
        "photographe-03.jpg",
        "photographe-04.jpg",
        "photographe-05.jpg",
        "photographe-06.jpg",
    ],
}

# Subject tags for caption↔photo matching (scratch prompts + QA)
IMAGE_SUBJECTS: dict[str, str] = {
    "patisserie-01.jpg": "viennoiserie — croissants",
    "patisserie-tarte.jpg": "tarte — tarte aux fruits",
    "patisserie-02.jpg": "gâteau — gâteau festif",
    "patisserie-03.jpg": "gâteau — gâteau chocolat",
    "patisserie-04.jpg": "gâteau — cupcake",
    "patisserie-05.jpg": "dessert — dessert plaqué",
    "patisserie-06.jpg": "gâteau — cupcakes fraise",
    "patisserie-07.jpg": "macaron — macarons colorés",
    "patisserie-08.jpg": "verrine — panna cotta fraise",
    "patisserie-09.jpg": "chocolat — brownies",
    "patisserie-10.jpg": "gâteau — gâteau d'anniversaire",
    "patisserie-11.jpg": "crêpe — crêpes crème fraises",
    "patisserie-12.jpg": "tarte — tarte citron meringuée",
    "patisserie-13.jpg": "viennoiserie — spirales sucre perlé",
    "patisserie-14.jpg": "gâteau — cupcakes cookies",
    "plombier-01.jpg": "tuyauterie — installation gaz / conduits",
    "plombier-02.jpg": "robinetterie — baignoire chrome",
    "plombier-04.jpg": "salle de bain — installation contemporaine",
    "plombier-09.jpg": "évier — robinet cuisine jet d'eau",
    "plombier-10.jpg": "soudure — atelier métaux / chauffagiste",
    "plombier-11.jpg": "salle de bain — baignoire et vasque",
    "plombier-12.jpg": "robinetterie — mitigeur baignoire",
    # Paysagiste stock = ambiance jardin / illustration only (no architecture)
    "paysagiste-07.jpg": "exemple — allée fleurie",
    "paysagiste-09.jpg": "exemple — potager",
    "paysagiste-10.jpg": "exemple — jardin créatif",
    "paysagiste-02.jpg": "exemple — outils de plantation",
    "paysagiste-28.jpg": "exemple — plants en godets",
    "paysagiste-36.jpg": "exemple — travail de la terre",
    "paysagiste-40.jpg": "exemple — feuillage",
    "paysagiste-43.jpg": "exemple — carré potager",
    "paysagiste-47.jpg": "exemple — massif végétal",
}


def subject_of(filename: str) -> str:
    name = (filename or "").split("?")[0].lstrip("/").split("/")[-1]
    return IMAGE_SUBJECTS.get(name) or ""


# Fichier → propriétaire (métier dédié ou famille) pour tests d’isolation
IMAGE_OWNER: dict[str, str] = {}
for _trade, _files in TRADE_IMAGES.items():
    for _f in _files:
        # Prefixed métier files owned by that trade; bouche-* owned by family
        if _f.startswith("bouche-"):
            IMAGE_OWNER.setdefault(_f, FAMILY_ALIMENTATION)
        elif _f.startswith("local-"):
            IMAGE_OWNER.setdefault(_f, FAMILY_BATIMENT)
        elif _f.startswith("electricien-"):
            IMAGE_OWNER.setdefault(_f, "électricien")
        else:
            prefix = _f.split("-", 1)[0]
            IMAGE_OWNER.setdefault(_f, _trade if prefix in (_trade.split()[0],) else _trade)

for _fam, _files in FAMILY_IMAGES.items():
    for _f in _files:
        IMAGE_OWNER.setdefault(_f, _fam)

# Refine ownership for dedicated-prefix files
for _f, _t in list(IMAGE_OWNER.items()):
    if _f.startswith("coach-"):
        IMAGE_OWNER[_f] = "coach sportif"
    elif _f.startswith("plombier-"):
        IMAGE_OWNER[_f] = "plombier"
    elif _f.startswith("electricien-"):
        IMAGE_OWNER[_f] = "électricien"
    elif _f.startswith("coiffeur-"):
        IMAGE_OWNER[_f] = "coiffeur"
    elif _f.startswith("paysagiste-"):
        IMAGE_OWNER[_f] = "paysagiste"
    elif _f.startswith("peintre-"):
        IMAGE_OWNER[_f] = "peintre"
    elif _f.startswith("menuisier-"):
        IMAGE_OWNER[_f] = "menuisier"
    elif _f.startswith("photographe-"):
        IMAGE_OWNER[_f] = "photographe"
    elif _f.startswith("bouche-"):
        IMAGE_OWNER[_f] = FAMILY_ALIMENTATION
    elif _f.startswith("patisserie-"):
        IMAGE_OWNER[_f] = "pâtisserie"
    elif _f.startswith("local-"):
        IMAGE_OWNER[_f] = FAMILY_BATIMENT

# Explicit alias files
IMAGE_OWNER.setdefault("patisserie-tarte.jpg", "pâtisserie")


def _norm(s: str) -> str:
    s = (s or "").strip().casefold()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9\s-]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def canonicalize_trade(trade: str) -> str:
    """Map free-text métier → clé banque (ou chaîne normalisée)."""
    raw = (trade or "").strip()
    if not raw:
        return ""
    low = raw.casefold()
    if low in TRADE_IMAGES:
        return low
    if low in TRADE_ALIASES:
        return TRADE_ALIASES[low]
    n = _norm(raw)
    if n in TRADE_ALIASES:
        return TRADE_ALIASES[n]
    # accent-insensitive match on TRADE_IMAGES / aliases
    for key in list(TRADE_IMAGES) + list(TRADE_ALIASES):
        if _norm(key) == n:
            return TRADE_ALIASES.get(key, key)
    # substring heuristics (bouche first)
    bouche_keys = (
        ("patiss", "pâtisserie"),
        ("boulang", "boulangerie"),
        ("traite", "traiteur"),
        ("boucher", "boucherie"),
        ("restaur", "restaurant"),
        ("plomb", "plombier"),
        ("electri", "électricien"),
        ("peintre", "peintre"),
        ("couvre", "couvreur"),
        ("serrur", "serrurier"),
        ("menuis", "menuisier"),
        ("paysag", "paysagiste"),
        ("garage", "garagiste"),
        ("coiff", "coiffeur"),
        ("esthet", "esthéticienne"),
        ("coach", "coach sportif"),
        ("photo", "photographe"),
    )
    for needle, canon in bouche_keys:
        if needle in n:
            return canon
    return low


def resolve_family(trade: str) -> str | None:
    key = canonicalize_trade(trade)
    if not key:
        return None
    if key in TRADE_FAMILY:
        return TRADE_FAMILY[key]
    n = _norm(key)
    for k, fam in TRADE_FAMILY.items():
        if _norm(k) == n or n in _norm(k) or _norm(k) in n:
            return fam
    return None


def owner_of(filename: str) -> str | None:
    name = (filename or "").split("?")[0].lstrip("/").split("/")[-1]
    return IMAGE_OWNER.get(name)


def family_of_owner(owner: str | None) -> str | None:
    if not owner:
        return None
    if owner in FAMILIES:
        return owner
    return TRADE_FAMILY.get(owner) or TRADE_FAMILY.get(canonicalize_trade(owner))


def resolve_trade_images(trade: str) -> list[str]:
    """
    Images autorisées pour ce métier.
    Retourne [] si aucune banque / famille → caller doit utiliser un visuel neutre.
    """
    key = canonicalize_trade(trade)
    if key in TRADE_IMAGES:
        return list(TRADE_IMAGES[key])
    fam = resolve_family(trade)
    if fam and fam in FAMILY_IMAGES:
        return list(FAMILY_IMAGES[fam])
    return []


def _images_dir() -> Path | None:
    from pathlib import Path

    for cand in (
        Path(__file__).resolve().parent / "static" / "demo-assets" / "images",
        Path("/opt/waas-factory/static/demo-assets/images"),
        Path("/var/www/sites/_vitrine/demo-assets/images"),
    ):
        if cand.is_dir():
            return cand
    return None


def file_content_hash(filename: str) -> str | None:
    """MD5 of image bytes — identity for anti-dupe (not bank entry IDs)."""
    import hashlib
    from pathlib import Path

    name = (filename or "").split("?")[0].lstrip("/").split("/")[-1]
    if not name:
        return None
    root = _images_dir()
    if not root:
        return None
    path = root / name
    if not path.is_file():
        return None
    return hashlib.md5(path.read_bytes()).hexdigest()


# Same-trade demo peers that MUST receive disjoint photo partitions (all sections).
PHOTO_PEER_GROUPS: list[tuple[str, ...]] = [
    ("var3-sucre-osny", "var3-brioche-tls", "var3-madeleine-metz"),
    ("var2-sucre-osny", "var2-brioche-tls", "var2-madeleine-metz"),
    ("var3-fuite-cergy", "var3-tuyau-pontoise"),
    ("var2-fuite-cergy", "var2-tuyau-pontoise"),
]


def _peer_slot(slug: str, slots: int) -> int | None:
    """Stable unique slot among PHOTO_PEER_GROUPS, or None if slug not listed."""
    if slots <= 0:
        return None
    s = (slug or "").casefold()
    for group in PHOTO_PEER_GROUPS:
        keys = [g.casefold() for g in group]
        if s in keys:
            return keys.index(s) % slots
    return None


def pick_trade_images(trade: str, slug: str = "", *, limit: int = 6) -> list[str]:
    """
    Sous-ensemble métier-safe, **disjoint par slug** quand la banque le permet.

    Algorithme (v4):
    - déduplique par hash de contenu
    - découpe la banque en partitions non chevauchantes de taille `chunk`
    - peer groups (var2/var3 demos) get forced unique slots (hash collisions ignored)
    - otherwise slug hash picks a slot; order shuffled inside the set
    → two demos of the same trade in a peer group share **zero** photos (all sections).
    """
    import hashlib
    import random

    files = resolve_trade_images(trade)
    if not files:
        return []
    # Collapse byte-identical assets (different bank IDs, same bytes)
    unique: list[str] = []
    seen_hash: set[str] = set()
    for f in files:
        h = file_content_hash(f) or f"name:{f.casefold()}"
        if h in seen_hash:
            continue
        seen_hash.add(h)
        unique.append(f)
    if not unique:
        return []
    n = max(1, min(int(limit) if limit else len(unique), len(unique)))
    if not slug:
        return unique[:n]

    # Chunk sized so peer groups fit (≥3 pâtisserie, ≥2 plombier)
    key = canonicalize_trade(trade)
    preferred = {"pâtisserie": 4, "patisserie": 4, "plombier": 3}.get(key)
    chunk = preferred or n
    chunk = max(1, min(chunk, n, len(unique)))
    slots = max(1, len(unique) // chunk)
    peer = _peer_slot(slug, slots)
    if peer is not None:
        slot = peer
    else:
        digest = hashlib.sha256(f"{slug.casefold()}:photos-v4".encode()).hexdigest()
        slot = int(digest[:12], 16) % slots
    start = slot * chunk
    picked = list(unique[start : start + chunk])
    if len(picked) < chunk:
        picked = list(unique[-chunk:])
    # Trim to requested limit, shuffle order within the disjoint set
    picked = picked[:n]
    rng = random.Random(int(hashlib.sha256(f"{slug.casefold()}:photo-order-v4".encode()).hexdigest()[:16], 16))
    rng.shuffle(picked)
    return picked


def assert_same_trade_photos_disjoint(
    slug_trade_pairs: list[tuple[str, str]], *, limit: int = 5
) -> None:
    """Raise if any two same-trade demos share a photo filename (full-page sets)."""
    by_trade: dict[str, dict[str, set[str]]] = {}
    for slug, trade in slug_trade_pairs:
        key = canonicalize_trade(trade) or trade
        seq = set(pick_trade_images(trade, slug, limit=limit))
        bucket = by_trade.setdefault(key, {})
        for other, other_seq in bucket.items():
            overlap = seq & other_seq
            if overlap:
                raise AssertionError(
                    f"photo overlap {key}: {slug!r} ∩ {other!r} = {sorted(overlap)}"
                )
        bucket[slug] = seq


def assert_images_allowed_for_trade(trade: str, filenames: Iterable[str]) -> None:
    """Raise AssertionError if any file belongs to another métier/family."""
    allowed_fam = resolve_family(trade)
    allowed_key = canonicalize_trade(trade)
    allowed_set = set(resolve_trade_images(trade))
    for raw in filenames:
        if not raw:
            continue
        name = raw.split("?")[0].lstrip("/").split("/")[-1]
        if name not in allowed_set:
            own = owner_of(name)
            own_fam = family_of_owner(own)
            raise AssertionError(
                f"image hors métier/famille: {name!r} (owner={own!r}, fam={own_fam!r}) "
                f"pour trade={trade!r} (key={allowed_key!r}, fam={allowed_fam!r})"
            )
        own = owner_of(name)
        own_fam = family_of_owner(own)
        if allowed_fam and own_fam and own_fam != allowed_fam:
            raise AssertionError(
                f"cross-family: {name!r} fam={own_fam!r} vs trade fam={allowed_fam!r}"
            )


# Back-compat: site_plan / scratch historically fell through to TRADE_IMAGES["default"]
# Keep key absent on purpose — callers must use resolve_trade_images().
