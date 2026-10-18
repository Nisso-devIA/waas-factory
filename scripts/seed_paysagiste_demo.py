#!/usr/bin/env python3
"""Seed a fictive paysagiste demo for scratch-mode visual diversity tests."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import lead_sites  # noqa: E402

SLUG = "jardins-verrier-nantes"


def main() -> int:
    os.environ.setdefault("DEMO_MODE", "scratch")
    out = Path(os.environ.get("SITES_DIR", "/var/www/sites")).resolve()
    cfg = lead_sites.Config()
    place = lead_sites.place_from_form(
        "Jardins Verrier",
        "Nantes",
        "0240123456",
        email="contact@jardins-verrier-demo.fr",
    )
    checkout = lead_sites.checkout_url(place, SLUG, cfg, dry_run=False)
    site_dir = out / SLUG
    site_dir.mkdir(parents=True, exist_ok=True)
    html = lead_sites.render_preview(
        place, checkout, cfg, "paysagiste", slug=SLUG, allow_llm=True
    )
    lead_sites.atomic_write_text(site_dir / "index.html", html)
    lead_sites.atomic_write_text(site_dir / "index.preview.html", html)
    metrics = lead_sites.last_render_metrics()
    meta = {
        "place_id": place.get("id"),
        "slug": SLUG,
        "name": "Jardins Verrier",
        "trade": "paysagiste",
        "phone": "02 40 12 34 56",
        "email": "contact@jardins-verrier-demo.fr",
        "address": "Nantes",
        "preview_url": f"{cfg.preview_base}/{SLUG}/",
        "checkout_url": checkout,
        "source": "seed-fictif",
        "content_source": metrics.get("content_source"),
        "demo_mode": metrics.get("mode"),
        "gen_metrics": {
            k: metrics.get(k)
            for k in (
                "duration_s",
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "attempts",
                "model",
                "fallback",
                "fallback_reason",
            )
        },
    }
    lead_sites.atomic_write_text(
        site_dir / "meta.json", json.dumps(meta, ensure_ascii=False, indent=2) + "\n"
    )
    print(json.dumps({"slug": SLUG, "metrics": metrics}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
