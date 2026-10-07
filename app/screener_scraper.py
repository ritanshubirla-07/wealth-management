"""Fetch current market prices from Screener.in company pages.

Design notes — this module is deliberately conservative:

* **Public pages only.** It requests ``/company/<CODE>/`` and nothing else. No
  login, no session cookie, no premium credentials are involved, so a scraping
  mistake can never expose or burn an account.
* **robots.txt is enforced in code**, not just honoured by convention. Screener
  disallows ``/*?q=`` (its search endpoint), ``/*?sort=``, ``/*?limit=``,
  ``/*?page=``, ``/user/*`` and ``/company/source/quarter/*``. Every URL is
  checked before it is fetched, so the crawler cannot drift onto a
  disallowed path later. Symbols are therefore resolved from NSE's own
  published equity master rather than Screener's search.
* **One request at a time, spaced out.** No concurrency. Screener publishes no
  Crawl-delay, so a conservative default is used.
* **A failure never produces a number.** If the price cannot be found the
  result is an explicit error; it is never silently recorded as 0.
"""

import csv
import io
import logging
import os
import re
import threading
import time
import urllib.robotparser
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from app.market_data import clean_number, normalise_name

log = logging.getLogger(__name__)

BASE = "https://www.screener.in"
COMPANY_URL = BASE + "/company/{code}/"
# Screener's default company URL serves STANDALONE financials. For a group with
# subsidiaries the consolidated statements are the meaningful ones — JSW Steel's
# standalone sales are Rs 1.33 lakh Cr against Rs 1.85 lakh Cr consolidated — so
# the consolidated page is preferred and the default is the fallback.
CONSOLIDATED_URL = BASE + "/company/{code}/consolidated/"
ROBOTS_URL = BASE + "/robots.txt"

# NSE's official list of listed equities: SYMBOL, NAME OF COMPANY, ISIN NUMBER.
# This is what makes a Screener URL resolvable from a demat statement's ISIN
# without touching Screener's (robots-disallowed) search endpoint.
NSE_EQUITY_MASTER = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"

# BSE publishes no scrip-master CSV that survives its WAF (the List_Scrips API
# returns 403), but the daily equity BhavCopy is a plain public file carrying
# ISIN, scrip code, ticker and name. One day only lists scrips that traded, so
# several days are merged to cover illiquid counters.
BSE_BHAVCOPY_URL = ("https://www.bseindia.com/download/BhavCopy/Equity/"
                    "BhavCopy_BSE_CM_0_0_0_{ymd}_F_0000.CSV")
BSE_BHAVCOPY_DAYS = int(os.getenv("BSE_BHAVCOPY_DAYS", "12"))

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
MASTER_CACHE = os.path.join(DATA_DIR, "nse_equity_master.csv")
BSE_MASTER_CACHE = os.path.join(DATA_DIR, "bse_equity_master.csv")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Screener starts returning HTTP 429 above roughly 1 request/second, so the
# default spacing sits under that and widens automatically if it is ever hit.
DEFAULT_DELAY = 1.1      # seconds between request starts (global, across workers)
DEFAULT_TIMEOUT = 20     # seconds per request
DEFAULT_RETRIES = 3
MAX_BACKOFF_DELAY = 6.0  # ceiling for the adaptive spacing


# ── BSE equity master ───────────────────────────────────────────────────────

def _bse_bhavcopy_day(day, session=None) -> list:
    """Fetch one day's BSE equity BhavCopy. Returns [] when there is no file."""
    url = BSE_BHAVCOPY_URL.format(ymd=day.strftime("%Y%m%d"))
    getter = session.get if session is not None else requests.get
    try:
        resp = getter(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*",
                                    "Referer": "https://www.bseindia.com/"}, timeout=40)
    except requests.RequestException as exc:
        log.info("BSE bhavcopy %s failed: %s", day, exc)
        return []
    # A non-trading day returns the site's HTML shell rather than a 404
    ctype = resp.headers.get("content-type", "")
    if resp.status_code != 200 or "html" in ctype.lower():
        return []
    try:
        rows = list(csv.DictReader(io.StringIO(resp.text)))
    except Exception as exc:
        log.info("BSE bhavcopy %s unparseable: %s", day, exc)
        return []
    out = []
    for row in rows:
        isin = (row.get("ISIN") or "").strip().upper()
        code = (row.get("FinInstrmId") or "").strip()
        name = (row.get("FinInstrmNm") or "").strip()
        symbol = (row.get("TckrSymb") or "").strip().upper()
        if not code or not name:
            continue
        out.append({"ISIN": isin, "BSE_CODE": code, "SYMBOL": symbol, "NAME": name})
    return out


def load_bse_master(days: int = BSE_BHAVCOPY_DAYS, max_age_days: int = 7,
                    force_refresh: bool = False) -> list:
    """Build (or reuse) a BSE scrip master of ISIN / code / symbol / name.

    Merges the last ``days`` BhavCopy files so scrips that trade rarely are still
    covered, then caches the result as data/bse_equity_master.csv.
    """
    if not force_refresh and os.path.exists(BSE_MASTER_CACHE):
        age = datetime.now() - datetime.fromtimestamp(os.path.getmtime(BSE_MASTER_CACHE))
        if age < timedelta(days=max_age_days):
            with open(BSE_MASTER_CACHE, encoding="utf-8") as fh:
                return list(csv.DictReader(fh))

    merged = {}
    session = requests.Session()
    fetched = 0
    day = datetime.now().date()
    # Walk back over calendar days; weekends and holidays simply return nothing
    for _ in range(days * 3):
        if fetched >= days:
            break
        if day.weekday() < 5:
            rows = _bse_bhavcopy_day(day, session)
            if rows:
                fetched += 1
                for row in rows:
                    key = row["BSE_CODE"]
                    # Keep the first sighting; later days do not overwrite a name
                    merged.setdefault(key, row)
        day -= timedelta(days=1)

    if not merged:
        log.warning("Could not build a BSE master (no BhavCopy files fetched)")
        if os.path.exists(BSE_MASTER_CACHE):
            with open(BSE_MASTER_CACHE, encoding="utf-8") as fh:
                return list(csv.DictReader(fh))
        return []

    rows = sorted(merged.values(), key=lambda r: r["BSE_CODE"])
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(BSE_MASTER_CACHE, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["ISIN", "BSE_CODE", "SYMBOL", "NAME"])
        writer.writeheader()
        writer.writerows(rows)
    log.info("Built BSE master: %d scrips from %d BhavCopy days", len(rows), fetched)
    return rows


# ── Symbol resolution ────────────────────────────────────────────────────────

class SymbolResolver:
    """Maps a holding to a Screener company code using NSE's equity master."""

    def __init__(self, master_rows=None, bse_rows=None):
        self.by_isin: dict = {}
        self.by_name: dict = {}
        self.symbols: set = set()
        # BSE side, built from the merged BhavCopy master
        self.bse_by_isin: dict = {}
        self.bse_by_name: dict = {}
        self.bse_codes: set = set()
        if master_rows:
            self._index(master_rows)
        if bse_rows:
            self._index_bse(bse_rows)

    def _index_bse(self, rows) -> None:
        for row in rows:
            code = (row.get("BSE_CODE") or "").strip()
            isin = (row.get("ISIN") or "").strip().upper()
            name = (row.get("NAME") or "").strip()
            if not code:
                continue
            self.bse_codes.add(code)
            if isin:
                self.bse_by_isin.setdefault(isin, code)
            key = normalise_name(name)
            if key:
                if key in self.bse_by_name and self.bse_by_name[key] != code:
                    self.bse_by_name[key] = None
                else:
                    self.bse_by_name.setdefault(key, code)

    def _index(self, rows) -> None:
        for row in rows:
            symbol = (row.get("SYMBOL") or "").strip().upper()
            isin = (row.get("ISIN NUMBER") or "").strip().upper()
            name = (row.get("NAME OF COMPANY") or "").strip()
            if not symbol:
                continue
            self.symbols.add(symbol)
            if isin:
                self.by_isin.setdefault(isin, symbol)
            key = normalise_name(name)
            if key:
                # A name shared by two listings is recorded as ambiguous (None)
                # so it is reported rather than resolved to an arbitrary one.
                if key in self.by_name and self.by_name[key] != symbol:
                    self.by_name[key] = None
                else:
                    self.by_name.setdefault(key, symbol)

    @classmethod
    def load(cls, max_age_days: int = 7, force_refresh: bool = False,
             with_bse: bool = True) -> "SymbolResolver":
        """Load the NSE master from local cache, downloading it when stale."""
        text = None
        if not force_refresh and os.path.exists(MASTER_CACHE):
            age = datetime.now() - datetime.fromtimestamp(os.path.getmtime(MASTER_CACHE))
            if age < timedelta(days=max_age_days):
                text = open(MASTER_CACHE, encoding="utf-8").read()
                log.info("Using cached NSE equity master (%.1f days old)", age.days + age.seconds / 86400)

        if text is None:
            log.info("Downloading NSE equity master")
            resp = requests.get(
                NSE_EQUITY_MASTER,
                headers={"User-Agent": USER_AGENT, "Accept": "text/csv,*/*",
                         "Accept-Language": "en-IN,en;q=0.9"},
                timeout=30,
            )
            resp.raise_for_status()
            text = resp.text
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(MASTER_CACHE, "w", encoding="utf-8") as fh:
                fh.write(text)

        reader = csv.DictReader(io.StringIO(text))
        # NSE ships some headers with leading spaces (" ISIN NUMBER")
        rows = [{(k or "").strip(): v for k, v in row.items()} for row in reader]

        bse_rows = []
        if with_bse:
            try:
                bse_rows = load_bse_master(force_refresh=force_refresh)
            except Exception as exc:
                # A missing BSE master only costs BSE-only coverage
                log.warning("BSE master unavailable: %s", exc)
        return cls(rows, bse_rows)

    def candidates(self, isin: str = "", name: str = "",
                   nse_symbol: str = "", bse_code: str = "") -> tuple:
        """Screener codes to try, best first, plus a reason if there are none.

        Returns ``(candidates, reason)`` where candidates is a list of
        ``(code, how)``. Several are returned because a code can be valid yet
        404 on Screener — a delisted NSE symbol, for instance — and a BSE-only
        listing is reachable only by its numeric BSE code. The caller tries them
        in order until a page loads.
        """
        out, seen = [], set()

        def add(code, how):
            code = (code or "").strip().upper()
            if code and code not in seen:
                seen.add(code)
                out.append((code, how))

        isin = (isin or "").strip().upper()
        # 1. ISIN via NSE's own master — the only unambiguous mapping
        if isin:
            add(self.by_isin.get(isin), "isin")
        # 2. Codes the statement stated outright
        add(nse_symbol, "statement nse_symbol")
        add(bse_code, "statement bse_code")
        # 3. ISIN via BSE's master — reaches BSE-only listings
        if isin:
            add(self.bse_by_isin.get(isin), "bse isin")
        # 4. Name against the NSE master, then BSE's
        key = normalise_name(name or "")
        ambiguous = False
        if key:
            if key in self.symbols:
                add(key, "symbol")
            mapped = self.by_name.get(key)
            if mapped:
                add(mapped, "name")
            elif key in self.by_name:
                ambiguous = True
            bse_mapped = self.bse_by_name.get(key)
            if bse_mapped:
                add(bse_mapped, "bse name")

        if out:
            return out, None
        if ambiguous:
            return [], "ambiguous name (two NSE listings share it)"
        if isin:
            return [], ("ISIN not in the NSE equity list and the statement carried no "
                        "NSE symbol or BSE code (unlisted, fund, bond, or BSE-only)")
        return [], "no ISIN, no exchange code on the statement, and name not in the NSE list"

    def listing(self, isin: str = "", name: str = "",
                nse_symbol: str = "", bse_code: str = "") -> Optional[str]:
        """Which exchange lists this security: "NSE", "BSE", "NSE+BSE" or None.

        Answers only the listing question — it says nothing about whether a row
        belongs in a portfolio. Mutual funds, bonds and cash are real holdings
        that appear on neither equity master.
        """
        isin = (isin or "").strip().upper()
        key = normalise_name(name or "")
        on_nse = bool(
            (isin and self.by_isin.get(isin))
            or (nse_symbol and nse_symbol.strip().upper() in self.symbols)
            or (key and (key in self.symbols or self.by_name.get(key)))
        )
        on_bse = bool(
            (isin and self.bse_by_isin.get(isin))
            or (bse_code and bse_code.strip() in self.bse_codes)
            or (key and self.bse_by_name.get(key))
        )
        if on_nse and on_bse:
            return "NSE+BSE"
        if on_nse:
            return "NSE"
        if on_bse:
            return "BSE"
        return None

    def resolve(self, isin: str = "", name: str = "",
                nse_symbol: str = "", bse_code: str = "") -> tuple:
        """First candidate only — kept for callers that want a single code."""
        found, reason = self.candidates(isin, name, nse_symbol, bse_code)
        return (found[0][0], found[0][1]) if found else (None, reason)


# ── Page fetching and parsing ────────────────────────────────────────────────

@dataclass
class ScrapeResult:
    code: str
    ok: bool = False
    cmp: Optional[float] = None
    name: Optional[str] = None
    ratios: dict = field(default_factory=dict)
    financials: dict = field(default_factory=dict)
    company_id: Optional[str] = None
    schedule_targets: list = field(default_factory=list)
    basis: Optional[str] = None      # "consolidated" or "standalone"
    error: Optional[str] = None
    status: Optional[int] = None


# The top "ratios" block of a company page is a list of
#   <li class="flex flex-space-between"><span class="name">X</span>
#   <span class="nowrap value"> ₹ <span class="number">1,255</span></span></li>
_RATIO_LI = re.compile(
    r'<li[^>]*class="[^"]*flex-space-between[^"]*"[^>]*>(.*?)</li>',
    re.S | re.I,
)
_NAME_SPAN = re.compile(r'<span[^>]*class="[^"]*\bname\b[^"]*"[^>]*>(.*?)</span>', re.S | re.I)
_VALUE_SPAN = re.compile(r'<span[^>]*class="[^"]*\bvalue\b[^"]*"[^>]*>(.*?)</span>\s*</li>?', re.S | re.I)
_NUMBER_SPAN = re.compile(r'<span[^>]*class="[^"]*\bnumber\b[^"]*"[^>]*>(.*?)</span>', re.S | re.I)
_TITLE = re.compile(r'<h1[^>]*>(.*?)</h1>', re.S | re.I)


def _text(html: str) -> str:
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', html)).strip()


# Financial statement sections, each rendered as a <section id=...> with one
# <table class="data-table"> of period columns.
FINANCIAL_SECTIONS = {
    "profit_loss": "profit-loss",
    "balance_sheet": "balance-sheet",
    "cash_flow": "cash-flow",
    "ratios": "ratios",
    "shareholding": "shareholding",
}

_SECTION_RE = r'<section[^>]*id="{sid}"[^>]*>(.*?)</section>'
_TABLE_RE = re.compile(r'<table[^>]*class="[^"]*data-table[^"]*"[^>]*>(.*?)</table>', re.S | re.I)
_THEAD_RE = re.compile(r'<thead>(.*?)</thead>', re.S | re.I)
_TBODY_RE = re.compile(r'<tbody>(.*?)</tbody>', re.S | re.I)
_TR_RE = re.compile(r'<tr[^>]*>(.*?)</tr>', re.S | re.I)
_CELL_RE = re.compile(r'<t[dh][^>]*>(.*?)</t[dh]>', re.S | re.I)
_RANGES_RE = re.compile(r'<table class="ranges-table">(.*?)</table>', re.S | re.I)


def _label(raw: str) -> str:
    """Clean a statement row label: 'Sales&nbsp; +' -> 'Sales'."""
    text = _text(raw.replace("&nbsp;", " "))
    return re.sub(r'\s*\+\s*$', '', text).strip()


def parse_financial_section(html: str, section_id: str) -> dict:
    """Parse one statement section into {"periods": [...], "rows": {label: [...]}}."""
    section = re.search(_SECTION_RE.format(sid=re.escape(section_id)), html, re.S | re.I)
    if not section:
        return {}
    table = _TABLE_RE.search(section.group(1))
    if not table:
        return {}

    head = _THEAD_RE.search(table.group(1))
    periods = []
    if head:
        cells = [_label(c) for c in _CELL_RE.findall(head.group(1))]
        periods = [c for c in cells[1:]]  # first header cell is the row-label column

    body = _TBODY_RE.search(table.group(1))
    rows = {}
    if body:
        for tr in _TR_RE.findall(body.group(1)):
            cells = _CELL_RE.findall(tr)
            if len(cells) < 2:
                continue
            label = _label(cells[0])
            if not label:
                continue
            rows[label] = [_text(c.replace("&nbsp;", " ")) for c in cells[1:]]
    return {"periods": periods, "rows": rows}


def parse_growth_tables(html: str) -> dict:
    """Parse the four 'Compounded ... Growth' / CAGR boxes.

    Returns {"Compounded Sales Growth": {"10 Years": "6%", ...}, ...}
    """
    out = {}
    for table in _RANGES_RE.finditer(html):
        trs = _TR_RE.findall(table.group(1))
        if not trs:
            continue
        title = _text(trs[0])
        entries = {}
        for tr in trs[1:]:
            cells = [_text(c) for c in _CELL_RE.findall(tr)]
            if len(cells) >= 2:
                entries[cells[0].rstrip(':').strip()] = cells[1]
        if title:
            out[title] = entries
    return out


def parse_financials(html: str) -> dict:
    """Extract every financial statement plus the CAGR boxes from a page."""
    data = {"growth": parse_growth_tables(html)}
    for key, section_id in FINANCIAL_SECTIONS.items():
        data[key] = parse_financial_section(html, section_id)
    return data


# Screener's condensed balance sheet collapses detail behind expandable rows,
# rendered as onclick="Company.showSchedule('Other Assets', 'balance-sheet', this)".
# Those schedules hold the line items (Inventories, Trade receivables, Trade
# Payables ...) that a current ratio needs, and they are served publicly.
_SCHEDULE_CALL = re.compile(
    r"Company\.showSchedule\(\s*'([^']+)'\s*,\s*'([^']+)'", re.I)

_COMPANY_ID_PATTERNS = (
    r'/company/id/(\d+)/',
    r'/api/company/(\d+)/add/',
    r'data-url="/notebook/(\d+)/"',
    r'/insights/company/(\d+)/',
    r'/results/rpt/(\d+)/',
    r'/wiki/company/(\d+)/',
)


def extract_company_id(html: str) -> Optional[str]:
    """Screener's internal numeric company id, needed for the schedule endpoint."""
    for pattern in _COMPANY_ID_PATTERNS:
        m = re.search(pattern, html)
        if m:
            return m.group(1)
    return None


# Schedules the checklist actually reads. Fetching the others (Fixed Assets,
# Sales, Expenses) would cost a request each for data nothing consumes.
NEEDED_SCHEDULES = ("Other Assets", "Other Liabilities", "Borrowings", "Borrowing")


def parse_schedule_targets(html: str, sections=("balance-sheet",), only=NEEDED_SCHEDULES) -> list:
    """The (parent, section) schedules this page itself offers to expand.

    Reading them off the page rather than hard-coding names keeps it working for
    lenders, whose rows are labelled differently ("Borrowing", "Deposits").
    """
    targets, seen = [], set()
    for m in _SCHEDULE_CALL.finditer(html):
        parent, section = m.group(1).strip(), m.group(2).strip()
        if sections and section not in sections:
            continue
        if only and parent not in only:
            continue
        key = (parent, section)
        if key not in seen:
            seen.add(key)
            targets.append(key)
    return targets


def _is_period(key: str) -> bool:
    """Schedule payloads carry stray control keys such as 'setAttributes'."""
    return bool(re.match(r'^[A-Za-z]{3}\s+\d{4}$', str(key).strip()))


def parse_company_page(html: str) -> dict:
    """Extract the top-ratio block from a company page as {label: raw_text}."""
    ratios = {}
    for block in _RATIO_LI.finditer(html):
        inner = block.group(1)
        name_m = _NAME_SPAN.search(inner)
        if not name_m:
            continue
        label = _text(name_m.group(1)).rstrip(':').strip()
        if not label:
            continue
        # Everything after the name span is the value
        rest = inner[name_m.end():]
        numbers = [_text(n.group(1)) for n in _NUMBER_SPAN.finditer(rest)]
        ratios[label] = {"text": _text(rest), "numbers": numbers}
    return ratios


class ScreenerClient:
    """Polite fetcher for Screener company pages.

    Requests may run on several worker threads, but a single shared rate limiter
    spaces every request start by ``delay`` seconds regardless of which thread
    issues it. Concurrency therefore hides network latency without raising the
    request rate Screener sees: N workers at a 0.5s spacing is still 2 req/s.
    """

    def __init__(self, delay: float = DEFAULT_DELAY, timeout: int = DEFAULT_TIMEOUT,
                 retries: int = DEFAULT_RETRIES, check_robots: bool = True):
        self.delay = delay
        self.timeout = timeout
        self.retries = retries
        self._lock = threading.Lock()
        self._next_slot = 0.0
        # requests.Session is not safe to share across threads; give each one its own
        self._local = threading.local()
        self.robots = None
        if check_robots:
            self._load_robots()

    @property
    def session(self) -> requests.Session:
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            s.headers.update({
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-IN,en;q=0.9",
            })
            self._local.session = s
        return s

    def _load_robots(self) -> None:
        try:
            resp = self.session.get(ROBOTS_URL, timeout=self.timeout)
            resp.raise_for_status()
            parser = urllib.robotparser.RobotFileParser()
            parser.parse(resp.text.splitlines())
            self.robots = parser
            log.info("Loaded robots.txt from %s", ROBOTS_URL)
        except Exception as exc:
            # Without robots.txt we cannot prove a path is allowed, so refuse.
            raise RuntimeError(
                f"Could not read {ROBOTS_URL} ({exc}). Refusing to crawl without it."
            ) from exc

    def allowed(self, url: str) -> bool:
        if self.robots is None:
            return True
        return self.robots.can_fetch(USER_AGENT, url) and self.robots.can_fetch("*", url)

    def _wait(self) -> None:
        """Reserve the next request slot, then sleep until it arrives.

        Reserving under the lock and sleeping outside it lets several workers
        queue up while keeping the global request rate at one per ``delay``.
        """
        with self._lock:
            now = time.monotonic()
            wait = max(0.0, self._next_slot - now)
            self._next_slot = max(now, self._next_slot) + self.delay
        if wait > 0:
            time.sleep(wait)

    def _penalise(self, seconds: float) -> None:
        """React to a 429/503 by slowing every worker, not just this request.

        Screener rate-limits the whole client, so retrying one URL while the
        others keep hammering at the old pace just spreads the failure. The
        spacing is widened for the rest of the run and the shared queue is
        pushed out so in-flight workers wait too.
        """
        with self._lock:
            self.delay = min(self.delay * 1.6, MAX_BACKOFF_DELAY)
            self._next_slot = max(self._next_slot, time.monotonic() + seconds)
        log.warning("throttled by Screener; request spacing widened to %.2fs", self.delay)

    def fetch_company(self, code: str, prefer_consolidated: bool = True) -> ScrapeResult:
        """Fetch and parse one company page.

        Tries the consolidated statements first and falls back to the default
        (standalone) page when a company has none. Never raises for a per-stock
        failure — the caller inspects ``result.ok``.
        """
        code = str(code).strip().upper()
        if prefer_consolidated:
            consolidated = self._fetch_one(code, CONSOLIDATED_URL.format(code=code),
                                           "consolidated")
            if consolidated.ok:
                return consolidated
        return self._fetch_one(code, COMPANY_URL.format(code=code), "standalone")

    def _fetch_one(self, code: str, url: str, basis: str) -> ScrapeResult:
        result = ScrapeResult(code=code)
        result.basis = basis

        if not self.allowed(url):
            result.error = "blocked by robots.txt"
            return result

        for attempt in range(1, self.retries + 1):
            self._wait()
            try:
                resp = self.session.get(url, timeout=self.timeout)
                result.status = resp.status_code

                if resp.status_code == 404:
                    result.error = "not found on Screener"
                    return result  # retrying a 404 is pointless

                if resp.status_code in (429, 503):
                    # Slow the whole client, not just this request
                    wait = float(resp.headers.get("Retry-After") or (self.delay * 2 * attempt))
                    self._penalise(wait)
                    result.error = f"throttled (HTTP {resp.status_code})"
                    if attempt < self.retries:
                        continue
                    return result

                if resp.status_code != 200:
                    result.error = f"HTTP {resp.status_code}"
                    if attempt < self.retries:
                        time.sleep(self.delay * attempt)
                        continue
                    return result

                result.company_id = extract_company_id(resp.text)
                result.schedule_targets = parse_schedule_targets(resp.text)
                ratios = parse_company_page(resp.text)
                price = ratios.get("Current Price")
                if not price or not price["numbers"]:
                    # Markup changed, or this page has no price block. Report it
                    # rather than inventing a value.
                    result.error = "could not find 'Current Price' on the page"
                    return result

                result.cmp = clean_number(price["numbers"][0])
                if result.cmp is None:
                    result.error = f"unparseable price {price['numbers'][0]!r}"
                    return result

                title = _TITLE.search(resp.text)
                result.name = _text(title.group(1)) if title else None
                result.ratios = ratios
                # Statements come from the same response, so capturing them costs
                # no extra request.
                try:
                    result.financials = parse_financials(resp.text)
                except Exception as exc:          # never fail a price over this
                    log.warning("%s: could not parse financials (%s)", code, exc)
                    result.financials = {}
                # A consolidated URL can answer 200 with an empty statement block
                # for a company that files standalone only; treat that as a miss
                # so the caller falls back rather than storing an empty page.
                if not (result.financials.get("profit_loss") or {}).get("rows"):
                    result.ok = False
                    result.error = f"no {basis} statements on the page"
                    return result
                result.ok = True
                return result

            except requests.RequestException as exc:
                result.error = f"{type(exc).__name__}: {exc}"
                if attempt < self.retries:
                    time.sleep(self.delay * attempt)
                    continue

        return result


    def fetch_schedules(self, company_id: str, targets, referer: str = "") -> dict:
        """Fetch the expandable balance-sheet schedules for one company.

        Returns {parent: {line_item: {period: value}}}. A schedule that fails is
        omitted rather than raising — a missing schedule degrades one checklist
        row, it should never lose the price we already have.
        """
        import json as _json
        import urllib.parse

        out = {}
        if not company_id:
            return out
        for parent, section in targets:
            query = urllib.parse.urlencode({"parent": parent, "section": section})
            url = f"{BASE}/api/company/{company_id}/schedules/?{query}"
            if not self.allowed(url):
                log.info("schedule %s blocked by robots.txt", parent)
                continue
            self._wait()
            try:
                resp = self.session.get(
                    url,
                    headers={"X-Requested-With": "XMLHttpRequest",
                             "Referer": referer or f"{BASE}/company/{company_id}/",
                             "Accept": "*/*"},
                    timeout=self.timeout,
                )
                if resp.status_code != 200:
                    log.info("schedule %s -> HTTP %s", parent, resp.status_code)
                    continue
                payload = _json.loads(resp.text)
                if not isinstance(payload, dict):
                    continue
                cleaned = {}
                for line, series in payload.items():
                    if not isinstance(series, dict):
                        continue
                    periods = {k: v for k, v in series.items() if _is_period(k)}
                    if periods:
                        cleaned[line] = periods
                if cleaned:
                    out[parent] = cleaned
            except Exception as exc:
                log.info("schedule %s failed: %s", parent, exc)
                continue
        return out


def ratio_value(ratios: dict, label: str) -> Optional[float]:
    """First numeric value of a named ratio, or None."""
    entry = ratios.get(label)
    if not entry or not entry.get("numbers"):
        return None
    return clean_number(entry["numbers"][0])
