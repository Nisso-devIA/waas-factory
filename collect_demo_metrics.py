#!/usr/bin/env python3
"""Collect scratch demo metrics + estimated xAI $ cost (grok-4.7)."""
from __future__ import annotations

import json
import re
from pathlib import Path

# grok-4.7 short-context (docs.x.ai): $2/1M input, $6/1M output
IN_PER_M = 2.0
OUT_PER_M = 6.0

SLUGS = [
    "grindfit",
    "atelier-force-lyon",
    "volt-services-cergy",
    "plomberie-chauffage-morel-fils",
    "jardins-verrier-nantes",
]


def usd(prompt: int, completion: int, total: int) -> float:
    if prompt or completion:
        return prompt / 1e6 * IN_PER_M + completion / 1e6 * OUT_PER_M
    if total:
        # stream sometimes omits prompt — treat total as output (conservative upper)
        return total / 1e6 * OUT_PER_M
    return 0.0


def main() -> None:
    rows = []
    print(
        f"{'slug':40} {'source':24} {'dur_s':>8} {'prompt':>8} "
        f"{'compl':>8} {'total':>8} {'usd':>8} note"
    )
    for s in SLUGS:
        meta_path = Path(f"/var/www/sites/{s}/meta.json")
        m = json.loads(meta_path.read_text())
        gm = m.get("gen_metrics") or {}
        src = m.get("content_source") or gm.get("content_source") or "?"
        pt = int(gm.get("prompt_tokens") or 0)
        ct = int(gm.get("completion_tokens") or 0)
        tt = int(gm.get("total_tokens") or (pt + ct) or 0)
        cost = usd(pt, ct, tt)
        note = ""
        if gm.get("fallback"):
            note = f"FALLBACK:{gm.get('fallback_reason') or '?'}"
        idx = Path(f"/var/www/sites/{s}/index.html")
        html_src = "?"
        if idx.exists():
            head = idx.read_text(errors="replace")[:3000]
            mm = re.search(r'data-content-source="([^"]+)"', head)
            if mm:
                html_src = mm.group(1)
        print(
            f"{s:40} {str(src)[:24]:24} {gm.get('duration_s') or '-':>8} "
            f"{pt:>8} {ct:>8} {tt:>8} {cost:>8.4f} {note}"
        )
        print(f"  html_source={html_src} model={gm.get('model')}")
        rows.append(
            {
                "slug": s,
                "source": src,
                "html_source": html_src,
                "duration_s": gm.get("duration_s"),
                "prompt_tokens": pt,
                "completion_tokens": ct,
                "total_tokens": tt,
                "usd_est": round(cost, 5),
                "fallback": bool(gm.get("fallback")),
                "fallback_reason": gm.get("fallback_reason") or "",
                "model": gm.get("model") or "grok-4.7",
            }
        )
    scratch_ok = sum(1 for r in rows if "scratch" in str(r["source"]) and not r["fallback"])
    print(f"\nSCRATCH_OK={scratch_ok}/{len(rows)}")
    print(f"TOTAL_USD_EST={sum(r['usd_est'] for r in rows):.4f}")
    Path("/tmp/demo_metrics.json").write_text(json.dumps(rows, indent=2))
    print("wrote /tmp/demo_metrics.json")


if __name__ == "__main__":
    main()
