"""Refresh prices and statements for held securities, on demand.

Prices are the **previous close**, read from the exchanges' own BhavCopy files
(and AMFI NAVs for mutual funds). Screener is visited only for the statements
behind the scorecard — its page price is an intraday quote, not a published
close, and is deliberately not used.

This is a thin wrapper over :func:`app.market_refresh.refresh_client`, which is
the same code path the upload route and the nightly job take, so a manual run
and an automatic one cannot drift apart.

    python -m scripts.fetch_cmp                 # every holding in the database
    python -m scripts.fetch_cmp --client 17     # one client only
    python -m scripts.fetch_cmp --max-age 30    # skip securities refreshed <30 min ago
    python -m scripts.fetch_cmp --dry-run       # resolve symbols, fetch nothing
    python -m scripts.fetch_cmp --no-schedules  # skip the extra balance-sheet requests
"""

import argparse
import os
import sys

# Allow `python scripts/fetch_cmp.py` as well as `python -m scripts.fetch_cmp`
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import create_tables                                  # noqa: E402
from app.market_refresh import DEFAULT_DELAY, DEFAULT_WORKERS, refresh_client  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--client", type=int, help="Only holdings of this client id.")
    p.add_argument("--delay", type=float, default=DEFAULT_DELAY,
                   help=f"Seconds between Screener requests (default {DEFAULT_DELAY}).")
    p.add_argument("--max-age", type=float, default=0,
                   help="Skip securities refreshed within this many minutes.")
    p.add_argument("--limit", type=int, help="Stop after this many securities.")
    p.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                   help=f"Parallel fetches (default {DEFAULT_WORKERS}).")
    p.add_argument("--no-schedules", action="store_true",
                   help="Skip the extra balance-sheet schedule requests.")
    p.add_argument("--dry-run", action="store_true",
                   help="Resolve symbols and report, fetch nothing.")
    args = p.parse_args()

    create_tables()
    report = refresh_client(
        client_id=args.client,
        delay=args.delay,
        max_age=args.max_age,
        with_schedules=not args.no_schedules,
        limit=args.limit,
        workers=args.workers,
        dry_run=args.dry_run,
        progress=lambda message: print(message, flush=True),
    )

    priced = len(report.get("priced") or [])
    unresolved = len(report.get("unresolved") or [])
    failed = len(report.get("failed") or [])
    print(f"\npriced {priced} at previous close | {unresolved} unresolved | {failed} failed")
    if report.get("price_error"):
        print(f"prices unchanged: {report['price_error']}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
