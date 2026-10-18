#!/usr/bin/env python3
"""Mint or rotate the private photo-upload token for an activated slug."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import photo_upload  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Mint upload token for activated artisan site")
    p.add_argument("slug", help="Site slug under SITES_DIR")
    p.add_argument("--rotate", action="store_true", help="Force a new token")
    p.add_argument(
        "--sites-dir",
        default=os.getenv("SITES_DIR", "/var/www/sites"),
        help="Sites directory (default SITES_DIR or /var/www/sites)",
    )
    args = p.parse_args(argv)

    sites_dir = Path(args.sites_dir).resolve()
    site_dir = sites_dir / args.slug
    if not site_dir.is_dir():
        print(f"ERROR: site not found: {site_dir}", file=sys.stderr)
        return 1
    if not (site_dir / ".activated").exists():
        print(f"ERROR: site not activated: {args.slug}", file=sys.stderr)
        return 2

    token = photo_upload.ensure_upload_token(site_dir, sites_dir, rotate=args.rotate)
    url = photo_upload.upload_url(args.slug, token)
    print(url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
