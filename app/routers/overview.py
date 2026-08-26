import json
from collections import defaultdict
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import select

from app.database import get_db
from app.models import Client, Account, Holding

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

    # ── Deterministic Math ──

    total_value = 0
    invested_value = 0
    total_gain = 0
    
    cost_account_ids = set()

    # Try to read from LLM summaries first
    for a in target_accounts:
        if a.overview_json:
            try:
                data = json.loads(a.overview_json)
                ov = data.get("overview", {})
                
                pval = float(ov.get("portfolio_value") or 0)
                total_value += pval
                
                if a.has_cost_data:
                    cost_account_ids.add(a.id)
                    invested_value += float(ov.get("invested_value") or 0)
                    total_gain += float(ov.get("total_gain") or 0)
            except:
                pass
                
    # Fallback to row sum if LLM failed or JSON was missing
    if total_value == 0:
        total_value = sum(h.current_value or 0 for h in holdings)
        
    if invested_value == 0:
        fallback_cost_accounts = {a.id for a in target_accounts if a.has_cost_data}
        if fallback_cost_accounts:
            cost_holdings = [h for h in holdings if h.account_id in fallback_cost_accounts]
            invested_value = sum(h.total_cost or 0 for h in cost_holdings)
            total_gain = sum((h.current_value or 0) - (h.total_cost or 0) for h in cost_holdings)

    return_pct = round((total_gain / invested_value * 100), 2) if invested_value > 0 else None

    # Flag: are some accounts missing cost data?
    all_have_cost = all(a.has_cost_data for a in target_accounts)
    has_partial_cost = any(a.has_cost_data for a in target_accounts) and not all_have_cost

    # Sector allocation (from ALL holdings)
    sector_map = defaultdict(float)
    for h in holdings:
        sector_map[h.sector or "Others"] += (h.current_value or 0)

    actual_sector_total = sum(sector_map.values())
    sector_allocation = sorted(
        [{"sector": k, "value": v, "pct": round(v / actual_sector_total * 100, 2) if actual_sector_total else 0}
         for k, v in sector_map.items()],
        key=lambda x: x["pct"], reverse=True
    )
    
    # Asset allocation (from ALL holdings, deterministic)
    asset_map = defaultdict(float)
    for h in holdings:
        name = (h.security_name or "").lower()
        is_cash = any(k in name for k in ["cash", "payable", "receivable", "income"])
        cls = "Cash and Equivalent" if is_cash else "Equity"
        asset_map[cls] += (h.current_value or 0)
        
    actual_aa_total = sum(asset_map.values())
    aa_list = [
        {"asset_class": k, "value": v, "pct": round(v / actual_aa_total * 100, 2) if actual_aa_total else 0}
        for k, v in asset_map.items()
    ]

    # Top holdings (from ALL holdings, sorted by value)
    top_holdings = sorted(
        [{"security": h.security_name, "value": h.current_value, "weight_pct": round((h.current_value or 0) / total_value * 100, 2) if total_value else 0, "gain_pct": h.gain_pct}
         for h in holdings if h.current_value and h.current_value > 0],
        key=lambda x: x["value"], reverse=True
    )[:5]

    # Health score (simple heuristic)
    h_count = len(holdings)
    div_score = 25 if h_count > 20 else 18 if h_count > 10 else 12
    ret_score = 25 if (return_pct or 0) > 20 else 20 if (return_pct or 0) > 10 else 15 if (return_pct or 0) > 0 else 8
    health_score = div_score + ret_score + 25 + 25  # simplified



    return {
        "client_name": client.name,
        "account_label": label,
        "is_processing": any(a.overview_json is None for a in target_accounts),
        "total_value": total_value,
        "invested_value": invested_value if cost_account_ids else None,
        "total_gain": total_gain if cost_account_ids else None,
        "return_pct": return_pct,
        "health_score": min(health_score, 100),
        "has_partial_cost_data": has_partial_cost,
        "all_have_cost_data": all_have_cost,
        "accounts": accounts_list,
        "asset_allocation": aa_list,
        "sector_allocation": sector_allocation,
        "top_holdings": top_holdings,
        "holding_count": len(holdings),
    }
