#!/usr/bin/env python3
"""Migration one-shot : expires_at = now + DEMO_TTL_DAYS pour démos existantes."""
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
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--days", type=int, default=None)
    parser.add_argument("--sites-dir", type=Path, default=None)
    args = parser.parse_args()
    if args.sites_dir:
        import os

        os.environ["SITES_DIR"] = str(args.sites_dir)
    result = demo_ttl.migrate_existing_ttls(
        root=args.sites_dir, days=args.days, dry_run=args.dry_run
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
