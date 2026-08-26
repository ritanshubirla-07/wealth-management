import json
from collections import defaultdict
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import select

from app.database import get_db
from app.models import Client, Account, Holding

router = APIRouter(prefix="/risk", tags=["Risk"])


@router.get("/{client_id}")
def get_risk(client_id: int, account: str = Query("family"), db: Session = Depends(get_db)):
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

        if target.risk_analysis_json:
            try:
                return {
                    "account_label": target.portfolio_name or f"Account {target.account_number}",
                    **json.loads(target.risk_analysis_json),
                }
            except:
                pass
        return {"account_label": target.portfolio_name, "_note": "Risk analysis pending"}

    # Family: compute cross-account risk deterministically
    account_ids = [a.id for a in all_accounts]
    holdings = db.execute(select(Holding).where(Holding.account_id.in_(account_ids))).scalars().all()

    if not holdings:
        return {"account_label": "Family", "_note": "No holdings found"}

    total_value = sum(h.current_value or 0 for h in holdings)
    account_map = {a.id: a.portfolio_name or f"Account {a.account_number}" for a in all_accounts}

    # Concentration: group by security across all accounts
    sec_vals = defaultdict(float)
    sec_accounts = defaultdict(set)
    sector_vals = defaultdict(float)

    for h in holdings:
        val = h.current_value or 0
        sec_vals[h.security_name] += val
        sec_accounts[h.security_name].add(account_map.get(h.account_id, "Unknown"))
        sector_vals[h.sector or "Others"] += val

    sorted_secs = sorted(sec_vals.items(), key=lambda x: x[1], reverse=True)
    top5_pct = round(sum(v for _, v in sorted_secs[:5]) / total_value * 100, 2) if total_value else 0
    top_holding_pct = round(sorted_secs[0][1] / total_value * 100, 2) if sorted_secs and total_value else 0

    # HHI concentration index
    hhi = sum((v / total_value) ** 2 for v in sec_vals.values()) if total_value else 1
    concentration = "Low" if hhi < 0.05 else "Medium" if hhi < 0.1 else "High"

    # Sector concentration
    sector_conc = sorted(
        [{"sector": k, "pct": round(v / total_value * 100, 1) if total_value else 0}
         for k, v in sector_vals.items()],
        key=lambda x: x["pct"], reverse=True
    )

    # Cross-account overlaps (family-specific insight)
    overlaps = []
    for sec, accts in sec_accounts.items():
        if len(accts) > 1:
            pct = round(sec_vals[sec] / total_value * 100, 2) if total_value else 0
            overlaps.append({"security": sec, "accounts": list(accts), "combined_pct": pct})

    # Risk flags
    risk_flags = []
    if top5_pct > 50:
        risk_flags.append(f"Top 5 holdings make up {top5_pct}% of your portfolio — high concentration risk")
    if top_holding_pct > 15:
        risk_flags.append(f"Largest holding ({sorted_secs[0][0]}) is {top_holding_pct}% of portfolio — single stock risk")
    for s in sector_conc:
        if s["pct"] > 30:
            risk_flags.append(f"{s['sector']} sector is overweight at {s['pct']}%")
    if overlaps:
        risk_flags.append(f"{len(overlaps)} securities are duplicated across multiple accounts")

    overall = "High" if len(risk_flags) >= 3 else "Medium" if risk_flags else "Low"

    return {
        "account_label": "Family",
        "concentration_risk": concentration,
        "top_holding_pct": top_holding_pct,
        "top5_pct": top5_pct,
        "sector_concentration": sector_conc,
        "cross_account_overlaps": overlaps,
        "risk_flags": risk_flags,
        "overall_risk_level": overall,
    }
