import json
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import select

from app.database import get_db
from app.models import Client, Account, Holding

router = APIRouter(prefix="/performance", tags=["Performance"])


@router.get("/{client_id}")
def get_performance(client_id: int, account: str = Query("family"), db: Session = Depends(get_db)):
    client = db.get(Client, client_id)
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    all_accounts = db.execute(select(Account).where(Account.client_id == client.id)).scalars().all()

    # Single account: return its pre-computed LLM JSON directly
    if account != "family":
        try:
            target = next(a for a in all_accounts if a.id == int(account))
        except (StopIteration, ValueError):
            raise HTTPException(status_code=404, detail="Account not found")

        if target.performance_json:
            try:
                data = json.loads(target.performance_json)
                # Re-sort top_gainers by gain_pct to ensure accuracy
                gainers = data.get("top_gainers", [])
                gainers.sort(key=lambda x: x.get("gain_pct", 0), reverse=True)
                if gainers and "kpis" in data:
                    data["kpis"]["best_performing_asset"] = gainers[0].get("security")
                
                return {
                    "account_label": target.portfolio_name or f"Account {target.account_number}",
                    "has_cost_data": target.has_cost_data,
                    **data,
                }
            except:
                pass
        return {"account_label": target.portfolio_name, "has_cost_data": target.has_cost_data,
                "kpis": None, "top_gainers": [], "top_losers": [], "_note": "LLM analysis pending"}

    # Family: aggregate from accounts that have cost data
    cost_accounts = [a for a in all_accounts if a.has_cost_data]

    # Merge top gainers/losers from all cost-data accounts
    all_gainers = []
    all_losers = []
    for a in cost_accounts:
        if not a.performance_json:
            continue
        try:
            perf = json.loads(a.performance_json)
            all_gainers.extend(perf.get("top_gainers", []))
            all_losers.extend(perf.get("top_losers", []))
        except (json.JSONDecodeError, TypeError):
            continue

    # Sort and take top 5 of each
    all_gainers.sort(key=lambda x: x.get("gain_pct", 0), reverse=True)
    all_losers.sort(key=lambda x: x.get("gain_pct", 0))

    # Family KPIs from deterministic math (not LLM)
    cost_account_ids = {a.id for a in cost_accounts}
    cost_holdings = db.execute(
        select(Holding).where(Holding.account_id.in_(cost_account_ids))
    ).scalars().all() if cost_account_ids else []

    total_cost = sum(h.total_cost or 0 for h in cost_holdings)
    total_value = sum(h.current_value or 0 for h in cost_holdings)
    absolute_return = round(((total_value - total_cost) / total_cost * 100), 2) if total_cost > 0 else None

    best_asset = all_gainers[0]["security"] if all_gainers else None

    no_cost_accounts = [a for a in all_accounts if not a.has_cost_data]

    return {
        "account_label": "Family",
        "has_cost_data": len(cost_accounts) > 0,
        "has_partial_cost_data": len(cost_accounts) > 0 and len(no_cost_accounts) > 0,
        "accounts_without_cost": [a.portfolio_name or f"Account {a.account_number}" for a in no_cost_accounts],
        "kpis": {
            "absolute_return_pct": absolute_return,
            "best_performing_asset": best_asset,
        },
        "top_gainers": all_gainers[:5],
        "top_losers": all_losers[:5],
    }
