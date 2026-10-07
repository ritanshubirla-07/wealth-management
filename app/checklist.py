"""Shared accessor over the scraped/fetched company statements.

``Financials`` wraps one company's payload — profit & loss, balance sheet, cash
flow, ratios, shareholding, plus Screener's compounded-growth boxes — and hands
back aligned numeric series regardless of which source produced it. It is the
single reading layer under the scorecard in :mod:`app.vm_scoring`.

The 34-point pass/fail checklist that used to live here was replaced by the
Growth / Quality / Ownership / Valuation scorecard in :mod:`app.vm_scoring`,
which scores against the same criteria instead of ticking and crossing them.
"""

import os
from typing import Optional

# ── Tunables ────────────────────────────────────────────────────────────────
# Used by the earnings-yield parameter, measured against the risk-free rate. It
# moves, so it is an environment variable rather than a number buried in code.
GSEC_10Y_YIELD = float(os.getenv("GSEC_10Y_YIELD", "6.5"))


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

# Screener uses different row names for lending businesses
SALES_LABELS = ("Sales", "Revenue")
BORROWING_LABELS = ("Borrowings", "Borrowing")
