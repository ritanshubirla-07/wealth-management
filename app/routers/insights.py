import json
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import select

from app.database import get_db
from app.models import Client, Account

router = APIRouter(prefix="/insights", tags=["Insights"])


@router.get("/{client_id}")
def get_insights(client_id: int, account: str = Query("family"), db: Session = Depends(get_db)):
    client = db.get(Client, client_id)
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    all_accounts = db.execute(select(Account).where(Account.client_id == client.id)).scalars().all()

    # Single account: return its pre-computed LLM JSON
    if account != "family":
        try:
            target = next(a for a in all_accounts if a.id == int(account))
        except (StopIteration, ValueError):
            raise HTTPException(status_code=404, detail="Account not found")

        if target.insights_json:
            try:
                data = json.loads(target.insights_json)
                return {
                    "account_label": target.portfolio_name or f"Account {target.account_number}",
                    "insights": data.get("insights", data) if isinstance(data, dict) else data,
                }
            except (json.JSONDecodeError, TypeError):
                pass
        return {"account_label": target.portfolio_name, "insights": [], "_note": "Insights pending"}

    # Family: merge insights from all accounts
    all_insights = []
    for a in all_accounts:
        if not a.insights_json:
            continue
        try:
            data = json.loads(a.insights_json)
            items = data.get("insights", data) if isinstance(data, dict) else data
            if isinstance(items, list):
                for item in items:
                    item["source_account"] = a.portfolio_name or f"Account {a.account_number}"
                all_insights.extend(items)
        except (json.JSONDecodeError, TypeError):
            continue

    # Deduplicate by title
    seen = set()
    unique = []
    for ins in all_insights:
        key = ins.get("title", "")
        if key not in seen:
            seen.add(key)
            unique.append(ins)

    # Sort: danger first, then warning, then success, then info
    priority = {"danger": 0, "warning": 1, "success": 2, "info": 3}
    unique.sort(key=lambda x: priority.get(x.get("type", "info"), 4))

    return {
        "account_label": "Family",
        "insights": unique,
    }
