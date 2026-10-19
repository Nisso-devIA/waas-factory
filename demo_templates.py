#!/usr/bin/env python3
"""Rendu HTML des démos ArtisanVisible — plans uniques, bibliothèque de sections."""
from __future__ import annotations

from site_plan import SitePlan

# Cache-bust local demo images/CSS behind Cloudflare (max-age 4h)
DEMO_ASSET_V = "20261017y"


def demo_img(filename: str) -> str:
    """Return asset URL, or empty string when no photo (neutral CSS visual)."""
    name = (filename or "").strip().lstrip("/")
    if not name:
        return ""
    if name.startswith("demo-assets/"):
        return f"/{name}?v={DEMO_ASSET_V}"
    return f"/demo-assets/images/{name}?v={DEMO_ASSET_V}"


def _media_block(src: str, *, soft: bool = False, veil: bool = False, w: int = 1600, h: int = 1067, priority: bool = False) -> str:
    """Photo <img> or elegant neutral gradient panel when bank is empty."""
    soft_cls = " demo-hero-media-soft" if soft else ""
    if not src:
        return (
            f'<div class="demo-hero-media demo-hero-media-neutral{soft_cls}" aria-hidden="true">'
            '<div class="demo-neutral-visual"></div>'
            + ('<div class="demo-hero-veil"></div>' if veil else "")
            + "</div>"
        )
    pri = ' fetchpriority="high"' if priority else ' loading="lazy"'
    img = f'<img src="{src}" alt="" width="{w}" height="{h}"{pri}>'
    if soft or veil:
        return (
            f'<div class="demo-hero-media{soft_cls}" aria-hidden="true">{img}'
            + ('<div class="demo-hero-veil"></div>' if veil else "")
            + "</div>"
        )
    return f'<div class="demo-hero-media" aria-hidden="true">{img}</div>'


ICONS: dict[str, str] = {
    "sparkle": '<path d="M9.937 15.5A2 2 0 0 0 8.5 14.063l-6.135-1.582a.5.5 0 0 1 0-.962L8.5 9.936A2 2 0 0 0 9.937 8.5l1.582-6.135a.5.5 0 0 1 .963 0L14.063 8.5A2 2 0 0 0 15.5 9.937l6.135 1.581a.5.5 0 0 1 0 .964L15.5 14.063a2 2 0 0 0-1.437 1.437l-1.582 6.135a.5.5 0 0 1-.963 0z"/>',
    "check": '<path d="M20 6 9 17l-5-5"/>',
    "users": '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
    "bolt": '<path d="M13 2 3 14h9l-1 8 10-12h-9l1-8z"/>',
    "map": '<polygon points="3 6 9 3 15 6 21 3 21 18 15 21 9 18 3 21"/><line x1="9" x2="9" y1="3" y2="18"/><line x1="15" x2="15" y1="6" y2="21"/>',
}


def icon_svg(icon_id: str = "sparkle") -> str:
    path = ICONS.get(icon_id) or ICONS["sparkle"]
    return (
        f'<svg class="demo-icon" viewBox="0 0 24 24" width="28" height="28" '
        f'fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" '
        f'stroke-linejoin="round" aria-hidden="true">{path}</svg>'
    )


def _font_css(font_pair: str) -> str:
    if font_pair == "plex-only":
        return "var(--font-sans)"
    return "var(--font-display)"


def _nav(plan: SitePlan, esc) -> str:
    if plan.nav_variant == "none":
        return ""
    links = [
        ('#prestations', "Prestations"),
        ('#approche', "Approche"),
        ('#zone', "Zone"),
        ('#contact', "Contact"),
    ]
    if plan.nav_variant == "minimal":
        links = [('#prestations', "Prestations"), ('#contact', "Contact")]
    items = "".join(
        f'<a href="{href}">{esc(label)}</a>' for href, label in links
    )
    return f'<nav class="demo-nav demo-nav-{esc(plan.nav_variant)}" aria-label="Navigation">{items}</nav>'


def _hero(plan: SitePlan, *, rating_badge: str, address: str, tel: str, esc) -> str:
    hero_src = esc(demo_img(plan.hero_image)) if plan.hero_image else ""
    call = (
        f'<a class="demo-btn demo-btn-primary" href="tel:{esc(tel)}">{esc(plan.cta_secondary)}</a>'
        if tel
        else ""
    )
    cta = (
        f'<div class="demo-cta-row">{call}'
        f'<a class="demo-btn demo-btn-light" href="#contact">{esc(plan.cta_primary)}</a></div>'
    )
    headline = f'<p class="demo-headline">{esc(plan.hero_title)}</p>' if plan.hero_title else ""
    hook = f'<p class="demo-hook">{esc(plan.hero_subtitle)}</p>' if plan.hero_subtitle else ""
    loc = f'<p class="demo-location">{esc(address or plan.city)}</p>'

    if plan.hero_variant == "split":
        return f"""
  <header class="demo-hero demo-hero-split">
    <div class="demo-hero-copy">
      <div class="demo-hero-copy-inner">
      {rating_badge}
      {_nav(plan, esc)}
      <p class="demo-kicker">{esc(plan.trade_label)}</p>
      <h1 class="demo-brand">{esc(plan.name)}</h1>
      {headline}{hook}{loc}{cta}
      </div>
    </div>
    {_media_block(hero_src, priority=True)}
  </header>"""

    if plan.hero_variant == "editorial":
        if hero_src:
            media = (
                f'<figure class="demo-hero-editorial-media">'
                f'<img src="{hero_src}" alt="" width="1600" height="900" fetchpriority="high">'
                f"</figure>"
            )
        else:
            media = (
                '<figure class="demo-hero-editorial-media demo-hero-media-neutral" aria-hidden="true">'
                '<div class="demo-neutral-visual"></div></figure>'
            )
        return f"""
  <header class="demo-hero demo-hero-editorial demo-wrap">
    {rating_badge}
    {_nav(plan, esc)}
    <p class="demo-kicker">{esc(plan.trade_label)}</p>
    <h1 class="demo-brand">{esc(plan.name)}</h1>
    {headline}{hook}
    {media}
    {loc}{cta}
  </header>"""

    if plan.hero_variant == "minimal":
        return f"""
  <header class="demo-hero demo-hero-minimal">
    {_media_block(hero_src, soft=True, veil=True, priority=True)}
    <div class="demo-hero-content demo-wrap">
      {rating_badge}
      {_nav(plan, esc)}
      <p class="demo-kicker">{esc(plan.trade_label)}</p>
      <h1 class="demo-brand">{esc(plan.name)}</h1>
      {headline}{hook}{loc}{cta}
    </div>
  </header>"""

    # overlay (default)
    return f"""
  <header class="demo-hero demo-hero-overlay">
    {_media_block(hero_src, veil=True, priority=True)}
    <div class="demo-hero-content demo-wrap">
      {rating_badge}
      {_nav(plan, esc)}
      <p class="demo-kicker">{esc(plan.trade_label)}</p>
      <h1 class="demo-brand">{esc(plan.name)}</h1>
      {headline}{hook}{loc}{cta}
    </div>
  </header>"""


def _sec_flagship(plan: SitePlan, esc) -> str:
    f = plan.flagship
    img = demo_img(plan.section_image) if plan.section_image else ""
    if img:
        media = (
            f'<figure class="demo-flagship-media">'
            f'<img src="{esc(img)}" alt="" loading="lazy" width="900" height="700">'
            f"</figure>"
        )
    else:
        media = (
            '<figure class="demo-flagship-media demo-hero-media-neutral" aria-hidden="true">'
            '<div class="demo-neutral-visual"></div></figure>'
        )
    return f"""
  <section class="demo-section demo-flagship" id="prestations">
    <div class="demo-wrap demo-flagship-grid">
      <div class="demo-flagship-text">
        <p class="demo-eyebrow">Prestation phare</p>
        <h2>{esc(f.get('title') or 'Notre expertise')}</h2>
        <p class="demo-lead">{esc(f.get('body') or '')}</p>
        <p class="demo-aside">{esc(f.get('aside') or '')}</p>
      </div>
      {media}
    </div>
  </section>"""


def _sec_services_numbered(plan: SitePlan, esc) -> str:
    items = []
    for i, s in enumerate(plan.services, 1):
        items.append(
            f'<article class="demo-num-item">'
            f'<span class="demo-num" aria-hidden="true">{i:02d}</span>'
            f'<div><h3>{esc(s.get("title") or "")}</h3>'
            f'<p class="demo-lead">{esc(s.get("lead") or "")}</p>'
            f'<p>{esc(s.get("detail") or "")}</p>'
            f'<p class="demo-muted-line">{esc(s.get("for_whom") or "")}</p></div></article>'
        )
    return f"""
  <section class="demo-section" id="prestations">
    <div class="demo-wrap">
      <header class="demo-section-head">
        <h2>Prestations</h2>
        <p>Des offres concrètes, rédigées pour {esc(plan.city)}.</p>
      </header>
      <div class="demo-num-list">{"".join(items)}</div>
    </div>
  </section>"""


def _sec_services_accordion(plan: SitePlan, esc) -> str:
    items = []
    for i, s in enumerate(plan.services):
        open_attr = " open" if i == 0 else ""
        items.append(
            f'<details class="demo-acc-item"{open_attr}>'
            f'<summary>{esc(s.get("title") or "")}</summary>'
            f'<p class="demo-lead">{esc(s.get("lead") or "")}</p>'
            f'<p>{esc(s.get("detail") or "")}</p>'
            f'<p class="demo-muted-line">{esc(s.get("for_whom") or "")}</p>'
            f"</details>"
        )
    return f"""
  <section class="demo-section demo-section-alt" id="prestations">
    <div class="demo-wrap demo-acc">
      <header class="demo-section-head">
        <h2>Ce que nous faisons</h2>
        <p>Ouvrez chaque prestation pour le détail.</p>
      </header>
      <div class="demo-acc-list">{"".join(items)}</div>
    </div>
  </section>"""


def _sec_process(plan: SitePlan, esc) -> str:
    steps = "".join(
        f'<li class="demo-step"><span class="demo-step-n">{i}</span>'
        f"<div><h3>{esc(s.get('title') or '')}</h3><p>{esc(s.get('body') or '')}</p></div></li>"
        for i, s in enumerate(plan.process_steps, 1)
    )
    return f"""
  <section class="demo-section" id="deroule">
    <div class="demo-wrap">
      <header class="demo-section-head">
        <h2>Comment ça se passe</h2>
        <p>Un déroulé simple, sans jargon.</p>
      </header>
      <ol class="demo-steps">{steps}</ol>
    </div>
  </section>"""


def _sec_story(plan: SitePlan, esc) -> str:
    vals = "".join(
        f'<li><strong>{esc(v.get("title") or "")}</strong><span>{esc(v.get("body") or "")}</span></li>'
        for v in plan.values
    )
    story_file = getattr(plan, "story_image", None) or plan.section_image
    if story_file == plan.section_image and plan.gallery_images:
        story_file = plan.gallery_images[0]
    img = demo_img(story_file) if story_file else ""
    if img:
        media = (
            f'<figure class="demo-story-media">'
            f'<img src="{esc(img)}" alt="" loading="lazy" width="800" height="600">'
            f"</figure>"
        )
    else:
        media = (
            '<figure class="demo-story-media demo-hero-media-neutral" aria-hidden="true">'
            '<div class="demo-neutral-visual"></div></figure>'
        )
    return f"""
  <section class="demo-section demo-section-alt" id="approche">
    <div class="demo-wrap demo-story-grid">
      <div>
        <header class="demo-section-head">
          <h2>{esc(plan.story.get("title") or "Notre approche")}</h2>
        </header>
        <p class="demo-lead">{esc(plan.story.get("body") or "")}</p>
        <ul class="demo-value-rail">{vals}</ul>
      </div>
      {media}
    </div>
  </section>"""


def _sec_audience(plan: SitePlan, esc) -> str:
    items = "".join(f"<li>{esc(a)}</li>" for a in plan.audience)
    return f"""
  <section class="demo-section" id="pour-qui">
    <div class="demo-wrap demo-audience">
      <header class="demo-section-head">
        <h2>Pour qui</h2>
        <p>Des profils que {esc(plan.name)} accompagne volontiers.</p>
      </header>
      <ul class="demo-audience-list">{items}</ul>
    </div>
  </section>"""


def _sec_zone(plan: SitePlan, esc) -> str:
    chips = "".join(f'<span class="demo-chip">{esc(c)}</span>' for c in plan.zone_cities)
    return f"""
  <section class="demo-section demo-section-alt" id="zone">
    <div class="demo-wrap demo-zone">
      <header class="demo-section-head">
        <h2>Zone d'intervention</h2>
        <p>{esc(plan.zone_blurb)}</p>
      </header>
      <div class="demo-chips">{chips}</div>
    </div>
  </section>"""


def _sec_faq(plan: SitePlan, esc) -> str:
    items = "".join(
        f'<details class="demo-faq-item"><summary>{esc(f.get("q") or "")}</summary>'
        f'<p>{esc(f.get("a") or "")}</p></details>'
        for f in plan.faq
    )
    return f"""
  <section class="demo-section" id="faq">
    <div class="demo-wrap demo-faq">
      <header class="demo-section-head"><h2>Questions fréquentes</h2></header>
      <div class="demo-faq-list">{items}</div>
    </div>
  </section>"""


def _sec_gallery(plan: SitePlan, esc) -> str:
    """Placeholder gallery — métier images when available, else elegant empty slots."""
    imgs = list(getattr(plan, "gallery_images", None) or [])
    # Never reuse hero/section/story on the same page; drop empties
    used = {plan.hero_image, plan.section_image, getattr(plan, "story_image", "") or ""}
    unique = [i for i in imgs if i and i not in used]
    slots: list[str] = []
    for i in range(3):
        if i < len(unique):
            src = esc(demo_img(unique[i]))
            slots.append(
                f'<figure class="demo-gallery-slot demo-gallery-slot-photo">'
                f'<img src="{src}" alt="" loading="lazy" width="800" height="600">'
                f'<figcaption><span class="demo-gallery-soon">Photo à venir</span>'
                f'<span class="demo-gallery-hint">Remplacée par vos photos</span></figcaption>'
                f"</figure>"
            )
        else:
            slots.append(
                '<figure class="demo-gallery-slot demo-gallery-slot-empty" aria-label="Emplacement photo client">'
                '<div class="demo-gallery-empty" role="img" aria-hidden="true"></div>'
                '<figcaption><span class="demo-gallery-soon">Photo à venir</span>'
                '<span class="demo-gallery-hint">Ajoutez vos réalisations</span></figcaption>'
                "</figure>"
            )
    grid = "\n      ".join(slots)
    cue = (
        '<p class="demo-gallery-cue">'
        "Ces emplacements se rempliront avec <strong>vos photos</strong> "
        "(espace client / upload) — jamais d’images hors métier."
        "</p>"
    )
    return (
        "<!-- REALISATIONS_START -->\n"
        '<section class="demo-section demo-section-alt" id="realisations">\n'
        '  <div class="demo-wrap">\n'
        f'    <header class="demo-section-head"><h2>Nos réalisations</h2>'
        f"<p>{esc(plan.gallery_caption)}</p></header>\n"
        f"    {cue}\n"
        f'    <div class="demo-gallery demo-gallery-placeholder">\n      {grid}\n    </div>\n'
        "  </div>\n"
        "</section>\n"
        "<!-- REALISATIONS_END -->\n"
    )


def _sec_final_cta(plan: SitePlan, *, tel: str, phone_display: str, maps: str, address: str, esc) -> str:
    call = (
        f'<a class="demo-btn demo-btn-ghost" href="tel:{esc(tel)}">📞 {esc(phone_display)}</a>'
        if tel
        else ""
    )
    maps_btn = (
        f'<a class="demo-btn demo-btn-outline" href="{esc(maps)}" target="_blank" rel="noopener">'
        f"Voir sur la carte</a>"
        if maps
        else ""
    )
    return f"""
  <section class="demo-section" id="contact">
    <div class="demo-wrap demo-contact">
      <header class="demo-section-head">
        <h2>Parlons de votre besoin</h2>
        <p>{esc(plan.cta_primary)} — réponse rapide et claire.</p>
      </header>
      <div class="demo-contact-card">
        <p class="demo-contact-name">{esc(plan.name)}</p>
        <p class="demo-contact-addr">{esc(address or plan.city)}</p>
        <div class="demo-cta-row">{call}{maps_btn}</div>
      </div>
    </div>
  </section>"""


# --------------------------------------------------------------------------- #
# Section devis (configurateur sur mesure) — auto-stylée, POST /api/quote/{slug}
# Reprend la grille de pricing.py ; l'estimation client est cosmétique, le serveur
# recalcule et fait foi. {{SLUG}} est injecté par _sec_devis().
# --------------------------------------------------------------------------- #
DEVIS_SECTION_TEMPLATE = """
<section class="va-devis" id="devis">
  <style>
  .va-devis{--va-accent:var(--demo-accent,#0f7a56);--va-ink:var(--demo-ink,#16241d);--va-paper:var(--demo-paper,#f6f7f4);--va-line:rgba(20,35,28,.14);--va-soft:rgba(20,35,28,.04);background:var(--va-paper);color:var(--va-ink);padding:60px 20px;font-family:inherit}
  .va-devis *{box-sizing:border-box}
  .va-devis .va-wrap{max-width:820px;margin:0 auto}
  .va-devis .va-head{text-align:center;margin-bottom:26px}
  .va-devis .va-eyebrow{text-transform:uppercase;letter-spacing:.14em;font-size:12px;font-weight:700;color:var(--va-accent);margin:0 0 8px}
  .va-devis h2{font-size:clamp(24px,4vw,34px);margin:0 0 10px;font-family:var(--demo-display,inherit)}
  .va-devis .va-sub{max-width:52ch;margin:0 auto;opacity:.75;font-size:15px}
  .va-devis .va-block{border:0;padding:0;margin:0 0 22px;min-width:0}
  .va-devis legend{font-weight:700;font-size:15px;margin-bottom:10px;padding:0}
  .va-devis .va-grid-2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
  .va-devis .va-grid-3{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
  .va-devis .va-grid-opts{display:grid;grid-template-columns:1fr 1fr;gap:10px}
  @media(max-width:560px){.va-devis .va-grid-2,.va-devis .va-grid-3,.va-devis .va-grid-opts{grid-template-columns:1fr}}
  .va-devis label{cursor:pointer}
  .va-devis input{position:absolute;opacity:0;width:0;height:0}
  .va-devis .va-card{display:flex;flex-direction:column;gap:2px;border:1.5px solid var(--va-line);border-radius:12px;padding:14px;background:#fff;transition:border-color .15s,box-shadow .15s}
  .va-devis .va-card:has(input:checked){border-color:var(--va-accent);box-shadow:0 0 0 3px color-mix(in srgb,var(--va-accent) 18%,transparent)}
  .va-devis .va-card-t{font-weight:700}
  .va-devis .va-card-d{font-size:13px;opacity:.65}
  .va-devis .va-opt{display:flex;align-items:center;gap:10px;border:1.5px solid var(--va-line);border-radius:10px;padding:11px 13px;background:#fff;transition:border-color .15s,background .15s;min-width:0}
  .va-devis .va-opt:has(input:checked){border-color:var(--va-accent);background:var(--va-soft)}
  .va-devis .va-ck{width:20px;height:20px;border:1.5px solid var(--va-line);border-radius:6px;flex:none;position:relative}
  .va-devis .va-opt:has(input:checked) .va-ck{background:var(--va-accent);border-color:var(--va-accent)}
  .va-devis .va-opt:has(input:checked) .va-ck::after{content:"";position:absolute;left:6px;top:2px;width:5px;height:10px;border:2px solid #fff;border-top:0;border-left:0;transform:rotate(45deg)}
  .va-devis .va-opt-n{flex:1;font-size:14px;font-weight:500;min-width:0}
  .va-devis .va-opt-p{font-size:13px;font-weight:700;color:var(--va-accent);white-space:nowrap}
  .va-devis .va-lang{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-top:10px;padding:10px 13px;background:var(--va-soft);border-radius:10px;font-size:14px;font-weight:600}
  .va-devis .va-lang[hidden]{display:none}
  .va-devis .va-step{display:inline-flex;align-items:center;gap:4px}
  .va-devis .va-step button{width:30px;height:30px;border:1px solid var(--va-line);background:#fff;border-radius:8px;font-size:18px;cursor:pointer;line-height:1}
  .va-devis .va-step b{min-width:28px;text-align:center;font-variant-numeric:tabular-nums}
  .va-devis .va-tier{display:flex;flex-direction:column;gap:4px;border:1.5px solid var(--va-line);border-radius:12px;padding:13px;background:#fff;transition:border-color .15s,box-shadow .15s}
  .va-devis .va-tier:has(input:checked){border-color:var(--va-accent);box-shadow:0 0 0 3px color-mix(in srgb,var(--va-accent) 18%,transparent)}
  .va-devis .va-tier-n{font-weight:700;font-size:14px}
  .va-devis .va-tier-n i{font-style:normal;font-size:10px;text-transform:uppercase;letter-spacing:.05em;background:var(--va-accent);color:#fff;padding:2px 6px;border-radius:5px;margin-left:4px}
  .va-devis .va-tier-p{font-weight:800;font-size:18px}
  .va-devis .va-tier-p small{font-size:11px;font-weight:500;opacity:.6}
  .va-devis .va-estimate{display:flex;align-items:center;justify-content:space-between;gap:14px;flex-wrap:wrap;background:#fff;border:1.5px solid var(--va-accent);border-radius:12px;padding:14px 16px;margin:4px 0 16px}
  .va-devis .va-est-lbl{font-size:12px;text-transform:uppercase;letter-spacing:.08em;opacity:.6;display:block}
  .va-devis .va-est-note{font-weight:700}
  .va-devis .va-est-val{font-size:15px;font-variant-numeric:tabular-nums}
  .va-devis .va-est-val b{font-size:22px;font-family:var(--demo-display,inherit)}
  .va-devis .va-est-val small{opacity:.6}
  .va-devis .va-btn{display:block;width:100%;border:0;border-radius:12px;background:var(--va-accent);color:#fff;font-size:16px;font-weight:700;padding:15px;cursor:pointer;font-family:inherit}
  .va-devis .va-btn:disabled{opacity:.6;cursor:default}
  .va-devis .va-fine{text-align:center;font-size:12px;opacity:.6;margin:10px 0 0}
  .va-devis .va-success{display:none;text-align:center;background:#fff;border:1.5px solid var(--va-accent);border-radius:16px;padding:40px 24px;max-width:520px;margin:0 auto}
  .va-devis .va-check{width:54px;height:54px;border-radius:50%;background:var(--va-accent);color:#fff;font-size:28px;display:flex;align-items:center;justify-content:center;margin:0 auto 14px}
  .va-devis .va-success h3{margin:0 0 8px;font-size:22px}
  .va-devis .va-success p{margin:0;opacity:.75}
  </style>
  <div class="va-wrap">
    <div class="va-head">
      <p class="va-eyebrow">Votre site, en ligne</p>
      <h2>Configurez votre projet</h2>
      <p class="va-sub">Choisissez votre formule et vos options. On vous recontacte avec un devis précis et le lien d'activation.</p>
    </div>
    <form class="va-form" id="va-form">
      <fieldset class="va-block">
        <legend>Formule</legend>
        <div class="va-grid-2">
          <label class="va-card"><input type="radio" name="type" value="vitrine" checked><span class="va-card-t">Site vitrine</span><span class="va-card-d">Comme cet aperçu</span></label>
          <label class="va-card"><input type="radio" name="type" value="ecom"><span class="va-card-t">Boutique en ligne</span><span class="va-card-d">Vente en ligne</span></label>
        </div>
      </fieldset>
      <fieldset class="va-block">
        <legend>Options</legend>
        <div class="va-grid-opts" id="va-opts"></div>
        <div class="va-lang" id="va-lang" hidden>
          <span>Nombre de langues</span>
          <span class="va-step"><button type="button" data-d="-1" aria-label="moins">-</button><b id="va-langv">2</b><button type="button" data-d="1" aria-label="plus">+</button></span>
        </div>
      </fieldset>
      <fieldset class="va-block">
        <legend>Abonnement mensuel</legend>
        <div class="va-grid-3" id="va-tiers"></div>
      </fieldset>
      <div class="va-estimate">
        <div><span class="va-est-lbl">Estimation</span><span class="va-est-note" id="va-level">-</span></div>
        <div class="va-est-val"><b id="va-creation">-</b> <small>creation</small> &middot; <b id="va-monthly">-</b><small>/mois</small></div>
      </div>
      <button type="submit" class="va-btn" id="va-submit">Demander mon devis</button>
      <p class="va-fine">Devis sans engagement. Montants indicatifs, confirmes avant tout paiement.</p>
    </form>
    <div class="va-success" id="va-success">
      <div class="va-check">&#10003;</div>
      <h3>Devis envoye !</h3>
      <p>Merci, nous revenons vers vous tres vite avec le lien d'activation securise.</p>
    </div>
  </div>
  <script src="/demo-assets/devis.js?v=20261019a" defer></script>
</section>
"""


def _sec_devis(plan: SitePlan, esc) -> str:
    """Configurateur de devis, injecté dans la démo (POST /api/quote/{slug})."""
    return DEVIS_SECTION_TEMPLATE.replace("{{SLUG}}", esc(plan.slug))


_SECTION_RENDERERS = {
    "flagship_split": lambda plan, esc, **kw: _sec_flagship(plan, esc),
    "services_numbered": lambda plan, esc, **kw: _sec_services_numbered(plan, esc),
    "services_accordion": lambda plan, esc, **kw: _sec_services_accordion(plan, esc),
    "process_steps": lambda plan, esc, **kw: _sec_process(plan, esc),
    "story_values": lambda plan, esc, **kw: _sec_story(plan, esc),
    "audience": lambda plan, esc, **kw: _sec_audience(plan, esc),
    "zone_cities": lambda plan, esc, **kw: _sec_zone(plan, esc),
    "faq": lambda plan, esc, **kw: _sec_faq(plan, esc),
    "gallery": lambda plan, esc, **kw: _sec_gallery(plan, esc),
    "final_cta": lambda plan, esc, **kw: _sec_final_cta(plan, esc=esc, **kw),
    "devis": lambda plan, esc, **kw: _sec_devis(plan, esc),
}


def render_page(
    *,
    plan: SitePlan,
    address: str,
    phone_display: str,
    tel: str,
    maps: str,
    banner: str,
    rating_badge: str,
    reviews_section: str,
    hours_section: str,
    meta_description: str,
    json_ld: str,
    esc,
) -> str:
    """Assemble full HTML from a unique SitePlan."""
    font_display = _font_css(plan.font_pair)
    sticky = (
        f'<a class="demo-sticky-call" href="tel:{esc(tel)}" aria-label="Appeler {esc(phone_display)}">'
        f'<span>Appeler</span><strong>{esc(phone_display)}</strong></a>'
        if tel
        else ""
    )

    sections_html = []
    # Ensure prestations id exists once
    seen_prestations = False
    for kind in plan.section_order:
        fn = _SECTION_RENDERERS.get(kind)
        if not fn:
            continue
        html = fn(
            plan,
            esc,
            tel=tel,
            phone_display=phone_display,
            maps=maps,
            address=address,
        )
        if kind in ("flagship_split", "services_numbered", "services_accordion"):
            if seen_prestations:
                html = html.replace(' id="prestations"', "", 1)
            seen_prestations = True
        sections_html.append(html)

    # If plan forgot services entirely, inject numbered
    if not seen_prestations:
        sections_html.insert(0, _sec_services_numbered(plan, esc))

    # Section devis (configurateur) — toujours présente, même si le plan ne l'ordonne pas
    if "devis" not in plan.section_order:
        sections_html.append(_sec_devis(plan, esc))

    body_sections = "\n".join(sections_html)
    if reviews_section:
        body_sections += "\n" + reviews_section
    if hours_section:
        body_sections += "\n" + hours_section

    return f"""<!DOCTYPE html>
<html lang="fr" data-template="{esc(plan.palette_id)}" data-hero="{esc(plan.hero_variant)}">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="robots" content="noindex, nofollow">
  <title>{esc(plan.name)} – {esc(plan.trade_label)} | {esc(plan.positioning)}</title>
  <meta name="description" content="{esc(meta_description or plan.meta_description)}">
  <link rel="stylesheet" href="/demo-assets/demo.css?v=20261017w">
  <script type="application/ld+json">{json_ld}</script>
  <style>
    :root {{
      --demo-accent: {plan.accent};
      --demo-accent-soft: {plan.accent_soft};
      --demo-ink: {plan.ink};
      --demo-paper: {plan.paper};
      --demo-display: {font_display};
    }}
  </style>
</head>
<body class="demo-body demo-palette-{esc(plan.palette_id)} demo-variant-{esc(plan.hero_variant)}" data-content-source="{esc(plan.content_source)}" data-plan-slug="{esc(plan.slug)}">
  {banner}
  {_hero(plan, rating_badge=rating_badge, address=address, tel=tel, esc=esc)}
  {body_sections}
  <footer class="demo-footer">
    <div class="demo-wrap demo-footer-inner">
      <p>© {esc(plan.name)} — Tous droits réservés</p>
      <p>{esc(phone_display)}</p>
    </div>
  </footer>
  {sticky}
</body>
</html>
"""


# Back-compat shim if anything still imports TradeProfile render (unused)
def render_page_legacy(**kwargs):  # pragma: no cover
    raise RuntimeError("Use render_page(plan=SitePlan, ...)")
