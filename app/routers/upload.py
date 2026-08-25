import os
import shutil
from fastapi import APIRouter, Depends, File, UploadFile, Form, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from sqlalchemy import select

from app.database import get_db
from app.models import Client, Account, Holding
from app.parsers.detector import detect_and_parse
from app.sector_map import lookup_sector
from app.analysis import run_analysis

router = APIRouter(prefix="/upload", tags=["Upload"])

UPLOAD_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "uploads")

@router.post("/")
def upload_file(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    client_id: int = Form(...),
    trigger_analysis: str = Form("true"),
    db: Session = Depends(get_db)
):
    """Upload a document for an existing client. Create the client first via POST /client."""
    client = db.get(Client, client_id)
    if not client:
        raise HTTPException(status_code=404, detail="Client not found. Create the client first via POST /api/client")

    os.makedirs(UPLOAD_DIR, exist_ok=True)
    file_path = os.path.join(UPLOAD_DIR, file.filename)
    
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
        
    try:
        parsed = detect_and_parse(file_path, file.filename)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to parse file: {str(e)}")
            
    account = db.execute(
        select(Account).where(
            Account.client_id == client.id,
            Account.account_number == parsed.account_number
        )
    ).scalars().first()
    
    if account:
        holdings_to_delete = db.execute(select(Holding).where(Holding.account_id == account.id)).scalars().all()
        for h in holdings_to_delete:
            db.delete(h)
        
        account.account_type = parsed.account_type
        account.portfolio_name = parsed.portfolio_name
        account.source_file = file.filename
        account.statement_date = parsed.statement_date
    else:
        account = Account(
            client_id=client.id,
            account_type=parsed.account_type,
            account_number=parsed.account_number,
            portfolio_name=parsed.portfolio_name,
            source_file=file.filename,
            statement_date=parsed.statement_date
        )
        db.add(account)
        db.commit()
        db.refresh(account)
        
    import json
    from app.llm import call_llm
    
    unknown_securities = []
    for ph in parsed.holdings:
        sector, market_cap = lookup_sector(ph.security_name)
        if sector == "Others" or sector == "Unknown":
            unknown_securities.append(ph.security_name)
            
    llm_sectors = {}
    if unknown_securities:
        prompt = (
            "You are a financial data categorization engine. Given a list of Indian security/stock/fund names, "
            "categorize them into one of these strict sectors: ['Financial Services', 'Industrials', 'Pharma', "
            "'Consumer Goods', 'Telecom', 'IT', 'Automobile', 'Healthcare', 'Hospitality', 'Chemicals', 'Others']. "
            "And assign a market cap: ['Large Cap', 'Mid Cap', 'Small Cap', 'Unknown']. "
            "Return a valid JSON object ONLY, where keys are the EXACT security names provided, "
            "and values are objects with keys 'sector' and 'market_cap'.\n\n"
            f"Securities to categorize: {json.dumps(list(set(unknown_securities)))}"
        )
        try:
            resp = call_llm("You return valid JSON.", prompt)
            llm_sectors = json.loads(resp)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"LLM sector categorization failed: {e}")

    holdings_to_add = []
    for ph in parsed.holdings:
        sector, market_cap = lookup_sector(ph.security_name)
        if sector == "Others" or sector == "Unknown":
            if ph.security_name in llm_sectors:
                sector = llm_sectors[ph.security_name].get("sector", "Others")
                market_cap = llm_sectors[ph.security_name].get("market_cap", "Unknown")
                
        h = Holding(
            account_id=account.id,
            security_name=ph.security_name,
            isin=ph.isin,
            quantity=ph.quantity,
            avg_cost=ph.avg_cost,
            total_cost=ph.total_cost,
            current_price=ph.current_price,
            current_value=ph.current_value,
            accrued_income=ph.accrued_income,
            unrealized_gain=ph.unrealized_gain,
            gain_pct=ph.gain_pct,
            weight_pct=ph.weight_pct,
            sector=sector or ph.sector,
            asset_class=ph.asset_class,
            market_cap=market_cap,
            scrip_type=ph.scrip_type,
            status=ph.status
        )
        holdings_to_add.append(h)
        
    db.add_all(holdings_to_add)
    db.commit()

    if trigger_analysis.lower() == "true":
        from app.models import AnalysisCache
        cache_to_delete = db.execute(select(AnalysisCache).where(AnalysisCache.client_id == client.id)).scalars().all()
        for c in cache_to_delete:
            db.delete(c)
        db.commit()
        # Pass None for target_account_id so it analyzes ALL accounts for this client
        background_tasks.add_task(_safe_run_analysis, client.id, None)

    return {
        "status": "success",
        "message": f"Parsed {len(holdings_to_add)} holdings from {file.filename}",
        "client_id": client.id,
        "account_id": account.id,
        "holdings_count": len(holdings_to_add)
    }

def _safe_run_analysis(client_id: int, account_id: int | None):
    # Get a fresh DB session for the background task
    db = next(get_db())
    try:
        run_analysis(db, client_id, target_account_id=account_id)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Analysis failed after upload: {e}")
    finally:
        db.close()
