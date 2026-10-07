"""Screener.in CSV ingestion and holding -> market-data matching.

Screener.in has no public API. A premium subscription can export any screen to
CSV with up to 50 user-chosen columns, so the column set differs from export to
export. Everything here is therefore driven by tolerant header rules rather than
fixed positions, and any column we do not recognise is preserved verbatim in
``extras_json`` instead of being dropped.
"""

import json
import re
from typing import Optional

# ── Header mapping ───────────────────────────────────────────────────────────
# Each entry is (field, ordered patterns, exclusions). Most specific first, so
# "Mar Cap" beats a bare "Cap" and "Qtr Sales Var %" does not steal "Sales".
_COLUMN_RULES = (
    ("name",           (r'^name$', r'company\s*name', r'^company$'), ()),
    ("nse_code",       (r'nse\s*code', r'^nse$', r'^symbol$'), ()),
    ("bse_code",       (r'bse\s*code', r'^bse$', r'scrip\s*code'), ()),
    ("isin",           (r'\bisin\b',), ()),
    ("industry",       (r'^industry$', r'\bindustry\b', r'^sector$', r'\bsector\b'), ()),
    ("cmp",            (r'^cmp', r'current\s*market\s*price', r'current\s*price',
                        r'^price\s*rs', r'^price$'), (r'book', r'high', r'low', r'change')),
    ("market_cap",     (r'mar\w*\s*cap', r'market\s*cap', r'\bmcap\b'), ()),
    ("pe",             (r'^p\s*/?\s*e$', r'price\s*to\s*earning', r'\bpe\s*ratio\b'), (r'industry',)),
    ("roe",            (r'\broe\b', r'return\s*on\s*equity'), ()),
    ("roce",           (r'\broce\b', r'return\s*on\s*capital'), ()),
    ("debt_to_equity", (r'debt\s*/?\s*eq', r'debt\s*to\s*equity'), ()),
    ("dividend_yield", (r'div\w*\s*yl?d', r'dividend\s*yield'), ()),
    ("book_value",     (r'book\s*value',), (r'price', r'p\s*/?\s*b')),
    ("eps",            (r'\beps\b', r'earning\s*per\s*share'), ()),
    ("face_value",     (r'face\s*value',), ()),
    ("sales_growth",   (r'sales\s*(var|growth)', r'qtr\s*sales\s*var',
                        r'revenue\s*growth'), ()),
    ("profit_growth",  (r'profit\s*(var|growth)', r'qtr\s*profit\s*var',
                        r'\bnp\s*(var|growth)'), ()),
)

NUMERIC_FIELDS = (
    "cmp", "market_cap", "pe", "roe", "roce", "debt_to_equity",
    "dividend_yield", "book_value", "eps", "face_value",
    "sales_growth", "profit_growth",
)

# Words that carry no identifying information when matching company names.
# "and" is included because statements spell out "AND" where Screener uses "&"
# ("Allied Blenders And Distillers" vs "Allied Blenders & Distillers Ltd"), and
# the ampersand is stripped as punctuation anyway.
_NAME_NOISE = re.compile(
    r'\b(ltd|limited|pvt|private|public|company|co|corp|corporation|the|and|'
    r'india|indian|industries|enterprises|holdings|group)\b',
    re.IGNORECASE,
)


def map_screener_columns(raw_columns) -> dict:
    """Map a Screener export's headers onto our fields.

    Returns {raw_header: field}. Each field is claimed once and each header used
    once, so the result can never collide.
    """
    mapping: dict = {}
    for field, patterns, exclusions in _COLUMN_RULES:
        found = None
        for pattern in patterns:
            for header in raw_columns:
                if header in mapping:
                    continue
                h = str(header).strip().lower()
                if not re.search(pattern, h):
                    continue
                if any(re.search(x, h) for x in exclusions):
                    continue
                found = header
                break
            if found is not None:
                break
        if found is not None:
            mapping[found] = field
    return mapping


def clean_number(value) -> Optional[float]:
    """Parse a Screener cell into a float, or None when it is not a number.

    Screener writes blanks, percent signs, thousands separators and sometimes a
    bare "-" for missing data. None (not 0.0) matters here: a P/E of 0 and an
    unknown P/E mean very different things on a dashboard.
    """
    if value is None:
        return None
    text = str(value).strip().replace(',', '').replace('%', '').replace('₹', '')
    if text in ("", "-", "--", "NA", "N/A", "nan", "None"):
        return None
    if text.startswith('(') and text.endswith(')'):
        text = '-' + text[1:-1]
    try:
        return float(text)
    except ValueError:
        return None


def normalise_name(name: str) -> str:
    """Reduce a company name to a comparable key.

    "PIRAMAL FINANCE LTD" and "Piramal Finance Ltd." both become "PIRAMALFINANCE".
    """
    if not name:
        return ""
    text = _NAME_NOISE.sub(' ', str(name))
    return re.sub(r'[^A-Za-z0-9]', '', text).upper()


def normalise_code(code) -> str:
    """Normalise an exchange code (NSE symbol or BSE scrip code)."""
    if code is None:
        return ""
    text = str(code).strip().upper()
    # BSE codes arrive from CSV/Excel as floats ("500325.0")
    if re.match(r'^\d+\.0$', text):
        text = text[:-2]
    return re.sub(r'[^A-Z0-9&-]', '', text)


def parse_screener_csv(text: str) -> dict:
    """Parse a Screener CSV export into rows plus a diagnostic report.

    Screener prefixes some exports with the screen's name and query before the
    real header row, so the header is found by scanning for the first row that
    maps to a recognisable set of fields rather than assuming row 0.
    """
    import csv
    import io

    raw_rows = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
    if not raw_rows:
        return {"rows": [], "mapping": {}, "unmapped_columns": [],
                "header_row": None, "error": "File contained no data rows."}

    # The header is the first row whose cells map to at least a name-ish column
    # plus one metric. Preamble rows ("Screen: My Portfolio") map to nothing.
    header_idx, mapping = None, {}
    for idx, row in enumerate(raw_rows[:15]):
        candidate = map_screener_columns([c.strip() for c in row if c.strip()])
        identifying = {"name", "nse_code", "bse_code", "isin"} & set(candidate.values())
        if identifying and len(candidate) >= 2:
            header_idx, mapping = idx, candidate
            break

    if header_idx is None:
        return {"rows": [], "mapping": {}, "unmapped_columns": [],
                "header_row": None,
                "error": ("Could not find a header row. Expected a row containing a "
                          "company name or NSE/BSE code column plus at least one "
                          "data column such as CMP or Industry.")}

    headers = [c.strip() for c in raw_rows[header_idx]]
    rows = []
    for raw in raw_rows[header_idx + 1:]:
        record = {headers[i]: raw[i] for i in range(min(len(headers), len(raw)))}
        mapped, extras = {}, {}
        for header, value in record.items():
            field = mapping.get(header)
            if field:
                mapped[field] = clean_number(value) if field in NUMERIC_FIELDS else str(value).strip()
            elif header and str(value).strip():
                extras[header] = str(value).strip()

        # Screener repeats the header on long exports and appends blank/total rows
        name = (mapped.get("name") or "").strip()
        code = normalise_code(mapped.get("nse_code") or mapped.get("bse_code") or "")
        if not name and not code:
            continue
        if name.lower() in ("name", "company name", "total", "grand total"):
            continue

        mapped["extras_json"] = json.dumps(extras) if extras else None
        mapped["match_key"] = normalise_name(name)
        mapped["nse_code"] = normalise_code(mapped.get("nse_code", ""))
        mapped["bse_code"] = normalise_code(mapped.get("bse_code", ""))
        rows.append(mapped)

    return {
        "rows": rows,
        "mapping": {v: k for k, v in mapping.items()},
        "unmapped_columns": [h for h in headers if h and h not in mapping],
        "header_row": header_idx + 1,
        "error": None,
    }


# ── Market-cap tiering ───────────────────────────────────────────────────────
# SEBI/AMFI define the tiers by RANK, not by an absolute rupee cap: the top 100
# companies by full market capitalisation are large cap, 101-250 mid cap, and
# 251 onward small cap. Ranking is therefore only meaningful when the import
# covers a broad universe; callers should surface `universe_size` alongside it.
LARGE_CAP_RANK = 100
MID_CAP_RANK = 250


def tier_for_rank(rank: int) -> str:
    if rank <= LARGE_CAP_RANK:
        return "Large Cap"
    if rank <= MID_CAP_RANK:
        return "Mid Cap"
    return "Small Cap"


def build_lookup(market_rows):
    """Index market rows for matching: by ISIN, exchange code, and name key."""
    by_isin, by_code, by_name = {}, {}, {}
    for row in market_rows:
        if getattr(row, "isin", None):
            by_isin.setdefault(row.isin, row)
        for code in (getattr(row, "nse_code", ""), getattr(row, "bse_code", "")):
            if code:
                by_code.setdefault(code, row)
        if getattr(row, "match_key", None):
            # Ambiguous name keys are recorded as None so we refuse to guess.
            if row.match_key in by_name and by_name[row.match_key] is not row:
                by_name[row.match_key] = None
            else:
                by_name.setdefault(row.match_key, row)
    return by_isin, by_code, by_name


def match_holding(holding, by_isin, by_code, by_name):
    """Find the market row for a holding.

    Returns (row, how) where `how` is 'isin', 'code', 'name' or None. ISIN wins
    because it is unambiguous; a name match is a last resort and is refused when
    two companies normalise to the same key.
    """
    isin = (getattr(holding, "isin", "") or "").strip().upper()
    if isin and isin in by_isin:
        return by_isin[isin], "isin"

    key = normalise_name(getattr(holding, "security_name", "") or "")
    if key and key in by_code:
        return by_code[key], "code"
    if key:
        hit = by_name.get(key)
        if hit is not None:
            return hit, "name"
    return None, None
