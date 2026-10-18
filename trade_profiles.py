#!/usr/bin/env python3
"""
Profils métier pour les démos ArtisanVisible : prestations, FAQ, hooks, design.
Jamais de fallback plombier pour un métier inconnu.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("trade_profiles")

# --------------------------------------------------------------------------- #
# Normalisation saisie
# --------------------------------------------------------------------------- #

_SMALL_WORDS = frozenset(
    {
        "de",
        "du",
        "des",
        "la",
        "le",
        "les",
        "et",
        "d",
        "l",
        "a",
        "au",
        "aux",
        "en",
        "sur",
        "sous",
        "chez",
    }
)
_UPPER_KEEP = frozenset(
    {
        "sarl",
        "sas",
        "sasu",
        "eurl",
        "snc",
        "sa",
        "sci",
        "ei",
        "auto-entrepreneur",
    }
)


def title_case_fr(text: str) -> str:
    """Capitalise proprement un nom d'entreprise ou une ville (FR)."""
    text = (text or "").strip()
    if not text:
        return text
    # Preserve intentional ALL CAPS short tokens later via word rules
    parts = re.split(r"(\s+|-|/|&)", text)
    out: list[str] = []
    for i, part in enumerate(parts):
        if not part or re.fullmatch(r"\s+", part) or part in "-/&":
            out.append(part)
            continue
        low = part.casefold()
        if low in _UPPER_KEEP:
            out.append(part.upper())
        elif low == "fils" or low == "cie":
            out.append(part.capitalize())
        elif low in _SMALL_WORDS and out and not (len(out) == 1 and out[0] in "-/&"):
            # Keep small words lowercase mid-phrase, but capitalize after hyphen start
            prev = out[-1] if out else ""
            if prev == "-":
                out.append(part.capitalize())
            else:
                out.append(low)
        else:
            out.append(part[:1].upper() + part[1:].lower() if len(part) > 1 else part.upper())
    # First meaningful word always capitalized
    result = "".join(out)
    for i, ch in enumerate(result):
        if ch.isalpha():
            return result[:i] + result[i].upper() + result[i + 1 :]
    return result


def format_fr_phone(phone: str) -> str:
    """Affiche un numéro FR type `06 68 61 90 55`."""
    raw = (phone or "").strip()
    if not raw:
        return ""
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("33") and len(digits) >= 11:
        digits = "0" + digits[2:11]
    if len(digits) == 10 and digits[0] == "0":
        return f"{digits[0:2]} {digits[2:4]} {digits[4:6]} {digits[6:8]} {digits[8:10]}"
    return raw


def normalize_inputs(name: str, city: str, phone: str) -> tuple[str, str, str]:
    return title_case_fr(name), title_case_fr(city), format_fr_phone(phone)


# --------------------------------------------------------------------------- #
# Contenu métier
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TradeProfile:
    key: str
    label: str
    template: str  # craft | pulse | field
    hero_image: str
    accent: str  # CSS color
    accent_soft: str
    ink: str
    paper: str
    display_font: str  # fraunces | plex
    hook: str
    cta_primary: str
    cta_secondary: str
    services: list[tuple[str, str, str]]  # title, desc, icon_id
    why_us: list[tuple[str, str]]
    zone_blurb: str
    faq: list[tuple[str, str]]
    gallery_caption: str
    meta_tagline: str
    hero_title: str = ""
    hero_subtitle: str = ""
    meta_description: str = ""
    content_source: str = "curated"  # curated | xai | fallback


def _svc(title: str, desc: str, icon: str) -> tuple[str, str, str]:
    return (title, desc, icon)


_SERVICE_PAD: list[tuple[str, str, str]] = [
    ("Conseil personnalisé", "Un échange pour comprendre votre besoin avant de proposer une solution.", "users"),
    ("Devis transparent", "Un chiffrage clair avant de démarrer, sans surprise.", "check"),
    ("Suivi jusqu'au bout", "Un interlocuteur joignable pendant et après la prestation.", "sparkle"),
]


def _ensure_six_services(
    services: list[tuple[str, str, str]],
) -> list[tuple[str, str, str]]:
    out = list(services)[:6]
    titles = {t for t, _, _ in out}
    for title, desc, icon in _SERVICE_PAD:
        if len(out) >= 6:
            break
        if title not in titles:
            out.append((title, desc, icon))
            titles.add(title)
    while len(out) < 6:
        out.append(_SERVICE_PAD[len(out) % len(_SERVICE_PAD)])
    return out


def _dedupe_hero(title: str, subtitle: str) -> tuple[str, str]:
    """Avoid short title + paragraph that repeats the same opening phrase."""
    t = (title or "").strip()
    s = (subtitle or "").strip()
    if not t:
        return "", s
    if not s:
        return t, ""
    if t.casefold() == s.casefold():
        return t, ""
    if s.casefold().startswith(t.casefold()):
        rest = s[len(t) :].lstrip(" \t:-–—.,")
        return (t, rest) if rest else (t, "")
    return t, s


def _finalize_profile(p: TradeProfile) -> TradeProfile:
    """Normalize hero copy + always 6 prestations (no duplicated headline/hook)."""
    from dataclasses import replace

    # Do not auto-split hook into title — that caused « Title » + « Title : rest » duplication.
    title = (p.hero_title or "").strip()
    subtitle = (p.hero_subtitle or "").strip() or (p.hook or "").strip()
    title, subtitle = _dedupe_hero(title, subtitle)
    meta = (p.meta_description or "").strip() or f"{p.label}. {subtitle or p.hook}".strip()
    return replace(
        p,
        services=_ensure_six_services(list(p.services)),
        hero_title=title[:120],
        hero_subtitle=subtitle[:220],
        meta_description=meta[:300],
        hook=subtitle[:220] if subtitle else p.hook,
    )


PROFILES: dict[str, TradeProfile] = {
    "plombier": TradeProfile(
        key="plombier",
        label="Plombier chauffagiste",
        template="craft",
        hero_image="plombier-01.jpg",
        accent="#1F6F5B",
        accent_soft="#E4F2ED",
        ink="#0F1C1A",
        paper="#F7F5F1",
        display_font="fraunces",
        hook="Fuite, chauffe-eau ou salle de bain : intervention soignée près de chez vous.",
        cta_primary="Demander un devis gratuit",
        cta_secondary="Appeler maintenant",
        services=[
            _svc("Dépannage & urgences", "Recherche de fuites, débouchage et remise en service rapide.", "wrench"),
            _svc("Sanitaire & salle de bain", "Pose de robinetterie, WC, douche et rénovation complète.", "bath"),
            _svc("Chauffage & eau chaude", "Chaudière, ballon, entretien et conseils d'économie d'énergie.", "flame"),
        ],
        why_us=[
            ("Réponse claire", "Vous savez ce qui sera fait et à quel tarif avant le démarrage."),
            ("Chantier propre", "Protection des sols, rangement en fin d'intervention."),
            ("Proximité", "Un interlocuteur local qui connaît votre secteur."),
        ],
        zone_blurb="Interventions à domicile et en entreprise dans votre ville et les communes voisines.",
        faq=[
            ("Intervenez-vous en urgence ?", "Oui, selon disponibilité. Appelez pour connaître le créneau le plus proche."),
            ("Le devis est-il gratuit ?", "Oui, le devis est gratuit et sans engagement pour les travaux planifiés."),
            ("Quels moyens de paiement ?", "Espèces, carte ou virement selon l'intervention."),
        ],
        gallery_caption="Photos de vos chantiers et finitions.",
        meta_tagline="Plomberie & chauffage — devis gratuit",
    ),
    "électricien": TradeProfile(
        key="électricien",
        label="Électricien",
        template="craft",
        hero_image="electricien-01.jpg",
        accent="#1B4F72",
        accent_soft="#E8F0F7",
        ink="#0D1B2A",
        paper="#F5F7FA",
        display_font="fraunces",
        hook="Panne, mise aux normes ou éclairage : une installation sûre et nette.",
        cta_primary="Obtenir un devis",
        cta_secondary="Appeler l'électricien",
        services=[
            _svc("Dépannage électrique", "Coupures, courts-circuits et diagnostic rapide.", "bolt"),
            _svc("Tableau & normes", "Mise en sécurité et remplacement de tableaux.", "shield"),
            _svc("Éclairage & prises", "Création de circuits, LED, bornes et domotique simple.", "light"),
        ],
        why_us=[
            ("Sécurité d'abord", "Chaque intervention vise une installation fiable au quotidien."),
            ("Explications simples", "Pas de jargon : vous comprenez les travaux proposés."),
            ("Finitions soignées", "Goulottes, découpes et rangement du chantier."),
        ],
        zone_blurb="Déplacements chez les particuliers et professionnels de votre secteur.",
        faq=[
            ("Faites-vous la mise aux normes ?", "Oui, après diagnostic de votre installation existante."),
            ("Combien de temps pour un devis ?", "Souvent sous 48 h après visite ou photos selon le besoin."),
            ("Intervenez-vous le week-end ?", "Selon planning — demandez lors de l'appel."),
        ],
        gallery_caption="Tableaux, éclairages et interventions.",
        meta_tagline="Électricité — dépannage & installation",
    ),
    "couvreur": TradeProfile(
        key="couvreur",
        label="Couvreur",
        template="craft",
        hero_image="local-03.jpg",
        accent="#5C4033",
        accent_soft="#F0E8E2",
        ink="#1A1210",
        paper="#F8F4F0",
        display_font="fraunces",
        hook="Fuite, tuiles ou zinguerie : votre toiture reprise dans les règles.",
        cta_primary="Signaler une fuite",
        cta_secondary="Appeler le couvreur",
        services=[
            _svc("Urgence & bâchage", "Mise hors d'eau après intempéries et recherche d'infiltrations.", "roof"),
            _svc("Réfection de toiture", "Tuiles, ardoises, zinc et traitement adapté.", "hammer"),
            _svc("Gouttières & isolation", "Zinguerie, démoussage et isolation des combles.", "leaf"),
        ],
        why_us=[
            ("Sécurisation rapide", "Priorité à la mise hors d'eau pour limiter les dégâts."),
            ("Devis lisible", "Fournitures et main-d'œuvre détaillées."),
            ("Respect du voisinage", "Chantier organisé, évacuation des déchets prévue."),
        ],
        zone_blurb="Toitures individuelles et petites copropriétés dans votre zone.",
        faq=[
            ("Que faire en cas de fuite ?", "Protégez vos biens et appelez : un bâchage peut être proposé rapidement."),
            ("Travaillez-vous sur tous types de toits ?", "Tuiles, ardoises et zinc selon diagnostic."),
            ("Le devis est-il gratuit ?", "Oui pour une visite de devis planifiée."),
        ],
        gallery_caption="Avant / après toiture — à illustrer avec vos chantiers.",
        meta_tagline="Couverture & zinguerie",
    ),
    "serrurier": TradeProfile(
        key="serrurier",
        label="Serrurier",
        template="craft",
        hero_image="local-01.jpg",
        accent="#2C3E50",
        accent_soft="#EAECEE",
        ink="#111827",
        paper="#F6F7F9",
        display_font="fraunces",
        hook="Porte claquée, serrure fatiguée ou renforcement : intervention discrète.",
        cta_primary="Demander une intervention",
        cta_secondary="Appeler le serrurier",
        services=[
            _svc("Ouverture de porte", "Porte claquée ou clé perdue, avec soin pour limiter les dégâts.", "key"),
            _svc("Changement de serrure", "Cylindres et multipoints adaptés à votre porte.", "lock"),
            _svc("Renforcement", "Blindage et conseils pour sécuriser l'accès.", "shield"),
        ],
        why_us=[
            ("Intervention mesurée", "On privilégie l'ouverture sans casse quand c'est possible."),
            ("Transparence", "Tarif annoncé avant intervention dans la mesure du possible."),
            ("Matériel adapté", "Serrures et cylindres selon votre besoin."),
        ],
        zone_blurb="Urgences et rendez-vous sur votre commune et alentours.",
        faq=[
            ("Intervenez-vous la nuit ?", "Selon disponibilité — précisez lors de l'appel."),
            ("Cassez-vous toujours la porte ?", "Non : l'ouverture non destructive est recherchée en priorité."),
            ("Puis-je changer seulement le cylindre ?", "Souvent oui, après vérification de la serrure."),
        ],
        gallery_caption="Pose de serrures — ajoutez vos photos de réalisations.",
        meta_tagline="Serrurerie & sécurisation",
    ),
    "menuisier": TradeProfile(
        key="menuisier",
        label="Menuisier",
        template="craft",
        hero_image="menuisier-01.jpg",
        accent="#6B4F3A",
        accent_soft="#F3EDE6",
        ink="#1C1410",
        paper="#FAF7F3",
        display_font="fraunces",
        hook="Portes, placards ou agencement : un travail du bois précis et durable.",
        cta_primary="Parler de mon projet",
        cta_secondary="Appeler l'atelier",
        services=[
            _svc("Pose & remplacement", "Portes, fenêtres bois et huisseries soignées.", "door"),
            _svc("Agencement intérieur", "Placards, bibliothèques et rangements sur mesure.", "ruler"),
            _svc("Réparation & finitions", "Remise en état, ponçage et finitions adaptées.", "hammer"),
        ],
        why_us=[
            ("Sur mesure", "Les dimensions et usages de votre pièce guident le projet."),
            ("Matériaux choisis", "Bois et quincaillerie adaptés à l'usage."),
            ("Suivi clair", "Du relevé de côtes à la pose, un seul interlocuteur."),
        ],
        zone_blurb="Atelier et pose à domicile dans votre secteur.",
        faq=[
            ("Travaillez-vous sur mesure uniquement ?", "Sur mesure et pose de solutions adaptées selon le projet."),
            ("Combien de temps pour un devis ?", "Après visite ou plans / photos selon la complexité."),
            ("Proposez-vous la finition complète ?", "Oui, selon le type de bois et le rendu souhaité."),
        ],
        gallery_caption="Agencements et poses — remplacez par vos photos.",
        meta_tagline="Menuiserie & agencement",
    ),
    "peintre": TradeProfile(
        key="peintre",
        label="Peintre en bâtiment",
        template="field",
        hero_image="peintre-01.jpg",
        accent="#3D5A80",
        accent_soft="#E8EEF5",
        ink="#15202B",
        paper="#F7F8FA",
        display_font="fraunces",
        hook="Murs, plafonds et finitions : une pièce nette, prête à vivre.",
        cta_primary="Demander un devis peinture",
        cta_secondary="Appeler le peintre",
        services=[
            _svc("Peinture intérieure", "Préparation des supports, enduits et application soignée.", "brush"),
            _svc("Rénovation & lessivage", "Remise à neuf des murs fatigués et finitions mates ou satinées.", "sparkle"),
            _svc("Conseils couleurs", "Aide au choix des teintes et finitions selon la lumière.", "palette"),
        ],
        why_us=[
            ("Préparation soignée", "Un beau rendu commence par des supports bien préparés."),
            ("Chantier protégé", "Bâches, déplacements de meubles et nettoyage."),
            ("Délais annoncés", "Planning communiqué avant démarrage."),
        ],
        zone_blurb="Appartements, maisons et locaux dans votre zone d'intervention.",
        faq=[
            ("Faut-il vider les pièces ?", "On protège et déplace le possible ; un dégagement facilite le travail."),
            ("Quelles peintures utilisez-vous ?", "Des peintures adaptées à chaque pièce (pièces humides incluses)."),
            ("Le devis est-il gratuit ?", "Oui après visite ou estimation sur photos / surfaces."),
        ],
        gallery_caption="Avant / après peinture — ajoutez vos chantiers.",
        meta_tagline="Peinture intérieure & finitions",
    ),
    "paysagiste": TradeProfile(
        key="paysagiste",
        label="Paysagiste",
        template="field",
        hero_image="paysagiste-01.jpg",
        accent="#2D6A4F",
        accent_soft="#E7F2EC",
        ink="#14261C",
        paper="#F5F8F5",
        display_font="fraunces",
        hook="Jardin net, haies propres ou nouvel aménagement : un extérieur qui respire.",
        cta_primary="Planifier mon jardin",
        cta_secondary="Appeler le paysagiste",
        services=[
            _svc("Entretien régulier", "Tonte, taille de haies et entretien saisonnier.", "leaf"),
            _svc("Élagage raisonné", "Taille et mise en sécurité des arbres selon besoin.", "tree"),
            _svc("Aménagement", "Massifs, clôtures, engazonnement et terrasses extérieures.", "map"),
        ],
        why_us=[
            ("Respect du vivant", "Tailles adaptées à chaque espèce et saison."),
            ("Jardin utilisable", "Un extérieur agréable au quotidien, pas seulement « joli »."),
            ("Évacuation incluse", "Déchets verts pris en charge selon devis."),
        ],
        zone_blurb="Jardins particuliers et espaces verts de proximité.",
        faq=[
            ("Proposez-vous un contrat d'entretien ?", "Oui, selon la fréquence souhaitée."),
            ("Intervenez-vous en hiver ?", "Oui pour tailles et travaux adaptés à la saison."),
            ("Faut-il un devis sur place ?", "Recommandé pour un chiffrage précis."),
        ],
        gallery_caption="Exemples d'ambiances de jardin (illustrations).",
        meta_tagline="Entretien & aménagement de jardin",
    ),
    "garagiste": TradeProfile(
        key="garagiste",
        label="Garagiste",
        template="craft",
        hero_image="local-01.jpg",
        accent="#1A3A4A",
        accent_soft="#E6EEF1",
        ink="#0E1A20",
        paper="#F5F6F7",
        display_font="plex",
        hook="Révision, freins ou diagnostic : un atelier clair sur les réparations.",
        cta_primary="Prendre rendez-vous",
        cta_secondary="Appeler l'atelier",
        services=[
            _svc("Entretien & révision", "Vidange, freinage, distribution selon préconisations.", "car"),
            _svc("Diagnostic", "Panne moteur, électronique et contrôles avant CT.", "search"),
            _svc("Pneumatiques", "Montage, équilibrage et conseils saisonniers.", "circle"),
        ],
        why_us=[
            ("Devis avant réparation", "Vous validez les travaux avant exécution."),
            ("Explications simples", "On vous dit ce qui est urgent et ce qui peut attendre."),
            ("Suivi véhicule", "Historique des interventions sur votre voiture."),
        ],
        zone_blurb="Accueil atelier pour les automobilistes de votre secteur.",
        faq=[
            ("Faut-il prendre rendez-vous ?", "Oui de préférence, sauf dépannage selon capacité."),
            ("Travaillez-vous toutes marques ?", "Oui pour l'entretien courant ; pièces selon disponibilité."),
            ("Puis-je attendre sur place ?", "Selon organisation de l'atelier — demandez en réservant."),
        ],
        gallery_caption="Atelier et véhicules — photos à ajouter.",
        meta_tagline="Garage — entretien & réparation",
    ),
    "coiffeur": TradeProfile(
        key="coiffeur",
        label="Coiffeur",
        template="pulse",
        hero_image="coiffeur-01.jpg",
        accent="#7A4E3A",
        accent_soft="#F6EEE8",
        ink="#1A1210",
        paper="#FBF8F5",
        display_font="fraunces",
        hook="Coupe, couleur ou soin : un rendez-vous pour un résultat qui vous ressemble.",
        cta_primary="Réserver un créneau",
        cta_secondary="Appeler le salon",
        services=[
            _svc("Coupe & brushing", "Coupes femme, homme et enfant selon morphologie.", "scissors"),
            _svc("Couleur & balayage", "Techniques adaptées à votre cheveu et à votre entretien.", "palette"),
            _svc("Soins & conseils", "Soins ciblés et routine simple à la maison.", "sparkle"),
        ],
        why_us=[
            ("Écoute du souhait", "On part de votre quotidien, pas d'une tendance imposée."),
            ("Conseil entretien", "Vous repartez avec des gestes simples."),
            ("Accueil soigné", "Un salon pensé pour que vous vous sentiez à l'aise."),
        ],
        zone_blurb="Salon de proximité — clientèle locale et sur rendez-vous.",
        faq=[
            ("Faut-il réserver ?", "Oui, pour garantir le créneau et le temps nécessaire."),
            ("Proposez-vous une consultation couleur ?", "Oui avant une première coloration importante."),
            ("Acceptez-vous les enfants ?", "Oui, selon créneaux — précisez l'âge à la réservation."),
        ],
        gallery_caption="Coupes et couleurs — ajoutez vos plus beaux résultats.",
        meta_tagline="Salon de coiffure",
    ),
    "esthéticienne": TradeProfile(
        key="esthéticienne",
        label="Esthéticienne",
        template="pulse",
        hero_image="coiffeur-03.jpg",
        accent="#8B5E6B",
        accent_soft="#F7EEF1",
        ink="#1A1216",
        paper="#FBF7F8",
        display_font="fraunces",
        hook="Soins visage, regard ou beauté des mains : un moment pour votre peau.",
        cta_primary="Prendre rendez-vous",
        cta_secondary="Appeler l'institut",
        services=[
            _svc("Soins visage", "Nettoyage, hydratation et soins adaptés à votre peau.", "sparkle"),
            _svc("Regard & sourcils", "Mise en beauté du regard selon votre morphologie.", "eye"),
            _svc("Mains & pieds", "Manucure, soins et finitions soignées.", "hand"),
        ],
        why_us=[
            ("Protocole personnalisé", "On adapte le soin à votre peau, pas l'inverse."),
            ("Produits adaptés", "Sélection selon sensibilité et objectifs."),
            ("Cadre apaisant", "Un rendez-vous sans précipitation."),
        ],
        zone_blurb="Institut de proximité pour une clientèle locale.",
        faq=[
            ("Puis-je venir sans rendez-vous ?", "Mieux vaut réserver pour garantir la disponibilité."),
            ("Les soins sont-ils adaptés aux peaux sensibles ?", "Oui, on adapte après un court échange."),
            ("Combien dure un soin visage ?", "Selon la formule — indiqué à la réservation."),
        ],
        gallery_caption="Ambiance et soins — photos de l'institut à venir.",
        meta_tagline="Institut de beauté",
    ),
    "coach sportif": TradeProfile(
        key="coach sportif",
        label="Coach sportif",
        template="pulse",
        hero_image="coach-01.jpg",
        accent="#2A9D8F",
        accent_soft="#E6F5F2",
        ink="#0F1A1C",
        paper="#F4F7F6",
        display_font="plex",
        hook="Perte de poids, reprise ou performance : un coaching clair, motivant et adapté.",
        cta_primary="Réserver un premier échange",
        cta_secondary="Appeler le coach",
        services=[
            _svc("Coaching personnalisé", "Séances sur mesure selon votre niveau et vos objectifs.", "bolt"),
            _svc("Remise en forme", "Progression douce pour reprendre le sport sans se blesser.", "heart"),
            _svc("Suivi & motivation", "Plan d'entraînement, ajustements et accountability.", "target"),
        ],
        why_us=[
            ("Objectifs réalistes", "On construit un plan tenable dans votre emploi du temps."),
            ("Technique & sécurité", "Priorité aux bons gestes pour progresser durablement."),
            ("Suivi humain", "Un coach qui s'adapte à vos avancées et freins."),
        ],
        zone_blurb="Séances en présentiel sur votre ville et alentours (parc, domicile ou salle selon organisation).",
        faq=[
            ("Faut-il être déjà sportif ?", "Non. Les programmes s'adaptent aux débutants comme aux confirmés."),
            ("Où se déroulent les séances ?", "Selon votre préférence : extérieur, domicile ou salle partenaire."),
            ("Comment se passe le premier contact ?", "Un échange pour comprendre vos objectifs, puis une proposition de créneaux."),
        ],
        gallery_caption="Séances et progressions (avec accord des clients).",
        meta_tagline="Coach sportif — coaching personnalisé",
    ),
    "photographe": TradeProfile(
        key="photographe",
        label="Photographe",
        template="field",
        hero_image="photographe-01.jpg",
        accent="#1D3557",
        accent_soft="#E8EEF5",
        ink="#0D1520",
        paper="#F6F7F9",
        display_font="fraunces",
        hook="Portrait, événement ou reportage : des images nettes qui racontent votre moment.",
        cta_primary="Discuter de mon projet",
        cta_secondary="Appeler le photographe",
        services=[
            _svc("Portrait & pro", "Portraits individuels, famille ou image professionnelle.", "camera"),
            _svc("Événements", "Reportage mariage, entreprise ou moments clés.", "users"),
            _svc("Retouche & livraison", "Sélection soignée et fichiers prêts à partager.", "image"),
        ],
        why_us=[
            ("Direction douce", "Vous êtes guidé sans stress devant l'objectif."),
            ("Style cohérent", "Une esthétique claire du shooting à la galerie."),
            ("Livraison propre", "Fichiers organisés et retouches soignées."),
        ],
        zone_blurb="Tournages en studio, extérieur ou sur site dans votre région.",
        faq=[
            ("Combien de temps pour recevoir les photos ?", "Délai indiqué au devis selon le type de prestation."),
            ("Puis-je choisir les photos ?", "Oui, selon la formule (galerie de sélection)."),
            ("Déplacement inclus ?", "Selon la distance — précisé dans le devis."),
        ],
        gallery_caption="Portfolio — remplacez par vos meilleures photos.",
        meta_tagline="Photographe — portrait & reportage",
    ),
    "pâtisserie": TradeProfile(
        key="pâtisserie",
        label="Pâtissier",
        template="craft",
        hero_image="patisserie-02.jpg",
        accent="#7A1F2B",
        accent_soft="#F7E8EA",
        ink="#1A1214",
        paper="#FBF7F4",
        display_font="fraunces",
        hook="Gâteaux, viennoiseries et commandes sur mesure — une pâtisserie artisanale, claire et accueillante.",
        cta_primary="Commander pour une occasion",
        cta_secondary="Appeler la boutique",
        services=[
            _svc("Gâteaux d'occasion", "Anniversaires, fêtes et pièces montées selon le nombre de parts.", "sparkle"),
            _svc("Viennoiseries & tartes", "Croissants, tartes aux fruits et classiques du comptoir.", "image"),
            _svc("Entremets & chocolat", "Créations plus fines pour un dessert qui marque le moment.", "heart"),
            _svc("Plateaux sucrés", "Assortiments pour entreprise, association ou pot partagé.", "users"),
            _svc("Conseil personnalisé", "Échange préalable pour comprendre l'occasion, les goûts et la date.", "check"),
            _svc("Retrait en boutique", "Préparation soignée et retrait à l'heure convenue (livraison selon offre).", "target"),
        ],
        why_us=[
            ("Savoir-faire artisanal", "Des recettes travaillées, sans jargon inutile."),
            ("Commande claire", "Vous savez ce qui est inclus avant de vous engager."),
            ("Proximité", "Une boutique de quartier pour les gourmands du coin."),
        ],
        zone_blurb="Boutique et commandes pour les particuliers et professionnels de votre ville et alentours.",
        faq=[
            ("Faut-il commander à l'avance ?", "Oui pour les gâteaux d'occasion — le délai dépend de la pièce."),
            ("Puis-je choisir les saveurs ?", "Oui : on part de vos goûts, du format et de la date de retrait."),
            ("Livraison possible ?", "Selon l'offre du moment — sinon retrait en boutique."),
        ],
        gallery_caption="Créations et vitrine — photos de la boutique à venir.",
        meta_tagline="Pâtisserie artisanale — gâteaux & commandes",
    ),
    "boulangerie": TradeProfile(
        key="boulangerie",
        label="Boulanger",
        template="craft",
        hero_image="patisserie-01.jpg",
        accent="#8B5A2B",
        accent_soft="#F6EDE2",
        ink="#1A1410",
        paper="#FBF8F3",
        display_font="fraunces",
        hook="Pains, viennoiseries et spécialités du jour — une boulangerie de proximité, simple et généreuse.",
        cta_primary="Voir les spécialités",
        cta_secondary="Appeler la boulangerie",
        services=[
            _svc("Pains du jour", "Baguettes, pains de campagne et spécialités selon le fournil.", "image"),
            _svc("Viennoiseries", "Croissants, pains au chocolat et classiques du matin.", "sparkle"),
            _svc("Sandwichs & snacking", "Formules rapides pour le déjeuner sur le pouce.", "users"),
            _svc("Commandes groupe", "Assortiments pour réunion, buffet ou événement local.", "heart"),
            _svc("Conseil au comptoir", "On vous oriente selon l'occasion et le nombre de convives.", "check"),
            _svc("Retrait prévu", "Préparation et retrait à l'heure convenue.", "target"),
        ],
        why_us=[
            ("Fournil du quotidien", "Des produits frais, sans superflu."),
            ("Accueil simple", "Une équipe de quartier, joignable et claire."),
            ("Commandes anticipées", "Idéal pour les groupes et les occasions."),
        ],
        zone_blurb="Boutique de proximité pour les habitants et entreprises du secteur.",
        faq=[
            ("Quels sont les horaires ?", "Indiqués en boutique et sur la vitrine — variables selon les jours."),
            ("Puis-je commander pour un groupe ?", "Oui, avec un peu d'avance selon le volume."),
            ("Livraison ?", "Selon l'offre — sinon retrait en boutique."),
        ],
        gallery_caption="Fournil et vitrine — photos de la boulangerie à venir.",
        meta_tagline="Boulangerie — pains & viennoiseries",
    ),
    "traiteur": TradeProfile(
        key="traiteur",
        label="Traiteur",
        template="craft",
        hero_image="bouche-07.jpg",
        accent="#1F6F5B",
        accent_soft="#E4F2ED",
        ink="#0F1C1A",
        paper="#F7F5F1",
        display_font="fraunces",
        hook="Buffets, plateaux et menus pour vos réceptions — du conseil à la livraison.",
        cta_primary="Demander un menu",
        cta_secondary="Appeler le traiteur",
        services=[
            _svc("Buffets & plateaux", "Formules salées et sucrées pour réunions et fêtes.", "sparkle"),
            _svc("Menus événements", "Mariages, anniversaires et cocktails sur mesure.", "users"),
            _svc("Livraison & service", "Livraison sur site et options de service selon la formule.", "map"),
        ],
        why_us=[
            ("Menus adaptés", "On calibre portions et allergies avec vous."),
            ("Présentation soignée", "Dressage pensé pour impressionner vos invités."),
            ("Organisation simple", "Un interlocuteur pour le planning et le jour J."),
        ],
        zone_blurb="Réceptions et livraisons sur votre ville et communes voisines.",
        faq=[
            ("Quel délai pour une réception ?", "En général 1 à 2 semaines ; plus tôt en haute saison."),
            ("Gérez-vous le matériel ?", "Selon formule — vaisselle et service en option."),
            ("Menus végétariens ?", "Oui, on propose des alternatives adaptées."),
        ],
        gallery_caption="Buffets et dressages — ajoutez vos réalisations.",
        meta_tagline="Traiteur — buffets & événements",
    ),
    "boucherie": TradeProfile(
        key="boucherie",
        label="Boucher",
        template="craft",
        hero_image="bouche-08.jpg",
        accent="#8B3A3A",
        accent_soft="#F8ECE9",
        ink="#1C1412",
        paper="#F9F6F4",
        display_font="plex",
        hook="Viandes sélectionnées, conseils de cuisson et commandes pour vos repas.",
        cta_primary="Passer au comptoir",
        cta_secondary="Appeler la boucherie",
        services=[
            _svc("Viandes du jour", "Bœuf, volaille, porc et spécialités selon arrivage.", "sparkle"),
            _svc("Préparations", "Marinades, brochettes et pièces pour barbecue ou rôti.", "check"),
            _svc("Commandes festives", "Plateaux et pièces pour fêtes et grandes tables.", "users"),
        ],
        why_us=[
            ("Conseil au comptoir", "Cuisson, morceau et quantité adaptés à votre repas."),
            ("Fraîcheur", "Découpe et préparation au plus près de la vente."),
            ("Sur commande", "Pièces particulières réservées à l'avance."),
        ],
        zone_blurb="Boucherie de proximité pour votre quartier.",
        faq=[
            ("Puis-je commander à l'avance ?", "Oui, surtout pour le week-end et les fêtes."),
            ("Faites-vous des plateaux ?", "Oui selon disponibilité — demandez au comptoir."),
            ("Origine des viandes ?", "Indiquée en boutique selon les lots."),
        ],
        gallery_caption="Comptoir et préparations — photos à ajouter.",
        meta_tagline="Boucherie — viandes & conseils",
    ),
    "restaurant": TradeProfile(
        key="restaurant",
        label="Restaurant",
        template="craft",
        hero_image="bouche-07.jpg",
        accent="#1B4F72",
        accent_soft="#E8F0F7",
        ink="#0D1B2A",
        paper="#F5F7FA",
        display_font="fraunces",
        hook="Une table accueillante : cuisine du moment, réservation et service soigné.",
        cta_primary="Réserver une table",
        cta_secondary="Appeler le restaurant",
        services=[
            _svc("Carte du moment", "Plats de saison et suggestions du chef.", "sparkle"),
            _svc("Réservation", "Tables pour déjeuner, dîner ou occasions spéciales.", "users"),
            _svc("Groupes & événements", "Menus groupes selon disponibilité et salle.", "check"),
        ],
        why_us=[
            ("Accueil chaleureux", "Une équipe attentive du premier au dernier service."),
            ("Produits soignés", "Une carte qui change avec la saison."),
            ("Occasions", "Anniversaires et groupes sur réservation."),
        ],
        zone_blurb="Restaurant pour la clientèle locale et de passage.",
        faq=[
            ("Faut-il réserver ?", "Recommandé le week-end et pour les groupes."),
            ("Menu enfant ?", "Selon carte — demandez en réservant."),
            ("Allergies alimentaires ?", "Prévenez-nous à la réservation ou à l'arrivée."),
        ],
        gallery_caption="Salle et assiettes — ajoutez vos photos.",
        meta_tagline="Restaurant — table & réservation",
    ),
}

# Alias keys (normalized match)
_ALIASES: dict[str, str] = {
    "plombier chauffagiste": "plombier",
    "chauffagiste": "plombier",
    "plombier-chauffagiste": "plombier",
    "electricien": "électricien",
    "électricien": "électricien",
    "elec": "électricien",
    "couvreur zingueur": "couvreur",
    "zingueur": "couvreur",
    "serrurerie": "serrurier",
    "menuiserie": "menuisier",
    "peintre en batiment": "peintre",
    "peintre en bâtiment": "peintre",
    "jardinier": "paysagiste",
    "paysagiste jardinier": "paysagiste",
    "garage": "garagiste",
    "mécanicien": "garagiste",
    "mecanicien": "garagiste",
    "coiffeuse": "coiffeur",
    "salon de coiffure": "coiffeur",
    "estheticienne": "esthéticienne",
    "esthéticienne": "esthéticienne",
    "institut de beauté": "esthéticienne",
    "coach": "coach sportif",
    "coach sportif": "coach sportif",
    "personal trainer": "coach sportif",
    "entraîneur sportif": "coach sportif",
    "entraineur sportif": "coach sportif",
    "photographe pro": "photographe",
    "photo": "photographe",
    # Bouche — profiles live mainly in photo_banks / site_plan curated; aliases for match
    "patisserie": "pâtisserie",
    "pâtisserie": "pâtisserie",
    "patissier": "pâtisserie",
    "pâtissier": "pâtisserie",
    "patissiere": "pâtisserie",
    "pâtissière": "pâtisserie",
    "boulanger": "boulangerie",
    "boulangerie": "boulangerie",
    "traiteur": "traiteur",
    "boucher": "boucherie",
    "boucherie": "boucherie",
    "restaurant": "restaurant",
    "restaurateur": "restaurant",
    "chef cuisinier": "restaurant",
}


def _norm_trade(trade: str) -> str:
    t = unicodedata_fold(trade or "").strip().lower()
    t = re.sub(r"\s+", " ", t)
    return t


def unicodedata_fold(s: str) -> str:
    import unicodedata

    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def match_profile_key(trade: str) -> str | None:
    n = _norm_trade(trade)
    if not n:
        return None
    if n in _ALIASES:
        return _ALIASES[n]
    if n in PROFILES:
        return n
    # substring contains
    for alias, key in sorted(_ALIASES.items(), key=lambda x: -len(x[0])):
        if alias in n or n in alias:
            return key
    for key in PROFILES:
        kn = _norm_trade(key)
        if kn in n or n in kn:
            return key
    return None


def intelligent_fallback(trade: str) -> TradeProfile:
    """Generic local-service profile — never plumber-specific copy."""
    label = title_case_fr(trade) if trade.strip() else "Professionnel local"
    trade_low = trade.strip().lower() or "service local"
    return TradeProfile(
        key="default",
        label=label,
        template="field",
        hero_image="local-01.jpg",
        accent="#1F4E5F",
        accent_soft="#E6F0F3",
        ink="#102028",
        paper="#F6F8F9",
        display_font="plex",
        hook=f"{label} à votre écoute : une présence claire, des prestations adaptées à vos besoins.",
        cta_primary="Demander un devis",
        cta_secondary="Nous appeler",
        services=[
            _svc(
                f"Prestations {trade_low}",
                f"Accompagnement sur vos besoins en {trade_low}, avec un devis clair avant démarrage.",
                "sparkle",
            ),
            _svc(
                "Conseil personnalisé",
                "Échange préalable pour comprendre votre projet et proposer une solution adaptée.",
                "users",
            ),
            _svc(
                "Suivi jusqu'à la livraison",
                "Un interlocuteur dédié pour le planning, les questions et la finalisation.",
                "check",
            ),
        ],
        why_us=[
            ("Transparence", "Vous savez ce qui est inclus avant de vous engager."),
            ("Proximité", "Un professionnel local, joignable et réactif."),
            ("Qualité de service", "Focus sur le résultat et la satisfaction client."),
        ],
        zone_blurb="Zone d'intervention autour de votre ville et communes voisines.",
        faq=[
            ("Comment obtenir un devis ?", "Décrivez votre besoin par téléphone ou message : un devis vous est proposé."),
            ("Intervenez-vous près de chez moi ?", "Oui dans la zone indiquée ; confirmez votre commune à l'appel."),
            ("Quels sont les délais ?", "Ils dépendent du planning et de la nature de la demande."),
        ],
        gallery_caption="Vos réalisations apparaîtront ici après ajout de photos.",
        meta_tagline=f"{label} — devis gratuit",
    )


XAI_API_URL = "https://api.x.ai/v1/chat/completions"
# Default only when XAI_MODEL is unset/empty — never overwrite .env
DEFAULT_XAI_MODEL = "grok-4.3"
XAI_TIMEOUT_S = int(os.getenv("XAI_TIMEOUT_S") or "90")  # generation JSON; check uses same
XAI_MAX_ATTEMPTS = int(os.getenv("XAI_MAX_ATTEMPTS") or "3")  # 1 try + 2 retries
XAI_RETRY_BACKOFF_S = (2.0, 4.0, 8.0)


class XaiUnknownModelError(RuntimeError):
    """Raised when SpaceXSI rejects the configured model id."""

    def __init__(self, model: str, http_code: int | None = None):
        self.model = model
        self.http_code = http_code
        super().__init__(f"unknown xAI model: {model}")


def resolved_xai_model() -> str:
    """Read XAI_MODEL from the environment; do not mutate .env."""
    return (os.getenv("XAI_MODEL") or "").strip() or DEFAULT_XAI_MODEL


def _is_unknown_model_body(body: str) -> bool:
    low = (body or "").lower()
    if "model" not in low:
        return False
    return any(
        m in low
        for m in (
            "not found",
            "does not exist",
            "unknown model",
            "invalid model",
            "model not",
            "unsupported model",
            "no such model",
            "is not a valid",
        )
    )


_SERVICE_ICONS = (
    "sparkle",
    "users",
    "check",
    "bolt",
    "heart",
    "target",
    "map",
    "shield",
)


def _clean_text(value: Any, *, max_len: int = 400) -> str:
    """Sanitize model text (strip, collapse space, no HTML tags). Escape is done at render."""
    if value is None:
        return ""
    text = str(value)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_len:
        text = text[: max_len - 1].rsplit(" ", 1)[0] + "…"
    return text


def _parse_services(items: Any) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    for i, it in enumerate(items or []):
        if not isinstance(it, dict):
            continue
        title = _clean_text(it.get("title") or it.get("name"), max_len=80)
        desc = _clean_text(it.get("desc") or it.get("description"), max_len=220)
        if not title:
            continue
        out.append((title, desc or "Prestation adaptée à votre besoin.", _SERVICE_ICONS[i % len(_SERVICE_ICONS)]))
        if len(out) >= 6:
            break
    return _ensure_six_services(out)


def _parse_pairs(items: Any, a: str, b: str, *, limit: int = 4) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        left = _clean_text(it.get(a) or it.get("title") or it.get("q"), max_len=120)
        right = _clean_text(it.get(b) or it.get("desc") or it.get("a"), max_len=280)
        if left and right:
            out.append((left, right))
        if len(out) >= limit:
            break
    return out


def _xai_request(api_key: str, body: dict[str, Any]) -> dict[str, Any]:
    """POST chat completions; never logs the API key."""
    model = str(body.get("model") or "")
    data = json.dumps(body).encode("utf-8")
    last_err: Exception | None = None
    for attempt in range(1, XAI_MAX_ATTEMPTS + 1):
        req = urllib.request.Request(
            XAI_API_URL,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=XAI_TIMEOUT_S) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                err_body = e.read().decode("utf-8", errors="replace")
            except OSError:
                err_body = ""
            if _is_unknown_model_body(err_body) or (
                e.code in (400, 404) and "model" in err_body.lower() and "invalid" in err_body.lower()
            ):
                raise XaiUnknownModelError(model, http_code=e.code) from e
            last_err = e
            log.warning(
                "xAI chat completions échec tentative %d/%d (métier call) : HTTPError %s",
                attempt,
                XAI_MAX_ATTEMPTS,
                e.code,
            )
            # No retry on auth / spend limit / client errors other than transient
            if e.code in (401, 403, 404):
                break
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
            last_err = e
            log.warning(
                "xAI chat completions échec tentative %d/%d (métier call) : %s",
                attempt,
                XAI_MAX_ATTEMPTS,
                type(e).__name__,
            )
        if attempt < XAI_MAX_ATTEMPTS and last_err is not None:
            if isinstance(last_err, urllib.error.HTTPError) and last_err.code in (401, 403, 404):
                break
            delay = XAI_RETRY_BACKOFF_S[min(attempt - 1, len(XAI_RETRY_BACKOFF_S) - 1)]
            time.sleep(delay)
    assert last_err is not None
    raise last_err


def check_xai() -> tuple[bool, str]:
    """
    Probe XAI_API_KEY + XAI_MODEL with a minimal chat completion (no site generation).
    Returns (ok, message). Never includes the API key in the message.
    """
    api_key = (os.getenv("XAI_API_KEY") or "").strip()
    model = resolved_xai_model()
    if not api_key:
        return False, "XAI_API_KEY manquant"
    body = {
        "model": model,
        "temperature": 0,
        "max_tokens": 16,
        "messages": [
            {"role": "user", "content": 'Réponds uniquement: {"ok":true}'},
        ],
    }
    try:
        payload = _xai_request(api_key, body)
        content = (payload.get("choices") or [{}])[0].get("message", {}).get("content")
        used = payload.get("model") or model
        if not content:
            return False, f"Réponse vide (modèle demandé: {model})"
        return True, f"OK — modèle {used}"
    except XaiUnknownModelError as e:
        return False, f"Modèle inconnu refusé par xAI: {e.model}"
    except urllib.error.HTTPError as e:
        return False, f"HTTPError {e.code} (modèle: {model})"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__} (modèle: {model})"


def xai_generate_profile(
    trade: str,
    *,
    name: str = "",
    city: str = "",
    design_base: TradeProfile | None = None,
) -> TradeProfile | None:
    """
    Generate métier copy via SpaceXSI Grok (OpenAI-compatible chat completions).
    Returns None if XAI_API_KEY missing or call/validation fails.
    Design tokens (template, colors, hero image) come from design_base when provided.
    Uses XAI_MODEL from env as-is (no overwrite); falls back to curated on failure.
    """
    api_key = (os.getenv("XAI_API_KEY") or "").strip()
    if not api_key:
        return None

    model = resolved_xai_model()
    base = design_base or intelligent_fallback(trade)

    system = (
        "Tu rédiges le contenu d'une vitrine web pour un professionnel local en France. "
        "Réponds UNIQUEMENT en JSON valide (pas de markdown). Clés requises:\n"
        "hero_title (string, court), hero_subtitle (string), "
        "services (exactement 6 objets {title, desc}), "
        "why_us (3 à 4 objets {title, desc}), "
        "faq (3 à 4 objets {q, a}), "
        "zone_blurb (string), "
        "meta_description (string SEO, max 160 car.), "
        "cta_primary (string), cta_secondary (string), "
        "gallery_caption (string), label (string métier).\n"
        "Français simple, bénéfice client, sans jargon. "
        "INTERDIT: inventer des avis, notes, chiffres, années d'expérience, certifications, labels. "
        "INTERDIT: contenu plombier/chauffage sauf si le métier fourni est clairement plombier. "
        "Adapte tout au métier indiqué."
    )
    user = (
        f"Métier: {trade}\n"
        f"Nom commercial: {name or '(non fourni)'}\n"
        f"Ville: {city or '(non fournie)'}\n"
        "Rédige le JSON demandé."
    )
    body = {
        "model": model,
        "temperature": 0.35,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }

    try:
        payload = _xai_request(api_key, body)
        content = payload["choices"][0]["message"]["content"]
        if not isinstance(content, str):
            raise ValueError("empty content")
        # Strip accidental markdown fences
        content = content.strip()
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*", "", content)
            content = re.sub(r"\s*```$", "", content)
        data = json.loads(content)
        if not isinstance(data, dict):
            raise ValueError("not an object")
    except XaiUnknownModelError as e:
        log.warning(
            "WARNING: modèle xAI inconnu / refusé %r (HTTP %s) — fallback contenu curaté métier",
            e.model,
            e.http_code,
        )
        return None
    except Exception as e:  # noqa: BLE001 — fallback path
        log.warning("xAI contenu métier indisponible pour %r : %s", trade, type(e).__name__)
        return None

    services = _parse_services(data.get("services"))
    why = _parse_pairs(data.get("why_us"), "title", "desc", limit=4) or list(base.why_us)
    faq = _parse_pairs(data.get("faq"), "q", "a", limit=4) or list(base.faq)
    hero_title = _clean_text(data.get("hero_title"), max_len=100)
    hero_subtitle = _clean_text(data.get("hero_subtitle") or data.get("hook"), max_len=220)
    meta_desc = _clean_text(data.get("meta_description"), max_len=160)
    zone = _clean_text(data.get("zone_blurb"), max_len=280) or base.zone_blurb
    label = _clean_text(data.get("label"), max_len=80) or base.label

    if not hero_title and not hero_subtitle:
        return None

    from dataclasses import replace

    return _finalize_profile(
        replace(
            base,
            key=base.key if base.key != "default" else "xai",
            label=label,
            hook=hero_subtitle or base.hook,
            hero_title=hero_title or base.hero_title,
            hero_subtitle=hero_subtitle or base.hook,
            meta_description=meta_desc or base.meta_description,
            cta_primary=_clean_text(data.get("cta_primary"), max_len=60) or base.cta_primary,
            cta_secondary=_clean_text(data.get("cta_secondary"), max_len=60) or base.cta_secondary,
            services=services,
            why_us=why,
            zone_blurb=zone,
            faq=faq,
            gallery_caption=_clean_text(data.get("gallery_caption"), max_len=160) or base.gallery_caption,
            meta_tagline=_clean_text(data.get("meta_tagline"), max_len=80) or base.meta_tagline,
            content_source="xai",
        )
    )


def curated_or_fallback(trade: str) -> TradeProfile:
    key = match_profile_key(trade)
    if key and key in PROFILES:
        return _finalize_profile(PROFILES[key])
    return _finalize_profile(
        __import__("dataclasses").replace(intelligent_fallback(trade), content_source="fallback")
    )


def resolve_profile(
    trade: str,
    *,
    name: str = "",
    city: str = "",
    allow_llm: bool = True,
) -> TradeProfile:
    """
    Resolve design + copy for a trade.
    Prefer SpaceXSI Grok (XAI_API_KEY) for text; always fall back to curated / intelligent
    generic — never plumber defaults for unrelated métiers.
    """
    base = curated_or_fallback(trade)
    if allow_llm:
        generated = xai_generate_profile(trade, name=name, city=city, design_base=base)
        if generated:
            return generated
    return base


# Back-compat: list of (title, desc) for older callers
def resolve_services(trade: str) -> list[tuple[str, str]]:
    p = resolve_profile(trade, allow_llm=False)
    return [(t, d) for t, d, _ in p.services]


def main(argv: list[str] | None = None) -> int:
    """CLI: python -m trade_profiles --check-xai"""
    import argparse

    ap = argparse.ArgumentParser(description="Profils métier / vérification xAI")
    ap.add_argument(
        "--check-xai",
        action="store_true",
        help="Teste XAI_API_KEY + XAI_MODEL (sans générer de site). N'affiche jamais la clé.",
    )
    args = ap.parse_args(argv)
    if args.check_xai:
        ok, msg = check_xai()
        print(msg)
        return 0 if ok else 1
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
