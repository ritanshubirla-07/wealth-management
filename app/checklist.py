"""The 34-point stock checklist evaluated against scraped Screener.in data.

Each row is returned as:

    {"sr", "parameter", "assumption", "output", "status", "note"}

``status`` is one of:

* ``pass``    — threshold met (green tick)
* ``fail``    — threshold missed (red cross)
* ``info``    — measured, but the criterion is "higher/lower the better" with no
                numeric threshold, so there is nothing to tick or cross
* ``manual``  — cannot come from Screener: needs an annual report, a concall
                transcript, a rating agency, or human judgement
* ``no_data`` — Screener does not publish what the calculation requires

Deliberate design rule: **no invented thresholds and no invented numbers.** Where
a criterion has no numeric cut-off, the measured value is reported as ``info``
rather than being forced into a pass/fail. Where the input simply is not
available, the row says so instead of showing a plausible-looking figure.
"""

import os
import re
from typing import Optional

# ── Tunables ────────────────────────────────────────────────────────────────
# Used by check 32 (earnings yield vs the risk-free rate). It moves, so it is an
# environment variable rather than a number buried in the code.
GSEC_10Y_YIELD = float(os.getenv("GSEC_10Y_YIELD", "6.5"))

# Check 11: cumulative CFO should roughly track cumulative PAT. "Roughly" needs a
# number to be testable; 0.8 is the conventional floor for this screen.
CFO_PAT_MIN_RATIO = 0.8

PASS, FAIL, INFO, MANUAL, NO_DATA = "pass", "fail", "info", "manual", "no_data"


# ── Series helpers ──────────────────────────────────────────────────────────

def _num(value) -> Optional[float]:
    """Parse a Screener statement cell. Returns None when not a number."""
    if value is None:
        return None
    text = str(value).strip().replace(",", "").replace("%", "").replace("₹", "")
    if text in ("", "-", "--", "NA", "N/A", "nan", "None"):
        return None
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]
    try:
        return float(text)
    except ValueError:
        return None


class Financials:
    """Convenience accessor over the parsed company page.

    ``yahoo`` is an optional second payload in the same shape (see
    ``app/yahoo_fundamentals``). It is never blended into the primary series —
    the two sources define several lines differently, so splicing would plant a
    step change mid-series — but it is read for the handful of figures Screener
    does not publish at all, via :meth:`yahoo_row` and :meth:`yahoo_latest`.
    """

    def __init__(self, data: dict, yahoo: dict = None):
        self.data = data or {}
        self.yahoo = yahoo or {}

    def yahoo_row(self, section: str, label, drop_ttm: bool = True) -> list:
        """A series from the Yahoo payload, or [] when it has nothing."""
        if not self.yahoo:
            return []
        return Financials(self.yahoo).row(section, label, drop_ttm)

    def yahoo_latest(self, section: str, label, drop_ttm: bool = True):
        series = [v for v in self.yahoo_row(section, label, drop_ttm) if v is not None]
        return series[-1] if series else None

    def section(self, name: str) -> dict:
        return self.data.get(name) or {}

    def periods(self, section: str) -> list:
        return self.section(section).get("periods") or []

    def has(self, section: str, label: str) -> bool:
        rows = self.section(section).get("rows") or {}
        wanted = label.lower().strip()
        return any(k.lower().strip() == wanted for k in rows)

    @property
    def is_lender(self) -> bool:
        """True for banks and NBFCs, which Screener renders on a different schema.

        Their P&L has Revenue / Financing Profit instead of Sales / Operating
        Profit, and their balance sheet carries Deposits. Several ratios that
        are meaningful for a manufacturer are meaningless for a lender.
        """
        return (self.has("profit_loss", "Financing Profit")
                or self.has("balance_sheet", "Deposits"))

    def row(self, section: str, label, drop_ttm: bool = True) -> list:
        """Numeric series for one statement row, aligned to fiscal periods.

        ``label`` may be a single label or several alternatives, tried in order —
        Screener names the same line differently for lenders ("Revenue") and
        non-lenders ("Sales").

        ``drop_ttm`` removes the trailing TTM column so year-on-year maths and
        sums only ever span complete financial years.
        """
        labels = [label] if isinstance(label, str) else list(label)
        sec = self.section(section)
        rows = sec.get("rows") or {}
        raw = None
        for candidate in labels:
            if candidate in rows:
                raw = rows[candidate]
                break
            # Labels drift slightly ("Net Profit" vs "Net profit"); match loosely
            wanted = candidate.lower().strip()
            for key, value in rows.items():
                if key.lower().strip() == wanted:
                    raw = value
                    break
            if raw is not None:
                break
        if raw is None:
            return []
        values = [_num(v) for v in raw]
        if drop_ttm:
            periods = sec.get("periods") or []
            keep = [i for i, p in enumerate(periods) if "ttm" not in str(p).lower()]
            if keep and len(periods) == len(values):
                values = [values[i] for i in keep]
        return values

    def latest(self, section: str, label: str, drop_ttm: bool = True) -> Optional[float]:
        series = [v for v in self.row(section, label, drop_ttm) if v is not None]
        return series[-1] if series else None

    def growth(self, table: str, horizon: str) -> Optional[float]:
        entry = (self.data.get("growth") or {}).get(table) or {}
        return _num(entry.get(horizon))

    def schedule_total(self, parent: str, items) -> Optional[float]:
        """Sum the latest period of the named lines in a balance-sheet schedule.

        Returns None when the schedule was not captured, so "not fetched" stays
        distinguishable from "genuinely zero".
        """
        schedules = self.data.get("schedules") or {}
        block = schedules.get(parent)
        if not isinstance(block, dict) or not block:
            return None
        total, seen = 0.0, False
        for line in items:
            series = block.get(line)
            if not isinstance(series, dict) or not series:
                continue
            # Period keys are insertion-ordered oldest -> newest
            value = _num(list(series.values())[-1])
            if value is not None:
                total += value
                seen = True
        return total if seen else None

    def top_ratio(self, label: str) -> Optional[float]:
        """A value from the header ratio block (Market Cap, Stock P/E, ...)."""
        entry = (self.data.get("top_ratios") or {}).get(label)
        if not entry:
            return None
        numbers = entry.get("numbers") or []
        return _num(numbers[0]) if numbers else None


def _fmt(value, suffix="", digits=2) -> str:
    if value is None:
        return "n/a"
    return f"{value:,.{digits}f}{suffix}"


def _trend(series: list) -> Optional[str]:
    """Describe the direction of a series using its first and last thirds."""
    clean = [v for v in series if v is not None]
    if len(clean) < 4:
        return None
    window = max(1, len(clean) // 3)
    early = sum(clean[:window]) / window
    late = sum(clean[-window:]) / window
    if early == 0:
        return None
    change = (late - early) / abs(early) * 100
    if change > 10:
        return "rising"
    if change < -10:
        return "falling"
    return "flat"


def _row(sr, parameter, assumption, output, status, note="") -> dict:
    return {"sr": sr, "parameter": parameter, "assumption": assumption,
            "output": output, "status": status, "note": note}


# ── The checklist ───────────────────────────────────────────────────────────

# Screener uses different row names for lending businesses
SALES_LABELS = ("Sales", "Revenue")
BORROWING_LABELS = ("Borrowings", "Borrowing")

# Line items inside Screener's balance-sheet schedules that make up working
# capital. Short-term borrowings are added to current liabilities separately.
CURRENT_ASSET_ITEMS = (
    "Inventories", "Trade receivables", "Cash Equivalents",
    "Loans n Advances", "Other asset items",
)
CURRENT_LIABILITY_ITEMS = (
    "Trade Payables", "Advance from Customers", "Other liability items",
)


def evaluate(fundamentals: dict, market=None) -> list:
    """Build all 34 checklist rows. ``market`` is the MarketData row, if any."""
    f = Financials(fundamentals)
    rows = []

    cmp_value = getattr(market, "cmp", None) if market else None
    lender = f.is_lender

    # 1 ─ Sales growth
    s10 = f.growth("Compounded Sales Growth", "10 Years")
    s5 = f.growth("Compounded Sales Growth", "5 Years")
    horizon, sales_cagr = ("10Y", s10) if s10 is not None else ("5Y", s5)
    parts = [f"10Y: {_fmt(s10, '%', 1)}", f"5Y: {_fmt(s5, '%', 1)}"]
    rows.append(_row(
        1, "Sales Growth", "CAGR > 15% over 7 to 10 Yrs", " | ".join(parts),
        PASS if (sales_cagr is not None and sales_cagr > 15) else (FAIL if sales_cagr is not None else NO_DATA),
        "" if s10 is not None else "Screener publishes 10Y/5Y/3Y CAGR, not 7Y",
    ))

    # 2 ─ Net profit margin
    sales = f.latest("profit_loss", SALES_LABELS)
    net_profit = f.latest("profit_loss", "Net Profit")
    npm = (net_profit / sales * 100) if (sales and net_profit is not None and sales != 0) else None
    rows.append(_row(
        2, "Profitability", "NPM >= 8%", _fmt(npm, "%"),
        PASS if (npm is not None and npm >= 8) else (FAIL if npm is not None else NO_DATA),
        "Latest full year: Net Profit / Sales",
    ))

    # 3 ─ Tax payout
    tax_pct = f.latest("profit_loss", "Tax %")
    rows.append(_row(
        3, "Tax Payout Ratio", ">= 30%", _fmt(tax_pct, "%", 1),
        PASS if (tax_pct is not None and tax_pct >= 30) else (FAIL if tax_pct is not None else NO_DATA),
    ))

    # 4 ─ Interest coverage
    pbt = f.latest("profit_loss", "Profit before tax")
    interest = f.latest("profit_loss", "Interest")
    if lender:
        # For a bank or NBFC, interest is the cost of funds — an input to revenue,
        # not a fixed charge on borrowings. The ratio is not meaningful here.
        rows.append(_row(
            4, "Interest Coverage Ratio", ">= 3", "Not applicable", NO_DATA,
            "This is a lending business: interest is its cost of funds, not a "
            "leverage burden, so interest coverage does not apply.",
        ))
    elif interest is not None and interest > 0 and pbt is not None:
        coverage = (pbt + interest) / interest
        rows.append(_row(4, "Interest Coverage Ratio", ">= 3", f"{coverage:,.1f}x",
                         PASS if coverage >= 3 else FAIL, "EBIT / Interest"))
    elif interest is not None and interest == 0:
        rows.append(_row(4, "Interest Coverage Ratio", ">= 3", "No interest cost",
                         PASS, "Company paid no interest in the latest year"))
    else:
        rows.append(_row(4, "Interest Coverage Ratio", ">= 3", "n/a", NO_DATA))

    # 5 ─ Debt to equity
    borrowings = f.latest("balance_sheet", BORROWING_LABELS)
    equity_capital = f.latest("balance_sheet", "Equity Capital")
    reserves = f.latest("balance_sheet", "Reserves")
    equity = (equity_capital or 0) + (reserves or 0)
    de = (borrowings / equity) if (borrowings is not None and equity) else None
    rows.append(_row(
        5, "Debt to Equity Ratio", "<= 0.5", _fmt(de, "", 3),
        (INFO if lender else (PASS if de <= 0.5 else FAIL)) if de is not None else NO_DATA,
        ("Borrowing / (Equity Capital + Reserves), excluding customer deposits. "
         "A lender is leveraged by design, so the <= 0.5 screen is reported rather "
         "than scored." if lender else "Borrowings / (Equity Capital + Reserves)"),
    ))

    # 6 ─ Current ratio, from the expandable balance-sheet schedules
    ca = f.schedule_total("Other Assets", CURRENT_ASSET_ITEMS)
    cl = f.schedule_total("Other Liabilities", CURRENT_LIABILITY_ITEMS)
    st_debt = f.schedule_total("Borrowings", ("Short term Borrowings",)) \
        or f.schedule_total("Borrowing", ("Short term Borrowings",))
    if ca is not None and cl is not None:
        cl_total = cl + (st_debt or 0)
        cr = (ca / cl_total) if cl_total else None
        if cr is None:
            rows.append(_row(6, "Current Ratio", ">= 1.25", "No current liabilities", INFO))
        else:
            rows.append(_row(
                6, "Current Ratio", ">= 1.25", _fmt(cr),
                PASS if cr >= 1.25 else FAIL,
                f"Current assets Rs {ca:,.0f} Cr / current liabilities Rs {cl_total:,.0f} Cr, "
                "from Screener's balance-sheet schedules (inventories, receivables, cash and "
                "advances over payables, customer advances and short-term borrowings). "
                "Screener's grouping is not a strict current/non-current split, so treat as close "
                "but not identical to the annual report's figure.",
            ))
    else:
        rows.append(_row(
            6, "Current Ratio", ">= 1.25", "Not available", NO_DATA,
            "The balance-sheet schedules were not captured for this company. "
            "Re-run `python -m scripts.fetch_cmp` to collect them.",
        ))

    # 7 ─ Net fixed asset turnover
    sales_series = f.row("profit_loss", SALES_LABELS)
    fa_series = f.row("balance_sheet", "Fixed Assets")
    nfat_series = [
        (s / fa) for s, fa in zip(sales_series[-len(fa_series):], fa_series)
        if s is not None and fa
    ] if fa_series else []
    nfat = nfat_series[-1] if nfat_series else None
    trend = _trend(nfat_series)
    rows.append(_row(
        7, "Net Fixed Assets Turnover", "Higher the better; rising trend healthy",
        f"{_fmt(nfat)}x" + (f" ({trend})" if trend else ""),
        INFO if nfat is not None else NO_DATA, "Sales / Net Fixed Assets",
    ))

    # 8 ─ Inventory turnover (from inventory days)
    inv_days = f.row("ratios", "Inventory Days")
    inv_turn = [365 / d for d in inv_days if d]
    it = inv_turn[-1] if inv_turn else None
    trend = _trend(inv_turn)
    rows.append(_row(
        8, "Inventory Turnover Ratio", "Higher the better; rising trend healthy",
        f"{_fmt(it)}x" + (f" ({trend})" if trend else ""),
        INFO if it is not None else NO_DATA, "365 / Inventory Days",
    ))

    # 9 ─ Debtors turnover (from debtor days)
    deb_days = f.row("ratios", "Debtor Days")
    deb_turn = [365 / d for d in deb_days if d]
    dt = deb_turn[-1] if deb_turn else None
    trend = _trend(deb_turn)
    rows.append(_row(
        9, "Debtors Turnover Ratio", "Higher the better; rising trend healthy",
        f"{_fmt(dt)}x" + (f" ({trend})" if trend else ""),
        INFO if dt is not None else NO_DATA, "365 / Debtor Days",
    ))

    # 10 ─ CFO positive
    cfo_series = [v for v in f.row("cash_flow", "Cash from Operating Activity") if v is not None]
    cfo = cfo_series[-1] if cfo_series else None
    rows.append(_row(
        10, "Cash Flow from Operations", "> 0", f"Rs {_fmt(cfo, ' Cr', 0)}",
        PASS if (cfo is not None and cfo > 0) else (FAIL if cfo is not None else NO_DATA),
        "Latest full year",
    ))

    # 11 ─ Cumulative CFO vs cumulative PAT
    pat_series = [v for v in f.row("profit_loss", "Net Profit") if v is not None]
    if cfo_series and pat_series:
        years = min(len(cfo_series), len(pat_series))
        c_cfo, c_pat = sum(cfo_series[-years:]), sum(pat_series[-years:])
        ratio = (c_cfo / c_pat) if c_pat else None
        output = f"cCFO Rs {c_cfo:,.0f} Cr vs cPAT Rs {c_pat:,.0f} Cr" + (
            f" ({ratio:.2f}x)" if ratio is not None else "")
        if ratio is None:
            status = NO_DATA
        else:
            status = PASS if ratio >= CFO_PAT_MIN_RATIO else FAIL
        rows.append(_row(11, "Cumulative CFO vs Cumulative PAT", "Cumu CFO ~ Cumu PAT",
                         output, status, f"Over {years} years; pass at >= {CFO_PAT_MIN_RATIO:g}x"))
    else:
        rows.append(_row(11, "Cumulative CFO vs Cumulative PAT", "Cumu CFO ~ Cumu PAT",
                         "n/a", NO_DATA))

    # 12 ─ Peer comparison: Screener loads its peer table separately
    industry = getattr(market, "industry", None) if market else None
    rows.append(_row(
        12, "Comparison with Industry Peers", "Compare vs sector peers",
        f"Industry: {industry}" if industry else "Not fetched", MANUAL,
        "Screener renders its peer table via a separate request that is not "
        "part of the company page, so peer metrics are not collected here.",
    ))

    # 13 ─ Production capacity
    rows.append(_row(
        13, "Production Capacity vs Sales Volume", "Capacity CAGR ~ Sales CAGR",
        "Not available", MANUAL,
        "Installed capacity and volumes are disclosed in the annual report, not on Screener.",
    ))

    # 14 ─ Sales converting to profit
    p10 = f.growth("Compounded Profit Growth", "10 Years")
    p5 = f.growth("Compounded Profit Growth", "5 Years")
    profit_cagr = p10 if p10 is not None else p5
    output = f"Profit {_fmt(profit_cagr, '%', 1)} vs Sales {_fmt(sales_cagr, '%', 1)} ({horizon})"
    if profit_cagr is None or sales_cagr is None:
        status = NO_DATA
    else:
        status = PASS if profit_cagr >= sales_cagr else FAIL
    rows.append(_row(14, "Conversion of Sales to Profits", "Profit CAGR ~ Sales CAGR",
                     output, status))

    # 15 ─ Market cap growth vs retained profits
    payout_series = f.row("profit_loss", "Dividend Payout %")
    market_cap = f.top_ratio("Market Cap") or (getattr(market, "market_cap", None) if market else None)
    price_cagr10 = f.growth("Stock Price CAGR", "10 Years")
    if pat_series and market_cap and price_cagr10 is not None:
        years = min(10, len(pat_series))
        retained = 0.0
        for index in range(-years, 0):
            pat = pat_series[index]
            payout = payout_series[index] if len(payout_series) >= years and payout_series[index] is not None else 0
            retained += pat * (1 - (payout or 0) / 100)
        past_cap = market_cap / ((1 + price_cagr10 / 100) ** years)
        cap_gain = market_cap - past_cap
        ratio = (cap_gain / retained) if retained else None
        output = (f"Mkt cap +Rs {cap_gain:,.0f} Cr vs retained Rs {retained:,.0f} Cr"
                  + (f" ({ratio:.2f}x)" if ratio is not None else ""))
        status = NO_DATA if ratio is None else (PASS if ratio >= 1 else FAIL)
        rows.append(_row(15, "Creating Shareholder Value", "Mkt cap increase (10Y) > retained profits (10Y)",
                         output, status,
                         "Market cap 10Y ago is derived from Screener's stock-price CAGR, so it "
                         "ignores any change in share count (buybacks/dilution). Approximate."))
    else:
        rows.append(_row(15, "Creating Shareholder Value", "Mkt cap increase (10Y) > retained profits (10Y)",
                         "n/a", NO_DATA))

    # 16-19 ─ qualitative / external
    rows.append(_row(16, "Background Check of Promoters & Directors",
                     "Web search: fraud / penalty / SEBI fine", "Not checked", MANUAL,
                     "Requires a web search; deliberately not automated."))
    rows.append(_row(17, "Management Succession Plan",
                     "Read annual reports / concall transcripts", "Not checked", MANUAL,
                     "Screener links the filings under Documents; reading them is manual."))
    rows.append(_row(18, "Promoters Salary vs Net Profits",
                     "Within 10% of PAT; no rise while PAT declines", "Not available", MANUAL,
                     "Managerial remuneration is in the annual report, not on Screener."))
    rows.append(_row(19, "Project Execution Skills", "Greenfield / brownfield track record",
                     "Not available", MANUAL, "Judgement call from filings and history."))

    # 20 ─ Dividend CAGR
    if pat_series and payout_series:
        years = min(len(pat_series), len(payout_series))
        dividends = [
            pat_series[-years + i] * ((payout_series[-years + i] or 0) / 100)
            for i in range(years)
            if pat_series[-years + i] is not None
        ]
        dividends = [d for d in dividends if d is not None]
        first = next((d for d in dividends if d > 0), None)
        last = dividends[-1] if dividends else None
        if first and last and last > 0 and len(dividends) > 1:
            span = len(dividends) - 1
            div_cagr = ((last / first) ** (1 / span) - 1) * 100
            rows.append(_row(20, "Dividend CAGR", "Div CAGR > 0; rise only with rising PAT",
                             _fmt(div_cagr, "%", 1),
                             PASS if div_cagr > 0 else FAIL,
                             "Dividend derived as PAT x Dividend Payout % (Screener does not "
                             "publish the absolute dividend), so this is approximate."))
        else:
            rows.append(_row(20, "Dividend CAGR", "Div CAGR > 0; rise only with rising PAT",
                             "No dividend history", FAIL if dividends else NO_DATA))
    else:
        rows.append(_row(20, "Dividend CAGR", "Div CAGR > 0; rise only with rising PAT",
                         "n/a", NO_DATA))

    # 21 ─ Promoter holding
    prom_series = [v for v in f.row("shareholding", "Promoters", drop_ttm=False) if v is not None]
    promoter = prom_series[-1] if prom_series else None
    rows.append(_row(
        21, "Promoter Holding", "> 51% (higher the better)", _fmt(promoter, "%"),
        PASS if (promoter is not None and promoter > 51) else (FAIL if promoter is not None else NO_DATA),
    ))

    # 22 ─ Promoter buying
    if len(prom_series) >= 2:
        change = prom_series[-1] - prom_series[0]
        direction = "increasing" if change > 0.01 else ("decreasing" if change < -0.01 else "unchanged")
        rows.append(_row(
            22, "Promoter Buying Shares", "Insider buying++",
            f"{prom_series[0]:.2f}% -> {prom_series[-1]:.2f}% ({direction})",
            PASS if change > 0.01 else (FAIL if change < -0.01 else INFO),
            "Proxy: direction of promoter stake across the quarters Screener shows. "
            "Actual insider transaction filings are not read.",
        ))
    else:
        rows.append(_row(22, "Promoter Buying Shares", "Insider buying++", "n/a", NO_DATA))

    # 23 ─ FII holding: "lower the better" with no cut-off, so reported not scored
    fii_series = [v for v in f.row("shareholding", "FIIs", drop_ttm=False) if v is not None]
    fii = fii_series[-1] if fii_series else None
    rows.append(_row(
        23, "FII Shareholding", "~ 0% (lower the better)", _fmt(fii, "%"),
        INFO if fii is not None else NO_DATA,
        "No numeric threshold was specified, so this is reported rather than scored.",
    ))

    # 24-25 ─ qualitative
    rows.append(_row(24, "Product Diversification", "Pure play better",
                     f"Industry: {industry}" if industry else "Not available", MANUAL,
                     "Segment mix comes from the annual report."))
    rows.append(_row(25, "Govt Influence",
                     "No cap on profit/pricing, no subsidy dependence, no forced clients",
                     "Not available", MANUAL, "Judgement call on the sector and regulation."))

    # 26 ─ SSGR: formula not implemented rather than guessed
    dep = f.latest("profit_loss", "Depreciation")
    fa_latest = f.latest("balance_sheet", "Fixed Assets")
    dpr = f.latest("profit_loss", "Dividend Payout %")
    dep_pct = (dep / fa_latest * 100) if (dep is not None and fa_latest) else None
    inputs = (f"NFAT {_fmt(nfat)}x, NPM {_fmt(npm, '%')}, "
              f"DPR {_fmt(dpr, '%', 0)}, Dep {_fmt(dep_pct, '%')} of NFA")
    rows.append(_row(
        26, "Margin of Safety: SSGR", "SSGR > 10Y sales growth", inputs, MANUAL,
        "The self-sustainable-growth-rate formula is not implemented because its exact "
        f"definition could not be verified; the inputs are shown so you can apply it. "
        f"10Y sales growth is {_fmt(s10, '%', 1)}.",
    ))

    # 27 ─ FCF / CFO
    fcf_series = [v for v in f.row("cash_flow", "Free Cash Flow") if v is not None]
    if fcf_series and cfo_series:
        years = min(len(fcf_series), len(cfo_series))
        c_fcf, c_cfo2 = sum(fcf_series[-years:]), sum(cfo_series[-years:])
        ratio = (c_fcf / c_cfo2) if c_cfo2 else None
        rows.append(_row(
            27, "Margin of Safety: FCF / CFO", "FCF / CFO > 0",
            f"{ratio:.2f}" + f" (cFCF Rs {c_fcf:,.0f} Cr / cCFO Rs {c_cfo2:,.0f} Cr)" if ratio is not None else "n/a",
            (PASS if ratio > 0 else FAIL) if ratio is not None else NO_DATA,
            f"Cumulative over {years} years",
        ))
    else:
        rows.append(_row(27, "Margin of Safety: FCF / CFO", "FCF / CFO > 0", "n/a", NO_DATA))

    # 28 ─ Credit rating
    rows.append(_row(
        28, "Credit Rating & Improvement", "BBB- or above; improving", "Not available", MANUAL,
        "Ratings come from CRISIL/ICRA/CARE releases or the annual report, not Screener.",
    ))

    # ── Valuation ──
    pe = f.top_ratio("Stock P/E") or (getattr(market, "pe", None) if market else None)
    rows.append(_row(29, "PE Ratio", "< 10", _fmt(pe),
                     PASS if (pe is not None and pe < 10) else (FAIL if pe is not None else NO_DATA)))

    book_value = f.top_ratio("Book Value") or (getattr(market, "book_value", None) if market else None)
    pb = (cmp_value / book_value) if (cmp_value and book_value) else None
    rows.append(_row(30, "Price to Book Ratio", "< 1", _fmt(pb),
                     PASS if (pb is not None and pb < 1) else (FAIL if pb is not None else NO_DATA),
                     "CMP / Book Value"))

    # PEG is meaningless on negative earnings growth, so it is not forced
    if pe is not None and profit_cagr is not None and profit_cagr > 0:
        peg = pe / profit_cagr
        rows.append(_row(31, "PEG Ratio", "< 1", _fmt(peg),
                         PASS if peg < 1 else FAIL,
                         f"P/E / {horizon} profit CAGR ({_fmt(profit_cagr, '%', 1)})"))
    elif profit_cagr is not None and profit_cagr <= 0:
        rows.append(_row(31, "PEG Ratio", "< 1", "Not meaningful", NO_DATA,
                         f"{horizon} profit CAGR is {_fmt(profit_cagr, '%', 1)}; PEG is undefined "
                         "when earnings growth is not positive."))
    else:
        rows.append(_row(31, "PEG Ratio", "< 1", "n/a", NO_DATA))

    earnings_yield = (100 / pe) if pe else None
    rows.append(_row(
        32, "Earnings Yield", f">= 10Y G-Sec yield ({GSEC_10Y_YIELD:g}%)", _fmt(earnings_yield, "%"),
        PASS if (earnings_yield is not None and earnings_yield >= GSEC_10Y_YIELD)
        else (FAIL if earnings_yield is not None else NO_DATA),
        "Earnings yield taken as 1 / P/E. The G-Sec rate is the GSEC_10Y_YIELD "
        "environment variable — update it as rates move.",
    ))

    ps = (market_cap / sales) if (market_cap and sales) else None
    rows.append(_row(33, "Price to Sales Ratio", "< 1.5", _fmt(ps),
                     PASS if (ps is not None and ps < 1.5) else (FAIL if ps is not None else NO_DATA),
                     "Market Cap / latest full-year Sales"))

    div_yield = f.top_ratio("Dividend Yield") or (getattr(market, "dividend_yield", None) if market else None)
    rows.append(_row(34, "Dividend Yield", "> 0", _fmt(div_yield, "%"),
                     PASS if (div_yield is not None and div_yield > 0) else (FAIL if div_yield is not None else NO_DATA)))

    return rows


def summarise(rows: list) -> dict:
    counts = {PASS: 0, FAIL: 0, INFO: 0, MANUAL: 0, NO_DATA: 0}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    scored = counts[PASS] + counts[FAIL]
    return {
        "passed": counts[PASS],
        "failed": counts[FAIL],
        "informational": counts[INFO],
        "manual": counts[MANUAL],
        "no_data": counts[NO_DATA],
        "scored": scored,
        "score_pct": round(counts[PASS] / scored * 100, 1) if scored else None,
        "total": len(rows),
    }
