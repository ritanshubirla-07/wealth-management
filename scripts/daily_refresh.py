"""Refresh every held security's fundamentals. The 01:00 IST job, standalone.

The API process runs this on its own schedule (``app/scheduler.py``), but that
thread only ticks while the server is up. Point Windows Task Scheduler at this
script to have the refresh happen regardless:

    schtasks /create /tn "VIKABH daily fundamentals" /sc daily /st 01:00 ^
      /tr "cmd /c cd /d C:\\Users\\Ritansu\\Documents\\vikabh-wealth\\wealth-management ^
           && .venv\\Scripts\\python.exe scripts\\daily_refresh.py >> logs\\daily_refresh.log 2>&1"

The machine's clock must be IST for /st 01:00 to mean 01:00 IST.

Usage:
    python scripts/daily_refresh.py                # fundamentals + prices
    python scripts/daily_refresh.py --no-prices    # fundamentals only
    python scripts/daily_refresh.py --force        # ignore the freshness window
    python scripts/daily_refresh.py --limit 5      # first 5 securities, for a smoke test
"""

import argparse
import logging
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.daily_refresh import IST, refresh_fundamentals, run_daily  # noqa: E402
from app.database import create_tables                              # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--no-prices", action="store_true",
                        help="refresh statements only, leave CMP alone")
    parser.add_argument("--force", action="store_true",
                        help="refetch even securities read within the freshness window")
    parser.add_argument("--limit", type=int, default=0, help="cap the number of securities")
    parser.add_argument("--workers", type=int, default=4, help="parallel fetches (default 4)")
    parser.add_argument("--quiet", action="store_true", help="only print the summary")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    create_tables()

    def say(message):
        if not args.quiet:
            print(message, flush=True)

    started = datetime.now(IST)
    say(f"VIKABH fundamentals refresh — {started:%Y-%m-%d %H:%M:%S} IST")

    if args.no_prices:
        result = refresh_fundamentals(workers=args.workers, limit=args.limit,
                                      force=args.force, progress=say)
    else:
        result = run_daily(progress=say)

    elapsed = (datetime.now(IST) - started).total_seconds()
    print(f"\nchecked={result.get('checked')} updated={result.get('updated')} "
          f"no_data={result.get('no_data')} failed={result.get('failed')} "
          f"in {elapsed:.0f}s")

    # Non-zero only when nothing could be fetched at all, so Task Scheduler
    # reports a genuine outage but tolerates the odd unreachable symbol.
    if result.get("checked") and not result.get("updated"):
        print("every fetch failed — check connectivity", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
