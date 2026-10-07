import pdfplumber
import pandas as pd
import json
import os
import re
import numpy as np

from app.sector_map import lookup_sector

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
        # isin is the reliable join key to market data; quantity is what makes
        # mark-to-market revaluation possible (qty x live price).
        self.isin = ""
        self.quantity = 0.0
        # Exchange codes lifted straight from the statement when it supplies them.
        # A BSE code is the only way to reach a BSE-only listing on Screener.
        self.nse_symbol = ""
        self.bse_code = ""

def clean_num(val):
    if pd.isna(val) or str(val).strip() == "": return 0.0
    val_str = str(val).replace(',', '').replace('%', '').strip()
    if val_str.startswith('(') and val_str.endswith(')'): val_str = '-' + val_str[1:-1]
    try: return float(val_str)
    except: return 0.0

# Core columns the LLM is allowed to fill in when the regex rules come up short.
SCHEMA_COLUMNS = ("security_name", "current_value", "total_cost", "gain_pct", "weight_pct")

# Extras found by regex only. They are never worth an LLM round-trip, but they
# are what market-data enrichment depends on, so we keep them when present.
EXTRA_COLUMNS = ("isin", "quantity", "nse_symbol", "bse_code")

# Ordered patterns per schema column. Patterns are tried in order and the first
# header that matches (and trips no exclusion) wins, so the most specific
# spelling beats the generic one — "market value" before a bare "value", and
# "inv. cost" before a bare "cost".
_HEADER_RULES = (
    ("security_name",
     (r'company\s*name', r'scheme\s*name', r'security', r'scrip\s*name',
      r'instrument', r'particulars', r'stock', r'\bname\b'),
     (r'scrip\s*type', r'account\s*type', r'\bnav\b', r'date', r'\bcode\b', r'symbol')),
    ("current_value",
     (r'market\s*value', r'cur\.?\s*value', r'current\s*value', r'closing\s*value',
      r'mkt\.?\s*val', r'\bvalue\b', r'\bamount\b'),
     (r'unit', r'\bnav\b', r'price', r'rate', r'aver', r'\bavg\b', r'cost', r'\bper\b')),
    ("total_cost",
     (r'inv\.?\s*cost', r'total\s*cost', r'invested', r'purchase', r'\bcost\b',
      r'book\s*value'),
     (r'unit', r'aver', r'\bavg\b', r'\bper\b', r'price')),
    ("gain_pct",
     (r'%\s*g\s*/?\s*l', r'abs\.?\s*rtn', r'absolute\s*return', r'gain\s*%',
      r'return\s*%', r'%\s*(gain|return|chg|change)', r'\bxirr\b'),
     ()),
    ("weight_pct",
     (r'%\s*assets', r'weight', r'%\s*(of\s*)?(port|holding)', r'allocation'),
     ()),
    ("isin",
     (r'\bisin\b',),
     ()),
    # Demat statements call the share count "Balance"; funds call it "Units".
    ("quantity",
     (r'\bquantity\b', r'\bqty\b', r'\bunits\b', r'\bshares\b', r'\bbalance\b'),
     (r'value', r'amount', r'cost', r'price')),
    # Exchange codes, when the statement supplies them
    ("nse_symbol",
     (r'nse[\s_]*symbol', r'nse[\s_]*code', r'\bnse\b', r'\bsymbol\b', r'\bticker\b'),
     (r'\bbse\b',)),
    ("bse_code",
     (r'bse[\s_]*code', r'\bbse\b', r'scrip[\s_]*code', r'security[\s_]*code'),
     (r'\bnse\b',)),
    # Per-unit prices. Never a value or a cost on their own, but a statement that
    # reports only a price and a quantity (several broker holdings exports do)
    # has everything needed to derive both — see _derive_missing_amounts.
    ("unit_price",
     (r'(prev\.?\w*|previous|last|closing|close|market)\s*(closing\s*)?price',
      r'\bltp\b', r'\bcmp\b', r'current\s*price', r'\bnav\b', r'\brate\b'),
     (r'aver', r'\bavg\b', r'cost', r'buy', r'purchase')),
    ("unit_cost",
     (r'(aver\w*|avg\.?)\s*(price|cost|rate)?', r'buy\s*(price|rate)',
      r'purchase\s*(price|rate)', r'cost\s*(price|per|\/)\s*(unit|share)?'),
     (r'\btotal\b', r'\bvalue\b')),
)


def map_headers_deterministic(raw_columns) -> dict:
    """Map raw statement headers onto the schema with regex rules.

    Returns {raw_header: schema_column}. Each schema column is claimed at most
    once and each raw header is used at most once, so the result can never
    produce duplicate DataFrame columns.
    """
    mapping: dict = {}
    for target, patterns, exclusions in _HEADER_RULES:
        found = None
        for pattern in patterns:
            for header in raw_columns:
                h = str(header).lower()
                if header in mapping:
                    continue
                if not re.search(pattern, h):
                    continue
                if any(re.search(x, h) for x in exclusions):
                    continue
                found = header
                break
            if found is not None:
                break
        if found is not None:
            mapping[found] = target
    return mapping


# Whole-label matches for rows that are portfolio aggregates or asset-class
# headers rather than securities. Matched against the FULL cell, so a company
# called "Apar Industries" or "Nesco Limited" is never caught by "industries".
_AGGREGATE_LABELS = {
    "net worth", "networth", "net value", "grand total", "total", "sub total",
    "subtotal", "portfolio", "portfolio value", "portfolio total", "summary",
    "holdings", "grand totals",
    # Asset-class header rows that repeat a subtotal
    "stocks", "stock", "equity", "equities", "mutual funds", "mutual fund",
    "debt", "debt funds", "bonds", "gold", "etf", "etfs", "others", "other",
    "cash and equivalent", "cash & equivalent", "cash and equivalents",
    "liquid funds", "hybrid", "direct equity",
}

# Vendor footers ("Powered by MProfit") and similar non-data lines
_FOOTER_RE = re.compile(r'^(powered by|generated (by|on)|disclaimer|report as on|page \d)', re.I)


def _is_total_row(name: str) -> bool:
    """True for aggregate, asset-class-header and footer rows.

    These carry a value but are not securities. Counting them double- or
    triple-counts the portfolio, so they must never become holdings.
    """
    if not name:
        return True
    text = re.sub(r'\s+', ' ', str(name)).strip()
    # "Total :-", "Grand Total -", "Equity:" etc.
    bare = re.sub(r'[\s:\-–—.,()]+$', '', text).strip().lower()
    if bare in _AGGREGATE_LABELS:
        return True
    if _FOOTER_RE.match(text):
        return True
    # Any row that still says "total" somewhere ("Liquid Fund Total :-")
    return bool(re.search(r'\btotals?\b', text, re.IGNORECASE))


def _clean_code(value) -> str:
    """Normalise an exchange-code cell; spreadsheets hand BSE codes back as floats."""
    text = str(value or "").strip().upper()
    if text in ("", "NAN", "NONE", "-"):
        return ""
    if re.match(r'^\d+\.0$', text):
        text = text[:-2]
    return re.sub(r'[^A-Z0-9&-]', '', text)


_ISIN_RE = re.compile(r'^[A-Z]{2}[A-Z0-9]{9}[0-9]$')


def normalise_isin(value) -> str:
    """Return a clean ISIN, or "" if the cell does not hold one.

    Statement cells pick up stray whitespace and the occasional footnote, and a
    wrong ISIN is worse than none at all: it would join a holding to the wrong
    company's market data.
    """
    candidate = re.sub(r'[^A-Za-z0-9]', '', str(value or "")).upper()
    return candidate if _ISIN_RE.match(candidate) else ""


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
        
    # Columns are only strings when a header row was found; a RangeIndex has no .str
    df.columns = [str(c) for c in df.columns]
    df = df.loc[:, ~df.columns.str.contains('^unnamed')]
    
    # Forward-fill merged cells (e.g. "Free Balance" spanning multiple rows in Demat)
    if 'account type' in df.columns:
        df['account type'] = df['account type'].replace("", np.nan).ffill()
    
    # ── Store RAW unmodified payloads for the LLM ──
    doc.raw_markdown = df.to_markdown(index=False)
    doc.raw_json = json.dumps(df.to_dict(orient='records'))
        
    populate_holdings(df, doc)
    return doc


def _derive_missing_amounts(df) -> None:
    """Fill current_value / total_cost from quantity x price, in place.

    Several broker holdings exports report a quantity and a per-unit price but
    no money column at all, so there is nothing to map and the statement used to
    be rejected outright. Quantity x price is the same arithmetic the broker
    would have printed. Nothing is overwritten: a statement that states its own
    value keeps it, because a stated figure may include charges this product
    cannot see.
    """
    if "quantity" not in df.columns:
        return
    qty = df["quantity"].map(clean_num)

    for target, price_column in (("current_value", "unit_price"),
                                 ("total_cost", "unit_cost")):
        if target in df.columns or price_column not in df.columns:
            continue
        price = df[price_column].map(clean_num)
        derived = qty * price
        if derived.fillna(0).abs().sum() == 0:
            continue                      # nothing usable; leave the column absent
        df[target] = derived
        print(f"Derived {target} as quantity x {price_column}")


def populate_holdings(df, doc: ParsedDocument) -> ParsedDocument:
    """Map a statement DataFrame onto the schema and fill ``doc.holdings``.

    Shared by the PDF and Excel parsers so header mapping and row filtering
    behave identically for both.
    """
    # ── Header Mapping ──
    # Deterministic regex rules first (they are reliable and free), then the LLM
    # only for schema columns the rules could not find — an unseen broker format.
    raw_columns = list(df.columns)
    mapping = map_headers_deterministic(raw_columns)

    from app.llm import call_llm_json
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

    missing = [c for c in SCHEMA_COLUMNS if c not in mapping.values()]
    if missing:
        try:
            unmapped = [c for c in raw_columns if c not in mapping]
            llm_mapping = call_llm_json(sys_prompt, str(unmapped)) if unmapped else None
            if isinstance(llm_mapping, dict):
                # The LLM happily maps several raw headers onto the same schema
                # column (e.g. both "Market Value Gain /" and "% G/L" -> gain_pct).
                # Renaming blindly would create duplicate columns and pandas then
                # silently drops all but one. Let the first claimant win, and never
                # override a column the deterministic rules already found.
                claimed = set(mapping.values())
                for raw_header in unmapped:
                    target = llm_mapping.get(raw_header)
                    if target in missing and target not in claimed:
                        mapping[raw_header] = target
                        claimed.add(target)
        except Exception as e:
            print(f"LLM Header Mapping Failed: {e}")

    df = df.copy()

    df = df.rename(columns=mapping)
    # Defensive: a raw statement can itself repeat a header label
    df = df.loc[:, ~df.columns.duplicated()]

    # A broker export may identify a security only by its ticker ("Symbol"),
    # with no company-name column at all. The ticker is a perfectly good
    # identifier — the resolver maps it to a company later — so it stands in as
    # the name rather than the statement being rejected. It is copied, not
    # moved, because the ticker is also the key that prices the holding.
    if "security_name" not in df.columns:
        for source in ("nse_symbol", "bse_code"):
            if source in df.columns:
                df["security_name"] = df[source]
                print(f"No name column; using {source!r} as the security name")
                break

    if "security_name" not in df.columns:
        print(f"Could not locate a name column in {raw_columns} (mapped: {mapping})")
        return doc

    _derive_missing_amounts(df)

    if "current_value" not in df.columns:
        print(f"Could not locate or derive a value column in {raw_columns} (mapped: {mapping})")
        return doc
        
    # ── Set has_cost_data flag ──
    doc.has_cost_data = 'total_cost' in df.columns

    # When the statement reports a share/unit count, a row without one is an
    # aggregate ("Net Worth", "Stocks") or a footer, not a security. This is the
    # structural test; the label test in _is_total_row is the second net.
    has_quantity_column = 'quantity' in df.columns

    # ── Build Holdings ──
    for r in df.to_dict(orient='records'):
        if 'security_name' not in r or 'current_value' not in r:
            continue
        name = str(r['security_name']).strip()
        val = clean_num(r['current_value'])
        if not name or val == 0 or _is_total_row(name) or str(r['current_value']) == 'nan':
            continue
        qty = clean_num(r.get('quantity', 0))
        if has_quantity_column and qty == 0:
            # Carries a value but no units: an aggregate or asset-class header.
            # Tested for exactly zero, not <= 0 — a payable or short position
            # legitimately carries a negative quantity.
            continue
            
        h = ParsedHolding()
        h.security_name = name.replace(' LTD', '').replace(' LIMITED', '').title()
        h.current_value = val
        h.total_cost = clean_num(r.get('total_cost', 0))
        h.gain_pct = clean_num(r.get('gain_pct', 0))
        h.weight_pct = clean_num(r.get('weight_pct', 0))
        h.isin = normalise_isin(r.get('isin', ''))
        h.quantity = qty
        h.nse_symbol = _clean_code(r.get('nse_symbol', ''))
        h.bse_code = _clean_code(r.get('bse_code', ''))
        # Deterministic map first; leave "Unknown" on a miss so the upload router
        # knows to ask the LLM to categorise it.
        mapped_sector = lookup_sector(h.security_name)[0]
        h.sector = mapped_sector if mapped_sector != "Others" else "Unknown"
        doc.holdings.append(h)

    # Third net, independent of labels and of any quantity column: a row whose
    # value equals the sum of every smaller row is arithmetically a total, not a
    # holding. This catches an aggregate however it is spelled.
    doc.holdings = _drop_arithmetic_totals(doc.holdings)
    return doc


def _drop_arithmetic_totals(holdings: list) -> list:
    """Remove rows whose value equals the sum of all strictly smaller rows.

    "Net Worth" and an asset-class subtotal both equal the sum of the securities
    beneath them, so both are caught even when their labels are unrecognised.
    Rows of equal value are compared against the same smaller set, so a repeated
    subtotal is removed too.

    The tolerance is deliberately tight and a minimum number of contributing rows
    is required, so a single large genuine holding is never mistaken for a total.
    """
    if len(holdings) < 4:
        return holdings

    ordered = sorted(holdings, key=lambda h: h.current_value, reverse=True)
    values = [h.current_value for h in ordered]
    # suffix_sums[i] = sum of values[i:]
    suffix_sums = [0.0] * (len(values) + 1)
    for i in range(len(values) - 1, -1, -1):
        suffix_sums[i] = suffix_sums[i + 1] + values[i]

    drop = set()
    for index, holding in enumerate(ordered):
        value = holding.current_value
        if value <= 0:
            continue
        # First position whose value is strictly smaller than this one
        start = index + 1
        while start < len(values) and abs(values[start] - value) < 1e-9:
            start += 1
        smaller_count = len(values) - start
        if smaller_count < 3:
            continue
        if abs(suffix_sums[start] - value) <= max(1.0, abs(value) * 0.001):
            drop.add(id(holding))

    if not drop:
        return holdings
    return [h for h in holdings if id(h) not in drop]
