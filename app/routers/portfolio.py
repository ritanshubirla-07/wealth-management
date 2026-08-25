from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import select
from collections import defaultdict

from app.database import get_db
from app.models import Client, Account, Holding

router = APIRouter(prefix="/portfolio", tags=["Portfolio"])

@router.get("/{client_id}")
def get_portfolio(
    client_id: int,
    account: str = Query("family"),
    db: Session = Depends(get_db)
):
    client = db.get(Client, client_id)
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    query = select(Account).where(Account.client_id == client.id)
    if account != "family":
        try:
            account_id = int(account)
            query = query.where(Account.id == account_id)
        except ValueError:
            pass
            
    accounts = db.execute(query).scalars().all()
    if not accounts:
        return {"holdings": [], "total_value": 0, "holding_count": 0}
        
    account_map = {a.id: a.portfolio_name or f"Account {a.account_number}" for a in accounts}
    account_ids = list(account_map.keys())

    holdings = db.execute(
        select(Holding)
        .where(Holding.account_id.in_(account_ids))
        .order_by(Holding.current_value.desc())
    ).scalars().all()
    total_value = sum((h.current_value or 0) for h in holdings)
    
    holdings_data = []
    sec_map = defaultdict(list)
    
    for h in holdings:
        val = h.current_value or 0
        weight_pct = (val / total_value * 100) if total_value else 0.0
        
        holdings_data.append({
            "security_name": h.security_name,
            "isin": h.isin,
            "quantity": h.quantity,
            "avg_cost": h.avg_cost,
            "current_price": h.current_price,
            "current_value": h.current_value,
            "total_cost": h.total_cost,
            "gain": h.unrealized_gain,
            "gain_pct": h.gain_pct,
            "weight_pct": round(weight_pct, 2),
            "sector": h.sector,
            "market_cap": h.market_cap,
            "account_label": account_map.get(h.account_id, "Unknown")
        })
        sec_map[h.security_name].append({
            "account_label": account_map.get(h.account_id, "Unknown"),
            "value": val
        })
    
    overlaps = []
    if account == "family":
        for sec, data_list in sec_map.items():
            if len(data_list) > 1:
                accts = [d["account_label"] for d in data_list]
                combined = sum(d["value"] for d in data_list)
                overlaps.append({
                    "security": sec,
                    "accounts": list(set(accts)),
                    "combined_value": combined
                })

    return {
        "account_label": "Family" if account == "family" else accounts[0].portfolio_name or f"Account {accounts[0].account_number}",
        "holding_count": len(holdings_data),
        "total_value": total_value,
        "holdings": holdings_data,
        "overlaps": overlaps
    }
