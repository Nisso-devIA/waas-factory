#!/usr/bin/env python3
from collections import Counter
from pathlib import Path
import json
import re
import sys

sys.path.insert(0, "/opt/waas-factory")
from photo_banks import assert_images_allowed_for_trade, resolve_trade_images

CHECKS = [
    ("grindfit", "coach sportif"),
    ("atelier-force-lyon", "coach sportif"),
    ("volt-services-cergy", "électricien"),
    ("plomberie-chauffage-morel-fils", "plombier"),
    ("jardins-verrier-nantes", "paysagiste"),
    ("patisserie", "Patissier"),
]

IN, OUT = 2.0, 6.0
print("electricien bank", resolve_trade_images("électricien"))
rows = []
for slug, trade in CHECKS:
    html = Path(f"/var/www/sites/{slug}/index.html").read_text(errors="replace")
    meta = json.loads(Path(f"/var/www/sites/{slug}/meta.json").read_text())
    gm = meta.get("gen_metrics") or {}
    names = re.findall(r"/([a-z0-9-]+\.jpg)", html)
    dups = [n for n, c in Counter(names).items() if c > 1]
    assert_images_allowed_for_trade(trade, names)
    bad_lock = [
        n
        for n in names
        if n
        in (
            "electricien-03.jpg",
            "electricien-07.jpg",
            "electricien-04.jpg",
            "electricien-05.jpg",
        )
    ]
    has_header_gallery = bool(re.search(r'id="realisations"[^>]*>\s*<header', html, re.I))
    internal = bool(
        re.search(
            r"(?i)remplacez par vos|espace client\s*/\s*upload|client upload",
            html,
        )
    )
    src_m = re.search(r'data-content-source="([^"]+)"', html)
    src = src_m.group(1) if src_m else meta.get("content_source")
    pt = int(gm.get("prompt_tokens") or 0)
    ct = int(gm.get("completion_tokens") or 0)
    tt = int(gm.get("total_tokens") or pt + ct)
    usd = pt / 1e6 * IN + ct / 1e6 * OUT if (pt or ct) else tt / 1e6 * OUT
    print(
        f"{slug}: src={src} dups={dups} lock={bad_lock} "
        f"gallery_header={has_header_gallery} internal={internal} "
        f"dur={gm.get('duration_s')} tok={tt} usd={usd:.4f}"
    )
    assert not dups, dups
    assert not bad_lock
    assert not has_header_gallery
    assert not internal
    assert "scratch" in str(src), src
    rows.append(
        {
            "slug": slug,
            "source": src,
            "duration_s": gm.get("duration_s"),
            "prompt_tokens": pt,
            "completion_tokens": ct,
            "total_tokens": tt,
            "usd_est": round(usd, 5),
            "fallback": bool(gm.get("fallback")),
            "model": gm.get("model") or "grok-4.7",
            "expires_at": meta.get("expires_at"),
        }
    )

print("ALL_QA_CHECKS_OK")
Path("/tmp/demo_metrics.json").write_text(
    json.dumps(
        {
            "pricing_note": "grok-4.7: $2/1M in + $6/1M out",
            "demos": rows,
            "scratch_success": f"{sum(1 for r in rows if 'scratch' in str(r['source']))}/{len(rows)}",
            "total_usd": round(sum(r['usd_est'] for r in rows), 4),
        },
        indent=2,
    )
)
print("wrote /tmp/demo_metrics.json total_usd", sum(r["usd_est"] for r in rows))
