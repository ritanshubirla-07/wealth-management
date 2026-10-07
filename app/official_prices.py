"""Official closing prices and NAVs, straight from the exchanges and AMFI.

Three statically published files cover almost every holding, so pricing a whole
portfolio costs three downloads rather than one scrape per security:

* **NSE BhavCopy** — official close for every NSE equity, keyed by ISIN.
* **BSE BhavCopy** — same for BSE, and the only source for BSE-only scrips.
* **AMFI NAVAll.txt** — official NAV for ~14,000 mutual-fund schemes, which no
  equity source carries at all.

These are downloads of published files, not API calls: nothing here scrapes, and
nothing needs a login. The figures are **previous close**, not intraday, which is
the deliberate trade-off for using the authoritative number.

Matching is exact wherever possible (ISIN, then exchange code/symbol) and falls
back to a normalised name only for equities. A mutual fund is matched on its
scheme name plus plan, and an ambiguous match is refused rather than guessed —
Direct and Regular plans of the same scheme have materially different NAVs.
"""

import csv
import io
import logging
import os
import re
import zipfile
from datetime import date, datetime, timedelta
from typing import Optional

import requests

from app.market_data import normalise_name

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

NSE_BHAVCOPY = ("https://nsearchives.nseindia.com/content/cm/"
                "BhavCopy_NSE_CM_0_0_0_{ymd}_F_0000.csv.zip")
BSE_BHAVCOPY = ("https://www.bseindia.com/download/BhavCopy/Equity/"
                "BhavCopy_BSE_CM_0_0_0_{ymd}_F_0000.CSV")
AMFI_NAV = "https://www.amfiindia.com/spages/NAVAll.txt"

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
# How many calendar days back to look for the most recent published file
LOOKBACK_DAYS = 8
# Reuse a cached file for this long before re-downloading
CACHE_MAX_AGE_HOURS = float(os.getenv("OFFICIAL_PRICES_MAX_AGE_HOURS", "6"))


def _cache_path(name: str) -> str:
    return os.path.join(DATA_DIR, name)


def _cached_text(name: str) -> Optional[str]:
    path = _cache_path(name)
    if not os.path.exists(path):
        return None
    age = datetime.now() - datetime.fromtimestamp(os.path.getmtime(path))
    if age > timedelta(hours=CACHE_MAX_AGE_HOURS):
        return None
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _store_text(name: str, text: str) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(_cache_path(name), "w", encoding="utf-8") as fh:
        fh.write(text)


def _get(url: str, referer: str, session=None, timeout: int = 45):
    getter = session.get if session is not None else requests.get
    return getter(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*",
                                "Accept-Language": "en-IN,en;q=0.9",
                                "Referer": referer}, timeout=timeout)


# ── NSE ─────────────────────────────────────────────────────────────────────

def load_nse_bhavcopy(session=None, force_refresh: bool = False) -> tuple:
    """Latest NSE equity BhavCopy as (rows, trade_date)."""
    if not force_refresh:
        cached = _cached_text("nse_bhavcopy_latest.csv")
        if cached:
            rows = list(csv.DictReader(io.StringIO(cached)))
            return rows, (rows[0].get("TradDt") if rows else None)

    day = date.today()
    for _ in range(LOOKBACK_DAYS):
        if day.weekday() < 5:
            try:
                resp = _get(NSE_BHAVCOPY.format(ymd=day.strftime("%Y%m%d")),
                            "https://www.nseindia.com/", session)
                if resp.status_code == 200 and resp.content[:2] == b"PK":
                    archive = zipfile.ZipFile(io.BytesIO(resp.content))
                    text = archive.read(archive.namelist()[0]).decode("utf-8", errors="replace")
                    _store_text("nse_bhavcopy_latest.csv", text)
                    rows = list(csv.DictReader(io.StringIO(text)))
                    log.info("NSE bhavcopy %s: %d rows", day, len(rows))
                    return rows, day.isoformat()
            except Exception as exc:
                log.info("NSE bhavcopy %s failed: %s", day, exc)
        day -= timedelta(days=1)
    log.warning("No NSE bhavcopy found in the last %d days", LOOKBACK_DAYS)
    return [], None


# ── BSE ─────────────────────────────────────────────────────────────────────

def load_bse_bhavcopy(session=None, force_refresh: bool = False) -> tuple:
    """Latest BSE equity BhavCopy as (rows, trade_date)."""
    if not force_refresh:
        cached = _cached_text("bse_bhavcopy_latest.csv")
        if cached:
            rows = list(csv.DictReader(io.StringIO(cached)))
            return rows, (rows[0].get("TradDt") if rows else None)

    day = date.today()
    for _ in range(LOOKBACK_DAYS):
        if day.weekday() < 5:
            try:
                resp = _get(BSE_BHAVCOPY.format(ymd=day.strftime("%Y%m%d")),
                            "https://www.bseindia.com/", session)
                ctype = resp.headers.get("content-type", "").lower()
                if resp.status_code == 200 and "html" not in ctype and len(resp.content) > 1000:
                    _store_text("bse_bhavcopy_latest.csv", resp.text)
                    rows = list(csv.DictReader(io.StringIO(resp.text)))
                    log.info("BSE bhavcopy %s: %d rows", day, len(rows))
                    return rows, day.isoformat()
            except Exception as exc:
                log.info("BSE bhavcopy %s failed: %s", day, exc)
        day -= timedelta(days=1)
    log.warning("No BSE bhavcopy found in the last %d days", LOOKBACK_DAYS)
    return [], None


# ── AMFI ────────────────────────────────────────────────────────────────────

_AMFI_NOISE = re.compile(r'\b(arn|folio)\b[-\s]*\w*|\d[\d/.\-]*', re.IGNORECASE)
_PLAN_WORDS = re.compile(r'\b(direct|regular)\b', re.IGNORECASE)
_OPTION_WORDS = re.compile(r'\b(growth|idcw|dividend|payout|reinvest\w*)\b', re.IGNORECASE)


def load_amfi_navs(session=None, force_refresh: bool = False) -> tuple:
    """AMFI's all-scheme NAV file as (rows, nav_date)."""
    text = None if force_refresh else _cached_text("amfi_navall.txt")
    if text is None:
        try:
            resp = _get(AMFI_NAV, "https://www.amfiindia.com/", session, timeout=90)
            if resp.status_code == 200 and len(resp.content) > 10000:
                text = resp.text
                _store_text("amfi_navall.txt", text)
        except Exception as exc:
            log.warning("AMFI NAV download failed: %s", exc)
    if not text:
        return [], None

    rows, nav_date = [], None
    for line in text.splitlines():
        parts = line.split(";")
        if len(parts) < 8 or parts[0].strip() == "Scheme Code":
            continue
        try:
            nav = float(parts[6].strip())
        except (ValueError, IndexError):
            continue
        rows.append({
            "code": parts[0].strip(),
            "isin_growth": parts[1].strip().upper(),
            "isin_reinvest": parts[2].strip().upper(),
            "name": parts[3].strip(),
            "plan": parts[4].strip(),
            "option": parts[5].strip(),
            "nav": nav,
            "date": parts[7].strip(),
        })
        nav_date = nav_date or parts[7].strip()
    log.info("AMFI NAVs: %d schemes as of %s", len(rows), nav_date)
    return rows, nav_date


# ── Combined index ──────────────────────────────────────────────────────────

class OfficialPrices:
    """Lookup of official close / NAV by ISIN, exchange code, symbol or name."""

    def __init__(self):
        self.by_isin: dict = {}
        self.by_symbol: dict = {}
        self.by_bse_code: dict = {}
        self.by_name: dict = {}
        self.funds: list = []
        self.nse_date = self.bse_date = self.amfi_date = None
        self.counts = {"nse": 0, "bse": 0, "amfi": 0}

    # -- loading ----------------------------------------------------------
    @classmethod
    def load(cls, force_refresh: bool = False) -> "OfficialPrices":
        self = cls()
        session = requests.Session()

        nse_rows, self.nse_date = load_nse_bhavcopy(session, force_refresh)
        for row in nse_rows:
            if (row.get("SctySrs") or "").strip() != "EQ":
                continue
            price = _num(row.get("ClsPric"))
            if price is None:
                continue
            self.counts["nse"] += 1
            self._add(row.get("ISIN"), row.get("TckrSymb"), None,
                      row.get("FinInstrmNm"), price, "NSE close")

        bse_rows, self.bse_date = load_bse_bhavcopy(session, force_refresh)
        for row in bse_rows:
            price = _num(row.get("ClsPric"))
            if price is None:
                continue
            self.counts["bse"] += 1
            self._add(row.get("ISIN"), row.get("TckrSymb"), row.get("FinInstrmId"),
                      row.get("FinInstrmNm"), price, "BSE close")

        self.funds, self.amfi_date = load_amfi_navs(session, force_refresh)
        self.counts["amfi"] = len(self.funds)
        for fund in self.funds:
            for isin in (fund["isin_growth"], fund["isin_reinvest"]):
                if isin and isin != "-":
                    self.by_isin.setdefault(isin, (fund["nav"], "AMFI NAV"))
        return self

    def _add(self, isin, symbol, bse_code, name, price, source) -> None:
        # NSE is loaded first, so setdefault keeps NSE's close where both list a scrip
        if isin and isin.strip():
            self.by_isin.setdefault(isin.strip().upper(), (price, source))
        if symbol and symbol.strip():
            self.by_symbol.setdefault(symbol.strip().upper(), (price, source))
        if bse_code and bse_code.strip():
            self.by_bse_code.setdefault(bse_code.strip(), (price, source))
        key = normalise_name(name or "")
        if key:
            clean_isin = (isin or "").strip().upper()
            existing = self.by_name.get(key)
            if existing is None:
                self.by_name[key] = (price, source, clean_isin)
            else:
                _, _, prior_isin = existing
                # A dual-listed scrip appears twice with two different closes.
                # That is the SAME company, so keep the first (NSE) price. Only a
                # genuine name collision — two different ISINs — is ambiguous.
                if prior_isin and clean_isin and prior_isin != clean_isin:
                    self.by_name[key] = (None, "ambiguous name", "")

    # -- lookup -----------------------------------------------------------
    def lookup(self, isin: str = "", name: str = "",
               nse_symbol: str = "", bse_code: str = "") -> tuple:
        """Return (price, source) or (None, reason-ish None)."""
        isin = (isin or "").strip().upper()
        if isin and isin in self.by_isin:
            price, source = self.by_isin[isin]
            if price is not None:
                return price, source

        symbol = (nse_symbol or "").strip().upper()
        if symbol and symbol in self.by_symbol:
            price, source = self.by_symbol[symbol]
            if price is not None:
                return price, source

        code = (bse_code or "").strip()
        if code and code in self.by_bse_code:
            price, source = self.by_bse_code[code]
            if price is not None:
                return price, source

        key = normalise_name(name or "")
        if key and key in self.by_name:
            price, source = self.by_name[key][0], self.by_name[key][1]
            if price is not None:
                return price, source

        # Mutual funds: match scheme name + plan
        nav = self.lookup_fund(name or "")
        if nav is not None:
            return nav, "AMFI NAV"
        return None, None

    def lookup_fund(self, name: str) -> Optional[float]:
        """NAV for a mutual-fund holding, or None when it cannot be pinned down.

        Statement names carry folio and ARN codes ("... Growth 13299633 ARN-112705").
        An ARN is a distributor code, so its presence means a REGULAR plan; a name
        without one is treated as Direct. Scheme + plan + option must all agree,
        and anything still ambiguous returns None.
        """
        if not self.funds or not name:
            return None
        raw = str(name)
        wants_regular = bool(re.search(r'\barn\b', raw, re.IGNORECASE))
        plan_hint = _PLAN_WORDS.search(raw)
        if plan_hint:
            wants_regular = plan_hint.group(1).lower() == "regular"
        option_hint = _OPTION_WORDS.search(raw)
        wants_growth = not option_hint or option_hint.group(1).lower() == "growth"

        stripped = _AMFI_NOISE.sub(" ", raw)
        stripped = _PLAN_WORDS.sub(" ", stripped)
        stripped = _OPTION_WORDS.sub(" ", stripped)
        key = normalise_name(stripped)
        if len(key) < 8:
            return None

        matches = []
        for fund in self.funds:
            fkey = normalise_name(fund["name"])
            if not fkey or fkey != key:
                continue
            is_regular = "regular" in fund["plan"].lower()
            is_growth = "growth" in fund["option"].lower()
            if is_regular != wants_regular:
                continue
            if is_growth != wants_growth:
                continue
            matches.append(fund)

        if len(matches) == 1:
            return matches[0]["nav"]
        if len(matches) > 1:
            # Same scheme/plan/option listed twice — refuse rather than pick one
            navs = {round(m["nav"], 4) for m in matches}
            if len(navs) == 1:
                return matches[0]["nav"]
            log.info("AMFI match ambiguous for %r (%d candidates)", name, len(matches))
        return None

    def as_dict(self) -> dict:
        return {
            "nse_close_date": self.nse_date,
            "bse_close_date": self.bse_date,
            "amfi_nav_date": self.amfi_date,
            "nse_scrips": self.counts["nse"],
            "bse_scrips": self.counts["bse"],
            "amfi_schemes": self.counts["amfi"],
        }


def _num(value) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text or text in ("-", "NA"):
        return None
    try:
        return float(text)
    except ValueError:
        return None
