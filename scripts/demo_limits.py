#!/usr/bin/env python3
"""Admin anti-abus démo : reset e-mail / IP, purge, stats."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import demo_limits  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Limites demandes de démo (admin).")
    parser.add_argument("--reset", metavar="EMAIL_OR_IP", help="Reset blocage e-mail ou IP")
    parser.add_argument("--purge", action="store_true", help="Purge journaux IP / champs > rétention")
    parser.add_argument("--stats", action="store_true", help="Compteurs process + hits IP")
    args = parser.parse_args()

    if args.purge:
        n_hits = demo_limits.purge_ip_hits()
        n_req = demo_limits.purge_old_request_ip_fields()
        print(json.dumps({"purged_ip_hits": n_hits, "purged_request_fields": n_req}))
        return 0

    if args.stats:
        hits = demo_limits.load_ip_hits()
        print(
            json.dumps(
                {
                    "ip_hits": len(hits),
                    "block_counter": demo_limits.get_block_counter(),
                    "email_refused_days": demo_limits.EMAIL_REFUSED_COOLDOWN_DAYS,
                    "ip_limit_24h": demo_limits.IP_LIMIT_24H,
                    "ip_limit_30d": demo_limits.IP_LIMIT_30D,
                },
                indent=2,
            )
        )
        return 0

    if args.reset:
        target = args.reset.strip()
        if "@" in target:
            n = demo_limits.reset_email(target)
            print(json.dumps({"reset_email": target, "requests_updated": n}))
        else:
            n = demo_limits.reset_ip(target)
            print(json.dumps({"reset_ip": "(hash/ip)", "hits_removed": n}))
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
