import json
from collections import defaultdict
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import select

from app.database import get_db
from app.models import Client, Account, Holding, MarketData
from app.valuation import value_holdings

router = APIRouter(prefix="/overview", tags=["Overview"])


@router.get("/{client_id}")
def get_overview(client_id: int, account: str = Query("family"), db: Session = Depends(get_db)):
    client = db.get(Client, client_id)
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    all_accounts = db.execute(select(Account).where(Account.client_id == client.id)).scalars().all()
    if not all_accounts:
        raise HTTPException(status_code=404, detail="No accounts found")

    accounts_list = [
        {"id": a.id, "label": a.portfolio_name or f"Account {a.account_number}", "has_cost_data": a.has_cost_data}
        for a in all_accounts
    ]

    # Filter accounts based on selection
    if account == "family":
        target_accounts = all_accounts
        label = "Family"
    else:
        try:
            aid = int(account)
            target_accounts = [a for a in all_accounts if a.id == aid]
        except ValueError:
            target_accounts = all_accounts
        label = target_accounts[0].portfolio_name if target_accounts else "Unknown"

    if not target_accounts:
        raise HTTPException(status_code=404, detail="Account not found")

    # Get all holdings for the target accounts
    account_ids = [a.id for a in target_accounts]
    holdings = db.execute(
        select(Holding).where(Holding.account_id.in_(account_ids))
    ).scalars().all()

    # ── Valuation ──
    # The headline total is the CURRENT market value: CMP x quantity for every
    # holding we have a live price for, statement value for the rest. Valued via
    # the shared module so this agrees with the Portfolio and Risk tabs exactly.
    market_rows = db.execute(select(MarketData)).scalars().all()
    valuation = value_holdings(holdings, market_rows)
    total_value = valuation.market_total

    # The statement figure is kept for reference. The LLM's extracted total is
    # preferred over a row sum because it picks up lines the table parser misses
    # (accrued income, for instance).
    statement_value = 0.0
    for a in target_accounts:
        if a.overview_json:
            try:
                ov = json.loads(a.overview_json).get("overview", {})
                statement_value += float(ov.get("portfolio_value") or 0)
            except Exception:
                pass
    if statement_value == 0:
        statement_value = valuation.statement_total

    # ── Cost basis and gain ──
    # Cost is historical and does not move with the market. Gain must compare
    # like with like: only accounts that HAVE a cost basis contribute, otherwise
    # a cost-less demat account would inflate the gain by its entire value.
    cost_account_ids = {a.id for a in target_accounts if a.has_cost_data}
    invested_value = 0.0
    total_gain = None

    if cost_account_ids:
        cost_holdings = [h for h in holdings if h.account_id in cost_account_ids]
        invested_value = sum(h.total_cost or 0 for h in cost_holdings)
        if invested_value <= 0:
            # Fall back to the LLM's extracted cost when rows carry none
            for a in target_accounts:
                if a.has_cost_data and a.overview_json:
                    try:
                        ov = json.loads(a.overview_json).get("overview", {})
                        invested_value += float(ov.get("invested_value") or 0)
                    except Exception:
                        pass
        if invested_value > 0:
            # Market value of just the cost-bearing accounts
            cost_market_value = sum(
                valuation.get(h.id).effective_value for h in cost_holdings
            )
            total_gain = cost_market_value - invested_value
        else:
            cost_account_ids = set()

    return_pct = round((total_gain / invested_value * 100), 2) if (
        total_gain is not None and invested_value > 0
    ) else None

    # Flag: are some accounts missing cost data?
    all_have_cost = all(a.has_cost_data for a in target_accounts)
    has_partial_cost = any(a.has_cost_data for a in target_accounts) and not all_have_cost

    # Sector allocation — at market value, so the slices add up to the headline
    sector_map = defaultdict(float)
    for h in holdings:
        sector_map[h.sector or "Others"] += valuation.get(h.id).effective_value

    actual_sector_total = sum(sector_map.values())
    sector_allocation = sorted(
        [{"sector": k, "value": v, "pct": round(v / actual_sector_total * 100, 2) if actual_sector_total else 0}
         for k, v in sector_map.items()],
        key=lambda x: x["pct"], reverse=True
    )
    
    # Asset allocation (deterministic, at market value)
    asset_map = defaultdict(float)
    for h in holdings:
        name = (h.security_name or "").lower()
        is_cash = any(k in name for k in ["cash", "payable", "receivable", "income"])
        cls = "Cash and Equivalent" if is_cash else "Equity"
        asset_map[cls] += valuation.get(h.id).effective_value
        
    actual_aa_total = sum(asset_map.values())
    aa_list = [
        {"asset_class": k, "value": v, "pct": round(v / actual_aa_total * 100, 2) if actual_aa_total else 0}
        for k, v in asset_map.items()
    ]

    # Top holdings — ranked and weighted at market value
    top_holdings = sorted(
        [{"security": h.security_name,
          "value": valuation.get(h.id).effective_value,
          "weight_pct": valuation.weight(h.id),
          "cmp": valuation.get(h.id).cmp,
          "valued_at": valuation.get(h.id).basis,
          "gain_pct": h.gain_pct}
         for h in holdings if valuation.get(h.id).effective_value > 0],
        key=lambda x: x["value"], reverse=True
    )[:5]

    # ── Portfolio health ──
    # Each holding's 34-parameter score, weighted by that holding's share of the
    # portfolio's market value: health = sum(score% x weight) / sum(weight).
    #
    # Dividing by the weight actually scored matters. Cash lines, mutual funds and
    # delisted scrips have no company fundamentals, so counting them as zero would
    # not mean "unhealthy", it would mean "not measured" — and would drag the
    # figure down in proportion to how much cash a client holds. They are excluded
    # from both sides instead, and the share of the portfolio that could be scored
    # is reported alongside so a thin coverage is visible.
    health = _portfolio_health(holdings, valuation, market_rows)
    health_score = health["score"]



    return {
        "client_name": client.name,
        "account_label": label,
        "is_processing": any(a.overview_json is None for a in target_accounts),
        # Current market value of the portfolio (CMP x qty where priced)
        "total_value": round(total_value, 2),
        # What the statements alone said, for reference/comparison
        "statement_value": round(statement_value, 2),
        "valuation": valuation.as_dict(),
        "invested_value": round(invested_value, 2) if cost_account_ids else None,
        "total_gain": round(total_gain, 2) if (cost_account_ids and total_gain is not None) else None,
        "return_pct": return_pct,
        "health_score": health_score,
        "health": health,
        "has_partial_cost_data": has_partial_cost,
        "all_have_cost_data": all_have_cost,
        "accounts": accounts_list,
        "asset_allocation": aa_list,
        "sector_allocation": sector_allocation,
        "top_holdings": top_holdings,
        "holding_count": len(holdings),
    }

def _portfolio_health(holdings, valuation, market_rows) -> dict:
    """Value-weighted average of each holding's 34-parameter score.

    Returns the overall figure, the same weighting applied to each of the four
    classes, and how much of the portfolio by value could be scored at all.
    """
    import json as _json
    from app.market_data import build_lookup, match_holding
    from app.vm_scoring import evaluate as vm_evaluate, CLASS_ORDER

    by_isin, by_code, by_name = build_lookup(market_rows or [])
    total_value = valuation.market_total or 0.0

    cache: dict = {}
    scored_weight = 0.0
    weighted_total = 0.0
    class_weighted = {name: 0.0 for name in CLASS_ORDER}
    scored_names, unscored_names = [], []

    for h in holdings:
        entry = valuation.get(h.id)
        value = entry.effective_value if entry else 0.0
        if not total_value or value <= 0:
            continue
        weight = value / total_value

        row, _ = match_holding(h, by_isin, by_code, by_name)
        if row is None or not (row.fundamentals_json or row.yahoo_json):
            unscored_names.append(h.security_name)
            continue

        if row.id not in cache:
            try:
                # Same precedence as the per-holding checklist: Screener for the
                # long history, Yahoo whole when Screener has nothing, so the
                # headline figure and the dropdowns cannot disagree.
                yahoo = _json.loads(row.yahoo_json) if row.yahoo_json else None
                screener = _json.loads(row.fundamentals_json) if row.fundamentals_json else None
                cache[row.id] = vm_evaluate(screener or yahoo, row, yahoo=yahoo)
            except Exception:
                cache[row.id] = None
        result = cache[row.id]
        if not result or result["total"]["pct"] is None:
            unscored_names.append(h.security_name)
            continue

        scored_weight += weight
        weighted_total += result["total"]["pct"] * weight
        for cls in result["classes"]:
            if cls["pct"] is not None:
                class_weighted[cls["name"]] += cls["pct"] * weight
        scored_names.append(h.security_name)

    if scored_weight <= 0:
        return {
            "score": None,
            "scored_weight_pct": 0.0,
            "scored_holdings": 0,
            "unscored_holdings": len(unscored_names),
            "classes": {},
            "note": "No holding has company fundamentals yet, so health cannot be scored.",
        }

    return {
        "score": round(weighted_total / scored_weight, 1),
        "scored_weight_pct": round(scored_weight * 100, 1),
        "scored_holdings": len(scored_names),
        "unscored_holdings": len(unscored_names),
        "classes": {name: round(value / scored_weight, 1)
                    for name, value in class_weighted.items()},
        "note": ("Value-weighted average of each holding's 34-parameter score, over the "
                 f"{round(scored_weight * 100, 1)}% of the portfolio that has fundamentals."),
    }
