"""Refresh prices and fundamentals for a client's holdings from Screener.in.

Shared by the CLI (``scripts/fetch_cmp.py``) and the upload router, so a manual
run and an automatic post-upload run behave identically.

Resolution order per security, best first:

1. **ISIN** against NSE's published equity master — the only unambiguous mapping.
2. **NSE symbol** printed on the statement, if it carries one.
3. **BSE code** printed on the statement — the only route to a BSE-only listing.
4. **Company name** against the NSE master.

Every candidate is tried in turn, because a code can be valid yet 404 on
Screener (a delisted NSE symbol, say) while another still resolves.
"""

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.database import SessionLocal
from app.models import Account, Holding, MarketData
from app.market_data import normalise_name
from app.official_prices import OfficialPrices
from app.screener_scraper import (
    ScreenerClient,
    SymbolResolver,
    ratio_value,
    DEFAULT_DELAY,
)

log = logging.getLogger(__name__)

# Auto-refresh after an upload. Off by default so a first run is never a
# surprise; the upload router reads this.
AUTO_REFRESH = os.getenv("SCREENER_AUTO_REFRESH", "true").strip().lower() in (
    "1", "true", "yes", "on")
# Don't re-scrape a security priced within this window (minutes)
AUTO_REFRESH_MAX_AGE = float(os.getenv("SCREENER_AUTO_REFRESH_MAX_AGE", "720"))
# Parallel workers. The client's rate limiter still caps the global request
# rate, so this only overlaps network round-trips; Screener is not hit harder.
DEFAULT_WORKERS = int(os.getenv("SCREENER_WORKERS", "4"))
# Statements change quarterly while prices change by the minute, so the extra
# schedule requests are skipped while the stored statements are still fresh.
FUNDAMENTALS_MAX_AGE_DAYS = float(os.getenv("SCREENER_FUNDAMENTALS_MAX_AGE_DAYS", "7"))
# CMP is the previous close, taken from the exchanges' own BhavCopy files
# (and AMFI NAVs for funds). Screener is never read for a price: it is kept
# only for the statements behind the scorecard. A published close is the
# figure a client can reconcile against a contract note, and three file
# downloads price the whole book rather than one request per security.


def distinct_securities(db, client_id=None) -> list:
    """One entry per security, carrying the best identifiers found on any row."""
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
        if h.isin and not entry["isin"]:
            entry["isin"] = h.isin
            entry["name"] = h.security_name
        # Any row of the same security may be the one carrying an exchange code
        if h.nse_symbol and not entry["nse_symbol"]:
            entry["nse_symbol"] = h.nse_symbol
        if h.bse_code and not entry["bse_code"]:
            entry["bse_code"] = h.bse_code
    return list(securities.values())


def upsert_price(db, *, isin, symbol, name, cmp_value, ratios, financials, now,
                 schedules_refreshed: bool = False, price_source: str = None):
    """Write price and statements onto the matching MarketData row.

    Matching mirrors the dashboard's own lookup order so a scraped price lands
    on the row the portfolio actually reads.
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
    if cmp_value is not None:
        row.cmp = cmp_value
    if price_source:
        row.price_source = price_source
    row.source_file = price_source or "screener.in"
    row.as_of = now

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

    if financials:
        payload = dict(financials)
        payload["top_ratios"] = ratios

        # A price-only refresh parses the page's statements but does NOT re-fetch
        # the balance-sheet schedules. Writing the payload as-is would discard
        # schedules already collected, losing the current ratio and forcing a
        # full re-fetch next run. Carry them over instead, and leave the
        # statements' timestamp alone so staleness tracking stays honest.
        if not payload.get("schedules") and row.fundamentals_json:
            try:
                previous = json.loads(row.fundamentals_json)
            except (json.JSONDecodeError, TypeError):
                previous = {}
            kept = previous.get("schedules")
            if kept:
                payload["schedules"] = kept

        row.fundamentals_json = json.dumps(payload)
        # Only advance the statements' timestamp when they were actually refetched
        if schedules_refreshed or row.fundamentals_as_of is None:
            row.fundamentals_as_of = now
    return row


def _fresh_keys(db, cutoff) -> set:
    """Identifiers of securities already priced after ``cutoff``."""
    fresh = set()
    for row in db.execute(select(MarketData)).scalars().all():
        if row.cmp is None or row.as_of is None:
            continue
        stamp = row.as_of if row.as_of.tzinfo else row.as_of.replace(tzinfo=timezone.utc)
        if stamp >= cutoff:
            for key in (row.isin, row.nse_code):
                if key:
                    fresh.add(key.upper())
    return fresh


def _needs_fundamentals(db, sec, code, cutoff) -> bool:
    """True when this security's stored statements are missing or stale."""
    row = None
    isin = (sec.get("isin") or "").strip().upper()
    if isin:
        row = db.execute(select(MarketData).where(MarketData.isin == isin)).scalars().first()
    if row is None and code:
        row = db.execute(select(MarketData).where(MarketData.nse_code == code)).scalars().first()
    if row is None:
        key = normalise_name(sec["name"])
        if key:
            row = db.execute(select(MarketData).where(MarketData.match_key == key)).scalars().first()
    if row is None or not row.fundamentals_json or row.fundamentals_as_of is None:
        return True
    try:
        payload = json.loads(row.fundamentals_json)
    except (json.JSONDecodeError, TypeError):
        return True
    if not payload.get("schedules"):
        return True
    stamp = row.fundamentals_as_of
    stamp = stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
    return stamp < cutoff


def refresh_client(client_id=None, *, delay: float = DEFAULT_DELAY, max_age: float = 0,
                   with_schedules: bool = True, limit: int = None,
                   workers: int = DEFAULT_WORKERS,
                   dry_run: bool = False, progress=None) -> dict:
    """Fetch prices (and statements) for one client's holdings, or all holdings.

    Prices are the previous close from the exchanges' BhavCopy files; Screener
    is visited only for the statements behind the scorecard.

    Returns a report dict. Never raises for a single security's failure — one
    unreachable page must not abandon the rest of the portfolio.
    """
    def say(msg):
        if progress:
            progress(msg)
        else:
            log.info(msg)

    db = SessionLocal()
    now = datetime.now(timezone.utc)
    report = {"securities": 0, "resolved": 0, "unresolved": [], "priced": [],
              "failed": [], "skipped": 0, "dry_run": dry_run}
    try:
        securities = distinct_securities(db, client_id)
        report["securities"] = len(securities)
        if not securities:
            return report

        resolver = SymbolResolver.load()

        resolved = []
        for sec in securities:
            cands, reason = resolver.candidates(
                sec["isin"] or "", sec["name"],
                sec.get("nse_symbol") or "", sec.get("bse_code") or "")
            if cands:
                resolved.append({**sec, "candidates": cands,
                                 "code": cands[0][0], "how": cands[0][1]})
            else:
                report["unresolved"].append({"name": sec["name"], "reason": reason})

        # One page fetch per security even when held in several accounts
        deduped, seen = [], {}
        for sec in resolved:
            if sec["code"] in seen:
                seen[sec["code"]]["holdings"] += sec["holdings"]
                continue
            seen[sec["code"]] = sec
            deduped.append(sec)
        resolved = deduped
        report["resolved"] = len(resolved)

        if max_age > 0:
            cutoff = now - timedelta(minutes=max_age)
            fresh = _fresh_keys(db, cutoff)
            kept = []
            for sec in resolved:
                ident = {(sec["isin"] or "").upper(), sec["code"].upper()} - {""}
                if ident & fresh:
                    report["skipped"] += 1
                else:
                    kept.append(sec)
            resolved = kept

        if limit:
            resolved = resolved[:limit]
        if dry_run:
            report["priced"] = [{"name": s["name"], "code": s["code"], "how": s["how"]}
                                for s in resolved]
            return report
        if not resolved:
            return report

        # ── Prices: previous close, from the exchanges' own files ──
        # The only source of CMP. If the files cannot be downloaded the run
        # leaves the stored prices alone rather than falling back to a scrape,
        # so a price on the dashboard is always a published close.
        official = None
        try:
            official = OfficialPrices.load()
            report["official"] = official.as_dict()
        except Exception as exc:
            log.warning("official price files unavailable, prices left unchanged: %s", exc)
            report["price_error"] = str(exc)

        if official is not None:
            unpriced = []
            for sec in resolved:
                # The statement does not always carry an exchange symbol, but by
                # this point the security has resolved to one; falling back to it
                # prices holdings whose statement listed only a name.
                price, source = official.lookup(
                    sec.get("isin") or "", sec["name"],
                    sec.get("nse_symbol") or sec.get("code") or "",
                    sec.get("bse_code") or "")
                if price is None:
                    unpriced.append(sec["name"])
                    continue
                upsert_price(db, isin=(sec["isin"] or "").upper() or None,
                             symbol=sec["code"], name=sec["name"], cmp_value=price,
                             ratios={}, financials=None, now=now, price_source=source)
                report["priced"].append({"name": sec["name"], "code": sec["code"],
                                         "how": source, "cmp": price})
            db.commit()
            report["unpriced"] = unpriced
            say(f"priced {len(report['priced'])} at previous close"
                + (f"; {len(unpriced)} not in the close files" if unpriced else ""))

        # Screener is now only visited for statements, never for a price, so the
        # list narrows to securities whose stored financials have gone stale.
        # A security the close files could not price is not fetched for a price
        # it would not be allowed to use.
        resolved = [sec for sec in resolved
                    if _needs_fundamentals(db, sec, sec["code"],
                                           now - timedelta(days=FUNDAMENTALS_MAX_AGE_DAYS))]

        if not resolved:
            return report

        # Re-fetch the extra balance-sheet schedules only where the stored
        # statements are missing or stale; a price alone costs one request.
        fundamentals_cutoff = now - timedelta(days=FUNDAMENTALS_MAX_AGE_DAYS)
        for sec in resolved:
            sec["want_schedules"] = bool(with_schedules) and _needs_fundamentals(
                db, sec, sec["code"], fundamentals_cutoff)
        wanting = sum(1 for s2 in resolved if s2["want_schedules"])
        requests_total = len(resolved) + wanting * 3
        workers = max(1, int(workers))
        report["requests"] = requests_total
        report["needing_statements"] = wanting
        say(f"fetching {len(resolved)} securities ({requests_total} requests, "
            f"{wanting} need statements) at {1 / delay:.1f} req/s over {workers} workers "
            f"~ {requests_total * delay / 60:.1f} min")

        client = ScreenerClient(delay=delay)

        def work(sec):
            """Worker thread: network only, never touches the database."""
            result, tried = None, []
            for code, how in sec["candidates"]:
                attempt = client.fetch_company(code)
                tried.append(f"{code}({how})")
                result = attempt
                if attempt.ok:
                    sec["code"], sec["how"] = code, how
                    break
            if result is not None and result.ok and result.basis:
                result.financials["basis"] = result.basis
            if (result is not None and result.ok and sec["want_schedules"]
                    and result.schedule_targets):
                schedules = client.fetch_schedules(
                    result.company_id, result.schedule_targets,
                    referer=f"https://www.screener.in/company/{sec['code']}/")
                if schedules:
                    result.financials["schedules"] = schedules
            return sec, result, tried

        done = 0
        # Fetch in parallel; write from this thread only, because one SQLAlchemy
        # session must not be shared across threads.
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for sec, result, tried in pool.map(work, resolved):
                done += 1
                if result is None or not result.ok:
                    error = (result.error if result else "no candidate code")
                    if len(tried) > 1:
                        error = f"{error} (tried {', '.join(tried)})"
                    report["failed"].append({"name": sec["name"], "error": error})
                    say(f"  [{done}/{len(resolved)}] {sec['name'][:34]} FAILED - {error}")
                    continue

                # Statements only. The price on this page is an intraday quote,
                # not a published close, so it is deliberately discarded —
                # cmp_value=None leaves whatever the BhavCopy pass stored.
                upsert_price(db, isin=(sec["isin"] or "").upper() or None,
                             symbol=sec["code"], name=sec["name"],
                             cmp_value=None,
                             ratios=result.ratios, financials=result.financials, now=now,
                             schedules_refreshed=bool(result.financials.get("schedules")),
                             price_source=None)
                db.commit()
                say(f"  [{done}/{len(resolved)}] {sec['code']:12} {'statements':>12}  "
                    f"{sec['name'][:32]}")

        # ── Gap fill ──
        # Screener has no page for a mutual fund, so those securities never even
        # resolve to a code. AMFI publishes their NAV, and NSE/BSE close covers
        # anything whose Screener page failed. This only fills blanks; it never
        # overwrites a live CMP already fetched above.
        priced_names = {p["name"] for p in report["priced"]}
        gaps = [g for g in ([{"name": u["name"], "isin": "", "nse_symbol": "", "bse_code": ""}
                             for u in report["unresolved"]]
                            + [{"name": f["name"], "isin": "", "nse_symbol": "", "bse_code": ""}
                               for f in report["failed"]])
                if g["name"] not in priced_names]
        if gaps:
            by_name = {sec["name"]: sec for sec in securities}
            # A mutual fund never resolves to a Screener code, so it is absent
            # from the pass above and only reachable here, by AMFI NAV.
            filler = official
            if filler is None:
                try:
                    filler = OfficialPrices.load()
                except Exception as exc:
                    log.warning("official gap-fill unavailable: %s", exc)
                    filler = None
            if filler is not None:
                filled = 0
                for gap in gaps:
                    sec = by_name.get(gap["name"], gap)
                    price, source = filler.lookup(
                        sec.get("isin") or "", sec["name"],
                        sec.get("nse_symbol") or "", sec.get("bse_code") or "")
                    if price is None:
                        continue
                    upsert_price(db, isin=(sec.get("isin") or "").upper() or None,
                                 symbol=sec.get("code") or None, name=sec["name"],
                                 cmp_value=price, ratios={}, financials=None,
                                 now=now, price_source=source)
                    report["priced"].append({"name": sec["name"],
                                             "code": sec.get("code") or "-",
                                             "how": source, "cmp": price})
                    filled += 1
                if filled:
                    db.commit()
                    report["gap_filled"] = filled
                    say(f"filled {filled} price(s) for instruments with no Screener "
                        f"page at all (mutual funds / unlisted)")
        return report
    except Exception as exc:
        log.error("market refresh failed for client %s: %s", client_id, exc)
        report["failed"].append({"name": "(run)", "error": str(exc)})
        return report
    finally:
        db.close()


def refresh_client_background(client_id: int) -> None:
    """Entry point for the post-upload background task."""
    if not AUTO_REFRESH:
        log.info("SCREENER_AUTO_REFRESH is off; skipping market refresh")
        return
    report = refresh_client(client_id, max_age=AUTO_REFRESH_MAX_AGE)
    log.info("market refresh for client %s: priced=%d skipped=%d failed=%d unresolved=%d",
             client_id, len(report["priced"]), report["skipped"],
             len(report["failed"]), len(report["unresolved"]))
