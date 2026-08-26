import pdfplumber
import pandas as pd
import json
import os
import re
import numpy as np

class ParsedDocument:
    def __init__(self):
        self.account_type = ""
        self.account_number = "Unknown"
        self.portfolio_name = ""
        self.statement_date = ""
        self.has_cost_data = False
        self.raw_markdown = ""
        self.raw_json = ""
        self.holdings = []

class ParsedHolding:
    def __init__(self):
        self.security_name = ""
        self.current_value = 0.0
        self.total_cost = 0.0
        self.gain_pct = 0.0
        self.weight_pct = 0.0
        self.sector = "Unknown"

def clean_num(val):
    if pd.isna(val) or str(val).strip() == "": return 0.0
    val_str = str(val).replace(',', '').replace('%', '').strip()
    if val_str.startswith('(') and val_str.endswith(')'): val_str = '-' + val_str[1:-1]
    try: return float(val_str)
    except: return 0.0

def detect_and_parse(file_path: str, file_name: str = "") -> ParsedDocument:
    ext = os.path.splitext(file_name or file_path)[1].lower()
    
    if ext in ['.xlsx', '.xls']:
        from .excel import ExcelParser
        parser = ExcelParser()
        return parser.parse(file_path, file_name)

    doc = ParsedDocument()
    original_filename = file_name or file_path
    doc.portfolio_name = original_filename
    
    if "demat" in original_filename.lower():
        doc.account_type = "demat"
        match = re.search(r'\d{4}', original_filename)
        doc.account_number = match.group(0) if match else "Unknown"
    else:
        doc.account_type = "pms"
        doc.account_number = "Enam-Vision"
        
    # ── Extract tables from PDF ──
    # Strategy: Try default 'lines' first (works for Demat with physical lines).
    # If columns are mushed (PMS has no lines), fall back to 'text' strategy.
    all_data = []
    with pdfplumber.open(file_path) as pdf:
        for page in pdf.pages:
            tables = page.extract_tables()
            mushed = False
            if tables:
                for t in tables:
                    if len(t) > 2 and len(t[0]) <= 2:
                        if sum(len(str(c)) for c in t[0] if c) > 40:
                            mushed = True
                            break
            if not tables or mushed:
                tables = page.extract_tables(table_settings={
                    "vertical_strategy": "text",
                    "horizontal_strategy": "text"
                })
                
            for table in tables:
                for row in table:
                    clean_row = [str(cell).replace('\n', ' ').strip() if cell else "" for cell in row]
                    if not any(clean_row) or all(c == "" or c == " " for c in clean_row):
                        continue
                    all_data.append(clean_row)
    
    if not all_data:
        return doc  # Empty PDF, return empty document
                    
    # ── Build DataFrame ──
    max_cols = max(len(row) for row in all_data)
    df = pd.DataFrame([row + [""] * (max_cols - len(row)) for row in all_data])
    
    # Detect header row by looking for "value", "price", or "rate"
    header_idx = -1
    for idx, row in df.iterrows():
        row_str = " ".join([str(val).lower() for val in row])
        if "value" in row_str or "price" in row_str or "rate" in row_str:
            header_idx = idx
            break
            
    if header_idx != -1:
        headers = [str(h).strip().lower() if str(h).strip() else f"unnamed_{i}"
                   for i, h in enumerate(df.iloc[header_idx].values)]
        df.columns = headers
        df = df.iloc[header_idx + 1:].reset_index(drop=True)
        
    df = df.loc[:, ~df.columns.str.contains('^unnamed')]
    
    # Forward-fill merged cells (e.g. "Free Balance" spanning multiple rows in Demat)
    if 'account type' in df.columns:
        df['account type'] = df['account type'].replace("", np.nan).ffill()
    
    # ── Store RAW unmodified payloads for the LLM ──
    doc.raw_markdown = df.to_markdown(index=False)
    doc.raw_json = json.dumps(df.to_dict(orient='records'))
        
    # ── LLM Header Mapping ──
    # Instead of hardcoding a dictionary, we let the LLM dynamically map
    # whatever messy headers the bank uses to our strict 5-column schema.
    from app.llm import call_llm_json
    raw_columns = list(df.columns)
    sys_prompt = """You are a financial data mapping AI.
Given a list of raw bank statement headers, map them to our strict database schema.
Valid Columns: ["security_name", "current_value", "total_cost", "gain_pct", "weight_pct"]

Map each raw header to the closest valid database column.
CRITICAL RULES:
1. "current_value" means the total absolute market value (e.g. "Value", "Amount", "Market Value", "Total Value").
2. "total_cost" means the TOTAL invested amount (e.g. "Cost", "Invested", "Total Cost").
3. NEVER map "Price", "Rate", "NAV", "CMP", "Unit Cost", or "Average Cost" to ANY of the valid columns. Ignore them completely.
4. Ignore irrelevant columns (like "ISIN", "Status", "Account Type", "Scrip Type", "Quantity").
Return ONLY a JSON dictionary: {"raw_header_name": "valid_database_column"}"""

    try:
        mapping = call_llm_json(sys_prompt, str(raw_columns))
        if mapping:
            df = df.rename(columns=mapping)
        else:
            return doc
    except Exception as e:
        print(f"LLM Header Mapping Failed: {e}")
        return doc
        
    # ── Set has_cost_data flag ──
    doc.has_cost_data = 'total_cost' in df.columns
    
    # ── Build Holdings ──
    for r in df.to_dict(orient='records'):
        if 'security_name' not in r or 'current_value' not in r:
            continue
        name = str(r['security_name']).strip()
        val = clean_num(r['current_value'])
        if not name or val == 0 or "Total" in name or str(r['current_value']) == 'nan':
            continue
            
        h = ParsedHolding()
        h.security_name = name.replace(' LTD', '').replace(' LIMITED', '').title()
        h.current_value = val
        h.total_cost = clean_num(r.get('total_cost', 0))
        h.gain_pct = clean_num(r.get('gain_pct', 0))
        h.weight_pct = clean_num(r.get('weight_pct', 0))
        h.asset_class = "Cash and Equivalent" if any(k in name.lower() for k in ["cash", "payable", "receivable", "income"]) else "Equity"
        doc.holdings.append(h)
        
    return doc
