"""34-parameter stock score, grouped into Growth / Quality / Ownership / Valuation.

Modelled on the "VM Analysis" workbook (JSW_Steel analysis.xlsx). That sheet
groups its 34 parameters into Quality (61 points) and Valuation (10 points) for a
71-point total; this module keeps every parameter and its max weight identical
but splits them across four classes, so the totals still reconcile:

    Growth      4 params    8 points
    Quality    19 params   42 points
    Ownership   5 params   11 points
    Valuation   6 params   10 points
    -----------------------------------
    Total      34 params   71 points   <- same as the workbook

**On the scores.** In the workbook the score cells are hardcoded numbers
(``J6 = 1.5``), i.e. the analyst's own judgement, not a formula that can be
recovered from the file. The rubric below is a deterministic reconstruction
against the same criteria and the same max weights, calibrated against a
manually-scored reference (Allied Blenders & Distillers): a plain threshold
check ("D/E <= 0.5", "Current Ratio >= 1.25", "PEG < 1" ...) is **binary on
the latest/TTM figure** — full marks if it clears the bar, zero otherwise —
rather than a historical average, since the reference scored full marks on a
pass even where several prior years had failed. A genuine consistency
criterion (cash flow positive "every year", a turnover ratio's trend) keeps
partial credit. Every row reports the measured value alongside its score so a
number can always be traced back. Every score is rounded to the nearest 0.5.

Sales and profit growth use the **average of the 1Y, 3Y and 5Y CAGRs**, as
requested, rather than a single long-horizon figure.

A parameter that cannot be measured from Screener's data — promoter salary, a
credit rating, segment mix — scores ``None`` and is reported as "not assessed".
Its max still counts towards the denominator, exactly as the workbook does, so
the ratio is never flattered by simply skipping what could not be checked.
"""

from typing import Optional

from app.checklist import Financials, GSEC_10Y_YIELD, SALES_LABELS, BORROWING_LABELS

GROWTH, QUALITY, OWNERSHIP, VALUATION = "Growth", "Quality", "Ownership", "Valuation"
CLASS_ORDER = (GROWTH, QUALITY, OWNERSHIP, VALUATION)

# Recent years matter more than distant ones; used for every "% of years met" score
PERIOD_WEIGHTS = ((1, 3.0), (3, 2.0), (5, 1.5), (10, 1.0))

SALES_CAGR_TARGET = 15.0   # parameter 1
NPM_TARGET = 8.0           # parameter 2
TAX_TARGET = 30.0          # parameter 3
ICR_TARGET = 3.0           # parameter 4
DE_TARGET = 0.5            # parameter 5
CURRENT_RATIO_TARGET = 1.25  # parameter 6
PROMOTER_TARGET = 51.0     # parameter 21
# Parameter 29 (PE Ratio): below this the score tapers off gradually, but
# above it the stock is treated as expensive outright and scores zero,
# overriding the gradient.
PE_ZERO_AT = 25.0


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _share_meeting(series: list, predicate) -> Optional[float]:
    """Weighted share of years a predicate held, recent years weighted higher."""
    clean = [v for v in series if v is not None]
    if not clean:
        return None
    total_weight = hits = 0.0
    for window, weight in PERIOD_WEIGHTS:
        window_values = clean[-window:]
        if not window_values:
            continue
        met = sum(1 for v in window_values if predicate(v))
        hits += weight * (met / len(window_values))
        total_weight += weight
    return (hits / total_weight) if total_weight else None


def _cagr(series: list, years: int) -> Optional[float]:
    """CAGR in percent over the last ``years`` full years of a series."""
    clean = [v for v in series if v is not None]
    if len(clean) < years + 1:
        return None
    start, end = clean[-(years + 1)], clean[-1]
    if start is None or end is None or start <= 0 or end <= 0:
        return None
    return ((end / start) ** (1 / years) - 1) * 100


def _avg_growth(f: Financials, table: str, series: list) -> tuple:
    """Average of the 1Y, 3Y and 5Y CAGRs, plus the components.

    Screener's own compounded-growth box is preferred where available (it is the
    published figure); the annual series fills any gap.
    """
    one = f.growth(table, "TTM")
    three = f.growth(table, "3 Years")
    five = f.growth(table, "5 Years")
    if one is None:
        one = _cagr(series, 1)
    if three is None:
        three = _cagr(series, 3)
    if five is None:
        five = _cagr(series, 5)
    parts = [p for p in (one, three, five) if p is not None]
    average = (sum(parts) / len(parts)) if parts else None
    return average, one, three, five


def _trend_rising(series: list, recent_years: int = 3) -> Optional[bool]:
    """True when the latest value tops the value from a few years back.

    Averaging whole-history thirds was skewed by one-off early-year lows (a
    COVID dip, an IPO-year restatement) into calling a since-peaked ratio
    "rising" just because the start of the series was worse still. Comparing
    the latest figure to a fixed recency window tracks what the parameter is
    actually asking: has the ratio improved lately, not over the entire past.
    """
    clean = [v for v in series if v is not None]
    if len(clean) < 2:
        return None
    window = min(recent_years, len(clean) - 1)
    return clean[-1] > clean[-1 - window]


def _fmt(value, suffix="", digits=2) -> str:
    return "n/a" if value is None else f"{value:,.{digits}f}{suffix}"


class _Rows:
    """Collects scored rows in workbook order."""

    def __init__(self):
        self.rows = []

    def add(self, sr, cls, parameter, criteria, value, score, maximum, note=""):
        self.rows.append({
            "sr": sr,
            "cls": cls,
            "parameter": parameter,
            "criteria": criteria,
            "value": value,
            # Snapped to the nearest 0.5 — scores are reported in half-point steps.
            "score": None if score is None else round(round(float(score) * 2) / 2, 2),
            "max": maximum,
            "assessed": score is not None,
            "note": note,
        })


def evaluate(fundamentals: dict, market=None, yahoo: dict = None) -> dict:
    """Score all 34 parameters. Returns {"rows", "classes", "total"}.

    ``yahoo`` is the optional Yahoo Finance payload for the same company. It is
    used only where it supplies something Screener does not — currently the real
    current assets and liabilities behind parameter 6 — never blended into a
    Screener series.
    """
    f = Financials(fundamentals, yahoo)
    out = _Rows()
    lender = f.is_lender

    sales_series = f.row("profit_loss", SALES_LABELS)
    pat_series = f.row("profit_loss", "Net Profit")
    cmp_value = getattr(market, "cmp", None) if market else None

    # ── GROWTH ──────────────────────────────────────────────────────────────
    sales_avg, s1, s3, s5 = _avg_growth(f, "Compounded Sales Growth", sales_series)
    out.add(1, GROWTH, "Sales Growth", f"Avg of 1Y/3Y/5Y CAGR > {SALES_CAGR_TARGET:g}%",
            f"{_fmt(sales_avg, '%', 1)}  (1Y {_fmt(s1, '%', 1)}, 3Y {_fmt(s3, '%', 1)}, 5Y {_fmt(s5, '%', 1)})",
            None if sales_avg is None else 3 * _clamp(sales_avg / SALES_CAGR_TARGET),
            3, "Scored on the average of the three horizons against the 15% target.")

    out.add(13, GROWTH, "Production Capacity vs Sales Volume", "Capacity CAGR ~ Sales CAGR",
            "Not assessed", None, 1,
            "Installed capacity and volumes appear only in the annual report.")

    profit_avg, p1, p3, p5 = _avg_growth(f, "Compounded Profit Growth", pat_series)
    if profit_avg is None or sales_avg is None:
        conversion_score, conversion_note = None, "Needs both sales and profit growth."
    elif sales_avg <= 0:
        conversion_score = 3.0 if profit_avg > 0 else 0.0
        conversion_note = "Sales did not grow; scored on whether profit still grew."
    else:
        conversion_score = 3 * _clamp(profit_avg / sales_avg)
        conversion_note = "Profit growth should at least match sales growth."
    out.add(14, GROWTH, "Conversion of Sales to Profits", "Profit CAGR >= Sales CAGR",
            f"Profit {_fmt(profit_avg, '%', 1)} vs Sales {_fmt(sales_avg, '%', 1)}",
            conversion_score, 3, conversion_note)

    payout_series = f.row("profit_loss", "Dividend Payout %")
    dividends = []
    for index in range(-min(len(pat_series), len(payout_series)), 0):
        pat, payout = pat_series[index], payout_series[index]
        if pat is None:
            continue
        dividends.append(pat * ((payout or 0) / 100))
    div_cagr = None
    if len(dividends) > 1:
        first = next((d for d in dividends if d > 0), None)
        last = dividends[-1]
        if first and last > 0:
            span = len(dividends) - 1
            div_cagr = ((last / first) ** (1 / span) - 1) * 100
    out.add(20, GROWTH, "Dividend CAGR", "Dividend CAGR > 0",
            _fmt(div_cagr, "%", 1),
            None if div_cagr is None else (1.0 if div_cagr > 0 else 0.0), 1,
            "Dividend derived as PAT x payout %, which Screener publishes.")

    # ── QUALITY ─────────────────────────────────────────────────────────────
    latest_pat = f.latest("profit_loss", "Net Profit", drop_ttm=False)
    latest_turnover = f.latest("profit_loss", SALES_LABELS, drop_ttm=False)
    latest_npm = (latest_pat / latest_turnover * 100) if (latest_pat is not None and latest_turnover) else None
    out.add(2, QUALITY, "Profitability", f"NPM >= {NPM_TARGET:g}%",
            f"latest {_fmt(latest_npm, '%')}",
            None if latest_npm is None else (2.0 if latest_npm >= NPM_TARGET else 0.0), 2,
            "Scored on the latest (TTM) margin against the bar, not a historical average.")

    latest_tax = f.latest("profit_loss", "Tax %", drop_ttm=False)
    out.add(3, QUALITY, "Tax Payout Ratio", f">= {TAX_TARGET:g}%",
            f"latest {_fmt(latest_tax, '%', 1)}",
            None if latest_tax is None else (1.0 if latest_tax >= TAX_TARGET else 0.0), 1)

    latest_pbt = f.latest("profit_loss", "Profit before tax", drop_ttm=False)
    latest_interest = f.latest("profit_loss", "Interest", drop_ttm=False)
    if latest_pbt is None or latest_interest is None:
        latest_icr = None
    elif latest_interest == 0:
        latest_icr = 999.0                     # no interest burden at all
    else:
        latest_icr = (latest_pbt + latest_interest) / latest_interest
    if lender:
        out.add(4, QUALITY, "Interest Coverage Ratio", f">= {ICR_TARGET:g}x",
                "Not applicable", None, 2,
                "A lender's interest is its cost of funds, not a fixed charge.")
    else:
        out.add(4, QUALITY, "Interest Coverage Ratio", f">= {ICR_TARGET:g}x",
                f"latest {_fmt(latest_icr, 'x', 1)}",
                None if latest_icr is None else (2.0 if latest_icr >= ICR_TARGET else 0.0), 2)

    latest_borrow = f.latest("balance_sheet", BORROWING_LABELS, drop_ttm=False)
    latest_cap = f.latest("balance_sheet", "Equity Capital", drop_ttm=False)
    latest_res = f.latest("balance_sheet", "Reserves", drop_ttm=False)
    latest_equity = ((latest_cap or 0) + (latest_res or 0)) if (latest_cap is not None or latest_res is not None) else None
    latest_de = (latest_borrow / latest_equity) if (latest_borrow is not None and latest_equity) else None
    out.add(5, QUALITY, "Debt to Equity Ratio", f"<= {DE_TARGET:g}",
            f"latest {_fmt(latest_de, '', 3)}",
            None if latest_de is None else (3.0 if latest_de <= DE_TARGET else 0.0), 3,
            "Customer deposits are excluded for lenders." if lender else "")

    # Current ratio. Yahoo reports real current assets and current liabilities;
    # Screener's condensed balance sheet does not split current from non-current,
    # so the reference workbook had to divide Other Assets by Other Liabilities as
    # a stand-in. Use the true figures where we have them and fall back to the
    # workbook's proxy otherwise, saying in the note which one produced the number.
    true_ca = f.yahoo_latest("balance_sheet", "Current Assets")
    true_cl = f.yahoo_latest("balance_sheet", "Current Liabilities")
    if true_ca is not None and true_cl:
        current_ratio = true_ca / true_cl
        cr_note = ("Current Assets / Current Liabilities, as reported by Yahoo Finance "
                   f"(Rs {true_ca:,.0f} Cr / Rs {true_cl:,.0f} Cr).")
    else:
        latest_other_assets = f.latest("balance_sheet", "Other Assets", drop_ttm=False)
        latest_other_liabs = f.latest("balance_sheet", "Other Liabilities", drop_ttm=False)
        current_ratio = ((latest_other_assets / latest_other_liabs)
                         if (latest_other_assets is not None and latest_other_liabs) else None)
        cr_note = ("Other Assets / Other Liabilities, the reference workbook's proxy; "
                   "Screener's balance sheet does not separate current from non-current.")
    out.add(6, QUALITY, "Current Ratio", f">= {CURRENT_RATIO_TARGET:g}",
            f"latest {_fmt(current_ratio)}",
            None if current_ratio is None else (1.0 if current_ratio >= CURRENT_RATIO_TARGET else 0.0), 1,
            cr_note)

    def turnover_rows(sr, label, series, criteria, note):
        latest = next((v for v in reversed(series) if v is not None), None)
        rising = _trend_rising(series)
        out.add(sr, QUALITY, label, criteria,
                f"latest {_fmt(latest, 'x')}"
                + (f", trend {'rising' if rising else 'not rising'}" if rising is not None else ""),
                None if rising is None else (1.0 if rising else 0.0),
                1, note)

    nfa_series = f.row("balance_sheet", "Fixed Assets")
    nfat_series = [
        (s / a) if (s is not None and a) else None
        for s, a in zip(sales_series[-len(nfa_series):] if nfa_series else [], nfa_series)
    ]
    turnover_rows(7, "Net Fixed Assets Turnover", nfat_series,
                  "Higher the better; rising trend healthy",
                  "Full point when the ratio's recent trend is rising against its own "
                  "history; none otherwise.")

    inv_days = f.row("ratios", "Inventory Days")
    turnover_rows(8, "Inventory Turnover Ratio",
                  [(365 / d) if d else None for d in inv_days],
                  "Higher the better; rising trend healthy", "Derived as 365 / inventory days.")

    deb_days = f.row("ratios", "Debtor Days")
    turnover_rows(9, "Debtors Turnover Ratio",
                  [(365 / d) if d else None for d in deb_days],
                  "Higher the better; rising trend healthy", "Derived as 365 / debtor days.")

    cfo_series = f.row("cash_flow", "Cash from Operating Activity")
    cfo_share = _share_meeting(cfo_series, lambda v: v > 0)
    out.add(10, QUALITY, "Cash Flow from Operations", "> 0 every year",
            f"latest Rs {_fmt(f.latest('cash_flow', 'Cash from Operating Activity'), ' Cr', 0)}; "
            f"positive in {_fmt((cfo_share or 0) * 100, '%', 0)} of years",
            None if cfo_share is None else 3 * cfo_share, 3)

    cfo_clean = [v for v in cfo_series if v is not None]
    pat_clean = [v for v in pat_series if v is not None]
    cfo_pat_ratio = None
    if cfo_clean and pat_clean:
        years = min(len(cfo_clean), len(pat_clean))
        c_cfo, c_pat = sum(cfo_clean[-years:]), sum(pat_clean[-years:])
        cfo_pat_ratio = (c_cfo / c_pat) if c_pat else None
    out.add(11, QUALITY, "Cumulative CFO vs Cumulative PAT", "Cumulative CFO >= Cumulative PAT",
            _fmt(cfo_pat_ratio, "x") + (" of PAT converted to cash" if cfo_pat_ratio else ""),
            None if cfo_pat_ratio is None else 3 * _clamp(cfo_pat_ratio), 3)

    out.add(12, QUALITY, "Comparison with Industry Peers",
            "Sales/profit growth and margins vs peers", "Not assessed", None, 2,
            "Screener serves its peer table from a separate request that returns "
            "the wrong company's peers, so it is deliberately not used.")

    # Shareholder value: market-cap growth against retained profits
    market_cap = f.top_ratio("Market Cap") or (getattr(market, "market_cap", None) if market else None)
    price_cagr10 = f.growth("Stock Price CAGR", "10 Years")
    value_ratio = None
    if pat_clean and market_cap and price_cagr10 is not None:
        years = min(10, len(pat_clean))
        retained = 0.0
        for index in range(-years, 0):
            pat = pat_clean[index]
            payout = payout_series[index] if len(payout_series) >= years else None
            retained += pat * (1 - ((payout or 0) / 100))
        past_cap = market_cap / ((1 + price_cagr10 / 100) ** years)
        if retained:
            value_ratio = (market_cap - past_cap) / retained
    out.add(15, QUALITY, "Creating Shareholder Value",
            "Market-cap growth > retained profits (10Y)",
            (f"Rs {value_ratio:,.2f} of market cap per Rs 1 retained" if value_ratio is not None else "n/a"),
            None if value_ratio is None else 3 * _clamp(value_ratio), 3,
            "Market cap 10Y ago is derived from Screener's price CAGR, so it ignores "
            "any change in share count. Approximate.")

    out.add(17, QUALITY, "Management Succession Plan", "Annual reports / concall transcripts",
            "Not assessed", None, 1, "Requires reading the filings.")
    out.add(19, QUALITY, "Project Execution Skills", "Greenfield / brownfield record",
            "Not assessed", None, 2, "Judgement call from filings and history.")
    out.add(24, QUALITY, "Product Diversification", "Pure play better",
            "Not assessed", None, 2, "Segment revenue appears in the annual report.")
    out.add(25, QUALITY, "Govt Influence",
            "No cap on profit or pricing; no subsidy dependence",
            "Not assessed", None, 3, "Judgement call on the sector and its regulation.")

    # SSGR, per Dr Vijay Malik's published formula
    dep_series = f.row("profit_loss", "Depreciation")
    ssgr = None
    latest_nfat = next((v for v in reversed(nfat_series) if v is not None), None)
    latest_dpr = f.latest("profit_loss", "Dividend Payout %")
    latest_dep = dep_series[-1] if dep_series else None
    latest_nfa = nfa_series[-1] if nfa_series else None
    if None not in (latest_nfat, latest_npm, latest_dep, latest_nfa) and latest_nfa:
        dep_rate = latest_dep / latest_nfa
        ssgr = (latest_nfat * (latest_npm / 100) * (1 - (latest_dpr or 0) / 100) - dep_rate) * 100
    sales_10y = f.growth("Compounded Sales Growth", "10 Years")
    out.add(26, QUALITY, "Margin of Safety: SSGR", "SSGR > 10Y sales growth",
            f"SSGR {_fmt(ssgr, '%')} vs 10Y sales growth {_fmt(sales_10y, '%', 1)}",
            None if (ssgr is None or sales_10y is None) else (3.0 if ssgr > sales_10y else 0.0),
            3, "SSGR = NFAT x NPM x (1 - payout) - depreciation rate. The reference "
               "workbook computes it in its own sheet, so figures may differ slightly.")

    fcf_series = f.row("cash_flow", "Free Cash Flow")
    fcf_ratio = None
    fcf_clean = [v for v in fcf_series if v is not None]
    if fcf_clean and cfo_clean:
        years = min(len(fcf_clean), len(cfo_clean))
        c_fcf, c_cfo = sum(fcf_clean[-years:]), sum(cfo_clean[-years:])
        fcf_ratio = (c_fcf / c_cfo) if c_cfo else None
    out.add(27, QUALITY, "Margin of Safety: FCF / CFO", "Cumulative FCF / CFO > 0",
            _fmt(None if fcf_ratio is None else fcf_ratio * 100, "% of CFO is free cash", 1),
            None if fcf_ratio is None else (3.0 if fcf_ratio > 0 else 0.0), 3)

    out.add(28, QUALITY, "Credit Rating & Improvement", "BBB- or above, and improving",
            "Not assessed", None, 2,
            "Ratings come from CRISIL/ICRA/CARE releases, not Screener.")

    # ── OWNERSHIP ───────────────────────────────────────────────────────────
    out.add(16, OWNERSHIP, "Background Check of Promoters & Directors",
            "No fraud / penalty / SEBI action", "Not assessed", None, 2,
            "Requires a web search; deliberately not automated.")
    out.add(18, OWNERSHIP, "Promoters Salary vs Net Profits",
            "Within 10% of PAT; no rise while PAT falls", "Not assessed", None, 3,
            "Managerial remuneration is in the annual report.")

    promoter_series = [v for v in f.row("shareholding", "Promoters", drop_ttm=False) if v is not None]
    promoter = promoter_series[-1] if promoter_series else None
    out.add(21, OWNERSHIP, "Promoter Holding", f"> {PROMOTER_TARGET:g}% (higher the better)",
            _fmt(promoter, "%"),
            None if promoter is None else (2.0 if promoter >= PROMOTER_TARGET else 0.0), 2)

    out.add(22, OWNERSHIP, "Promoter Buying Shares", "Insider buying (open-market purchases)",
            "Not assessed", None, 3,
            "The promoter % Screener publishes also moves from ESOPs, conversions and "
            "pledge releases, so it cannot stand in for actual insider buying. Needs "
            "insider-trading filings or BSE/NSE bulk-deal announcements.")

    out.add(23, OWNERSHIP, "FII Shareholding", "~ 0% (lower the better)",
            "Not assessed", None, 1,
            "The direction and quality of FII flows need more context than the "
            "aggregate % Screener publishes; treated as a manual check.")

    # ── VALUATION ───────────────────────────────────────────────────────────
    # The reference workbook computes these itself from CMP, TTM EPS, book value
    # and market cap rather than taking Screener's headline "Stock P/E", which is
    # built on a different earnings basis. Reproducing its method:
    #   PE = CMP / TTM EPS      P/B = CMP / Book Value
    #   P/S = Market Cap / TTM Sales    Earnings Yield = TTM EPS / CMP
    price = cmp_value or f.top_ratio("Current Price")
    eps_series = f.row("profit_loss", "EPS in Rs", drop_ttm=False)
    ttm_eps = next((v for v in reversed(eps_series) if v is not None), None)
    pe = (price / ttm_eps) if (price and ttm_eps and ttm_eps > 0) else None
    out.add(29, VALUATION, "PE Ratio", "< 10", _fmt(pe),
            None if pe is None else (
                0.0 if pe > PE_ZERO_AT else
                (2.0 if 0 < pe < 10 else (2 * _clamp(10 / pe) if pe > 0 else 0.0))
            ),
            2, f"CMP {_fmt(price)} / TTM EPS {_fmt(ttm_eps)}."
               + (" Negative EPS, so no ratio." if (ttm_eps is not None and ttm_eps <= 0) else "")
               + (f" PE above {PE_ZERO_AT:g} scores zero outright." if (pe is not None and pe > PE_ZERO_AT) else ""))

    book_value = f.top_ratio("Book Value") or (getattr(market, "book_value", None) if market else None)
    pb = (price / book_value) if (price and book_value) else None
    out.add(30, VALUATION, "Price to Book Ratio", "< 1", _fmt(pb),
            None if pb is None else (1.0 if pb < 1 else 0.0), 1,
            f"CMP {_fmt(price)} / book value per share {_fmt(book_value)}.")

    peg = None
    if pe and profit_avg and profit_avg > 0:
        peg = pe / profit_avg
    if peg is None:
        out.add(31, VALUATION, "PEG Ratio", "< 1",
                "Not meaningful" if (profit_avg is not None and profit_avg <= 0) else "n/a",
                None if (profit_avg is None or pe is None) else 0.0, 3,
                "PEG is undefined when earnings growth is not positive."
                if (profit_avg is not None and profit_avg <= 0) else "")
    else:
        out.add(31, VALUATION, "PEG Ratio", "< 1", _fmt(peg),
                3.0 if peg < 1 else 0.0, 3,
                "P/E divided by the average of the 1Y/3Y/5Y profit CAGRs.")

    earnings_yield = (ttm_eps / price * 100) if (price and ttm_eps) else None
    out.add(32, VALUATION, "Earnings Yield", f">= 10Y G-Sec ({GSEC_10Y_YIELD:g}%)",
            _fmt(earnings_yield, "%"),
            None if earnings_yield is None else (2.0 if earnings_yield >= GSEC_10Y_YIELD else 0.0),
            2, "TTM EPS / CMP. The G-Sec rate is the GSEC_10Y_YIELD "
                "environment variable.")

    latest_sales = next((v for v in reversed(sales_series) if v is not None), None)
    ps = (market_cap / latest_sales) if (market_cap and latest_sales) else None
    out.add(33, VALUATION, "Price to Sales Ratio", "< 1.5", _fmt(ps),
            None if ps is None else (1.0 if ps < 1.5 else 0.0), 1)

    div_yield = f.top_ratio("Dividend Yield") or (getattr(market, "dividend_yield", None) if market else None)
    out.add(34, VALUATION, "Dividend Yield", "> 0", _fmt(div_yield, "%"),
            None if div_yield is None else (1.0 if div_yield > 0 else 0.0), 1)

    return _summarise(out.rows)


def _summarise(rows: list) -> dict:
    """Group rows by class, keeping only the parameters that could be measured.

    A parameter that needs an annual report, a credit rating or a web search is
    dropped from the output entirely, and its weight is dropped from the
    denominator with it — a class scored out of points for rows nobody can see
    would understate every holding. The counts of what was excluded are still
    reported so the omission is visible rather than silent.

    Within each class the rows are renumbered from 1. The parameter's number in
    the source workbook is preserved as ``sr_ref`` for traceability.
    """
    classes = []
    for name in CLASS_ORDER:
        members = sorted((r for r in rows if r["cls"] == name), key=lambda r: r["sr"])
        scored = [r for r in members if r["score"] is not None]
        excluded = [r for r in members if r["score"] is None]

        # Renumber the surviving rows 1..n inside this class
        for index, row in enumerate(scored, start=1):
            row["sr_ref"] = row["sr"]
            row["sr"] = index

        actual = sum(r["score"] for r in scored)
        maximum = sum(r["max"] for r in scored)
        classes.append({
            "name": name,
            "score": round(actual, 2),
            "max": maximum,
            "pct": round(actual / maximum * 100, 1) if maximum else None,
            "params": len(scored),
            "excluded_params": len(excluded),
            "excluded_max": sum(r["max"] for r in excluded),
            "excluded_names": [r["parameter"] for r in excluded],
            "rows": scored,
        })

    total_actual = sum(c["score"] for c in classes)
    total_max = sum(c["max"] for c in classes)
    excluded_params = sum(c["excluded_params"] for c in classes)
    excluded_max = sum(c["excluded_max"] for c in classes)
    return {
        "classes": classes,
        "rows": [r for c in classes for r in c["rows"]],
        "total": {
            "score": round(total_actual, 2),
            "max": total_max,
            "pct": round(total_actual / total_max * 100, 1) if total_max else None,
            "params": sum(c["params"] for c in classes),
            "excluded_params": excluded_params,
            "excluded_max": excluded_max,
            # The workbook's full 71-point universe, for reference
            "workbook_max": total_max + excluded_max,
        },
    }
