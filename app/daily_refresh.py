"""Daily fundamentals refresh for every security any client actually holds.

**Non-redundant by construction.** The work is keyed on the security, not on the
holding: clients between them hold ~1,000 holding rows covering only ~116
distinct companies, and a company's statements are identical whoever owns them.
So each company is fetched once per run into the single ``market_data`` row that
every client's holdings join against — one row per security, no per-client
duplication of fundamentals. A security held by nine clients is fetched once and
read nine times.

Securities nobody holds any more are skipped rather than deleted, so a client
re-uploading an old statement still finds its data without a refetch.
"""

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import MarketData
from app.market_data import build_lookup, match_holding
from app.market_refresh import distinct_securities
from app import yahoo_fundamentals as yahoo

log = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

# Yahoo publishes annual statements once a year, so refetching a company whose
# figures were read hours ago buys nothing. A daily run with this window still
# repairs anything that failed yesterday.
DEFAULT_MAX_AGE_HOURS = 20


def securities_held(db: Session, client_id: int = None) -> list:
    """The distinct ``market_data`` rows that current holdings point at.

    ``distinct_securities`` already collapses the holding rows to one entry per
    security with the best identifiers found on any of them; this resolves those
    to the stored rows through the same matcher the dashboard values with, so a
    refresh and a page view can never disagree about which row a holding means.
    """
    market_rows = db.execute(select(MarketData)).scalars().all()
    by_isin, by_code, by_name = build_lookup(market_rows)

    class _Probe:
        """Minimal stand-in carrying just what match_holding reads."""
        def __init__(self, entry):
            self.isin = entry.get("isin")
            self.security_name = entry.get("name")

    seen, out = set(), []
    for entry in distinct_securities(db, client_id):
        row, _ = match_holding(_Probe(entry), by_isin, by_code, by_name)
        if row is None or row.id in seen:
            continue
        seen.add(row.id)
        out.append(row)
    return out


def _is_fresh(row: MarketData, max_age_hours: int) -> bool:
    if not row.yahoo_json or row.yahoo_as_of is None:
        return False
    stamp = row.yahoo_as_of
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - stamp) < timedelta(hours=max_age_hours)


def refresh_fundamentals(max_age_hours: int = DEFAULT_MAX_AGE_HOURS,
                         workers: int = 4,
                         limit: int = 0,
                         force: bool = False,
                         client_id: int = None,
                         progress=None) -> dict:
    """Pull Yahoo statements for every held security into its market_data row.

    Returns a summary dict. Fetching runs on a pool; every database write
    happens on this thread, because a SQLAlchemy Session is not thread-safe.
    """
    db = SessionLocal()
    try:
        targets = securities_held(db, client_id)
        if not force:
            targets = [r for r in targets if not _is_fresh(r, max_age_hours)]
        if limit:
            targets = targets[:limit]

        total = len(targets)
        if progress:
            progress(f"{total} securities to refresh")
        if not total:
            return {"checked": 0, "updated": 0, "no_data": 0, "failed": 0, "names": []}

        jobs = [(r.id, r.name or "", r.nse_code or "", r.bse_code or "") for r in targets]

        def work(job):
            row_id, name, nse_code, bse_code = job
            try:
                statements = yahoo.fetch_fundamentals(nse_code, bse_code)
                # The classification is worth having even for securities whose
                # statements Yahoo does not carry, so it is fetched either way.
                profile = yahoo.fetch_profile(nse_code, bse_code)
                return row_id, name, statements, profile, None
            except Exception as exc:                      # noqa: BLE001 - one bad symbol must not end the run
                return row_id, name, None, None, repr(exc)

        updated = no_data = failed = tagged = 0
        names: list = []
        now = datetime.now(timezone.utc)

        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            for index, (row_id, name, payload, profile, error) in enumerate(
                    pool.map(work, jobs), start=1):
                row = db.get(MarketData, row_id)
                if profile:
                    row.yahoo_industry = profile.get("industry") or row.yahoo_industry
                    row.yahoo_sector = profile.get("sector") or row.yahoo_sector
                    tagged += 1
                if error:
                    failed += 1
                    log.warning("Yahoo fetch failed for %s: %s", name, error)
                elif payload is None:
                    no_data += 1
                else:
                    row.yahoo_json = json.dumps(payload)
                    row.yahoo_as_of = now
                    row.yahoo_symbol = payload.get("symbol")
                    updated += 1
                    names.append(name)
                if progress and (index % 10 == 0 or index == total):
                    progress(f"  {index}/{total} — {updated} updated, "
                             f"{no_data} without Yahoo data, {failed} failed")
        db.commit()
        return {"checked": total, "updated": updated, "no_data": no_data,
                "failed": failed, "tagged": tagged, "names": names}
    finally:
        db.close()


def run_daily(progress=None) -> dict:
    """One scheduled pass: fundamentals now, prices through the existing path."""
    started = datetime.now(IST)
    if progress:
        progress(f"Daily refresh started {started:%Y-%m-%d %H:%M} IST")

    result = refresh_fundamentals(progress=progress)

    # Prices keep running through the module that already owns them, so the CMP
    # source stays whatever is configured there rather than changing under foot.
    # client_id=None covers every holding in one pass.
    try:
        from app.market_refresh import refresh_client
        result["prices"] = refresh_client(client_id=None, progress=progress)
    except Exception as exc:                              # noqa: BLE001
        log.warning("Price refresh failed: %r", exc)
        result["prices"] = f"failed: {exc!r}"

    if progress:
        progress(f"Daily refresh finished in "
                 f"{(datetime.now(IST) - started).total_seconds():.0f}s")
    return result
