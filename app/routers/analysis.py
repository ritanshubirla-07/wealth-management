"""Per-stock fundamental checklist, served for the expandable Portfolio rows."""

import json
import re

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import MarketData
from app.checklist import GSEC_10Y_YIELD
from app.vm_scoring import evaluate as vm_evaluate
from app.market_data import normalise_name, normalise_code

router = APIRouter(prefix="/analysis", tags=["Analysis"])


_FUND_WORDS = re.compile(
    r'\b(fund|etf|nav|scheme|growth|dividend|liquid|arbitrage|gilt|debt|bond|'
    r'ncd|sdl|g-?sec|treasury|index|nifty|sensex|fof|sip|amc|mutual)\b', re.I)


def _is_fund_like(name: str) -> bool:
    """True when the name reads as a fund or debt instrument, not a company.

    Used only to word the "no financials" panel: a fund has no company
    statements by nature, so telling someone to go and fetch them is wrong.
    """
    return bool(_FUND_WORDS.search(name or ""))


def _find_market_row(db: Session, isin: str = "", name: str = "", code: str = ""):
    """Locate a security using the same order the dashboard's valuation uses."""
    if isin:
        row = db.execute(
            select(MarketData).where(MarketData.isin == isin.strip().upper())
        ).scalars().first()
        if row:
            return row, "isin"
    if code:
        clean = normalise_code(code)
        row = db.execute(
            select(MarketData).where(MarketData.nse_code == clean)
        ).scalars().first()
        if row:
            return row, "code"
    if name:
        key = normalise_name(name)
        if key:
            matches = db.execute(
                select(MarketData).where(MarketData.match_key == key)
            ).scalars().all()
            # Refuse an ambiguous name rather than showing another company's books
            if len(matches) == 1:
                return matches[0], "name"
            if len(matches) > 1:
                return None, "ambiguous"
    return None, None


@router.get("/checklist")
def get_checklist(
    isin: str = Query("", description="ISIN of the holding, when known"),
    name: str = Query("", description="Security name as it appears on the statement"),
    code: str = Query("", description="NSE/BSE code, if known"),
    db: Session = Depends(get_db),
):
    """Evaluate the 34-point checklist for one security.

    Returns rows even when data is missing: each row carries its own status so
    the caller can distinguish "failed the test" from "we could not measure it".
    """
    if not any([isin, name, code]):
        raise HTTPException(status_code=400, detail="Provide isin, name or code.")

    row, how = _find_market_row(db, isin=isin, name=name, code=code)

    if how == "ambiguous":
        return {
            "found": False,
            "security": name,
            "reason": ("More than one security matches this name, so the checklist is not "
                       "shown rather than risk reporting the wrong company's financials."),
        }
    if row is None:
        return {
            "found": False,
            "security": name or isin or code,
            "reason": ("Mutual funds, ETFs and bonds have no company financials to score. "
                       "This holding is valued at its published NAV."
                       if _is_fund_like(name) else
                       "This security has not been matched to a listing yet, so there is "
                       "nothing to score. It is picked up by the next refresh."),
        }
    if not row.fundamentals_json and not row.yahoo_json:
        # Say which of these it is rather than printing a command to run. A
        # fund has no company statements and never will, which is a fact about
        # the instrument, not a job someone forgot to run.
        looks_like_fund = _is_fund_like(row.name or name or "")
        return {
            "found": False,
            "security": row.name or name,
            "code": row.nse_code,
            "reason": ("Mutual funds, ETFs and bonds have no company financials to score. "
                       "This holding is valued at its published NAV."
                       if looks_like_fund else
                       "Company financials for this security are not published by either "
                       "data source yet, which is usual for a recent listing. The holding "
                       "is still priced and counted in the portfolio; the scorecard fills "
                       "in once statements appear."),
        }

    def _load(raw, what):
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            raise HTTPException(status_code=500,
                                detail=f"Stored {what} for this security are not valid JSON.")

    screener = _load(row.fundamentals_json, "Screener fundamentals")
    yahoo = _load(row.yahoo_json, "Yahoo fundamentals")

    # Screener is the backbone because it carries ten-plus years on one set of
    # definitions, which the multi-year trend and CAGR parameters need. Yahoo
    # stands in whole when Screener has nothing for this security, so a failed
    # scrape degrades the history depth rather than emptying the scorecard.
    if screener:
        fundamentals, primary = screener, "screener.in"
        as_of = row.fundamentals_as_of
    else:
        fundamentals, primary = yahoo, "finance.yahoo.com"
        as_of = row.yahoo_as_of

    result = vm_evaluate(fundamentals, row, yahoo=yahoo)
    return {
        "found": True,
        "security": row.name or name,
        "code": row.nse_code,
        "isin": row.isin,
        "industry": row.industry,
        "cmp": row.cmp,
        "price_source": row.price_source,
        "matched_by": how,
        "as_of": as_of.isoformat() if as_of else None,
        "source": primary,
        "yahoo_symbol": row.yahoo_symbol,
        "yahoo_as_of": row.yahoo_as_of.isoformat() if row.yahoo_as_of else None,
        # Consolidated statements where the company files them, else standalone
        "basis": (fundamentals or {}).get("basis") or "unknown",
        "gsec_10y_yield": GSEC_10Y_YIELD,
        "classes": result["classes"],
        "total": result["total"],
        "rows": result["rows"],
    }
