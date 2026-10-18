#!/usr/bin/env python3
"""Create or overwrite one demo site at a fixed slug (diversity fixtures)."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import lead_sites  # noqa: E402
from trade_profiles import format_fr_phone, normalize_inputs  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--trade", required=True)
    ap.add_argument("--city", required=True)
    ap.add_argument("--phone", required=True)
    ap.add_argument("--sites-dir", type=Path, default=Path(os.environ.get("SITES_DIR", "/var/www/sites")))
    ap.add_argument("--no-llm", action="store_true")
    args = ap.parse_args(argv)

    if args.no_llm:
        os.environ.pop("XAI_API_KEY", None)

    name, city, phone = normalize_inputs(args.name, args.city, args.phone)
    place = lead_sites.place_from_form(name, city, phone)
    cfg = lead_sites.Config()
    slug = args.slug.strip().lower()
    site_dir = args.sites_dir / slug
    site_dir.mkdir(parents=True, exist_ok=True)
    checkout = f"{cfg.api_base or cfg.preview_base}/checkout/{slug}"
    html = lead_sites.render_preview(
        place,
        checkout,
        cfg,
        args.trade,
        slug=slug,
        allow_llm=not args.no_llm,
    )
    lead_sites.atomic_write_text(site_dir / "index.html", html)
    lead_sites.atomic_write_text(site_dir / "index.preview.html", html)
    meta = {
        "place_id": place["id"],
        "slug": slug,
        "name": name,
        "trade": args.trade,
        "phone": format_fr_phone(phone) or phone,
        "address": city,
        "preview_url": f"{cfg.preview_base}/{slug}/",
        "checkout_url": checkout,
        "source": "seed",
        "fictional": True,
    }
    import re

    m = re.search(r'data-content-source="([^"]+)"', html)
    meta["content_source"] = m.group(1) if m else "unknown"
    lead_sites.atomic_write_text(site_dir / "meta.json", json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
    print(f"OK {slug} → {meta['preview_url']} (source={meta['content_source']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
