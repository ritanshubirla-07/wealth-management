"""
Analysis engine — computes all dashboard sections and stores in DB.

Flow: Upload → compute metrics (SQL/math) → call LLM for narratives → store in analysis_cache
Dashboard endpoints read from cache — zero latency, deterministic.

Runs per-account analysis first, then aggregates for family view.
"""
import json
import logging
from datetime import datetime
from collections import defaultdict
from sqlalchemy.orm import Session
from sqlalchemy import select

from app.models import Client, Account, Holding, AnalysisCache


log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════

def _save_cache(db: Session, client_id: int, account_id: int | None, section: str, data: dict | list):
    """Upsert analysis cache entry."""
    existing = db.query(AnalysisCache).filter(
        AnalysisCache.client_id == client_id,
        AnalysisCache.account_id == account_id if account_id else AnalysisCache.account_id.is_(None),
        AnalysisCache.section == section,
    ).first()

    json_str = json.dumps(data, default=str)

    if existing:
        existing.data_json = json_str
        existing.updated_at = datetime.utcnow()
    else:
        db.add(AnalysisCache(
            client_id=client_id,
            account_id=account_id,
            section=section,
            data_json=json_str,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        ))
    db.commit()


def get_cached(db: Session, client_id: int, account_id: int | None, section: str) -> dict | list | None:
    """Read from cache. Returns parsed JSON or None."""
    q = db.query(AnalysisCache).filter(
        AnalysisCache.client_id == client_id,
        AnalysisCache.section == section,
    )
    if account_id:
        q = q.filter(AnalysisCache.account_id == account_id)
    else:
        q = q.filter(AnalysisCache.account_id.is_(None))

    row = q.first()
    if row:
        try:
            return json.loads(row.data_json)
        except json.JSONDecodeError:
            return None
    return None


# ══════════════════════════════════════════════════════════════
#  CORE COMPUTATION (SQL + math — deterministic)
# ══════════════════════════════════════════════════════════════

def _compute_holdings_metrics(holdings: list[Holding], total_value: float):
    """Shared computation used across sections."""
    sector_alloc = defaultdict(float)
    asset_alloc = defaultdict(float)
    cap_alloc = defaultdict(float)
    large_cap_val = 0.0
    eq_val = 0.0

    for h in holdings:
        val = h.current_value or 0
        sector_alloc[h.sector or "Unknown"] += val
        asset_alloc[(h.asset_class or "equity").capitalize()] += val
        cap_alloc[h.market_cap or "Unknown"] += val
        if h.market_cap and "large" in h.market_cap.lower():
            large_cap_val += val
        if (h.asset_class or "").lower() == "equity":
            eq_val += val

    invested = sum(h.total_cost or 0 for h in holdings)
    gain = total_value - invested
    return_pct = round((gain / invested * 100), 2) if invested > 0 else 0.0
    hhi = sum(((h.current_value or 0) / total_value) ** 2 for h in holdings) if total_value > 0 else 1.0

    return {
        "invested": invested,
        "gain": gain,
        "return_pct": return_pct,
        "hhi": hhi,
        "large_cap_val": large_cap_val,
        "eq_val": eq_val,
        "sector_alloc": dict(sector_alloc),
        "asset_alloc": dict(asset_alloc),
        "cap_alloc": dict(cap_alloc),
    }


# ══════════════════════════════════════════════════════════════
#  PER-SECTION GENERATORS
# ══════════════════════════════════════════════════════════════

def _build_overview(client_name: str, holdings: list[Holding], total_value: float,
                    accounts_data: list, is_family: bool) -> dict:
    m = _compute_holdings_metrics(holdings, total_value)

    h_count = len(holdings)
    div_score = 25 if h_count > 20 else 18 if h_count > 10 else 12 if h_count > 5 else 8
    ret_score = 25 if m["return_pct"] > 20 else 20 if m["return_pct"] > 10 else 15 if m["return_pct"] > 0 else 8
    risk_score = 25 if m["hhi"] < 0.05 else 20 if m["hhi"] < 0.1 else 15 if m["hhi"] < 0.15 else 10
    lc_pct = (m["large_cap_val"] / total_value * 100) if total_value > 0 else 0
    qual_score = 25 if lc_pct > 60 else 20 if lc_pct > 40 else 15 if lc_pct > 20 else 10

    sector_list = sorted(
        [{"sector": k, "value": v, "pct": round(v / total_value * 100, 2) if total_value else 0}
         for k, v in m["sector_alloc"].items()], key=lambda x: x["pct"], reverse=True)
    asset_list = sorted(
        [{"class": k, "value": v, "pct": round(v / total_value * 100, 2) if total_value else 0}
         for k, v in m["asset_alloc"].items()], key=lambda x: x["pct"], reverse=True)

    result = {
        "client_name": client_name,
        "account_label": "Family" if is_family else (accounts_data[0]["label"] if accounts_data else ""),
        "total_value": total_value,
        "invested_value": m["invested"],
        "total_gain": m["gain"],
        "return_pct": m["return_pct"],
        "health_score": div_score + ret_score + risk_score + qual_score,
        "health_breakdown": {"diversification": div_score, "returns": ret_score, "risk": risk_score, "quality": qual_score},
        "accounts": accounts_data,
        "asset_allocation": asset_list,
        "sector_allocation": sector_list,
    }

    return result


def _build_performance(holdings: list[Holding], total_value: float, is_family: bool, label: str) -> dict:
    m = _compute_holdings_metrics(holdings, total_value)

    items = []
    for h in holdings:
        val = h.current_value or 0
        cost = h.total_cost or 0
        gain_val = val - cost
        items.append({
            "security_name": h.security_name,
            "gain_pct": h.gain_pct or 0,
            "gain_value": gain_val,
            "weight_pct": round(val / total_value * 100, 2) if total_value else 0,
        })

    positives = [x for x in items if x["gain_pct"] > 0]
    negatives = [x for x in items if x["gain_pct"] < 0]

    top_perf = sorted(positives, key=lambda x: x["gain_pct"], reverse=True)[:5]
    worst_perf = sorted(negatives, key=lambda x: x["gain_pct"])[:5]
    sorted_by_abs = sorted(items, key=lambda x: x["gain_value"], reverse=True)

    biggest_gain = sorted_by_abs[0] if sorted_by_abs and sorted_by_abs[0]["gain_value"] > 0 else None
    biggest_loss = sorted_by_abs[-1] if sorted_by_abs and sorted_by_abs[-1]["gain_value"] < 0 else None

    weighted_avg = sum(x["gain_pct"] * (x["weight_pct"] / 100) for x in items)

    result = {
        "account_label": label,
        "total_return_pct": m["return_pct"],
        "total_gain": m["gain"],
        "top_performers": top_perf,
        "worst_performers": worst_perf,
        "gainers_count": len(positives),
        "losers_count": len(negatives),
        "biggest_absolute_gain": {"security_name": biggest_gain["security_name"], "gain_value": biggest_gain["gain_value"]} if biggest_gain else None,
        "biggest_absolute_loss": {"security_name": biggest_loss["security_name"], "loss_value": biggest_loss["gain_value"]} if biggest_loss else None,
        "weighted_avg_return": round(weighted_avg, 2),
    }

    return result


def _build_risk(holdings: list[Holding], total_value: float, is_family: bool,
                label: str, account_map: dict | None = None) -> dict:
    m = _compute_holdings_metrics(holdings, total_value)

    hhi = m["hhi"]
    hhi_label = "Low" if hhi < 0.05 else "Moderate" if hhi < 0.1 else "High" if hhi < 0.15 else "Very High"

    # Concentration
    sec_vals = defaultdict(float)
    sec_accounts = defaultdict(set)
    for h in holdings:
        sec_vals[h.security_name] += (h.current_value or 0)
        if account_map:
            sec_accounts[h.security_name].add(account_map.get(h.account_id, "Unknown"))

    sorted_secs = sorted(sec_vals.items(), key=lambda x: x[1], reverse=True)
    top5 = [{"name": s, "pct": round(v / total_value * 100, 2)} for s, v in sorted_secs[:5]] if total_value else []
    top10 = [{"name": s, "pct": round(v / total_value * 100, 2)} for s, v in sorted_secs[:10]] if total_value else []

    sector_conc = sorted(
        [{"sector": k, "pct": round(v / total_value * 100, 1) if total_value else 0,
          "risk_level": "High" if (v / total_value * 100 if total_value else 0) > 30 else "Medium" if (v / total_value * 100 if total_value else 0) > 20 else "Low"}
         for k, v in m["sector_alloc"].items()], key=lambda x: x["pct"], reverse=True)

    eq_pct = round(m["eq_val"] / total_value * 100, 2) if total_value else 0
    div_rating = "Poor" if eq_pct > 90 else "Fair" if eq_pct > 70 else "Good"

    cap_dist = [{"cap": k, "pct": round(v / total_value * 100, 1) if total_value else 0}
                for k, v in m["cap_alloc"].items()]

    # Cross-account overlaps (family only)
    overlaps = []
    if is_family:
        for sec, accts in sec_accounts.items():
            if len(accts) > 1:
                pct = round(sec_vals[sec] / total_value * 100, 2) if total_value else 0
                overlaps.append({"security": sec, "accounts": list(accts), "total_pct": pct})

    result = {
        "account_label": label,
        "hhi": round(hhi, 4),
        "hhi_label": hhi_label,
        "concentration": {
            "top5_pct": round(sum(x["pct"] for x in top5), 2),
            "top10_pct": round(sum(x["pct"] for x in top10), 2),
            "top5_holdings": top5,
        },
        "sector_concentration": sector_conc,
        "asset_class_risk": {"equity_pct": eq_pct, "debt_pct": round(100 - eq_pct, 2), "diversification_rating": div_rating},
        "market_cap_distribution": cap_dist,
        "cross_account_overlaps": overlaps,
    }

    return result


def _build_insights(holdings: list[Holding], total_value: float, is_family: bool,
                    account_map: dict | None = None, overview_data: dict | None = None) -> list:
    """Compute rule-based insights, then enhance with LLM."""
    insights = []
    sec_vals = defaultdict(float)
    sector_vals = defaultdict(float)
    eq_val = 0.0
    sec_accounts = defaultdict(set)

    for h in holdings:
        val = h.current_value or 0
        sec_vals[h.security_name] += val
        sector_vals[h.sector or "Unknown"] += val
        if (h.asset_class or "").lower() == "equity":
            eq_val += val
        if account_map:
            sec_accounts[h.security_name].add(account_map.get(h.account_id, "Unknown"))

        if h.gain_pct is not None:
            if h.gain_pct < -10:
                insights.append({"type": "danger", "title": "Significant Underperformer",
                                 "description": f"{h.security_name} has fallen by {h.gain_pct:.1f}%"})
            elif h.gain_pct > 100:
                insights.append({"type": "success", "title": "Star Performer",
                                 "description": f"{h.security_name} has gained {h.gain_pct:.1f}%"})

    # Top 5 concentration
    sorted_secs = sorted(sec_vals.items(), key=lambda x: x[1], reverse=True)
    top5_val = sum(v for _, v in sorted_secs[:5])
    if total_value and (top5_val / total_value) > 0.4:
        insights.append({"type": "warning", "title": "Concentration Risk",
                         "description": f"Top 5 holdings account for {top5_val / total_value * 100:.1f}% of the portfolio."})

    # Single stock risk
    for sec, val in sec_vals.items():
        if total_value and (val / total_value) > 0.15:
            insights.append({"type": "danger", "title": "Single Stock Risk",
                             "description": f"{sec} constitutes {val / total_value * 100:.1f}% of total value."})

    # Sector overweight
    for sec, val in sector_vals.items():
        if total_value and (val / total_value) > 0.3:
            insights.append({"type": "warning", "title": "Sector Overweight",
                             "description": f"The {sec} sector represents {val / total_value * 100:.1f}% of your portfolio."})

    # Low diversification
    if total_value and eq_val / total_value > 0.95:
        insights.append({"type": "info", "title": "Low Asset Diversification",
                         "description": "More than 95% of your portfolio is in Equities."})

    if len(sec_vals) < 10:
        insights.append({"type": "warning", "title": "Limited Diversification",
                         "description": f"Portfolio contains only {len(sec_vals)} unique securities."})

    # Cross-account overlaps (family only)
    if is_family:
        overlapping = [s for s, a in sec_accounts.items() if len(a) > 1]
        if overlapping:
            insights.append({"type": "info", "title": "Cross-Account Overlap",
                             "description": f"Securities held in multiple accounts: {', '.join(overlapping[:5])}"})

    # Deduplicate
    unique = []
    seen = set()
    for ins in insights:
        key = ins["title"] + ins["description"]
        if key not in seen:
            seen.add(key)
            unique.append(ins)

    return unique


# ══════════════════════════════════════════════════════════════
#  MAIN ENTRY POINT — called after upload
# ══════════════════════════════════════════════════════════════

def run_analysis(db: Session, client_id: int, target_account_id: int | None = None):
    """Generate analysis sections.
    If target_account_id is provided, only that account and the family view will be processed.
    Otherwise, all accounts and the family view will be processed.
    """
    client = db.get(Client, client_id)
    if not client:
        log.warning(f"Client {client_id} not found")
        return

    accounts = db.execute(
        select(Account).where(Account.client_id == client_id)
    ).scalars().all()

    if not accounts:
        log.info(f"No accounts for client {client_id}")
        return

    account_map = {a.id: a.portfolio_name or f"{a.account_type} — {a.account_number}" for a in accounts}
    all_holdings = db.execute(
        select(Holding).where(Holding.account_id.in_(list(account_map.keys())))
    ).scalars().all()

    # Group holdings by account
    acct_holdings: dict[int, list[Holding]] = defaultdict(list)
    for h in all_holdings:
        acct_holdings[h.account_id].append(h)

    per_account_data: dict[int, dict] = {}

    # ── 1. Per-account analysis ──
    for acct in accounts:
        ah = acct_holdings.get(acct.id, [])
        if not ah:
            continue

        ah = acct_holdings.get(acct.id, [])
        tv = sum(h.current_value or 0 for h in ah)
        label = account_map[acct.id]

        if target_account_id and acct.id != target_account_id:
            continue

        log.info(f"Analyzing account {acct.id} ({label}): {len(ah)} holdings, ₹{tv:,.0f}")
        acct_summary = [{
            "id": acct.id, "label": label, "type": acct.account_type,
            "value": tv, "invested": sum(h.total_cost or 0 for h in ah),
            "gain": tv - sum(h.total_cost or 0 for h in ah),
            "return_pct": round((tv - sum(h.total_cost or 0 for h in ah)) / max(sum(h.total_cost or 0 for h in ah), 1) * 100, 2),
            "holding_count": len(ah), "source_file": acct.source_file, "statement_date": acct.statement_date,
        }]

        log.info(f"Analyzing account {acct.id} ({label}): {len(ah)} holdings, ₹{tv:,.0f}")

        overview = _build_overview(client.name, ah, tv, acct_summary, False)
        performance = _build_performance(ah, tv, False, label)
        risk = _build_risk(ah, tv, False, label)
        insights = _build_insights(ah, tv, False, overview_data=overview)

        # 4 Specialized LLM Calls per account (Parallelized)
        from app.llm import generate_overview_narrative, generate_performance_narrative, generate_risk_narrative, generate_insights
        import concurrent.futures

        top_sec = max(risk.get("sector_alloc", {}).items(), key=lambda x: x[1], default=("N/A", 0))
        insights_context = {
            "total_value": tv,
            "return_pct": overview.get("return_pct", 0),
            "holding_count": len(ah),
            "top_sector": top_sec[0],
            "top_sector_pct": round(top_sec[1] / tv * 100, 1) if tv else 0,
            "health_score": overview.get("health_score", 0),
        }

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            future_ov = executor.submit(generate_overview_narrative, overview)
            future_perf = executor.submit(generate_performance_narrative, performance)
            future_risk = executor.submit(generate_risk_narrative, risk)
            future_ins = executor.submit(generate_insights, insights, insights_context)
            
            overview["narrative"] = future_ov.result()
            perf_llm = future_perf.result()
            risk_llm = future_risk.result()
            insights = future_ins.result()

        performance["performance_summary"] = perf_llm.get("performance_summary", "")
        performance["top_performer_insight"] = perf_llm.get("top_performer_insight", "")
        performance["concern_areas"] = perf_llm.get("concern_areas", [])
        
        risk["risk_summary"] = risk_llm.get("risk_summary", "")
        risk["key_risks"] = risk_llm.get("key_risks", [])
        risk["recommendations"] = risk_llm.get("recommendations", [])
        
        _save_cache(db, client_id, acct.id, "overview", overview)
        _save_cache(db, client_id, acct.id, "performance", performance)
        _save_cache(db, client_id, acct.id, "risk", risk)
        _save_cache(db, client_id, acct.id, "insights", insights)

        per_account_data[acct.id] = {"overview": overview, "performance": performance, "risk": risk}

    # ── 2. Family analysis (cross-account) ──
    if len(accounts) >= 1:
        total_fam = sum(h.current_value or 0 for h in all_holdings)
        fam_accounts_data = []
        for acct in accounts:
            ah = acct_holdings.get(acct.id, [])
            tv = sum(h.current_value or 0 for h in ah)
            inv = sum(h.total_cost or 0 for h in ah)
            fam_accounts_data.append({
                "id": acct.id, "label": account_map[acct.id], "type": acct.account_type,
                "value": tv, "invested": inv, "gain": tv - inv,
                "return_pct": round((tv - inv) / max(inv, 1) * 100, 2),
                "holding_count": len(ah), "source_file": acct.source_file,
                "statement_date": acct.statement_date,
            })

        log.info(f"Analyzing family view: {len(all_holdings)} holdings across {len(accounts)} accounts, ₹{total_fam:,.0f}")

        fam_overview = _build_overview(client.name, all_holdings, total_fam, fam_accounts_data, True)
        fam_perf = _build_performance(all_holdings, total_fam, True, "Family")
        fam_risk = _build_risk(all_holdings, total_fam, True, "Family", account_map)
        fam_insights = _build_insights(all_holdings, total_fam, True, account_map, fam_overview)

        # 4 Specialized LLM Calls for family (Parallelized)
        top_sec_fam = max(fam_risk.get("sector_alloc", {}).items(), key=lambda x: x[1], default=("N/A", 0))
        fam_insights_context = {
            "total_value": total_fam,
            "return_pct": fam_overview.get("return_pct", 0),
            "holding_count": len(all_holdings),
            "top_sector": top_sec_fam[0],
            "top_sector_pct": round(top_sec_fam[1] / total_fam * 100, 1) if total_fam else 0,
            "health_score": fam_overview.get("health_score", 0),
        }

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            fut_ov = executor.submit(generate_overview_narrative, fam_overview)
            fut_perf = executor.submit(generate_performance_narrative, fam_perf)
            fut_risk = executor.submit(generate_risk_narrative, fam_risk)
            fut_ins = executor.submit(generate_insights, fam_insights, fam_insights_context)

            fam_overview["narrative"] = fut_ov.result()
            fam_perf_llm = fut_perf.result()
            fam_risk_llm = fut_risk.result()
            fam_insights = fut_ins.result()

        fam_perf["performance_summary"] = fam_perf_llm.get("performance_summary", "")
        fam_perf["top_performer_insight"] = fam_perf_llm.get("top_performer_insight", "")
        fam_perf["concern_areas"] = fam_perf_llm.get("concern_areas", [])
        
        fam_risk["risk_summary"] = fam_risk_llm.get("risk_summary", "")
        fam_risk["key_risks"] = fam_risk_llm.get("key_risks", [])
        fam_risk["recommendations"] = fam_risk_llm.get("recommendations", [])

        _save_cache(db, client_id, None, "overview", fam_overview)
        _save_cache(db, client_id, None, "performance", fam_perf)
        _save_cache(db, client_id, None, "risk", fam_risk)
        _save_cache(db, client_id, None, "insights", fam_insights)

    log.info(f"Analysis complete for client {client_id}: {len(per_account_data)} accounts + family")
