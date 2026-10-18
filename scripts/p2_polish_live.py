#!/usr/bin/env python3
"""Surgical P2 polish on Lyon + Toulouse demos (Osny untouched)."""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, "/opt/waas-factory")
from scratch_site import (  # noqa: E402
    _ensure_contact_block,
    _inject_platform,
    _repair_truncated_list_items,
    _strip_empty_sections,
)

SITES = Path("/var/www/sites")


def polish(slug: str, name: str, phone: str, tel: str, city: str) -> None:
    p = SITES / slug / "index.html"
    html = p.read_text(encoding="utf-8")
    html = _strip_empty_sections(html)
    html = _repair_truncated_list_items(html)
    # Drop broken truncation fragments before contact inject
    html = re.sub(
        r"(?is)<li>\s*<strong>\s*Revenir\s+entre\s*</strong>\s*</section>.*?(?=<section\b)",
        "",
        html,
        count=1,
    )
    html = re.sub(r"(?is)</li>\s*</section>\s*</div>\s*(?=<section\b)", "</li></ul></div></section>\n", html)
    html = _inject_platform(
        html,
        name=name,
        phone_display=phone,
        tel=tel,
        checkout=f"https://visibleartisan.fr/checkout/{slug}",
        price_label="49€/mois",
        banner_html="",
        gallery_caption="",
        meta_description="",
        json_ld="",
        catalog=[],
    )
    if "av-contact-card" not in html:
        html = re.sub(
            r'(?is)<section\b[^>]*id=["\']contact["\'][^>]*>.*?</section\s*>',
            "",
            html,
            count=1,
        )
        html = _ensure_contact_block(
            html, name=name, phone_display=phone, tel=tel, city=city
        )
    else:
        html = _ensure_contact_block(
            html, name=name, phone_display=phone, tel=tel, city=city
        )
    html = _repair_truncated_list_items(html)
    html = _strip_empty_sections(html)
    # Ensure contact card styling even if Grok left an unstyled block
    if 'id="contact"' in html and "av-contact-card" not in html:
        html = re.sub(
            r'(?is)<section\b[^>]*id=["\']contact["\'][^>]*>.*?</section\s*>',
            "",
            html,
            count=1,
        )
        html = _ensure_contact_block(
            html, name=name, phone_display=phone, tel=tel, city=city
        )
    p.write_text(html, encoding="utf-8")
    print(
        "polished",
        slug,
        "empty_faq=",
        "souvent au comptoir" in html,
        "cue=",
        "Vos photos de chantier" in html,
        "card=",
        "av-contact-card" in html,
        "plus_boxes=",
        "demo-gallery-empty" in html,
    )


if __name__ == "__main__":
    polish(
        "test-patisserie-galerie-lyon",
        "Atelier Sucré Lyon",
        "04 78 12 34 56",
        "+33478123456",
        "Lyon",
    )
    polish(
        "test-plombier-urgences-toulouse",
        "Flash Plomberie Toulouse",
        "05 61 23 45 67",
        "+33561234567",
        "Toulouse",
    )
    print("OK")
