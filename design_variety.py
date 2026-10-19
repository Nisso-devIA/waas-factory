#!/usr/bin/env python3
"""
Variété visuelle déterministe par slug — même métier, sites distincts.

Sélectionne palette + typo + layout vitrine seedés par le slug.
Garantit WCAG AA (texte ink sur paper, CTA ink sur accent clair ou paper sur accent).
"""
from __future__ import annotations

import hashlib
import random
import re
from dataclasses import dataclass
from typing import Any


def slug_rng(seed: str) -> random.Random:
    digest = hashlib.sha256((seed or "entreprise").encode("utf-8")).hexdigest()
    return random.Random(int(digest[:16], 16))


def _rel_luminance(hex_color: str) -> float:
    h = (hex_color or "").lstrip("#")
    if len(h) != 6:
        return 0.0
    rgb = [int(h[i : i + 2], 16) / 255.0 for i in (0, 2, 4)]

    def _lin(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (_lin(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a: str, b: str) -> float:
    la, lb = _rel_luminance(a), _rel_luminance(b)
    lighter, darker = max(la, lb), min(la, lb)
    return (lighter + 0.05) / (darker + 0.05)


# WCAG AA thresholds used by the post-render guardrail
BODY_MIN_RATIO = 4.5
LARGE_MIN_RATIO = 3.0


def pick_text_on_bg(
    bg: str,
    *,
    min_ratio: float = BODY_MIN_RATIO,
    light: str = "#FAFAFA",
    dark: str = "#121212",
) -> str:
    """Pick light or dark text so contrast vs *bg* meets *min_ratio* (prefer better)."""
    cl, cd = contrast_ratio(light, bg), contrast_ratio(dark, bg)
    if cl >= min_ratio and cl >= cd:
        return light
    if cd >= min_ratio:
        return dark
    return light if cl >= cd else dark


def assert_wcag_aa(palette: dict[str, str], *, min_ratio: float = 4.5) -> None:
    ink, paper, accent = palette["ink"], palette["paper"], palette["accent"]
    if contrast_ratio(ink, paper) < min_ratio:
        raise AssertionError(f"ink/paper contrast {contrast_ratio(ink, paper):.2f} < {min_ratio}")
    # Accent used as solid button → text must be paper or ink, pick best
    best = max(contrast_ratio(paper, accent), contrast_ratio(ink, accent))
    if best < LARGE_MIN_RATIO:
        raise AssertionError(f"accent CTA contrast {best:.2f} too low")
    hero = palette.get("hero") or ink
    hero_fg = pick_text_on_bg(hero, min_ratio=BODY_MIN_RATIO, light=paper, dark=ink)
    if contrast_ratio(hero_fg, hero) < BODY_MIN_RATIO:
        raise AssertionError(
            f"hero text/bg contrast {contrast_ratio(hero_fg, hero):.2f} < {BODY_MIN_RATIO}"
        )


# --------------------------------------------------------------------------- #
# Palettes — plusieurs par métier, contrastées AA
# --------------------------------------------------------------------------- #

PALETTES: dict[str, dict[str, str]] = {
    # Warm craft (legacy-ish) — kept as one option, not the only one
    "cocoa-cream": {
        "accent": "#8B6914",
        "accent_soft": "#F3EDE4",
        "ink": "#1A1510",
        "paper": "#FAF7F2",
        "hero": "#3C2419",
    },
    "wine-cream": {
        "accent": "#7A4E3A",
        "accent_soft": "#F6EEE8",
        "ink": "#1A1210",
        "paper": "#FBF8F5",
        "hero": "#2C1810",
    },
    # Cool / modern alternatives for bouche
    "berry-mist": {
        "accent": "#7B2D4B",
        "accent_soft": "#F7E8EF",
        "ink": "#1A0F14",
        "paper": "#FBF7F9",
        "hero": "#3A1528",
    },
    "sage-porcelain": {
        "accent": "#3D6B5A",
        "accent_soft": "#E7F1EC",
        "ink": "#122018",
        "paper": "#F5F8F6",
        "hero": "#1A3328",
    },
    "ink-apricot": {
        "accent": "#C45C26",
        "accent_soft": "#F8EDE6",
        "ink": "#14110E",
        "paper": "#FFF9F5",
        "hero": "#1E1814",
    },
    "slate-rose": {
        "accent": "#A64D6D",
        "accent_soft": "#F6E8EE",
        "ink": "#161018",
        "paper": "#FAF6F8",
        "hero": "#2A1A24",
    },
    "navy-butter": {
        "accent": "#1B4F72",
        "accent_soft": "#E8F0F7",
        "ink": "#0D1B2A",
        "paper": "#F7F9FC",
        "hero": "#0D1B2A",
    },
    # Plombier / bâtiment
    "evergreen": {
        "accent": "#1F6F5B",
        "accent_soft": "#E4F2ED",
        "ink": "#0F1C1A",
        "paper": "#F7F5F1",
        "hero": "#0F2A22",
    },
    "navy-mist": {
        "accent": "#1B4F72",
        "accent_soft": "#E8F0F7",
        "ink": "#0D1B2A",
        "paper": "#F5F7FA",
        "hero": "#0A1620",
    },
    "graphite-amber": {
        "accent": "#C17A2B",
        "accent_soft": "#F5EBD9",
        "ink": "#15202B",
        "paper": "#F7F8FA",
        "hero": "#1A2330",
    },
    "teal-sand": {
        "accent": "#0E7C7B",
        "accent_soft": "#E3F3F3",
        "ink": "#0E1F1F",
        "paper": "#F4F8F7",
        "hero": "#0A2A2A",
    },
    "charcoal-lime": {
        "accent": "#2A9D8F",
        "accent_soft": "#E6F5F2",
        "ink": "#0F1A1C",
        "paper": "#F4F7F6",
        "hero": "#122428",
    },
    # Shared extras
    "ink-brass": {
        "accent": "#8B7355",
        "accent_soft": "#F3EDE4",
        "ink": "#1A1510",
        "paper": "#FAF7F2",
        "hero": "#2A2218",
    },
    "forest-sand": {
        "accent": "#2D6A4F",
        "accent_soft": "#E7F2EC",
        "ink": "#14261C",
        "paper": "#F5F8F5",
        "hero": "#143024",
    },
    "slate-coral": {
        "accent": "#C45C4A",
        "accent_soft": "#F8ECE9",
        "ink": "#1C1412",
        "paper": "#F9F6F4",
        "hero": "#2A1814",
    },
    # Extra bouche / craft options (variety headroom)
    "plum-fog": {
        "accent": "#5C3D6E",
        "accent_soft": "#F0E8F4",
        "ink": "#160F1A",
        "paper": "#F9F6FB",
        "hero": "#2A1833",
    },
    "copper-linen": {
        "accent": "#A05A2C",
        "accent_soft": "#F6EBE1",
        "ink": "#1A120C",
        "paper": "#FBF7F2",
        "hero": "#2C1C12",
    },
    "ocean-foam": {
        "accent": "#1A6B7A",
        "accent_soft": "#E4F2F5",
        "ink": "#0C1A1E",
        "paper": "#F5FAFB",
        "hero": "#0E2A32",
    },
    "ember-ash": {
        "accent": "#B33B2E",
        "accent_soft": "#F8E8E5",
        "ink": "#1A100E",
        "paper": "#FAF6F5",
        "hero": "#2A1410",
    },
}

# Validate all palettes at import
for _pid, _cols in PALETTES.items():
    assert_wcag_aa(_cols)

FONT_PAIRS: dict[str, dict[str, str]] = {
    "fraunces-plex": {
        "display": '"Fraunces", Georgia, serif',
        "body": '"IBM Plex Sans", system-ui, sans-serif',
        "hint": "titres serif Fraunces, corps Plex",
    },
    "plex-only": {
        "display": '"IBM Plex Sans", system-ui, sans-serif',
        "body": '"IBM Plex Sans", system-ui, sans-serif',
        "hint": "tout en Plex Sans, titres bold",
    },
    "georgia-plex": {
        "display": "Georgia, \"Times New Roman\", serif",
        "body": '"IBM Plex Sans", system-ui, sans-serif',
        "hint": "titres Georgia classiques, corps Plex",
    },
    "plex-narrow": {
        "display": '"IBM Plex Sans", system-ui, sans-serif',
        "body": "system-ui, sans-serif",
        "hint": "titres Plex uppercase tracking, corps system",
    },
}

# Layout variants for vitrine / services — avoid always-3-col incomplete rows
LAYOUT_VARIANTS: dict[str, dict[str, Any]] = {
    "mosaic-2": {
        "label": "mosaïque 2 colonnes",
        "photo_limit": 4,  # aligned with disjoint photo chunks (pâtisserie peer slots)
        "grid_css": "display:grid;grid-template-columns:1fr 1fr;gap:1.25rem",
        "brief": (
            "VITRINE = grille 2 colonnes (PAS 3). 4–5 cartes produit (dernière centrée si impair). "
            "Pas de ligne incomplète « flottante ». Cartes égales (img object-fit:cover)."
        ),
    },
    "strip-scroll": {
        "label": "bandeau horizontal",
        "photo_limit": 5,
        "grid_css": (
            "display:flex;gap:1rem;overflow-x:auto;scroll-snap-type:x mandatory;"
            "padding-bottom:.5rem"
        ),
        "brief": (
            "VITRINE = bandeau horizontal scrollable (flex, PAS grille 3 col). "
            "5 cartes min-width ~16rem, scroll-snap. Pas de grille cassée."
        ),
    },
    "list-alt": {
        "label": "liste alternée",
        "photo_limit": 5,
        "grid_css": "display:flex;flex-direction:column;gap:1.75rem",
        "brief": (
            "VITRINE = liste verticale alternée (photo|texte / texte|photo). "
            "4–5 entrées. INTERDIT grille 3 colonnes."
        ),
    },
    "hero-feature": {
        "label": "une grande + rangée",
        "photo_limit": 5,
        "grid_css": "display:grid;grid-template-columns:1fr 1fr;gap:1rem",
        "brief": (
            "VITRINE = 1 grande photo full-bleed puis rangée de vignettes OU "
            "grille 2×2 + 1. Jamais 5 cartes en 3 colonnes."
        ),
    },
    "services-list": {
        "label": "liste services urgence",
        "photo_limit": 3,
        "grid_css": "display:flex;flex-direction:column;gap:.75rem",
        "brief": (
            "SERVICES / urgences en LISTE verticale (pas cartes 3 col). "
            "Photos en bandeau ou 2 col max — chaque photo = tuyauterie/robinet/sdb. "
            "Pas de grille 3 incomplète. Hero DOIT contenir une photo métier."
        ),
    },
    "split-editorial": {
        "label": "éditorial split",
        "photo_limit": 3,
        "grid_css": "display:grid;grid-template-columns:1.2fr 1fr;gap:1.5rem",
        "brief": (
            "Layout éditorial: hero split texte|photo (photo plomberie OBLIGATOIRE dans le hero), "
            "puis section story + 2 photos côte à côte (pas 3-col cards)."
        ),
    },
}

TRADE_PALETTE_POOLS: dict[str, list[str]] = {
    "pâtisserie": [
        "berry-mist",
        "sage-porcelain",
        "ink-apricot",
        "slate-rose",
        "navy-butter",
        "plum-fog",
        "copper-linen",
        "ocean-foam",
        "ember-ash",
        "cocoa-cream",
        "wine-cream",
    ],
    "boulangerie": [
        "ink-brass",
        "cocoa-cream",
        "ink-apricot",
        "sage-porcelain",
        "copper-linen",
        "wine-cream",
    ],
    "plombier": [
        "evergreen",
        "navy-mist",
        "graphite-amber",
        "teal-sand",
        "charcoal-lime",
        "ocean-foam",
    ],
    "électricien": [
        "navy-mist",
        "graphite-amber",
        "charcoal-lime",
        "teal-sand",
        "ink-apricot",
        "copper-linen",
        "slate-coral",
        "ember-ash",
    ],
    "paysagiste": [
        "forest-sand",
        "evergreen",
        "sage-porcelain",
        "teal-sand",
        "ocean-foam",
        "ink-brass",
        "copper-linen",
    ],
    "coach sportif": [
        "charcoal-lime",
        "slate-coral",
        "navy-mist",
        "graphite-amber",
        "ember-ash",
        "teal-sand",
        "evergreen",
        "ink-apricot",
    ],
    "coiffeur": ["slate-rose", "wine-cream", "berry-mist", "ink-apricot", "plum-fog"],
}

TRADE_LAYOUT_POOLS: dict[str, list[str]] = {
    "pâtisserie": ["mosaic-2", "strip-scroll", "list-alt", "hero-feature"],
    "boulangerie": ["mosaic-2", "list-alt", "hero-feature"],
    "plombier": ["services-list", "split-editorial", "list-alt", "mosaic-2"],
    "électricien": ["services-list", "split-editorial", "list-alt"],
    "paysagiste": ["hero-feature", "mosaic-2", "list-alt", "strip-scroll"],
    "coach sportif": ["split-editorial", "list-alt", "mosaic-2"],
}

# B-28 — page/hero STRUCTURES (distinct from vitrine layout). ≥5 families.
# Two demos of the same trade must never share hero_structure_id.
HERO_STRUCTURES: dict[str, dict[str, Any]] = {
    "fullbleed-photo": {
        "label": "hero photo plein cadre",
        "template_hero": "minimal",
        "brief": (
            "HERO STRUCTURE = full-bleed photo background + veil sombre + texte clair "
            "superposé en bas (pas de photo en carte flottante à droite). "
            "NAV = barre tout en haut du hero (full width), JAMAIS flottante à côté de la photo."
        ),
    },
    "centered-editorial": {
        "label": "éditorial centré",
        "template_hero": "editorial",
        "brief": (
            "HERO STRUCTURE = éditorial centré: titre + lead centrés sur fond paper/clair, "
            "puis BANDEAU photo full-width sous le texte (pas split latéral). "
            "NAV = top bar horizontale au-dessus du hero."
        ),
    },
    "split-photo-left": {
        "label": "split photo à gauche",
        "template_hero": "split",
        "brief": (
            "HERO STRUCTURE = split INVERSÉ: PHOTO à GAUCHE (dominant), texte à DROITE. "
            "Pas de photo-carte flottante dans une barre de nav. "
            "NAV = top bar au-dessus du split, full width."
        ),
    },
    "magazine-asymmetric": {
        "label": "magazine asymétrique",
        "template_hero": "overlay",
        "brief": (
            "HERO STRUCTURE = magazine asymétrique: grosse photo 60% + colonne texte 40% "
            "avec décalage vertical (pas grille symétrique). "
            "NAV = top bar. INTERDIT nav verticale coincée à côté d'une image."
        ),
    },
    "type-led-band": {
        "label": "typo d'abord + bandeau photo",
        "template_hero": "editorial",
        "brief": (
            "HERO STRUCTURE = type-led: grand nom (typo forte) sur fond paper, CTA, "
            "puis BANDEAU photo métier sous le hero (hauteur ~40vh). "
            "NAV = top bar. Zéro placeholder gris / marble / panneau neutre vide."
        ),
    },
}

TRADE_HERO_POOLS: dict[str, list[str]] = {
    "pâtisserie": list(HERO_STRUCTURES.keys()),
    "boulangerie": list(HERO_STRUCTURES.keys()),
    "plombier": list(HERO_STRUCTURES.keys()),
    "électricien": list(HERO_STRUCTURES.keys()),
    "paysagiste": list(HERO_STRUCTURES.keys()),
    "coach sportif": list(HERO_STRUCTURES.keys()),
}

# Forced unique assignment for B-28 proof demos (same-trade never collide)
B28_HERO_FORCE: dict[str, str] = {
    "var3-sucre-osny": "fullbleed-photo",
    "var3-brioche-tls": "split-photo-left",
    # type-led (not magazine) — must look NOTHING like Brioche split (QA #24)
    "var3-madeleine-metz": "type-led-band",
    "var3-fuite-cergy": "type-led-band",
    "var3-tuyau-pontoise": "centered-editorial",
}

TRADE_FONT_POOLS: dict[str, list[str]] = {
    "pâtisserie": ["fraunces-plex", "georgia-plex", "plex-only", "plex-narrow"],
    "plombier": ["plex-only", "plex-narrow", "fraunces-plex"],
    "électricien": ["plex-only", "plex-narrow", "fraunces-plex", "georgia-plex"],
    "paysagiste": ["fraunces-plex", "georgia-plex", "plex-only", "plex-narrow"],
    "coach sportif": ["plex-only", "plex-narrow", "fraunces-plex", "georgia-plex"],
}

# Structure briefs per métier × variant index
STRUCTURE_VARIANTS: dict[str, list[str]] = {
    "pâtisserie": [
        (
            "STRUCTURE A (pâtisserie): 1) hero clair/pastel (nom+ville+tél) "
            "2) GALERIE PRODUITS immédiate — layout imposé ci-dessous "
            "3) commande / contact 4) zone 5) FAQ courte EN DERNIER. "
            "INTERDIT hero brun chocolat + grille 3 col de 5 cartes. "
            "INTERDIT FAQ avant la galerie."
        ),
        (
            "STRUCTURE B (pâtisserie): 1) hero éditorial split (photo produit dominante) "
            "2) liste/mosaïque produits selon layout 3) histoire courte atelier "
            "4) contact 5) FAQ. INTERDIT le clone dark-hero→3-col vitrine."
        ),
        (
            "STRUCTURE C (pâtisserie): 1) hero minimal papier clair + nom fort "
            "2) bandeau produits horizontal 3) commande téléphone mise en avant "
            "4) zone 5) FAQ. INTERDIT cartes services génériques 3×."
        ),
        (
            "STRUCTURE D (pâtisserie): 1) hero sombre UNIQUEMENT si palette l'impose "
            "2) une grande photo + 3 vignettes OU grille 2×2 "
            "3) contact 4) zone 5) FAQ. Jamais 5 cartes en 3 colonnes."
        ),
    ],
    "plombier": [
        (
            "STRUCTURE A (plombier): 1) hero urgence + grand CTA tél "
            "2) LISTE verticale d'urgences (fuite, chauffe-eau, débouchage) "
            "3) interventions 4) zone 5) FAQ 6) contact. Pas de 3 cartes égales."
        ),
        (
            "STRUCTURE B (plombier): 1) hero split photo|urgence "
            "2) 2 colonnes interventions 3) zone villes 4) FAQ 5) contact."
        ),
        (
            "STRUCTURE C (plombier): 1) bandeau tél sticky-feel dans hero "
            "2) services en accordéon/liste 3) photo chantier 4) zone 5) contact 6) FAQ."
        ),
        (
            "STRUCTURE D (plombier): 1) hero minimal ink 2) mosaïque 2 col photos "
            "3) liste dépannages 4) zone 5) FAQ 6) contact."
        ),
    ],
    "boulangerie": [
        (
            "STRUCTURE A (boulangerie): 1) hero chaleureux (nom+ville+tél) "
            "2) BANDEAU PRODUITS immédiat (pains, viennoiseries, spécialités) selon layout "
            "3) le fournil / histoire courte 4) horaires + accès 5) FAQ EN DERNIER. "
            "INTERDIT hero sombre + grille 3 col de 5 cartes."
        ),
        (
            "STRUCTURE B (boulangerie): 1) hero éditorial split (photo pain doré dominante) "
            "2) liste alternée spécialités (photo|texte) 3) savoir-faire / farines "
            "4) horaires 5) contact 6) FAQ. INTERDIT clone dark-hero→3-col."
        ),
        (
            "STRUCTURE C (boulangerie): 1) hero minimal papier clair, nom fort "
            "2) mosaïque 2 col produits 3) commande/horaires mis en avant "
            "4) zone 5) FAQ. INTERDIT cartes services génériques 3×."
        ),
    ],
    "électricien": [
        (
            "STRUCTURE A (électricien): 1) hero pro + grand CTA tél "
            "2) LISTE verticale de prestations (dépannage, mise aux normes, tableau, bornes) "
            "3) interventions 4) zone 5) FAQ 6) contact. Pas de 3 cartes égales."
        ),
        (
            "STRUCTURE B (électricien): 1) hero split photo|texte (installation/tableau) "
            "2) 2 colonnes prestations 3) rassurance (devis clair, délais) "
            "4) zone villes 5) FAQ 6) contact."
        ),
        (
            "STRUCTURE C (électricien): 1) hero type-led (nom fort) + bandeau rassurance "
            "2) services en liste 3) photo chantier réelle 4) zone 5) contact 6) FAQ."
        ),
        (
            "STRUCTURE D (électricien): 1) hero minimal ink 2) liste dépannages urgents "
            "3) garanties / certifications 4) zone 5) FAQ 6) contact."
        ),
    ],
    "paysagiste": [
        (
            "STRUCTURE A (paysagiste): 1) hero photo jardin plein cadre (nom+ville) "
            "2) BANDEAU RÉALISATIONS (création, entretien, élagage) selon layout "
            "3) approche / engagement 4) zone 5) contact 6) FAQ. Pas de 3 cartes égales."
        ),
        (
            "STRUCTURE B (paysagiste): 1) hero éditorial split (photo jardin) "
            "2) liste alternée prestations par saison 3) zone 4) contact 5) FAQ."
        ),
        (
            "STRUCTURE C (paysagiste): 1) hero clair végétal, nom fort "
            "2) mosaïque 2 col réalisations (avant/après si dispo) 3) entretien annuel "
            "4) zone 5) FAQ 6) contact."
        ),
    ],
    "coach sportif": [
        (
            "STRUCTURE A (coach sportif): 1) hero dynamique (nom+promesse) + CTA séance "
            "2) LISTE de programmes (perte de poids, prise de masse, remise en forme) "
            "3) méthode 4) en ligne/à domicile/zone 5) FAQ 6) contact. Pas de 3 cartes égales."
        ),
        (
            "STRUCTURE B (coach sportif): 1) hero split photo|texte énergique "
            "2) 2 colonnes offres 3) approche / accompagnement "
            "4) tarifs SI fournis sinon prestations 5) contact 6) FAQ."
        ),
        (
            "STRUCTURE C (coach sportif): 1) hero type-led slogan fort "
            "2) parcours en étapes (liste numérotée) 3) accompagnement "
            "4) zone 5) FAQ 6) contact."
        ),
    ],
    "coiffeur": [
        (
            "STRUCTURE A (coiffeur): 1) hero élégant (nom+ville) "
            "2) BANDEAU PRESTATIONS (coupe, couleur, soins) selon layout "
            "3) ambiance salon 4) horaires + prise de RDV 5) contact 6) FAQ. "
            "Pas de 3 cartes égales."
        ),
        (
            "STRUCTURE B (coiffeur): 1) hero éditorial split (photo coiffure) "
            "2) liste alternée prestations 3) équipe / approche 4) horaires "
            "5) contact 6) FAQ."
        ),
        (
            "STRUCTURE C (coiffeur): 1) hero minimal clair, nom fort "
            "2) mosaïque 2 col réalisations 3) tarifs SI fournis sinon prestations "
            "4) horaires 5) zone 6) FAQ."
        ),
    ],
}


@dataclass(frozen=True)
class DesignPreset:
    slug: str
    trade_key: str
    palette_id: str
    font_id: str
    layout_id: str
    structure_idx: int
    hero_structure_id: str
    accent: str
    accent_soft: str
    ink: str
    paper: str
    hero: str
    font_display: str
    font_body: str
    font_hint: str
    layout_label: str
    photo_limit: int
    layout_brief: str
    structure_brief: str
    hero_structure_brief: str

    def hero_text_color(self) -> str:
        """Readable text on hero bg — prefer pure white for max pop on dark heroes."""
        # Pure white beats near-paper cream for thin nav / muted labels over photos.
        if contrast_ratio("#FFFFFF", self.hero) >= BODY_MIN_RATIO:
            return "#FFFFFF"
        return pick_text_on_bg(
            self.hero, min_ratio=BODY_MIN_RATIO, light=self.paper, dark=self.ink
        )

    def on_accent_color(self) -> str:
        """CTA label color on solid accent — prefer ≥4.5:1."""
        return pick_text_on_bg(
            self.accent, min_ratio=BODY_MIN_RATIO, light=self.paper, dark=self.ink
        )

    def css_root(self) -> str:
        hero_fg = self.hero_text_color()
        on_accent = self.on_accent_color()
        return (
            ":root{"
            f"--av-ink:{self.ink};--av-paper:{self.paper};--av-accent:{self.accent};"
            f"--av-accent-soft:{self.accent_soft};--av-hero:{self.hero};"
            f"--av-hero-fg:{hero_fg};--av-on-accent:{on_accent};"
            f"--av-font-display:{self.font_display};--av-font-body:{self.font_body};"
            "}"
        )

    def prompt_block(self) -> str:
        hero_fg = self.hero_text_color()
        on_accent = self.on_accent_color()
        return (
            f"PRESET OBLIGATOIRE (ne pas diverger):\n"
            f"- palette_id={self.palette_id}\n"
            f"- CSS variables EXACTES: ink={self.ink} paper={self.paper} "
            f"accent={self.accent} accent_soft={self.accent_soft} hero={self.hero} "
            f"hero_fg={hero_fg} on_accent={on_accent}\n"
            f"- typo: {self.font_hint} (display={self.font_display}, body={self.font_body})\n"
            f"- HERO STRUCTURE id={self.hero_structure_id}: {self.hero_structure_brief}\n"
            f"- layout vitrine/services: {self.layout_label} — {self.layout_brief}\n"
            f"- structure page: {self.structure_brief}\n"
            f"- nb photos cible: {self.photo_limit} (pas de ligne de grille incomplète)\n"
            "NAV: barre horizontale EN HAUT (classe .av-topnav ou .hero-bar full-width). "
            "INTERDIT nav flottante à mi-hauteur du hero à côté d'une photo / bandeau vide. "
            "INTERDIT tuiles placeholder gris / marble / panneau neutre sans photo réelle. "
            "Utilise ces variables dans :root et pour body/hero/boutons. "
            f"Sur fond hero ({self.hero}) le texte DOIT être {hero_fg} "
            f"(contraste ≥{BODY_MIN_RATIO}:1 body, ≥{LARGE_MIN_RATIO}:1 titres). "
            "Sur fond paper: texte ink. Jamais ink sombre sur hero sombre."
        )


def _trade_key(trade: str) -> str:
    from photo_banks import canonicalize_trade
    from trade_profiles import match_profile_key

    return (
        match_profile_key(trade)
        or canonicalize_trade(trade)
        or (trade or "").strip().casefold()
        or "default"
    )


def _pool_index(slug: str, kind: str, size: int) -> int:
    """Stable index into a pool — different kinds use independent hash lanes (v3)."""
    if size <= 0:
        return 0
    digest = hashlib.sha256(f"{(slug or 'x').casefold()}:{kind}:v3".encode()).hexdigest()
    return int(digest[:12], 16) % size


def _pool_pick(slug: str, kind: str, pool: list[str]) -> str:
    return pool[_pool_index(slug, kind, len(pool))]


def pick_hero_structure_id(slug: str, trade: str) -> str:
    """Seeded hero structure; B28_HERO_FORCE wins for proof demos."""
    s = (slug or "").strip().casefold()
    forced = B28_HERO_FORCE.get(s)
    if forced and forced in HERO_STRUCTURES:
        return forced
    key = _trade_key(trade)
    pool = [x for x in (TRADE_HERO_POOLS.get(key) or list(HERO_STRUCTURES.keys())) if x in HERO_STRUCTURES]
    if not pool:
        pool = list(HERO_STRUCTURES.keys())
    return pool[_pool_index(slug, "hero-structure", len(pool))]


def assert_same_trade_hero_structures_unique(
    slug_trade_pairs: list[tuple[str, str]],
) -> None:
    """Raise if two demos of the same trade share a hero_structure_id."""
    by_trade: dict[str, dict[str, str]] = {}
    for slug, trade in slug_trade_pairs:
        key = _trade_key(trade)
        hid = pick_hero_structure_id(slug, trade)
        bucket = by_trade.setdefault(key, {})
        for other_slug, other_hid in bucket.items():
            if other_hid == hid:
                raise AssertionError(
                    f"same-trade hero structure collision: {key} "
                    f"{slug!r} and {other_slug!r} both {hid!r}"
                )
        bucket[slug] = hid


def pick_design_preset(slug: str, trade: str) -> DesignPreset:
    key = _trade_key(trade)
    pal_pool = [p for p in (TRADE_PALETTE_POOLS.get(key) or list(PALETTES.keys())[:8]) if p in PALETTES]
    if not pal_pool:
        pal_pool = list(PALETTES.keys())
    lay_pool = [x for x in (TRADE_LAYOUT_POOLS.get(key) or ["mosaic-2", "list-alt", "split-editorial"]) if x in LAYOUT_VARIANTS]
    font_pool = [x for x in (TRADE_FONT_POOLS.get(key) or list(FONT_PAIRS.keys())) if x in FONT_PAIRS]
    structs = STRUCTURE_VARIANTS.get(key) or [
        "Varie hero + sections; INTERDIT toujours hero→3 cartes→déroulé→FAQ."
    ]

    # Layout + structure share the same lane so structure brief matches layout family
    layout_idx = _pool_index(slug, "layout", len(lay_pool))
    layout_id = lay_pool[layout_idx]
    structure_idx = layout_idx % len(structs)
    palette_id = _pool_pick(slug, "palette", pal_pool)
    font_id = _pool_pick(slug, "font", font_pool)
    hero_structure_id = pick_hero_structure_id(slug, trade)
    hero_struct = HERO_STRUCTURES[hero_structure_id]

    colors = PALETTES[palette_id]
    fonts = FONT_PAIRS[font_id]
    layout = LAYOUT_VARIANTS[layout_id]
    structure_brief = structs[structure_idx]
    if isinstance(structure_brief, str) and "{layout}" in structure_brief:
        structure_brief = structure_brief.format(layout=layout["brief"])

    return DesignPreset(
        slug=slug or "",
        trade_key=key,
        palette_id=palette_id,
        font_id=font_id,
        layout_id=layout_id,
        structure_idx=structure_idx,
        hero_structure_id=hero_structure_id,
        accent=colors["accent"],
        accent_soft=colors["accent_soft"],
        ink=colors["ink"],
        paper=colors["paper"],
        hero=colors.get("hero") or colors["ink"],
        font_display=fonts["display"],
        font_body=fonts["body"],
        font_hint=fonts["hint"],
        layout_label=str(layout["label"]),
        photo_limit=int(layout["photo_limit"]),
        layout_brief=str(layout["brief"]),
        structure_brief=structure_brief + " " + str(layout["brief"]),
        hero_structure_brief=str(hero_struct["brief"]),
    )


def _css_descendants(parents: list[str], suffix: str) -> str:
    """Expand `.a,.b` + ` img` → `.a img,.b img` (never attach suffix to only the last)."""
    return ",".join(f"{p}{suffix}" for p in parents)


def _contrast_force_css(preset: DesignPreset) -> str:
    """
    Hard WCAG AA overlay — must win over scratch/template inline colors.

    Nuclear rules for dark surfaces:
    - Pure --av-hero-fg (usually #fff) + webkit-text-fill-color
    - opacity:1 (kill muted 0.8 labels)
    - text-shadow over photo heroes
    - dark scrim on .demo-hero photo backgrounds

    IMPORTANT: never concatenate `hero_box + " img"` — that makes `.hero` itself
    match the img reset and sets -webkit-text-fill-color:initial on all hero text.
    """
    root = preset.css_root()
    hero_sels = [
        ".hero",
        ".demo-hero",
        "header.hero",
        "section.hero",
        "[id=hero]",
        ".hero-banner",
        ".av-hero",
    ]
    dark_sels = [
        ".order",
        "section.order",
        "div.order",
        ".cta-band",
        ".band-dark",
        ".promo-dark",
        ".av-contact-card",
    ]
    hero_box = ",".join(hero_sels)
    dark_box = ",".join(dark_sels)
    not_media = ":not(img):not(svg):not(path):not(source):not(picture)"
    hero_text = ",".join(
        [hero_box]
        + [f"{s} *{not_media}" for s in hero_sels]
        + [
            f"{s} {t}"
            for s in hero_sels
            for t in (
                "a",
                "span",
                "p",
                "li",
                "small",
                "nav",
                "h1",
                "h2",
                "h3",
                ".brand",
                ".logo-name",
                ".demo-brand",
                ".demo-kicker",
                ".demo-hook",
                ".demo-location",
                ".demo-headline",
                ".lede",
                ".hero-bar",
                ".muted",
                ".eyebrow",
                ".tagline",
            )
        ]
    )
    dark_text = ",".join(
        [dark_box]
        + [f"{s} *{not_media}" for s in dark_sels]
        + [
            f"{s} {t}"
            for s in dark_sels
            for t in ("a", "span", "p", "li", "h1", "h2", "h3", "small")
        ]
    )
    primary_btn = ",".join(
        [
            "a.btn",
            "a.cta",
            ".btn",
            ".btn-primary",
            "button.cta",
            'a[href^="tel:"].cta',
            ".demo-btn-primary",
        ]
        + [
            f"{s} {t}"
            for s in hero_sels
            for t in ("a.btn", "a.cta", ".btn", ".cta", ".demo-btn-primary")
        ]
    )
    light_btn = ",".join(
        [
            "a.btn-light",
            ".btn-light",
            "a.demo-btn-light",
            ".demo-btn-light",
            ".btn-secondary",
            ".demo-btn-secondary",
            ".order .cta",
            ".order a.btn",
        ]
        + [
            f"{s} {t}"
            for s in hero_sels
            for t in ("a.demo-btn-light", ".demo-btn-light", ".btn-light")
        ]
    )
    media_reset = _css_descendants(hero_sels, " img") + "," + _css_descendants(
        hero_sels, " svg"
    ) + "," + _css_descendants(dark_sels, " img")
    return (
        f"{root}"
        f"body{{background:var(--av-paper)!important;color:var(--av-ink)!important;"
        f"font-family:var(--av-font-body);}}"
        f"h1,h2,h3,.brand,.logo-name{{font-family:var(--av-font-display);}}"
        f"main,footer,.band,.av-realisations,.content,.services,.vitrine,"
        f".zone,.gallery{{color:var(--av-ink);}}"
        f".accent-soft,.band-soft{{"
        f"background:var(--av-accent-soft);color:var(--av-ink)!important;}}"
        # Photo heroes: keep template overlay layout (media must stay position:absolute).
        # NEVER set .demo-hero>* { position:relative } — that breaks .demo-hero-media
        # absolute inset and pushes .demo-hero-content below a ~2000px image.
        f".demo-hero{{color:var(--av-hero-fg)!important;}}"
        f".demo-hero-media{{position:absolute!important;inset:0!important;}}"
        f".demo-hero-veil{{opacity:1!important;background:"
        f"linear-gradient(180deg,rgb(0 0 0/.45) 0%,rgb(0 0 0/.72) 48%,rgb(0 0 0/.9) 100%),"
        f"linear-gradient(90deg,rgb(0 0 0/.5) 0%,transparent 60%)!important;}}"
        f".demo-hero-content{{position:relative!important;z-index:2!important;"
        f"color:var(--av-hero-fg)!important;}}"
        # HERO — solid bg for non-photo heroes; media covers it on .demo-hero
        f"{hero_box}{{background:var(--av-hero)!important;"
        f"color:var(--av-hero-fg)!important;}}"
        f"{hero_text}{{"
        f"color:var(--av-hero-fg)!important;"
        f"-webkit-text-fill-color:var(--av-hero-fg)!important;"
        f"opacity:1!important;"
        f"font-weight:inherit;}}"
        # Soft shadow only on photo heroes (solid-color heroes don't need it)
        f".demo-hero,.demo-hero *{not_media}{{"
        f"text-shadow:0 1px 3px rgba(0,0,0,.85)!important;}}"
        # Nav / brand labels: slightly heavier so thin caps stay visible
        + _css_descendants(hero_sels, " .hero-bar")
        + ","
        + _css_descendants(hero_sels, " nav")
        + ","
        + _css_descendants(hero_sels, " .brand")
        + ","
        + _css_descendants(hero_sels, " .demo-brand")
        + "{"
        f"font-weight:650!important;letter-spacing:.04em!important;}}"
        f"{dark_box}{{background:var(--av-accent)!important;"
        f"color:var(--av-on-accent)!important;}}"
        f"{dark_text}{{"
        f"color:var(--av-on-accent)!important;"
        f"-webkit-text-fill-color:var(--av-on-accent)!important;"
        f"opacity:1!important;}}"
        f"{primary_btn}{{background:var(--av-accent)!important;"
        f"color:var(--av-on-accent)!important;"
        f"-webkit-text-fill-color:var(--av-on-accent)!important;"
        f"opacity:1!important;text-shadow:none!important;}}"
        f"{light_btn}{{background:#FFFFFF!important;color:#121212!important;"
        f"-webkit-text-fill-color:#121212!important;"
        f"opacity:1!important;text-shadow:none!important;}}"
        f"a.demo-btn-ghost,.demo-btn-ghost,.btn-ghost,"
        + _css_descendants(hero_sels, " a.demo-btn-ghost")
        + "{"
        f"background:transparent!important;color:var(--av-hero-fg)!important;"
        f"-webkit-text-fill-color:var(--av-hero-fg)!important;"
        f"border:1px solid var(--av-hero-fg)!important;"
        f"opacity:1!important;text-shadow:0 1px 2px rgba(0,0,0,.75)!important;}}"
        # Media only — do NOT attach to bare .hero
        f"{media_reset}{{"
        f"opacity:1!important;-webkit-text-fill-color:unset!important;"
        f"text-shadow:none!important;}}"
    )


_HEX_COLOR_RE = re.compile(
    r"(?i)(^|[\s:;])((?:color|background-color|background|border-color)\s*:\s*)"
    r"(#[0-9A-Fa-f]{3,8}|rgb\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*\))"
)


def _normalize_hex(color: str) -> str | None:
    c = (color or "").strip()
    if c.startswith("#"):
        h = c[1:]
        if len(h) == 3:
            h = "".join(ch * 2 for ch in h)
        if len(h) >= 6:
            return f"#{h[:6].upper()}"
        return None
    m = re.match(r"(?i)rgb\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)", c)
    if m:
        return "#{:02X}{:02X}{:02X}".format(*(max(0, min(255, int(x))) for x in m.groups()))
    return None


def _repair_rule_colors(rule_body: str, bg_guess: str, preset: DesignPreset) -> str:
    """Rewrite `color:` hexes in a CSS rule body that fail vs bg_guess."""

    def _repl(m: re.Match[str]) -> str:
        prefix, prop, raw = m.group(1), m.group(2), m.group(3)
        if not prop.strip().lower().startswith("color"):
            return m.group(0)
        # Exact property name must be `color` (not background-color)
        if not re.match(r"(?i)color\s*:", prop.strip()):
            return m.group(0)
        hx = _normalize_hex(raw)
        if not hx:
            return m.group(0)
        # Body threshold (≥4.5) also covers large titles (≥3)
        if contrast_ratio(hx, bg_guess) >= BODY_MIN_RATIO:
            return m.group(0)
        fixed = pick_text_on_bg(
            bg_guess, min_ratio=BODY_MIN_RATIO, light=preset.paper, dark=preset.ink
        )
        return f"{prefix}{prop}{fixed}"

    return _HEX_COLOR_RE.sub(_repl, rule_body)


def repair_stylesheet_contrast(css: str, preset: DesignPreset) -> str:
    """
    Walk CSS rules: if a rule sets a dark/light background, fix failing `color` values.
    Also rewrite `color:var(--ink)` on hero/accent surfaces to a readable hex.
    """
    hero_bg = preset.hero
    paper_bg = preset.paper
    accent_bg = preset.accent
    hero_fg = preset.hero_text_color()
    on_accent = preset.on_accent_color()

    def _resolve_bg(selector: str, body: str) -> str | None:
        sel_l = selector.casefold()
        body_l = body.casefold()
        bm = re.search(
            r"(?i)(?:background-color|background)\s*:\s*"
            r"(#[0-9A-Fa-f]{3,8}|rgb\([^)]+\)|var\(\s*--(?:av-)?(hero|accent|paper|ink)[^)]*\))",
            body,
        )
        if bm:
            raw = bm.group(1)
            hx = _normalize_hex(raw)
            if hx:
                return hx
            token = (bm.group(2) or "").casefold()
            if token == "hero":
                return hero_bg
            if token == "accent":
                return accent_bg
            if token == "paper":
                return paper_bg
            if token == "ink":
                return preset.ink
        if any(t in sel_l for t in (".hero", "demo-hero", "#hero", "av-hero", ".hero-bar")):
            return hero_bg
        if any(t in sel_l for t in (".order", "contact-card", "cta-band", "promo-dark")):
            return accent_bg
        if "var(--av-hero)" in body_l or "var(--hero)" in body_l:
            return hero_bg
        if "var(--av-accent)" in body_l or "var(--accent)" in body_l:
            return accent_bg
        if "var(--av-paper)" in body_l or "var(--paper)" in body_l:
            return paper_bg
        return None

    def _fix_var_colors(body: str, bg: str) -> str:
        """Swap var(--ink) / dark ink refs when they fail on bg."""
        target = (
            hero_fg
            if contrast_ratio(hero_fg, bg) >= contrast_ratio(on_accent, bg)
            else on_accent
        )
        if contrast_ratio(preset.ink, bg) >= BODY_MIN_RATIO:
            return body

        def _ink_var(m: re.Match[str]) -> str:
            return f"{m.group(1)}{target}"

        body = re.sub(
            r"(?i)(color\s*:\s*)var\(\s*--(?:av-)?ink\s*\)",
            _ink_var,
            body,
        )
        return body

    def _fix_block(full: str, selector: str, body: str) -> str:
        bg = _resolve_bg(selector, body)
        if not bg:
            return full
        new_body = _fix_var_colors(body, bg)
        new_body = _repair_rule_colors(new_body, bg, preset)
        if new_body == body:
            return full
        return f"{selector}{{{new_body}}}"

    return re.sub(
        r"([^{}@][^{]*)\{([^{}]*)\}",
        lambda m: _fix_block(m.group(0), m.group(1), m.group(2)),
        css,
    )


def _kill_muted_opacity(css: str) -> str:
    """Force full opacity in author CSS — muted labels were failing visually."""
    return re.sub(
        r"(?i)(opacity\s*:\s*)(0?\.\d+|0)(\s*!important)?",
        r"\g<1>1 !important",
        css,
    )


def repair_html_contrast(html: str, preset: DesignPreset) -> str:
    """Repair low-contrast colors inside <style> blocks (scratch + template)."""
    if not html:
        return html

    def _fix_style(m: re.Match[str]) -> str:
        open_tag, css, close = m.group(1), m.group(2), m.group(3)
        if 'id="av-variety-css"' in open_tag or "id='av-variety-css'" in open_tag:
            return m.group(0)  # our overlay — leave alone
        fixed = repair_stylesheet_contrast(css, preset)
        fixed = _kill_muted_opacity(fixed)
        return f"{open_tag}{fixed}{close}"

    html = re.sub(
        r"(?is)(<style\b[^>]*>)(.*?)(</style\s*>)",
        _fix_style,
        html,
    )
    # Inline style opacity on any element
    html = re.sub(
        r'(?i)(style=["\'][^"\']*?)opacity\s*:\s*0?\.\d+\s*;?',
        lambda m: re.sub(r"(?i)opacity\s*:\s*0?\.\d+\s*;?", "opacity:1;", m.group(1)),
        html,
    )
    return html


def _structure_and_nav_css(preset: DesignPreset) -> str:
    """B-28: top-bar nav + structure hints; kill mid-hero floating navs."""
    hid = preset.hero_structure_id
    # Universal: nav is a full-width top bar — never mid-hero beside a photo.
    nav = (
        ".av-topnav,.demo-nav,nav.site-nav,.hero-bar,.demo-hero>.top,"
        "header.hero>.top,.demo-hero>.demo-nav{"
        "position:relative!important;display:flex!important;flex-wrap:wrap;"
        "align-items:center;justify-content:space-between;gap:.75rem 1.25rem;"
        "width:100%!important;max-width:100%!important;box-sizing:border-box;"
        "padding:.85rem clamp(1rem,3vw,2rem)!important;margin:0!important;"
        "left:auto!important;right:auto!important;float:none!important;"
        "grid-column:1/-1!important;}"
        # Absolute top-bar only when intentionally overlaying a full-bleed photo hero
        ".demo-hero-minimal>.demo-nav,.demo-hero-overlay>.demo-nav,"
        ".hero.fullbleed>.hero-bar,.demo-hero.av-fullbleed>.av-topnav{"
        "position:absolute!important;top:0!important;left:0!important;right:0!important;"
        "z-index:5!important;}"
        # Kill side-by-side nav|photo empty-band layouts
        ".hero-bar:has(img),.hero-bar:has(figure){flex-direction:column!important;"
        "align-items:stretch!important;}"
        ".hero-bar img,.hero-bar figure{display:none!important;}"  # photo belongs in media slot
        ".demo-hero-media-neutral,.demo-neutral-visual,"
        ".demo-gallery-placeholder{display:none!important;}"
    )
    struct = ""
    if hid == "fullbleed-photo":
        struct = (
            ".demo-hero,.hero{min-height:78vh;position:relative;overflow:hidden;}"
            ".demo-hero-media{position:absolute!important;inset:0!important;}"
        )
    elif hid == "centered-editorial":
        hs = f"html.av-hs-{hid}"
        struct = (
            f"{hs} .hero,{hs} .demo-hero,{hs} .demo-hero-editorial{{"
            "display:flex!important;flex-direction:column!important;"
            "text-align:center;align-items:center;min-height:auto!important;"
            "padding:1.25rem clamp(1rem,3vw,2rem) 0!important;}"
            f"{hs} .hero > h1,{hs} .demo-hero > h1,"
            f"{hs} .hero .hero-title,{hs} .demo-hero-content{{"
            "order:1;position:relative!important;z-index:2;max-width:40rem;}}"
            f"{hs} .hero > p,{hs} .demo-hero > p{{order:2;max-width:36rem;}}"
            f"{hs} .hero a.cta,{hs} .hero .cta,{hs} .demo-hero .cta,"
            f"{hs} .hero .btn,{hs} .phone-band{{order:3;margin:.75rem auto 1rem;}}"
            # Photo BELOW chrome — never alone above the fold
            f"{hs} .demo-hero-media,{hs} .av-hero-photo,{hs} .av-hero-photo-band,"
            f"{hs} .demo-hero-editorial-media,{hs} .hero figure{{"
            "order:50;position:relative!important;inset:auto!important;"
            "width:100%!important;max-width:100%!important;"
            "height:min(38vh,24rem)!important;margin:0!important;}}"
            f"{hs} .demo-hero-media img,{hs} .av-hero-photo-band img,"
            f"{hs} .demo-hero-editorial-media img,{hs} .hero figure img{{"
            "width:100%!important;height:100%!important;object-fit:cover;display:block;}}"
        )
    elif hid == "split-photo-left":
        struct = (
            ".demo-hero-split{display:grid!important;"
            "grid-template-columns:minmax(0,1.15fr) minmax(0,1fr)!important;gap:0;}"
            ".demo-hero-split .demo-hero-media,.demo-hero-split .av-hero-photo{"
            "order:-1;min-height:70vh;}"
            ".demo-hero-split .demo-hero-copy{order:1;}"
            "@media(max-width:720px){.demo-hero-split{grid-template-columns:1fr!important;}}"
        )
    elif hid == "magazine-asymmetric":
        struct = (
            ".demo-hero-overlay,.hero.av-magazine{display:grid!important;"
            "grid-template-columns:minmax(0,1.4fr) minmax(0,.9fr)!important;"
            "align-items:end;gap:1.5rem;min-height:72vh;}"
            "@media(max-width:720px){.demo-hero-overlay,.hero.av-magazine{"
            "grid-template-columns:1fr!important;}}"
        )
    elif hid == "type-led-band":
        hs = f"html.av-hs-{hid}"
        struct = (
            f"{hs} .hero,{hs} .demo-hero{{"
            "display:flex!important;flex-direction:column!important;"
            "grid-template-columns:none!important;grid-template-rows:none!important;"
            "min-height:auto!important;position:relative!important;"
            "width:100%!important;align-items:stretch!important;"
            "padding:1.25rem clamp(1rem,3vw,2rem) 0!important;}}"
            # Name + pitch + CTA above the fold; photo band below
            f"{hs} .hero > h1,{hs} .demo-hero > h1{{order:1;position:relative!important;z-index:2;}}"
            f"{hs} .hero > p,{hs} .demo-hero > p{{order:2;max-width:40rem;}}"
            f"{hs} .hero a.cta,{hs} .hero .cta,{hs} .demo-hero .cta,"
            f"{hs} .hero .btn{{order:3;margin:.85rem 0 1rem;}}"
            f"{hs} .phone-band,{hs} .hero-bar{{"
            "position:relative!important;top:auto!important;order:4;width:100%!important;}}"
            f"{hs} .demo-hero-media,{hs} .av-hero-photo,{hs} .av-hero-photo-band,"
            f"{hs} .hero-photo,{hs} .hero figure{{"
            "position:relative!important;inset:auto!important;order:50;"
            "width:100%!important;max-width:100%!important;"
            "height:min(40vh,26rem)!important;max-height:26rem!important;"
            "display:block!important;}}"
            f"{hs} .demo-hero-media img,{hs} .av-hero-photo img,"
            f"{hs} .av-hero-photo-band img,{hs} .hero-photo img{{"
            "width:100%!important;height:100%!important;object-fit:cover;display:block;}}"
        )
    return f"/* B-28 structure={hid} */" + nav + struct


def inject_preset_css(html: str, preset: DesignPreset) -> str:
    """Force palette/typo + WCAG contrast into scratch AND template HTML."""
    html = repair_html_contrast(html or "", preset)
    force = _contrast_force_css(preset) + _structure_and_nav_css(preset)
    marker = 'id="av-variety-css"'
    style = f'<style {marker}>\n{force}\n</style>'
    if marker in html:
        html = re.sub(
            r'(?is)<style[^>]*id=["\']av-variety-css["\'][^>]*>.*?</style>',
            style,
            html,
            count=1,
        )
        return html
    if re.search(r"(?i)</head>", html):
        return re.sub(r"(?i)</head>", style + "\n</head>", html, count=1)
    if re.search(r"(?i)<body\b", html):
        return re.sub(r"(?i)<body\b", style + "\n<body", html, count=1)
    return style + html


def assert_hero_contrast(preset: DesignPreset) -> None:
    """Guardrail helper — hero title/body colors meet WCAG AA vs hero bg."""
    fg = preset.hero_text_color()
    ratio = contrast_ratio(fg, preset.hero)
    if ratio < BODY_MIN_RATIO:
        raise AssertionError(
            f"hero fg/bg {fg}/{preset.hero} contrast {ratio:.2f} < {BODY_MIN_RATIO}"
        )
    on_a = preset.on_accent_color()
    if contrast_ratio(on_a, preset.accent) < LARGE_MIN_RATIO:
        raise AssertionError(
            f"CTA on accent {on_a}/{preset.accent} contrast too low"
        )


def extract_palette_fingerprint(html: str) -> str:
    """Stable id from forced preset attr or dominant CSS vars — for guardrail tests."""
    m = re.search(r'data-av-palette=["\']([^"\']+)["\']', html or "")
    if m:
        return m.group(1).strip().casefold()
    # Fallback: first --av-accent / --av-hero hex pair
    accent = re.search(r"--av-accent\s*:\s*(#[0-9A-Fa-f]{6})", html or "")
    hero = re.search(r"--av-hero\s*:\s*(#[0-9A-Fa-f]{6})", html or "")
    if accent and hero:
        return f"{accent.group(1).casefold()}|{hero.group(1).casefold()}"
    return ""


def extract_photo_sequence(html: str) -> list[str]:
    names: list[str] = []
    for m in re.finditer(
        r'(?i)(?:src|content)=["\']([^"\']*?(?:patisserie|plombier|bouche|coach|paysagiste|'
        r"electricien|peintre|menuisier|coiffeur|photographe|local)-[^\"'?#]+\.jpe?g)",
        html or "",
    ):
        name = m.group(1).split("?")[0].rstrip("/").split("/")[-1].casefold()
        if name not in names:
            names.append(name)
    return names


def stamp_preset_attrs(html: str, preset: DesignPreset) -> str:
    hid = preset.hero_structure_id
    attrs = (
        f'data-av-palette="{preset.palette_id}" '
        f'data-av-layout="{preset.layout_id}" '
        f'data-av-font="{preset.font_id}" '
        f'data-av-hero-structure="{hid}"'
    )
    hs_class = f"av-hs-{hid}"

    def _stamp_html_tag(m: re.Match[str]) -> str:
        rest = m.group(1) or ""
        # Strip prior variety attrs/classes from the <html> tag only (not CSS).
        rest = re.sub(r'\s*data-av-(?:palette|layout|font|hero-structure)="[^"]*"', "", rest)
        rest = re.sub(r'\s*class="([^"]*)"', lambda cm: _merge_hs_class(cm.group(1), hs_class), rest, count=1)
        if "class=" not in rest:
            rest = f'{rest} class="{hs_class}"'
        return f"<html{rest} {attrs}>"

    def _merge_hs_class(existing: str, add: str) -> str:
        parts = [p for p in existing.split() if not p.startswith("av-hs-")]
        parts.append(add)
        return f' class="{" ".join(parts)}"'

    if re.search(r"(?i)<html\b", html):
        return re.sub(r"(?i)<html\b([^>]*)>", _stamp_html_tag, html, count=1)
    return html
