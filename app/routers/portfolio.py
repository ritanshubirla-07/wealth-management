from collections import defaultdict
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import select

from app.database import get_db
from app.models import Client, Account, Holding, MarketData
from app.valuation import value_holdings
from app.market_data import normalise_name

router = APIRouter(prefix="/portfolio", tags=["Portfolio"])

# Sector names the holdings table has no use for: they say "we could not tell",
# which is better shown as no tag at all than as a tag reading "Others".
_VAGUE = {"others", "other", "unknown", "miscellaneous", "diversified", ""}

# One vocabulary for the sector tag. Yahoo's own names lead, with two kinds of
# fix-up: "Consumer Defensive" is industry jargon for staples and reads as
# "Consumer Goods" here, and the older upload-time sector strings are folded onto
# the same set so the column cannot show "Healthcare" beside "Pharma &
# Healthcare" and imply they are different groups.
_SECTOR_DISPLAY = {
    "consumer defensive": "Consumer Goods",
    "consumer staples": "Consumer Goods",
    "pharma": "Healthcare",
    "pharma & healthcare": "Healthcare",
    "it & tech": "Technology",
    "materials & cement": "Basic Materials",
    "metals & mining": "Basic Materials",
    "chemicals": "Basic Materials",
    "agriculture": "Basic Materials",
    "energy & utilities": "Energy",
    "automobile": "Consumer Cyclical",
    "telecom": "Communication Services",
    "real estate & infrastructure": "Real Estate",
}


def _industry_label(holding, market) -> str:
    """The most specific classification available for this security.

    Yahoo first (GICS-like and nearly complete), then Screener's own industry,
    then the keyword/LLM sector stored on the holding at upload. Returns "" when
    every source is missing or merely says "Others", so the UI can omit the tag.
    """
    for value in (getattr(market, "yahoo_industry", None) if market else None,
                  getattr(market, "industry", None) if market else None,
                  getattr(holding, "sector", None)):
        text = (value or "").strip()
        if text and text.lower() not in _VAGUE:
            return text
    return ""


def _sector_group(holding, market) -> str:
    """The broad sector shown on the tag, "" when unknown.

    This is what the holdings table labels a position with — the group, not the
    specific industry, so the column reads as a handful of recognisable buckets
    rather than fifty one-off trades.
    """
    for value in (getattr(market, "yahoo_sector", None) if market else None,
                  getattr(holding, "sector", None)):
        text = (value or "").strip()
        if text and text.lower() not in _VAGUE:
            return _SECTOR_DISPLAY.get(text.lower(), text)
    return ""


@router.get("/{client_id}")
def get_portfolio(client_id: int, account: str = Query("family"), db: Session = Depends(get_db)):
    client = db.get(Client, client_id)
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    query = select(Account).where(Account.client_id == client.id)
    if account != "family":
        try:
            query = query.where(Account.id == int(account))
        except ValueError:
            pass

    accounts = db.execute(query).scalars().all()
    if not accounts:
        return {"holdings": [], "total_value": 0, "holding_count": 0}

    account_map = {a.id: a.portfolio_name or f"Account {a.account_number}" for a in accounts}
    account_ids = list(account_map.keys())

    holdings = db.execute(
        select(Holding).where(Holding.account_id.in_(account_ids)).order_by(Holding.current_value.desc())
    ).scalars().all()

    # Market data is optional: with no prices loaded the rows below simply carry
    # nulls and the dashboard falls back to statement values.
    market_rows = db.execute(select(MarketData)).scalars().all()

    # Valued through the shared module so this table, the Overview headline and
    # the Risk concentrations are all computed off one basis.
    valuation = value_holdings(holdings, market_rows)
    effective_total = valuation.market_total

    holdings_data = []
    sec_map = defaultdict(list)

    for h in holdings:
        hv = valuation.get(h.id)
        market, matched_by, live_value = hv.market, hv.matched_by, hv.live_value
        effective_value = hv.effective_value
        weight_pct = valuation.weight(h.id)

        live_gain_pct = None
        if live_value is not None and h.total_cost:
            live_gain_pct = round((live_value - h.total_cost) / h.total_cost * 100, 2)

        holdings_data.append({
            "security_name": h.security_name,
            "isin": h.isin,
            "quantity": h.quantity,
            "current_value": h.current_value,
            "total_cost": h.total_cost,
            "gain_pct": h.gain_pct,
            "weight_pct": weight_pct,
            "sector": h.sector,
            "account_label": account_map.get(h.account_id, "Unknown"),
            # The value the dashboard shows and weights against: CMP-derived when
            # available, statement value otherwise. `valued_at` says which, so a
            # stale statement figure is never passed off as a live one.
            "effective_value": effective_value,
            "valued_at": "live" if live_value is not None else "statement",
            # ── Screener.in enrichment (null when unmatched) ──
            "matched_by": matched_by,
            "cmp": market.cmp if market else None,
            "price_source": market.price_source if market else None,
            "live_value": live_value,
            "live_gain_pct": live_gain_pct,
            # The tag the dashboard shows beside the holding, plus the sector it
            # is coloured by. Yahoo's classification leads because it is both
            # finer and far better populated than the alternatives.
            "industry": _industry_label(h, market),
            "sector_group": _sector_group(h, market),
            "market_cap": market.market_cap if market else None,
            "market_cap_tier": market.market_cap_tier if market else None,
            "pe": market.pe if market else None,
            "roe": market.roe if market else None,
            "roce": market.roce if market else None,
            "debt_to_equity": market.debt_to_equity if market else None,
            "dividend_yield": market.dividend_yield if market else None,
            "eps": market.eps if market else None,
            "sales_growth": market.sales_growth if market else None,
            "profit_growth": market.profit_growth if market else None,
        })
        sec_map[h.security_name].append({
            "account_label": account_map.get(h.account_id, "Unknown"),
            # Same basis as the displayed value, so cross-account exposure agrees
            # with the weights in the holdings table
            "value": effective_value,
        })

    # Cross-account overlaps (family only)
    overlaps = []
    if account == "family":
        for sec, data_list in sec_map.items():
            if len(data_list) > 1:
                overlaps.append({
                    "security": sec,
                    "accounts": list(set(d["account_label"] for d in data_list)),
                    "combined_value": sum(d["value"] for d in data_list),
                })

    # In the Family view one security held in several accounts is one position,
    # so the rows are consolidated. Individual account views are left untouched.
    if account == "family":
        holdings_data = _merge_family_holdings(holdings_data, effective_total)

    return {
        "account_label": "Family" if account == "family" else (accounts[0].portfolio_name or f"Account {accounts[0].account_number}"),
        "holding_count": len(holdings_data),
        # Current market value — the same basis as the per-row values and weights,
        # so the Market Value column sums to exactly this figure.
        "total_value": round(effective_total, 2),
        "statement_value": round(valuation.statement_total, 2),
        # Kept as an alias for clients written against the previous field name
        "effective_total": round(effective_total, 2),
        "holdings": holdings_data,
        "overlaps": overlaps,
        "market_data": valuation.as_dict(),
    }

def _merge_family_holdings(rows: list, effective_total: float) -> list:
    """Consolidate one security held across several accounts into a single row.

    Family-view only. Identity is the normalised security name rather than the
    ISIN, because the same stock often arrives with an ISIN from a demat
    statement and without one from a PMS statement — keying on ISIN would leave
    those as two rows. As a guard against two genuinely different companies
    whose names normalise alike, a name group carrying more than one distinct
    ISIN is split back apart by ISIN.

    Quantities, values and costs add up; ratios are recomputed from the merged
    totals rather than averaged, and per-share figures (CMP, P/E, ROE) are
    carried across unchanged since they describe the security, not the position.
    """
    groups: dict = {}
    for row in rows:
        key = normalise_name(row["security_name"]) or (row.get("isin") or row["security_name"])
        groups.setdefault(key, []).append(row)

    merged: list = []
    for members in groups.values():
        if len(members) == 1:
            merged.append(members[0])
            continue

        distinct_isins = {(m.get("isin") or "").strip().upper()
                          for m in members if (m.get("isin") or "").strip()}
        if len(distinct_isins) > 1:
            # Same normalised name, different securities — keep them apart
            by_isin: dict = {}
            for m in members:
                by_isin.setdefault((m.get("isin") or "").strip().upper(), []).append(m)
            for subset in by_isin.values():
                merged.append(subset[0] if len(subset) == 1
                              else _combine(subset, effective_total))
            continue

        merged.append(_combine(members, effective_total))

    merged.sort(key=lambda r: r.get("effective_value") or 0, reverse=True)
    return merged


def _combine(members: list, effective_total: float) -> dict:
    """Add up the positions in one security and recompute its ratios."""
    def total(field):
        values = [m.get(field) for m in members if m.get(field) is not None]
        return sum(values) if values else None

    base = dict(members[0])

    quantity = total("quantity")
    current_value = total("current_value")
    total_cost = total("total_cost")
    effective_value = total("effective_value") or 0.0
    live_value = total("live_value")

    base["quantity"] = quantity
    base["current_value"] = current_value
    base["total_cost"] = total_cost
    base["effective_value"] = effective_value
    base["live_value"] = live_value

    # Weight against the family's own total, so the column still sums to 100%
    base["weight_pct"] = round(effective_value / effective_total * 100, 2) if effective_total else 0.0

    # Gains recomputed from the merged cost base, never averaged
    base["gain_pct"] = (round((current_value - total_cost) / total_cost * 100, 2)
                        if (current_value is not None and total_cost) else None)
    base["live_gain_pct"] = (round((live_value - total_cost) / total_cost * 100, 2)
                             if (live_value is not None and total_cost) else None)

    # Only call the merged row live-priced when every leg of it was
    base["valued_at"] = ("live" if all(m.get("valued_at") == "live" for m in members)
                         else "statement")

    # An ISIN from any leg identifies the whole position
    base["isin"] = next((m.get("isin") for m in members if m.get("isin")), None)

    labels = []
    for m in members:
        label = m.get("account_label")
        if label and label not in labels:
            labels.append(label)
    base["accounts"] = labels
    base["account_count"] = len(labels)
    base["account_label"] = (labels[0] if len(labels) == 1
                             else f"{len(labels)} accounts")
    base["merged_from"] = len(members)
    return base
