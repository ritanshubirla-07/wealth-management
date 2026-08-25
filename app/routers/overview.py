from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Client, Account
from app.analysis import get_cached

router = APIRouter(prefix="/overview", tags=["Overview"])


@router.get("/{client_id}")
def get_overview(client_id: int, account: str = Query("family"), db: Session = Depends(get_db)):
    """Read pre-computed overview from analysis_cache."""
    client = db.get(Client, client_id)
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    all_accounts = db.execute(select(Account).where(Account.client_id == client.id)).scalars().all()
    
    query = select(Account).where(Account.client_id == client.id)
    if account != "family":
        try:
            account_id = int(account)
            query = query.where(Account.id == account_id)
        except ValueError:
            pass
            
    filtered_accounts = db.execute(query).scalars().all()
    if not filtered_accounts:
        raise HTTPException(status_code=404, detail="Account not found")

    account_id = None if account == "family" else _safe_int(account)
    cached = get_cached(db, client_id, account_id, "overview")

    accounts_list = [{"id": a.id, "label": a.portfolio_name or (f"Account {a.account_number}" if a.account_number else f"Account {a.id}")} for a in all_accounts]

    if cached:
        cached["accounts"] = accounts_list
        return cached

    # Fallback: no cache yet — return minimal
    return {
        "client_name": client.name,
        "account_label": "Family" if account == "family" else "",
        "total_value": 0, "invested_value": 0, "total_gain": 0, "return_pct": 0,
        "health_score": 0, "health_breakdown": {},
        "accounts": accounts_list,
        "asset_allocation": [], "sector_allocation": [], "narrative": "",
        "_note": "Analysis not yet run. Upload documents to trigger analysis."
    }


def _safe_int(s: str) -> int | None:
    try:
        return int(s)
    except (ValueError, TypeError):
        return None
