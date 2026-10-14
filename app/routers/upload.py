import os
import shutil
import json
import hashlib
import logging
from fastapi import APIRouter, Depends, File, UploadFile, Form, HTTPException, BackgroundTasks, Request
from sqlalchemy.orm import Session
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.database import get_db
from app.models import Client, Account, Holding, UploadedDocument
from app.parsers.detector import detect_and_parse
from app.sector_map import lookup_sector

router = APIRouter(prefix="/upload", tags=["Upload"])
log = logging.getLogger(__name__)

UPLOAD_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "uploads")


@router.post("/")
def upload_file(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    client_id: int = Form(...),
    trigger_analysis: bool = Form(True),
    db: Session = Depends(get_db)
):
    # 1. Validate client
    client = db.get(Client, client_id)
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")
    user = request.state.session
    if user["role"] == "client" and user.get("client_id") != client.id:
        raise HTTPException(status_code=403, detail="You can only upload to your own portfolio.")

    # 2. Reject exact repeats before parsing; filenames alone are not identity.
    safe_name = os.path.basename(file.filename or "upload")
    if not safe_name or safe_name in (".", ".."):
        raise HTTPException(status_code=400, detail="Invalid filename")
    contents = file.file.read()
    if not contents:
        raise HTTPException(status_code=400, detail="The uploaded file is empty.")
    digest = hashlib.sha256(contents).hexdigest()
    duplicate = db.execute(
        select(UploadedDocument.id).where(
            UploadedDocument.client_id == client.id,
            UploadedDocument.sha256 == digest,
        )
    ).first()
    if duplicate:
        raise HTTPException(status_code=409, detail=f"{safe_name} has already been uploaded.")

    client_upload_dir = os.path.join(UPLOAD_DIR, str(client.id))
    os.makedirs(client_upload_dir, exist_ok=True)
    file_path = os.path.join(client_upload_dir, f"{digest}_{safe_name}")
    with open(file_path, "wb") as buffer:
        buffer.write(contents)

    # 3. Parse (PDF or Excel -> ParsedDocument)
    try:
        parsed = detect_and_parse(file_path, safe_name)
    except Exception as e:
        try:
            os.remove(file_path)
        except OSError:
            pass
        raise HTTPException(status_code=400, detail=f"Failed to parse: {e}")

    # Every distinct document is an additive snapshot; older uploads remain visible.
    account = Account(
        client_id=client.id,
        account_type=parsed.account_type,
        account_number=parsed.account_number,
        portfolio_name=parsed.portfolio_name,
        source_file=safe_name,
        statement_date=parsed.statement_date,
        has_cost_data=parsed.has_cost_data,
        raw_markdown=parsed.raw_markdown,
        raw_json=parsed.raw_json,
    )
    db.add(account)

    db.add(UploadedDocument(client_id=client.id, sha256=digest, filename=safe_name))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail=f"{safe_name} has already been uploaded.")
    db.refresh(account)

    # 4b. Screen out aggregate rows using the NSE + BSE masters. A row that is on
    #     neither exchange, carries no ISIN and is not recognisable as a fund,
    #     bond or cash line is a total ("Net Worth", "Stocks"), not a holding.
    dropped_rows = []
    try:
        from app.screener_scraper import SymbolResolver
        from app.instrument_check import screen_holdings
        resolver = SymbolResolver.load()
        parsed.holdings, dropped_rows = screen_holdings(parsed.holdings, resolver)
        for holding, why in dropped_rows:
            log.info("Excluded non-holding row %r (%.2f): %s",
                     holding.security_name, holding.current_value, why)
    except Exception as e:
        # The masters are an extra safety net; never fail an upload over them
        log.warning(f"Listing screen unavailable, keeping all parsed rows: {e}")

    # 5. Sector categorization: deterministic map first, LLM only for what's left
    for ph in parsed.holdings:
        if ph.sector == "Unknown":
            mapped = lookup_sector(ph.security_name)[0]
            if mapped != "Others":
                ph.sector = mapped

    unknown_names = [ph.security_name for ph in parsed.holdings if ph.sector == "Unknown"]
    llm_sectors = _categorize_sectors(unknown_names) if unknown_names else {}

    # 6. Save Holdings
    for ph in parsed.holdings:
        sector = ph.sector
        if sector == "Unknown":
            entry = llm_sectors.get(ph.security_name)
            sector = entry.get("sector", "Others") if isinstance(entry, dict) else "Others"

        db.add(Holding(
            account_id=account.id,
            security_name=ph.security_name,
            current_value=ph.current_value,
            total_cost=ph.total_cost,
            gain_pct=ph.gain_pct,
            weight_pct=ph.weight_pct,
            sector=sector,
            # Store "" as NULL so "has no ISIN" is one value, not two
            isin=getattr(ph, "isin", "") or None,
            quantity=getattr(ph, "quantity", 0) or None,
            nse_symbol=getattr(ph, "nse_symbol", "") or None,
            bse_code=getattr(ph, "bse_code", "") or None,
        ))

    db.commit()

    # 7. Fire background LLM tasks (only on the last file of a batch).
    #    The frontend uploads sequentially and sets trigger_analysis on the final
    #    file, so this one task must cover every account still awaiting analysis —
    #    /api/overview reports is_processing until all of them are done.
    if trigger_analysis:
        background_tasks.add_task(_analyze_pending_accounts, client.id)
        # 8. Then refresh Screener prices + fundamentals for this client's
        #    holdings, so the checklist is populated without a manual run.
        #    Controlled by SCREENER_AUTO_REFRESH; already-fresh prices are skipped.
        background_tasks.add_task(_refresh_market_data, client.id)

    return {
        "status": "success",
        "message": f"Parsed {len(parsed.holdings)} holdings from {safe_name}",
        "client_id": client.id,
        "account_id": account.id,
        "has_cost_data": parsed.has_cost_data,
        "holdings_count": len(parsed.holdings),
        "excluded_rows": [
            {"name": h.security_name, "value": h.current_value, "reason": why}
            for h, why in dropped_rows
        ],
    }


@router.post("/analyze/{client_id}")
def analyze_uploads(
    client_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    client = db.get(Client, client_id)
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")
    user = request.state.session
    if user["role"] == "client" and user.get("client_id") != client.id:
        raise HTTPException(status_code=403, detail="You can only analyze your own portfolio.")
    background_tasks.add_task(_analyze_pending_accounts, client.id)
    background_tasks.add_task(_refresh_market_data, client.id)
    return {"status": "analysis queued", "client_id": client.id}


# ── Background: Screener market data ──

def _refresh_market_data(client_id: int):
    """Fetch prices and company statements for every holding of a client.

    Both halves run, because they come from different places: prices from the
    exchanges' close files via ``refresh_client``, company statements from
    Yahoo via ``refresh_fundamentals``. Only the first used to run here, so a
    freshly uploaded holding showed a price but an empty scorecard until the
    01:00 IST job came round — the panel said the statements "have not been
    captured yet", which was true but reads as a failure to someone who has
    just uploaded a statement.
    """
    try:
        from app.market_refresh import refresh_client_background
        refresh_client_background(client_id)
    except Exception as e:
        # Market data is an enrichment; never let it surface as an upload failure
        log.error(f"Price refresh failed for client {client_id}: {e}")

    try:
        from app.daily_refresh import refresh_fundamentals
        report = refresh_fundamentals(client_id=client_id)
        log.info("Fundamentals for client %s: %s updated, %s without data, %s failed",
                 client_id, report.get("updated"), report.get("no_data"), report.get("failed"))
    except Exception as e:
        log.error(f"Fundamentals refresh failed for client {client_id}: {e}")


# ── Helper: Sector Categorization ──

def _categorize_sectors(names: list[str]) -> dict:
    from app.llm import call_llm
    prompt = (
        "You are a financial data categorization engine. Given a list of Indian security names, "
        "categorize each into one of these strict sectors: ['Financial Services', 'Industrials', "
        "'Pharma & Healthcare', 'Consumer Goods', 'Telecom', 'IT & Tech', 'Automobile', "
        "'Real Estate & Infrastructure', 'Energy & Utilities', 'Metals & Mining', "
        "'Materials & Cement', 'Agriculture', 'Media & Entertainment', 'Chemicals', 'Others']. "
        "Return a valid JSON object where keys are the EXACT security names, "
        "and values are objects with key 'sector'.\n\n"
        f"Securities: {json.dumps(list(set(names)))}"
    )
    try:
        resp = call_llm("You return valid JSON.", prompt)
        if not resp:
            return {}
        data = json.loads(resp)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        log.warning(f"Sector LLM failed: {e}")
        return {}


# ── Background: Generate LLM JSON payloads ──
# Uses the EXACT prompts proven to work in test_overview_llm.py and test_performance_llm.py

def _analyze_pending_accounts(client_id: int):
    """Run the LLM payload generation for every account of a client that lacks it."""
    db = next(get_db())
    try:
        pending = db.execute(
            select(Account).where(Account.client_id == client_id)
        ).scalars().all()
        account_ids = [a.id for a in pending if a.overview_json is None]
    except Exception as e:
        log.error(f"Could not list accounts for client {client_id}: {e}")
        return
    finally:
        db.close()

    for aid in account_ids:
        _generate_llm_payloads(aid)


def _generate_llm_payloads(account_id: int):
    from app.llm import call_llm, call_llm_json
    db = next(get_db())
    try:
        account = db.get(Account, account_id)
        if not account or not account.raw_markdown:
            return

        md = account.raw_markdown

        # ── 1. Overview JSON (from test_overview_llm.py) ──
        overview_sys = """You are a financial data extraction AI.
You are given a raw markdown table extracted from a portfolio statement.
Your ONLY job is to extract the TOP-LEVEL OVERVIEW metrics and ASSET ALLOCATION of the entire portfolio.

Extract the following into strict JSON:
{
    "overview": {
        "portfolio_value": float (total current market value of all assets),
        "invested_value": float (total cost or invested amount, or null if not available),
        "total_gain": float (total unrealized gain/loss, or null if not available),
        "portfolio_return_pct": float (overall percentage return, or null if not available)
    },
    "asset_allocation": [
        {
            "asset_class": string (e.g. "Equity", "Cash and Equivalent"),
            "value": float (net total value of this asset class)
        }
    ]
}

RULES:
1. Return ONLY the JSON object.
2. DO NOT extract percentages for asset allocation. We only want the absolute net 'value'.
3. For 'Cash and Equivalent', ensure you extract the NET total value.
4. If invested_value, total_gain, or portfolio_return_pct are NOT available in the document, return null for those fields."""

        try:
            result = call_llm_json(overview_sys, md)
            if result:
                # Python post-processing for exact percentages (from test_overview_llm.py)
                total_val = result.get("overview", {}).get("portfolio_value", 0) or 0
                for asset in result.get("asset_allocation", []):
                    asset_val = asset.get("value", 0) or 0
                    asset["calculated_percentage"] = round((asset_val / total_val * 100), 2) if total_val else 0
                account.overview_json = json.dumps(result)
        except Exception as e:
            log.warning(f"Overview LLM failed for account {account_id}: {e}")

        # ── 2. Performance JSON (from test_performance_llm.py — EXACT prompt) ──
        perf_prompt = """You are a financial data extraction engine. 
I am going to give you a raw Markdown document of a portfolio statement.
I need you to extract the performance data and format it STRICTLY into this exact JSON schema for my UI dashboard:

{
  "kpis": {
    "absolute_return_pct": (float or null, e.g. 31.78),
    "best_performing_asset": "(string or null, name of asset with highest gain %)"
  },
  "top_gainers": [
    {"security": "(string)", "gain_pct": (float), "value": (float)}
  ],
  "top_losers": [
    {"security": "(string)", "gain_pct": (float, should be negative), "value": (float)}
  ]
}

If the document DOES NOT contain historical cost or gain/loss data (e.g. it is just a holding statement with current values), you MUST return null for `absolute_return_pct` and `best_performing_asset`, and return empty arrays `[]` for top_gainers and top_losers. Do not make up or hallucinate gains.

Extract the top 5 gainers and top 5 losers.

Here is the raw Markdown document:
""" + md + "\nReturn ONLY valid JSON."

        try:
            account.performance_json = call_llm("You return strict JSON.", perf_prompt)
        except Exception as e:
            log.warning(f"Performance LLM failed for account {account_id}: {e}")

        # ── 3. Risk Analysis JSON ──
        risk_prompt = """You are a portfolio risk analyst.
Analyze this raw Markdown portfolio statement for risk factors.
Format STRICTLY into this JSON schema:
{
  "concentration_risk": "(string: High/Medium/Low)",
  "top_holding_pct": (float or null, percentage of portfolio in largest single holding),
  "sector_concentration": [{"sector": "(string)", "pct": (float)}],
  "risk_flags": ["(string description of each specific risk found)"],
  "overall_risk_level": "(string: High/Medium/Low)"
}

If the document does not contain enough data to determine a field, return null for that field.

Here is the data:
""" + md + "\nReturn ONLY valid JSON."

        try:
            account.risk_analysis_json = call_llm("You return strict JSON.", risk_prompt)
        except Exception as e:
            log.warning(f"Risk LLM failed for account {account_id}: {e}")

        # ── 4. Insights JSON ──
        insights_prompt = """You are an AI financial advisor generating targeted alerts for a client.
Analyze this raw Markdown portfolio statement and generate 4-6 actionable insights.
Each insight must have a type, title, and description.
Format STRICTLY into this JSON schema:
{
  "insights": [
    {"type": "(danger|warning|success|info)", "title": "(2-3 words)", "description": "(1 sentence with specific numbers from the data)"}
  ]
}

Types: danger = losses or high risk, warning = caution needed, success = strong performance, info = neutral observation.
Be specific — cite actual stock names and numbers from the data.

Here is the data:
""" + md + "\nReturn ONLY valid JSON."

        try:
            account.insights_json = call_llm("You return strict JSON.", insights_prompt)
        except Exception as e:
            log.warning(f"Insights LLM failed for account {account_id}: {e}")

        db.commit()
        log.info(f"LLM payloads generated for account {account_id}")
    except Exception as e:
        log.error(f"Background LLM task failed: {e}")
    finally:
        db.close()
