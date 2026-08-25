from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Client
from app.analysis import get_cached

router = APIRouter(prefix="/insights", tags=["Insights"])


@router.get("/{client_id}")
def get_insights(client_id: int, account: str = Query("family"), db: Session = Depends(get_db)):
    """Read pre-computed insights from analysis_cache."""
    client = db.get(Client, client_id)
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    account_id = None if account == "family" else _safe_int(account)
    cached = get_cached(db, client_id, account_id, "insights")
    if cached:
        return cached

    return []


def _safe_int(s: str) -> int | None:
    try:
        return int(s)
    except (ValueError, TypeError):
        return None
