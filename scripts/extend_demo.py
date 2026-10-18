#!/usr/bin/env python3
"""Prolonge la durée de vie d'une démo : python scripts/extend_demo.py SLUG --days 4"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import demo_ttl  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Prolonger une démo ArtisanVisible.")
    parser.add_argument("slug", help="Slug de la démo (ex. grindfit)")
    parser.add_argument("--days", type=int, default=None, help="Jours à ajouter (défaut DEMO_TTL_DAYS)")
    parser.add_argument("--sites-dir", type=Path, default=None)
    parser.add_argument("--archive-dir", type=Path, default=None)
    args = parser.parse_args()

    if args.sites_dir:
        import os

        os.environ["SITES_DIR"] = str(args.sites_dir)
    if args.archive_dir:
        import os

        os.environ["DEMO_ARCHIVE_DIR"] = str(args.archive_dir)

    try:
        result = demo_ttl.extend_demo(
            args.slug,
            days=args.days,
            root=args.sites_dir,
            archive_root=args.archive_dir,
        )
    except (FileNotFoundError, ValueError) as e:
        print(f"Erreur : {e}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
