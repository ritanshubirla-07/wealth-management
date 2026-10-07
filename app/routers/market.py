"""Market-data endpoints backed by Screener.in CSV exports.

Screener.in offers no API, so the flow is: export a screen to CSV from Screener
(premium "Export" button), then POST that file here. The import is idempotent —
re-importing the same universe refreshes prices rather than duplicating rows.
"""

import json
import logging
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, UploadFile, HTTPException, Query
from sqlalchemy import select, delete
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Client, Account, Holding, MarketData
from app.market_data import (
    parse_screener_csv,
    build_lookup,
    match_holding,
    tier_for_rank,
    normalise_name,
)

router = APIRouter(prefix="/market", tags=["Market Data"])
log = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # a 50-column export of all listed Indian equities is ~2 MB


@router.post("/import")
async def import_screener_csv(
    file: UploadFile = File(...),
    replace: bool = Query(
        True,
        description="Replace the existing market-data universe. False merges into it.",
    ),
    db: Session = Depends(get_db),
):
    """Ingest a Screener.in CSV export.

    Returns a report of what was recognised so an unexpected export shape is
    visible immediately rather than silently producing empty columns.
    """
    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File larger than 20 MB.")
    if not raw.strip():
        raise HTTPException(status_code=400, detail="File is empty.")

    # Screener exports are UTF-8; fall back for files re-saved by Excel
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise HTTPException(status_code=400, detail="Could not decode file as text.")

    parsed = parse_screener_csv(text)
    if parsed["error"]:
        raise HTTPException(status_code=400, detail=parsed["error"])
    if not parsed["rows"]:
        raise HTTPException(
            status_code=400,
            detail="Header row was found but it had no data rows beneath it.",
        )

    safe_name = os.path.basename(file.filename or "screener.csv")
    now = datetime.now(timezone.utc)

    if replace:
        db.execute(delete(MarketData))
        db.commit()

    existing = {}
    if not replace:
        for row in db.execute(select(MarketData)).scalars().all():
            for key in (row.isin, row.nse_code, row.match_key):
                if key:
                    existing.setdefault(key, row)

    written = 0
    for record in parsed["rows"]:
        target = None
        if not replace:
            for key in (record.get("isin"), record.get("nse_code"), record.get("match_key")):
                if key and key in existing:
                    target = existing[key]
                    break
        if target is None:
            target = MarketData()
            db.add(target)

        for field, value in record.items():
            if hasattr(target, field):
                setattr(target, field, value)
        target.source_file = safe_name
        target.as_of = now
        written += 1

    db.commit()

    tiered = _recompute_market_cap_tiers(db)

    return {
        "status": "success",
        "source_file": safe_name,
        "rows_imported": written,
        "header_row": parsed["header_row"],
        "recognised_columns": parsed["mapping"],
        "ignored_columns": parsed["unmapped_columns"],
        "market_cap_tiers_assigned": tiered,
        "universe_size": db.query(MarketData).count(),
        "as_of": now.isoformat(),
        "note": (
            "Market-cap tiers use SEBI/AMFI rank bands (top 100 large, 101-250 mid, "
            "251+ small) over the rows you imported, so they are only accurate if this "
            "export covered a broad universe rather than a narrow screen."
        ),
    }


def _recompute_market_cap_tiers(db: Session) -> int:
    """Rank the whole stored universe by market cap and assign SEBI tier bands."""
    rows = db.execute(
        select(MarketData).where(MarketData.market_cap.is_not(None))
    ).scalars().all()
    ranked = sorted(rows, key=lambda r: r.market_cap or 0, reverse=True)
    for index, row in enumerate(ranked, start=1):
        row.market_cap_tier = tier_for_rank(index)
    db.commit()
    return len(ranked)


@router.get("/status")
def market_status(db: Session = Depends(get_db)):
    """Freshness and coverage of the stored market data."""
    total = db.query(MarketData).count()
    if not total:
        return {
            "loaded": False,
            "universe_size": 0,
            "message": "No market data imported yet. POST a Screener CSV to /api/market/import.",
        }
    newest = db.execute(
        select(MarketData).order_by(MarketData.as_of.desc()).limit(1)
    ).scalars().first()
    by_source = {}
    for row in db.execute(select(MarketData)).scalars().all():
        if row.cmp is not None:
            by_source[row.price_source or "unknown"] = by_source.get(row.price_source or "unknown", 0) + 1
    with_cmp = db.query(MarketData).filter(MarketData.cmp.is_not(None)).count()
    return {
        "loaded": True,
        "universe_size": total,
        "rows_with_price": with_cmp,
        "source_file": newest.source_file if newest else None,
        "as_of": newest.as_of.isoformat() if newest and newest.as_of else None,
        "prices_by_source": by_source,
    }


@router.get("/coverage/{client_id}")
def coverage(client_id: int, db: Session = Depends(get_db)):
    """Which of a client's holdings matched market data, and how.

    Use this to find holdings that need an ISIN or a Screener row before the
    live-price figures can be trusted.
    """
    client = db.get(Client, client_id)
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    account_ids = [
        a.id for a in db.execute(
            select(Account).where(Account.client_id == client_id)
        ).scalars().all()
    ]
    holdings = db.execute(
        select(Holding).where(Holding.account_id.in_(account_ids))
    ).scalars().all() if account_ids else []

    market_rows = db.execute(select(MarketData)).scalars().all()
    by_isin, by_code, by_name = build_lookup(market_rows)

    matched, unmatched = [], []
    for h in holdings:
        row, how = match_holding(h, by_isin, by_code, by_name)
        if row:
            matched.append({
                "security_name": h.security_name,
                "matched_to": row.name,
                "matched_by": how,
                "cmp": row.cmp,
                "industry": row.industry,
            })
        else:
            unmatched.append({
                "security_name": h.security_name,
                "isin": h.isin,
                "normalised_key": normalise_name(h.security_name),
                "reason": ("no ISIN on the holding and no name match in the imported universe"
                           if not h.isin else "ISIN not present in the imported universe"),
            })

    return {
        "client_name": client.name,
        "holdings_total": len(holdings),
        "matched_count": len(matched),
        "unmatched_count": len(unmatched),
        "match_rate_pct": round(len(matched) / len(holdings) * 100, 1) if holdings else 0,
        "matched": matched,
        "unmatched": unmatched,
    }
