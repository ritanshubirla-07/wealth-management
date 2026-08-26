from collections import defaultdict
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import select

from app.database import get_db
from app.models import Client, Account, Holding

router = APIRouter(prefix="/portfolio", tags=["Portfolio"])


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

    total_value = sum((h.current_value or 0) for h in holdings)

    holdings_data = []
    sec_map = defaultdict(list)

    for h in holdings:
        val = h.current_value or 0
        weight_pct = round(val / total_value * 100, 2) if total_value else 0.0

        holdings_data.append({
            "security_name": h.security_name,
            "current_value": h.current_value,
            "total_cost": h.total_cost,
            "gain_pct": h.gain_pct,
            "weight_pct": weight_pct,
            "sector": h.sector,
            "account_label": account_map.get(h.account_id, "Unknown"),
        })
        sec_map[h.security_name].append({
            "account_label": account_map.get(h.account_id, "Unknown"),
            "value": val,
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

    return {
        "account_label": "Family" if account == "family" else (accounts[0].portfolio_name or f"Account {accounts[0].account_number}"),
        "holding_count": len(holdings_data),
        "total_value": total_value,
        "holdings": holdings_data,
        "overlaps": overlaps,
    }
