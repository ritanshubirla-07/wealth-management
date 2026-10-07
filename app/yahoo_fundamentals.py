"""Company fundamentals from Yahoo Finance's public timeseries API.

**Why not Alpha Vantage.** It has no fundamentals for Indian equities at all:
``OVERVIEW``, ``INCOME_STATEMENT``, ``BALANCE_SHEET`` and ``CASH_FLOW`` each
return ``{}`` for RELIANCE, JSWSTEEL, MARUTI and AXISBANK in both ``.BSE`` and
``.NSE`` form, on a key that returns 55 populated fields for IBM. Its Indian
coverage is prices only, one symbol per request, against a 25-request daily
free tier — worse for this portfolio than the NSE/BSE BhavCopy files already
wired up, where one download prices every listed security with no quota.

Yahoo does serve Indian statements, as JSON rather than HTML, so a restyled
page cannot break the parse the way a scrape can. Measured coverage across the
securities held here: 65 of 69 with an NSE code. The four misses are delisted
or renamed micro-caps that Screener still covers.

**Units.** Yahoo reports absolute INR, Screener reports Rs Crore. Figures are
divided by 1e7 on the way in so both sources speak Crore. Per-share and
already-ratio values (EPS, the derived day-counts) are left alone.

**Definitional drift, measured against Screener for JSW Steel FY24-FY26.**

    Profit before tax, Cash from Operating Activity    exact
    Sales                      1-2% below   Screener folds in other op. income
    Net Profit                 0-12% below  minority-interest treatment
    Interest                   6-9% below
    Total Debt                 ~20% above   Yahoo counts lease liabilities
    Net PPE                   15-24% above  includes right-of-use assets
    Operating Income          20-38% below  EBIT, vs Screener's EBITDA-like line

Because of that drift a single series is never built from both sources. A row
comes from one source end to end or it is left out, since splicing would plant
a step change at the boundary year — a fake 20% jump in Borrowings would
corrupt exactly the debt trend and "share of years" checks that reading ten
years of history was meant to measure.
"""

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from typing import Optional

log = logging.getLogger(__name__)

TIMESERIES_URL = (
    "https://query2.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/{symbol}"
)
QUOTE_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=5d&interval=1d"
# Industry and sector come from the search endpoint rather than quoteSummary:
# quoteSummary/assetProfile answers 401 without a session crumb, while search
# returns the same two fields unauthenticated.
SEARCH_URL = (
    "https://query2.finance.yahoo.com/v1/finance/search"
    "?q={symbol}&quotesCount=6&newsCount=0&listsCount=0"
)

# Yahoo rejects the default urllib agent.
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

CRORE = 1e7

# Annual lines pulled in one request. Everything the 34-parameter model reads,
# plus the few extras the derived rows need.
FIELDS = (
    "TotalRevenue", "CostOfRevenue", "OperatingIncome", "EBITDA", "NetIncome",
    "PretaxIncome", "TaxProvision", "InterestExpense", "ReconciledDepreciation",
    "DilutedEPS", "CashDividendsPaid",
    "CommonStock", "StockholdersEquity", "TotalDebt", "NetPPE",
    "CurrentAssets", "CurrentLiabilities", "Inventory", "AccountsReceivable",
    "TotalAssets",
    "OperatingCashFlow", "FreeCashFlow", "CapitalExpenditure",
)

# Lines that are per-share or already a ratio, so the Crore conversion must skip them.
NOT_IN_CRORE = {"DilutedEPS"}

_MONTHS = {1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
           7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec"}


class _Throttle:
    """One shared request slot, so parallel callers stay polite to Yahoo."""

    def __init__(self, min_gap: float = 0.3):
        self.min_gap = min_gap
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self):
        with self._lock:
            now = time.monotonic()
            sleep_for = max(0.0, self._next - now)
            self._next = max(now, self._next) + self.min_gap
        if sleep_for:
            time.sleep(sleep_for)


_throttle = _Throttle()


def _get(url: str, timeout: int = 25) -> Optional[dict]:
    _throttle.wait()
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            json.JSONDecodeError, OSError) as exc:
        log.debug("Yahoo request failed for %s: %r", url, exc)
        return None


def candidate_symbols(nse_code: str = "", bse_code: str = "") -> list:
    """Yahoo symbols to try, NSE first then BSE, as Yahoo spells them."""
    out = []
    if nse_code:
        code = nse_code.strip().upper()
        # A numeric "NSE code" is really a BSE scrip number held in that column.
        out.append(f"{code}.BO" if code.isdigit() else f"{code}.NS")
    if bse_code:
        code = bse_code.strip().upper()
        suffix = f"{code}.BO"
        if suffix not in out:
            out.append(suffix)
    return out


def _raw_series(symbol: str) -> dict:
    """{field: {"YYYY-MM": value}} straight from Yahoo, no conversion."""
    types = ",".join("annual" + name for name in FIELDS)
    url = (TIMESERIES_URL.format(symbol=symbol)
           + f"?symbol={symbol}&type={types}&period1=1000000000&period2=1900000000")
    payload = _get(url)
    if not payload:
        return {}
    results = (payload.get("timeseries") or {}).get("result") or []
    out: dict = {}
    for block in results:
        for key in (k for k in block.keys() if k != "meta"):
            field = key[len("annual"):] if key.startswith("annual") else key
            for point in block.get(key) or []:
                if not isinstance(point, dict):
                    continue
                reported = point.get("reportedValue") or {}
                value = reported.get("raw")
                as_of = point.get("asOfDate")
                if value is None or not as_of:
                    continue
                out.setdefault(field, {})[as_of[:7]] = value
    return out


def _period_label(stamp: str) -> str:
    """"2026-03" -> "Mar 2026", matching the period labels Screener uses."""
    year, _, month = stamp.partition("-")
    try:
        return f"{_MONTHS[int(month)]} {year}"
    except (ValueError, KeyError):
        return stamp


def _aligned(raw: dict, field: str, stamps: list) -> list:
    """One row aligned to ``stamps``, converted to Crore unless exempt."""
    values = raw.get(field) or {}
    if not values:
        return []
    divisor = 1.0 if field in NOT_IN_CRORE else CRORE
    return [None if values.get(s) is None else values[s] / divisor for s in stamps]


def _ratio_row(numerator: list, denominator: list, factor: float = 100.0) -> list:
    out = []
    for top, bottom in zip(numerator, denominator):
        out.append(None if (top is None or not bottom) else top / bottom * factor)
    return out


def fetch_fundamentals(nse_code: str = "", bse_code: str = "") -> Optional[dict]:
    """Statements for one company, shaped like the Screener payload.

    Returns ``None`` when Yahoo has nothing, so the caller can fall back rather
    than store an empty shell. Only rows whose definition lines up with
    Screener's are given Screener's label; the rest keep a distinct name so
    nothing is silently conflated.
    """
    for symbol in candidate_symbols(nse_code, bse_code):
        raw = _raw_series(symbol)
        if not raw.get("TotalRevenue"):
            continue

        stamps = sorted({s for values in raw.values() for s in values})
        periods = [_period_label(s) for s in stamps]

        revenue = _aligned(raw, "TotalRevenue", stamps)
        net_income = _aligned(raw, "NetIncome", stamps)
        pretax = _aligned(raw, "PretaxIncome", stamps)
        tax = _aligned(raw, "TaxProvision", stamps)
        equity = _aligned(raw, "StockholdersEquity", stamps)
        capital = _aligned(raw, "CommonStock", stamps)
        dividends = _aligned(raw, "CashDividendsPaid", stamps)
        receivable = _aligned(raw, "AccountsReceivable", stamps)
        inventory = _aligned(raw, "Inventory", stamps)

        # Reserves is equity less the paid-up capital, the split Screener shows.
        reserves = [None if (e is None or c is None) else e - c
                    for e, c in zip(equity, capital)]
        # Yahoo signs dividends paid negative; Screener's payout is a positive %.
        payout = [None if (d is None or not n) else abs(d) / n * 100
                  for d, n in zip(dividends, net_income)]

        profit_loss = {
            "Sales": revenue,
            "Operating Profit": _aligned(raw, "OperatingIncome", stamps),
            "Interest": _aligned(raw, "InterestExpense", stamps),
            "Depreciation": _aligned(raw, "ReconciledDepreciation", stamps),
            "Profit before tax": pretax,
            "Tax %": _ratio_row(tax, pretax),
            "Net Profit": net_income,
            "EPS in Rs": _aligned(raw, "DilutedEPS", stamps),
            "Dividend Payout %": payout,
        }
        balance_sheet = {
            "Equity Capital": capital,
            "Reserves": reserves,
            "Borrowings": _aligned(raw, "TotalDebt", stamps),
            "Fixed Assets": _aligned(raw, "NetPPE", stamps),
            "Total Assets": _aligned(raw, "TotalAssets", stamps),
            # Real current assets and liabilities. Screener publishes neither,
            # so the scorecard's current ratio has had to divide Other Assets by
            # Other Liabilities as a stand-in; these are the actual figures and
            # are kept under their own labels rather than overwriting that proxy.
            "Current Assets": _aligned(raw, "CurrentAssets", stamps),
            "Current Liabilities": _aligned(raw, "CurrentLiabilities", stamps),
            "Inventory": inventory,
            "Receivables": receivable,
        }
        cash_flow = {
            "Cash from Operating Activity": _aligned(raw, "OperatingCashFlow", stamps),
            "Free Cash Flow": _aligned(raw, "FreeCashFlow", stamps),
            "Capital Expenditure": _aligned(raw, "CapitalExpenditure", stamps),
        }
        ratios = {
            "Debtor Days": _ratio_row(receivable, revenue, 365.0),
            "Inventory Days": _ratio_row(inventory, revenue, 365.0),
        }

        return {
            "symbol": symbol,
            "basis": "consolidated",
            "periods": periods,
            "profit_loss": {"periods": periods, "rows": _drop_empty(profit_loss)},
            "balance_sheet": {"periods": periods, "rows": _drop_empty(balance_sheet)},
            "cash_flow": {"periods": periods, "rows": _drop_empty(cash_flow)},
            "ratios": {"periods": periods, "rows": _drop_empty(ratios)},
            # Yahoo publishes no Indian shareholding pattern, so promoter and FII
            # holdings stay Screener-only; an empty block here keeps that explicit.
            "shareholding": {"periods": [], "rows": {}},
            "source": "yahoo",
        }
    return None


def _drop_empty(rows: dict) -> dict:
    """Keep only rows that carry at least one number."""
    return {label: series for label, series in rows.items()
            if series and any(v is not None for v in series)}


def fetch_profile(nse_code: str = "", bse_code: str = "") -> Optional[dict]:
    """``{"industry", "sector", "symbol"}`` for one company, or None.

    Yahoo classifies on a GICS-like taxonomy ("Steel" under "Basic Materials",
    "Banks - Regional" under "Financial Services"), which is both finer and far
    better populated for Indian listings than what this project had: Screener
    fills ``industry`` for well under half the securities held here, and the
    name-keyword sector map leaves most holdings as "Others".
    """
    for symbol in candidate_symbols(nse_code, bse_code):
        payload = _get(SEARCH_URL.format(symbol=symbol))
        for quote in ((payload or {}).get("quotes") or []):
            if quote.get("symbol") != symbol:
                continue
            industry = (quote.get("industry") or "").strip()
            sector = (quote.get("sector") or "").strip()
            if not (industry or sector):
                continue
            return {
                # Yahoo joins the two halves with an em dash ("Banks—Regional");
                # normalise it so the UI never has to carry the raw codepoint.
                "industry": industry.replace("—", " - ").replace("–", " - "),
                "sector": sector,
                "symbol": symbol,
            }
    return None


def fetch_price(nse_code: str = "", bse_code: str = "") -> Optional[tuple]:
    """Latest close as ``(price, symbol)``, or None.

    A gap-fill for securities the primary price source cannot quote; it is not
    wired in as the main CMP route, which stays where it is.
    """
    for symbol in candidate_symbols(nse_code, bse_code):
        payload = _get(QUOTE_URL.format(symbol=symbol))
        result = ((payload or {}).get("chart") or {}).get("result") or []
        if not result:
            continue
        meta = result[0].get("meta") or {}
        price = meta.get("regularMarketPrice")
        if price:
            return float(price), symbol
    return None
