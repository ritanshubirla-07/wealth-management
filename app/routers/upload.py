import os
import shutil
import json
import logging
from fastapi import APIRouter, Depends, File, UploadFile, Form, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from sqlalchemy import select

from app.database import get_db
from app.models import Client, Account, Holding
from app.parsers.detector import detect_and_parse
from app.sector_map import lookup_sector

router = APIRouter(prefix="/upload", tags=["Upload"])
log = logging.getLogger(__name__)

UPLOAD_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "uploads")


@router.post("/")
def upload_file(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    client_id: int = Form(...),
    db: Session = Depends(get_db)
):
    # 1. Validate client
    client = db.get(Client, client_id)
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    # 2. Save file to disk
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    file_path = os.path.join(UPLOAD_DIR, file.filename)
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    # 3. Parse (PDF or Excel -> ParsedDocument)
    try:
        parsed = detect_and_parse(file_path, file.filename)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to parse: {e}")

    # 4. Upsert Account based on the original filename so every document is stored separately
    account = db.execute(
        select(Account).where(
            Account.client_id == client.id,
            Account.source_file == file.filename
        )
    ).scalars().first()

    if account:
        # Wipe old holdings on re-upload
        for h in db.execute(select(Holding).where(Holding.account_id == account.id)).scalars().all():
            db.delete(h)
        account.account_type = parsed.account_type
        account.portfolio_name = parsed.portfolio_name
        account.source_file = file.filename
        account.statement_date = parsed.statement_date
        account.has_cost_data = parsed.has_cost_data
        account.raw_markdown = parsed.raw_markdown
        account.raw_json = parsed.raw_json
        # Clear stale LLM caches
        account.overview_json = None
        account.performance_json = None
        account.insights_json = None
        account.risk_analysis_json = None
    else:
        account = Account(
            client_id=client.id,
            account_type=parsed.account_type,
            account_number=parsed.account_number,
            portfolio_name=parsed.portfolio_name,
            source_file=file.filename,
            statement_date=parsed.statement_date,
            has_cost_data=parsed.has_cost_data,
            raw_markdown=parsed.raw_markdown,
            raw_json=parsed.raw_json,
        )
        db.add(account)

    db.commit()
    db.refresh(account)

    # 5. Sector categorization via LLM for unknowns
    unknown_names = [ph.security_name for ph in parsed.holdings if ph.sector == "Unknown"]
    llm_sectors = _categorize_sectors(unknown_names) if unknown_names else {}

    # 6. Save Holdings
    for ph in parsed.holdings:
        sector = ph.sector
        if sector == "Unknown" and ph.security_name in llm_sectors:
            sector = llm_sectors[ph.security_name].get("sector", "Others")

        db.add(Holding(
            account_id=account.id,
            security_name=ph.security_name,
            current_value=ph.current_value,
            total_cost=ph.total_cost,
            gain_pct=ph.gain_pct,
            weight_pct=ph.weight_pct,
            sector=sector,
        ))

    db.commit()

    # 7. Fire background LLM tasks
    background_tasks.add_task(_generate_llm_payloads, account.id)

    return {
        "status": "success",
        "message": f"Parsed {len(parsed.holdings)} holdings from {file.filename}",
        "client_id": client.id,
        "account_id": account.id,
        "has_cost_data": parsed.has_cost_data,
        "holdings_count": len(parsed.holdings),
    }


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
        return json.loads(resp)
    except Exception as e:
        log.warning(f"Sector LLM failed: {e}")
        return {}


# ── Background: Generate LLM JSON payloads ──
# Uses the EXACT prompts proven to work in test_overview_llm.py and test_performance_llm.py

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
