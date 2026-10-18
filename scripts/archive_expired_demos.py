#!/usr/bin/env python3
"""Archive quotidienne des démos expirées (hors web, récupérables). Idempotent."""
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
    parser = argparse.ArgumentParser(description="Archive les démos expirées non activées.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--sites-dir", type=Path, default=None)
    parser.add_argument("--archive-dir", type=Path, default=None)
    args = parser.parse_args()

    if args.sites_dir:
        import os

        os.environ["SITES_DIR"] = str(args.sites_dir)
    if args.archive_dir:
        import os

        os.environ["DEMO_ARCHIVE_DIR"] = str(args.archive_dir)

    result = demo_ttl.archive_expired_demos(
        root=args.sites_dir,
        archive_root=args.archive_dir,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result.get("errors") else 0


if __name__ == "__main__":
    raise SystemExit(main())
