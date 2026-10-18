#!/usr/bin/env python3
"""Regenerate one demo site in place from meta.json (+ optional Places-like fields)."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

# Ensure DEMO_MODE defaults to scratch for regenerations unless overridden
os.environ.setdefault("DEMO_MODE", "scratch")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import lead_sites  # noqa: E402
from trade_profiles import format_fr_phone, normalize_inputs, title_case_fr  # noqa: E402


def place_from_meta(meta: dict, mock_place: dict | None = None) -> dict:
    """
    Rebuild a place dict for re-render. Rating / review count are copied only when the
    place_id is an authentic Google Places id (never from form_* or mock ChIJ_test_*).
    """
    if mock_place and mock_place.get("id") == meta.get("place_id"):
        place = dict(mock_place)
    else:
        place = {
            "id": meta.get("place_id") or f"form_{meta.get('slug')}",
            "displayName": {"text": meta.get("name") or meta.get("slug") or "Entreprise"},
            "formattedAddress": meta.get("address") or "",
            "nationalPhoneNumber": meta.get("phone") or "",
        }
        if meta.get("maps_url"):
            place["googleMapsUri"] = meta["maps_url"]

    # Strip seeded/mock social proof unless id is a real Places id
    if lead_sites.is_authentic_google_place_id(place.get("id")):
        if mock_place is None:
            if meta.get("rating") is not None:
                place["rating"] = meta["rating"]
            if meta.get("reviews_count") is not None:
                place["userRatingCount"] = meta["reviews_count"]
        # mock_place may carry rating — still gated by authentic id check in render
    else:
        place.pop("rating", None)
        place.pop("userRatingCount", None)
        place.pop("reviews", None)

    name, city, phone = normalize_inputs(
        lead_sites.display_name(place),
        place.get("formattedAddress") or meta.get("address") or "",
        place.get("nationalPhoneNumber")
        or place.get("internationalPhoneNumber")
        or meta.get("phone")
        or "",
    )
    addr = place.get("formattedAddress") or ""
    if addr and "," not in addr:
        place["formattedAddress"] = title_case_fr(addr) or city
    place["displayName"] = {"text": name if name else lead_sites.display_name(place)}
    if phone:
        place["nationalPhoneNumber"] = phone
    return place


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Régénère une démo HTML in-place")
    ap.add_argument("slug")
    ap.add_argument(
        "--sites-dir",
        type=Path,
        default=Path(os.environ.get("SITES_DIR", "/var/www/sites")),
    )
    ap.add_argument("--mock", type=Path, help="Optional mock.json to enrich Google-like fields")
    ap.add_argument("--trade", help="Override trade from meta")
    ap.add_argument("--no-llm", action="store_true", help="Skip xAI even if XAI_API_KEY is set")
    args = ap.parse_args(argv)

    site_dir = args.sites_dir / args.slug
    meta_path = site_dir / "meta.json"
    if not meta_path.is_file():
        print(f"meta.json introuvable: {meta_path}", file=sys.stderr)
        return 1

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    trade = (args.trade or meta.get("trade") or "service local").strip()

    mock_place = None
    if args.mock and args.mock.is_file():
        data = json.loads(args.mock.read_text(encoding="utf-8"))
        for p in data.get("places") or []:
            if p.get("id") == meta.get("place_id"):
                mock_place = p
                break

    if args.no_llm:
        os.environ.pop("XAI_API_KEY", None)

    place = place_from_meta(meta, mock_place)
    cfg = lead_sites.Config()
    checkout = meta.get("checkout_url") or lead_sites.checkout_url(
        place, args.slug, cfg, dry_run=False
    )

    html_out = lead_sites.render_preview(
        place,
        checkout,
        cfg,
        trade,
        slug=args.slug,
        allow_llm=not args.no_llm,
    )
    lead_sites.atomic_write_text(site_dir / "index.html", html_out)
    lead_sites.atomic_write_text(site_dir / "index.preview.html", html_out)

    name = lead_sites.display_name(place)
    phone = place.get("nationalPhoneNumber") or meta.get("phone") or ""
    meta["name"] = name
    meta["trade"] = trade
    meta["phone"] = format_fr_phone(str(phone)) or phone
    meta["address"] = place.get("formattedAddress") or meta.get("address")
    metrics = lead_sites.last_render_metrics()
    m = re.search(r'data-content-source="([^"]+)"', html_out)
    meta["content_source"] = metrics.get("content_source") or (m.group(1) if m else "unknown")
    meta["demo_mode"] = metrics.get("mode") or os.environ.get("DEMO_MODE") or "scratch"
    meta["gen_metrics"] = {
        k: metrics[k]
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
        if k in metrics
    }
    # Drop seeded mock ratings from meta when place is not a real Google Places id
    if not lead_sites.has_google_places_rating(place):
        meta.pop("rating", None)
        meta.pop("reviews_count", None)
    lead_sites.atomic_write_text(meta_path, json.dumps(meta, ensure_ascii=False, indent=2) + "\n")

    gm = meta["gen_metrics"]
    print(
        f"OK {args.slug} → {cfg.preview_base}/{args.slug}/ "
        f"({trade}, source={meta['content_source']}, "
        f"mode={meta['demo_mode']}, "
        f"{gm.get('duration_s', '?')}s, tokens={gm.get('total_tokens', 0)})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
