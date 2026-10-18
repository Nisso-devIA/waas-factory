#!/usr/bin/env python3
"""Remove content/near-dup images from Rennes; refresh themed contact chrome."""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, "/opt/waas-factory")
from scratch_site import (  # noqa: E402
    _dedupe_page_images,
    _ensure_contact_block,
    _image_identity_key,
    _inject_platform,
)

SITES = Path("/var/www/sites")


def polish(slug: str, name: str, phone: str, tel: str, city: str) -> None:
    p = SITES / slug / "index.html"
    html = p.read_text(encoding="utf-8")
    before = len(re.findall(r"<img\b", html, re.I))
    html = _dedupe_page_images(html)
    # Also drop orphan empty figure/article shells left after img removal
    html = re.sub(
        r"(?is)<(figure|article)\b[^>]*>\s*(?:<(?:figcaption|h[2-4]|p)[^>]*>.*?</(?:figcaption|h[2-4]|p)>\s*)*</\1>",
        "",
        html,
    )
    html = re.sub(
        r"(?is)<(figure|article)\b[^>]*>\s*</\1>",
        "",
        html,
    )
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
    # Force themed contact card
    html = re.sub(
        r'(?is)<section\b[^>]*id=["\']contact["\'][^>]*>.*?</section\s*>',
        "",
        html,
    )
    html = _ensure_contact_block(
        html, name=name, phone_display=phone, tel=tel, city=city
    )
    after = len(re.findall(r"<img\b", html, re.I))
    # identity uniqueness check
    srcs = re.findall(r"""(?is)<img\b[^>]*\bsrc\s*=\s*["']([^"']+)["']""", html)
    keys = [_image_identity_key(s) for s in srcs]
    assert len(keys) == len(set(keys)), (srcs, keys)
    assert "av-contact-card" in html
    assert "var(--pine" in html or "var(--leaf" in html  # themed chrome present
    p.write_text(html, encoding="utf-8")
    print(f"{slug}: imgs {before}->{after}; keys={keys}")


if __name__ == "__main__":
    # Rennes is the blocking fix
    polish(
        "test-paysagiste-avant-apres-rennes",
        "Vert Clair Rennes",
        "02 98 76 54 32",
        "+33298765432",
        "Rennes",
    )
    # Contact theming refresh (allowed) on Lyon + Toulouse
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
