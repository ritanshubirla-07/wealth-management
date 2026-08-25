from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Client
from app.analysis import get_cached

router = APIRouter(prefix="/performance", tags=["Performance"])


@router.get("/{client_id}")
def get_performance(client_id: int, account: str = Query("family"), db: Session = Depends(get_db)):
    """Read pre-computed performance from analysis_cache."""
    client = db.get(Client, client_id)
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    account_id = None if account == "family" else _safe_int(account)
    cached = get_cached(db, client_id, account_id, "performance")
    if cached:
        return cached

    return {"account_label": "", "total_return_pct": 0, "total_gain": 0,
            "top_performers": [], "worst_performers": [], "gainers_count": 0, "losers_count": 0,
            "performance_summary": "", "top_performer_insight": "", "concern_areas": [],
            "_note": "Analysis not yet run."}


def _safe_int(s: str) -> int | None:
    try:
        return int(s)
    except (ValueError, TypeError):
        return None
