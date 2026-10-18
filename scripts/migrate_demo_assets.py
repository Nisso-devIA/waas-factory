#!/usr/bin/env python3
"""
Rewrite existing artisan demo HTML to drop Tailwind CDN in favour of
/demo-assets/demo.css (strict CSP). Safe to re-run.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

CDN_RE = re.compile(
    r'<script[^>]*src=["\']https://cdn\.tailwindcss\.com[^"\']*["\'][^>]*>\s*</script>\s*',
    re.IGNORECASE,
)
LINK_TAG = '<link rel="stylesheet" href="/demo-assets/demo.css">'


def migrate_html(text: str) -> tuple[str, bool]:
    if "/demo-assets/demo.css" in text and "cdn.tailwindcss.com" not in text:
        return text, False
    new = CDN_RE.sub("", text)
    if "/demo-assets/demo.css" not in new:
        # insert stylesheet after <head> or before </head>
        if "<head>" in new:
            new = new.replace("<head>", "<head>\n  " + LINK_TAG, 1)
        elif "</head>" in new:
            new = new.replace("</head>", "  " + LINK_TAG + "\n</head>", 1)
        else:
            new = LINK_TAG + "\n" + new
    changed = new != text
    return new, changed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--sites-dir",
        type=Path,
        default=Path("/var/www/sites"),
        help="Sites root (default /var/www/sites)",
    )
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    root: Path = args.sites_dir
    updated = 0
    skipped = 0
    for index in sorted(root.glob("*/index.html")):
        # skip vitrine / special dirs
        if index.parent.name.startswith("_"):
            skipped += 1
            continue
        raw = index.read_text(encoding="utf-8")
        new, changed = migrate_html(raw)
        if not changed:
            skipped += 1
            continue
        print(f"{'DRY ' if args.dry_run else ''}UPDATE {index}")
        if not args.dry_run:
            tmp = index.with_name(".index.html.tmp")
            tmp.write_text(new, encoding="utf-8")
            tmp.replace(index)
        updated += 1
    # merci
    merci = root / "merci" / "index.html"
    if merci.is_file():
        raw = merci.read_text(encoding="utf-8")
        new, changed = migrate_html(raw)
        # also ensure merci.js
        if "cdn.tailwindcss.com" in raw or "<script>" in raw and "merci.js" not in raw:
            # prefer shipping from package; if still old, rewrite fully not here
            pass
        if changed and not args.dry_run:
            tmp = merci.with_name(".index.html.tmp")
            tmp.write_text(new, encoding="utf-8")
            tmp.replace(merci)
            print(f"UPDATE {merci}")
            updated += 1
        elif changed:
            print(f"DRY UPDATE {merci}")
            updated += 1
    print(f"done updated={updated} skipped={skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
