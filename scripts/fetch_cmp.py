"""Fetch current market prices from Screener.in for the stocks you hold.

Only public company pages are requested, and every URL is checked against
Screener's robots.txt before it is fetched. No login or credentials are used.

Usage
-----
    python -m scripts.fetch_cmp                 # every holding in the database
    python -m scripts.fetch_cmp --client 17     # one client only
    python -m scripts.fetch_cmp --max-age 30    # skip prices fetched <30 min ago
    python -m scripts.fetch_cmp --dry-run       # resolve symbols, fetch nothing
    python -m scripts.fetch_cmp --delay 3       # be gentler (default 2s)

Exit code is 0 when every resolvable holding was priced, 1 otherwise.
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

# Allow `python scripts/fetch_cmp.py` as well as `python -m scripts.fetch_cmp`
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select

from app.database import SessionLocal, create_tables
from app.models import Account, Holding, MarketData
from app.market_data import normalise_name
from app.screener_scraper import (
    ScreenerClient,
    SymbolResolver,
    ratio_value,
    DEFAULT_DELAY,
)


def parse_args():
    p = argparse.ArgumentParser(description="Fetch CMP from Screener.in for held stocks.")
    p.add_argument("--client", type=int, help="Only holdings of this client id.")
    p.add_argument("--delay", type=float, default=DEFAULT_DELAY,
                   help=f"Seconds between requests (default {DEFAULT_DELAY}).")
    p.add_argument("--max-age", type=float, default=0,
                   help="Skip securities priced within this many minutes (default 0 = refetch all).")
    p.add_argument("--limit", type=int, help="Stop after this many securities (for testing).")
    p.add_argument("--dry-run", action="store_true",
                   help="Resolve symbols and report, but make no Screener requests.")
    p.add_argument("--refresh-symbols", action="store_true",
                   help="Force a re-download of the NSE equity master.")
    p.add_argument("--workers", type=int, default=None,
                   help="Parallel fetch workers (default 5). The global request rate is "
                        "still capped by --delay, so this only overlaps network latency.")
    p.add_argument("--no-schedules", action="store_true",
                   help="Skip the balance-sheet schedule requests (faster, but no current ratio).")
    p.add_argument("--verbose", "-v", action="store_true")
    return p.parse_args()


def distinct_securities(db, client_id=None):
    """One entry per security across all accounts, carrying the best ISIN found.

    The same stock can appear in several accounts; it only needs fetching once.
    """
    query = select(Holding)
    if client_id is not None:
        account_ids = [
            a.id for a in db.execute(
                select(Account).where(Account.client_id == client_id)
            ).scalars().all()
        ]
        if not account_ids:
            return []
        query = query.where(Holding.account_id.in_(account_ids))

    securities = {}
    for h in db.execute(query).scalars().all():
        key = (h.isin or "").strip().upper() or normalise_name(h.security_name)
        if not key:
            continue
        entry = securities.setdefault(key, {
            "name": h.security_name, "isin": h.isin, "holdings": 0,
            "nse_symbol": h.nse_symbol or "", "bse_code": h.bse_code or "",
        })
        entry["holdings"] += 1
        # Prefer a row that actually carries an ISIN
        if h.isin and not entry["isin"]:
            entry["isin"] = h.isin
            entry["name"] = h.security_name
        # Any row may be the one that carried an exchange code
        if h.nse_symbol and not entry["nse_symbol"]:
            entry["nse_symbol"] = h.nse_symbol
        if h.bse_code and not entry["bse_code"]:
            entry["bse_code"] = h.bse_code
    return list(securities.values())


def upsert_price(db, *, isin, symbol, name, cmp_value, ratios, financials, now):
    """Write the price onto the matching MarketData row, creating one if needed.

    Matching mirrors the dashboard's own lookup order (ISIN, then code, then
    normalised name) so a scraped price lands on the row the portfolio reads.
    """
    row = None
    if isin:
        row = db.execute(select(MarketData).where(MarketData.isin == isin)).scalars().first()
    if row is None and symbol:
        row = db.execute(select(MarketData).where(MarketData.nse_code == symbol)).scalars().first()
    if row is None:
        key = normalise_name(name)
        if key:
            row = db.execute(select(MarketData).where(MarketData.match_key == key)).scalars().first()
    if row is None:
        row = MarketData()
        db.add(row)

    row.isin = isin or row.isin
    row.nse_code = symbol or row.nse_code
    row.name = name or row.name
    row.match_key = normalise_name(name) or row.match_key
    row.cmp = cmp_value
    row.source_file = "screener.in"
    row.as_of = now

    # These come free with the same page request, so keep them current too.
    for field, label in (
        ("market_cap", "Market Cap"),
        ("pe", "Stock P/E"),
        ("book_value", "Book Value"),
        ("dividend_yield", "Dividend Yield"),
        ("roce", "ROCE"),
        ("roe", "ROE"),
        ("face_value", "Face Value"),
    ):
        value = ratio_value(ratios, label)
        if value is not None:
            setattr(row, field, value)

    # Statements for the 34-point checklist, captured from the same page request.
    # The header ratio block is stored with them so the checklist has one input.
    if financials:
        payload = dict(financials)
        payload["top_ratios"] = ratios
        row.fundamentals_json = json.dumps(payload)
        row.fundamentals_as_of = now
    return row


def main():
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(message)s",
    )

    create_tables()
    db = SessionLocal()
    now = datetime.now(timezone.utc)

    try:
        securities = distinct_securities(db, args.client)
        if not securities:
            print("No holdings found." if args.client is None
                  else f"No holdings found for client {args.client}.")
            return 0

        print(f"{len(securities)} distinct securities in scope.")

        print("Resolving Screener codes from NSE's equity master…")
        try:
            resolver = SymbolResolver.load(force_refresh=args.refresh_symbols)
        except Exception as exc:
            print(f"ERROR: could not load the NSE equity master: {exc}")
            return 1
        print(f"  {len(resolver.by_isin)} ISINs / {len(resolver.symbols)} symbols indexed.")

        # ── Resolve first, so the run's real scope is known before any request ──
        resolved, unresolved = [], []
        for sec in securities:
            cands, reason = resolver.candidates(
                sec["isin"] or "", sec["name"],
                sec.get("nse_symbol") or "", sec.get("bse_code") or "")
            if cands:
                resolved.append({**sec, "code": cands[0][0], "how": cands[0][1],
                                 "candidates": cands})
            else:
                unresolved.append({**sec, "reason": reason})

        # The same stock can enter twice — e.g. held in a demat account (keyed by
        # ISIN) and a PMS account (keyed by name). Fetch each Screener page once.
        deduped, seen_codes = [], {}
        for sec in resolved:
            if sec["code"] in seen_codes:
                seen_codes[sec["code"]]["holdings"] += sec["holdings"]
                # Keep an ISIN if this duplicate is the one that has it
                if sec["isin"] and not seen_codes[sec["code"]]["isin"]:
                    seen_codes[sec["code"]]["isin"] = sec["isin"]
                continue
            entry = dict(sec)
            seen_codes[sec["code"]] = entry
            deduped.append(entry)
        merged = len(resolved) - len(deduped)
        resolved = deduped

        print(f"  resolved {len(resolved)}, unresolved {len(unresolved)}"
              + (f" ({merged} duplicate page fetches avoided)" if merged else ""))

        # ── Skip anything priced recently ──
        skipped = []
        if args.max_age > 0:
            cutoff = now - timedelta(minutes=args.max_age)
            fresh = set()
            for row in db.execute(select(MarketData)).scalars().all():
                if row.cmp is None or row.as_of is None:
                    continue
                stamp = row.as_of if row.as_of.tzinfo else row.as_of.replace(tzinfo=timezone.utc)
                if stamp >= cutoff:
                    for key in (row.isin, row.nse_code):
                        if key:
                            fresh.add(key)
            still, skipped = [], []
            for sec in resolved:
                if (sec["isin"] or "").upper() in fresh or sec["code"] in fresh:
                    skipped.append(sec)
                else:
                    still.append(sec)
            resolved = still
            if skipped:
                print(f"  {len(skipped)} already priced within {args.max_age:g} min — skipping")

        if args.limit:
            resolved = resolved[:args.limit]

        if args.dry_run:
            print("\n--- DRY RUN: nothing fetched ---")
            for sec in resolved[:40]:
                print(f"  would fetch {sec['code']:14} ({sec['how']:6}) {sec['name'][:40]}")
            if unresolved:
                print(f"\n  unresolved ({len(unresolved)}):")
                for sec in unresolved[:40]:
                    print(f"    {sec['name'][:44]:46} {sec['reason']}")
            return 0

        if not resolved:
            print("\nNothing to fetch.")
            return 0

        # ── Fetch ──
        print(f"\nFetching {len(resolved)} prices from Screener "
              f"({args.delay:g}s apart, ~{len(resolved) * args.delay / 60:.1f} min)…")
        try:
            client = ScreenerClient(delay=args.delay)
        except RuntimeError as exc:
            print(f"ERROR: {exc}")
            return 1

        ok, failed = [], []
        for index, sec in enumerate(resolved, start=1):
            result = None
            tried = []
            for cand_code, cand_how in sec.get("candidates") or [(sec["code"], sec["how"])]:
                attempt = client.fetch_company(cand_code)
                tried.append(f"{cand_code}({cand_how})")
                if attempt.ok:
                    result = attempt
                    sec["code"], sec["how"] = cand_code, cand_how
                    break
                result = attempt   # keep the last error for reporting
            if result is None:
                failed.append((sec, "no candidate code resolved"))
                continue
            if not result.ok and len(tried) > 1:
                result.error = f"{result.error} (tried {', '.join(tried)})"
            if result.ok and not args.no_schedules and result.schedule_targets:
                # The balance-sheet schedules carry the line items a current ratio
                # needs (Inventories, Trade receivables, Trade Payables ...).
                schedules = client.fetch_schedules(
                    result.company_id, result.schedule_targets,
                    referer=f"https://www.screener.in/company/{sec['code']}/")
                if schedules:
                    result.financials["schedules"] = schedules
            if result.ok:
                upsert_price(db, isin=(sec["isin"] or "").upper() or None, symbol=sec["code"],
                             name=sec["name"], cmp_value=result.cmp,
                             ratios=result.ratios, financials=result.financials, now=now)
                db.commit()
                ok.append((sec, result))
                sched = len(result.financials.get("schedules") or {})
                print(f"  [{index}/{len(resolved)}] {sec['code']:14} ₹{result.cmp:>12,.2f}  "
                      f"{sec['name'][:30]:32} schedules:{sched}")
            else:
                failed.append((sec, result.error))
                print(f"  [{index}/{len(resolved)}] {sec['code']:14} {'FAILED':>13}  "
                      f"{sec['name'][:34]} — {result.error}")

        # ── Report ──
        print("\n" + "=" * 62)
        print(f"  priced        {len(ok)}")
        if skipped:
            print(f"  skipped fresh {len(skipped)}")
        if failed:
            print(f"  failed        {len(failed)}")
            for sec, err in failed:
                print(f"     {sec['code']:14} {sec['name'][:34]:36} {err}")
        if unresolved:
            print(f"  unresolved    {len(unresolved)}  (no Screener page — funds, bonds, unlisted)")
            for sec in unresolved:
                print(f"     {sec['name'][:44]:46} {sec['reason']}")
        print("=" * 62)

        return 0 if not failed else 1

    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
