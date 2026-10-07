"""Re-parse stored statements and rewrite their holdings with the current parser.

Use this after a parser fix, to correct data that was ingested by an older
version — for example aggregate rows ("Net Worth", "Stocks") that were being
counted as securities and multiplying a portfolio's value.

    python -m scripts.reparse_holdings --dry-run      # show what would change
    python -m scripts.reparse_holdings               # apply to every account
    python -m scripts.reparse_holdings --client 23    # one client only

Only accounts whose original file is still in uploads/ can be re-parsed; the
rest are listed as skipped.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select

from app.database import SessionLocal, create_tables
from app.models import Account, Client, Holding
from app.parsers.detector import detect_and_parse
from app.sector_map import lookup_sector

UPLOAD_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "uploads")


def parse_args():
    p = argparse.ArgumentParser(description="Re-parse stored statements with the current parser.")
    p.add_argument("--client", type=int, help="Limit to one client id.")
    p.add_argument("--dry-run", action="store_true", help="Report changes without writing.")
    return p.parse_args()


def main():
    args = parse_args()
    create_tables()
    db = SessionLocal()
    try:
        query = select(Account)
        if args.client is not None:
            query = query.where(Account.client_id == args.client)
        accounts = db.execute(query).scalars().all()

        changed = skipped = 0
        print(f"{len(accounts)} account(s) in scope\n")
        for account in accounts:
            client = db.get(Client, account.client_id)
            path = os.path.join(UPLOAD_DIR, account.source_file or "")
            label = f"[{account.client_id}:{(client.name if client else '?')[:16]}] {(account.source_file or '?')[:38]}"

            if not account.source_file or not os.path.isfile(path):
                print(f"  SKIP  {label:60} source file not in uploads/")
                skipped += 1
                continue

            existing = db.execute(
                select(Holding).where(Holding.account_id == account.id)
            ).scalars().all()
            old_n = len(existing)
            old_total = sum(h.current_value or 0 for h in existing)

            try:
                parsed = detect_and_parse(path, account.source_file)
            except Exception as exc:
                print(f"  ERROR {label:60} {type(exc).__name__}: {exc}")
                skipped += 1
                continue

            new_n = len(parsed.holdings)
            new_total = sum(h.current_value for h in parsed.holdings)
            delta = new_total - old_total

            # Columns added after an upload (isin, quantity, exchange codes) stay
            # NULL on old rows even when the totals are unchanged, which silently
            # blocks price resolution. Treat missing identifiers as needing repair.
            stored_ids = sum(1 for h in existing if h.isin or h.quantity or h.nse_symbol or h.bse_code)
            fresh_ids = sum(1 for h in parsed.holdings
                            if getattr(h, "isin", "") or getattr(h, "quantity", 0)
                            or getattr(h, "nse_symbol", "") or getattr(h, "bse_code", ""))
            missing_ids = fresh_ids > stored_ids

            if new_n == old_n and abs(delta) < 0.01 and not missing_ids:
                print(f"  same  {label:60} {new_n:3} holdings, {new_total:,.2f}")
                continue

            reason = ("identifiers" if (new_n == old_n and abs(delta) < 0.01) else "values")
            print(f"  FIX   {label:60} {old_n:3} -> {new_n:3} holdings, "
                  f"{old_total:,.2f} -> {new_total:,.2f}  ({delta:+,.2f})  [{reason}"
                  + (f": ids {stored_ids}->{fresh_ids}]" if missing_ids else "]"))
            changed += 1
            if args.dry_run:
                continue

            # Replace the holdings wholesale; the statement is the source of truth
            for h in existing:
                db.delete(h)
            db.flush()

            for ph in parsed.holdings:
                sector = ph.sector
                if sector == "Unknown":
                    mapped = lookup_sector(ph.security_name)[0]
                    sector = mapped if mapped != "Others" else "Others"
                db.add(Holding(
                    account_id=account.id,
                    security_name=ph.security_name,
                    current_value=ph.current_value,
                    total_cost=ph.total_cost,
                    gain_pct=ph.gain_pct,
                    weight_pct=ph.weight_pct,
                    sector=sector,
                    isin=getattr(ph, "isin", "") or None,
                    quantity=getattr(ph, "quantity", 0) or None,
                    nse_symbol=getattr(ph, "nse_symbol", "") or None,
                    bse_code=getattr(ph, "bse_code", "") or None,
                ))
            account.has_cost_data = parsed.has_cost_data
            account.raw_markdown = parsed.raw_markdown
            account.raw_json = parsed.raw_json
            # The cached AI narratives described the old, wrong numbers
            account.overview_json = None
            account.performance_json = None
            account.insights_json = None
            account.risk_analysis_json = None
            db.commit()

        print(f"\n  changed={changed} skipped={skipped}")
        if args.dry_run:
            print("  (dry run — nothing written)")
        elif changed:
            print("  AI narratives cleared for changed accounts; they regenerate on next view.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
