"""Decide whether a parsed statement row is a real holding.

The strongest available test is exchange listing: every genuine equity appears on
NSE's equity master or BSE's scrip master, while an aggregate row ("Net Worth",
"Stocks", "Consolidated Position") appears on neither. That makes the masters an
excellent junk filter — and it is why both are built and cached locally.

Listing alone is *not* sufficient, though. A portfolio legitimately contains
instruments that are on neither equity master:

* mutual funds, index funds and gold/silver fund-of-funds
* bonds, debentures and government securities
* cash and payable/receivable lines
* recently delisted or suspended equities the client still holds

Dropping those would erase real money — in this project's own data a listed-only
rule would have deleted a client's entire mutual-fund portfolio. So a row is kept
when it is either listed *or* recognisably one of those instruments, and dropped
only when it is neither: unlisted and unidentifiable, which is what an aggregate
row looks like.
"""

import logging
import os
import re
from typing import Optional

log = logging.getLogger(__name__)

# Set HOLDINGS_LISTED_ONLY=true to enforce the strict rule instead: keep only
# securities present on NSE or BSE. This DOES discard mutual funds, bonds and
# cash lines, so it is off by default.
LISTED_ONLY = os.getenv("HOLDINGS_LISTED_ONLY", "false").strip().lower() in (
    "1", "true", "yes", "on")

# Mutual funds / ETFs / fund-of-funds
_FUND_RE = re.compile(
    r'\b(fund|scheme|mutual|sip|nfo|etf|fof|index\s+fund|liquid|money\s*market|'
    r'gilt|arbitrage|flexi\s*cap|large\s*cap|mid\s*cap|small\s*cap|multi\s*cap|'
    r'balanced\s+advantage|hybrid|savings\s+plan|growth\s+plan|dividend\s+plan)\b'
    r'|arn-\d|\bgrowth\b|\bidcw\b',
    re.IGNORECASE)

# Bonds, debentures, government securities
_BOND_RE = re.compile(
    r'\b(bond|debenture|ncd|sgb|gsec|g-sec|govt\s+stock|treasury|tbill|t-bill|'
    r'perpetual|sdl)\b|\bbd\b|\bfvrs\b|\d+\.\d+%',
    re.IGNORECASE)

# Cash and near-cash lines
_CASH_RE = re.compile(
    r'^(cash|cash\s*&?\s*(and\s*)?(equivalents?|bank)?|bank\s+balance|'
    r'cash\s+rec/?payable|receivable|payable|margin|ledger\s+balance)',
    re.IGNORECASE)

# Mutual fund ISINs are allotted in the INF series; equities use INE
_MF_ISIN_RE = re.compile(r'^INF', re.IGNORECASE)
# Shape of a valid ISIN — presence of one is proof the row is a security
_ISIN_RE = re.compile(r'^[A-Z]{2}[A-Z0-9]{9}[0-9]$')


def classify_instrument(name: str, isin: str = "") -> Optional[str]:
    """Name/ISIN-based instrument type, or None when nothing is recognisable.

    A valid ISIN is decisive on its own: securities are allotted one, aggregate
    rows are not. So a suspended or freshly delisted equity — absent from both
    exchange masters — is still recognised as a real holding, while "Net Worth"
    and "Stocks" are not.
    """
    text = (name or "").strip()
    code = (isin or "").strip().upper()
    if code and _ISIN_RE.match(code):
        # INF is the mutual-fund series; INE and the rest are securities
        return "mutual_fund" if _MF_ISIN_RE.match(code) else "security"
    if not text:
        return None
    if _CASH_RE.match(text):
        return "cash"
    if _FUND_RE.search(text):
        return "mutual_fund"
    if _BOND_RE.search(text):
        return "bond"
    return None


def screen_holdings(holdings, resolver, listed_only: bool = None) -> tuple:
    """Split parsed holdings into (kept, dropped).

    ``dropped`` entries are ``(holding, reason)`` so the caller can log exactly
    what was discarded and why — a silent drop of a real holding would be worse
    than the aggregate rows this is meant to remove.
    """
    if listed_only is None:
        listed_only = LISTED_ONLY

    kept, dropped = [], []
    for h in holdings:
        listing = None
        if resolver is not None:
            try:
                listing = resolver.listing(
                    getattr(h, "isin", "") or "",
                    h.security_name,
                    getattr(h, "nse_symbol", "") or "",
                    getattr(h, "bse_code", "") or "",
                )
            except Exception as exc:          # a master failure must not drop rows
                log.warning("listing check failed for %s: %s", h.security_name, exc)
                kept.append(h)
                continue

        h.listed_on = listing
        instrument = classify_instrument(h.security_name, getattr(h, "isin", "") or "")
        h.instrument_type = "equity" if listing else instrument

        if listing:
            kept.append(h)
        elif listed_only:
            dropped.append((h, f"not on NSE or BSE (HOLDINGS_LISTED_ONLY is on;"
                               f" looks like {instrument or 'an aggregate row'})"))
        elif instrument:
            kept.append(h)
        else:
            dropped.append((h, "on neither exchange master and not recognisable as a "
                               "fund, bond or cash line — treated as a total/aggregate row"))
    return kept, dropped
