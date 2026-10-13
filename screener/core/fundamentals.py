"""Company fundamentals (P/E, ROE, margins, growth ...) from Yahoo's quoteSummary.

Yahoo asks for a session cookie and a "crumb" token before it answers, so a
Client fetches both once and reuses them. Everything is stored in the
`fundamentals` table with the date it was fetched; a value Yahoo does not
have stays NULL and screens simply skip those stocks.

Overrides for tests: STK_FUND_URL (API host), STK_FUND_COOKIE_URL (cookie host).
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Iterable, Optional

import requests

from .history import HistoryError, NotFound, RateLimited
from .indicators import FUND_COLUMNS
from .quote import IST, YAHOO_HEADERS

MODULES = "summaryDetail,defaultKeyStatistics,financialData,price"
MAX_AGE_DAYS = 7


def _base() -> str:
    return os.environ.get("STK_FUND_URL", "https://query1.finance.yahoo.com")


def _cookie_url() -> Optional[str]:
    if "STK_FUND_COOKIE_URL" in os.environ:
        return os.environ["STK_FUND_COOKIE_URL"] or None
    if "STK_FUND_URL" in os.environ:
        return None  # a test server: no cookie step
    return "https://fc.yahoo.com"


# --------------------------------------------------------------------- parsing
def _raw(v):
    """Yahoo wraps numbers as {"raw": 1.2, "fmt": "1.2"}; empty objects mean no data."""
    if isinstance(v, dict):
        v = v.get("raw")
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return float(v)


def _first(*vals):
    for v in vals:
        r = _raw(v)
        if r is not None:
            return r
    return None


def _scale(v, k):
    return None if v is None else v * k


def _round(v, digits=2):
    return None if v is None else round(v, digits)


def parse(payload: dict) -> dict:
    """quoteSummary JSON -> {column: value}. Raises NotFound if Yahoo has no such symbol."""
    qs = (payload or {}).get("quoteSummary") or {}
    err = qs.get("error")
    if err:
        if "not found" in str(err.get("code", "")).lower() or "no fundamentals" in str(err.get("description", "")).lower():
            raise NotFound("no fundamentals for this symbol")
        raise HistoryError(f"data source error: {err.get('description') or err.get('code')}", transient=True)
    result = (qs.get("result") or [None])[0]
    if not result:
        raise NotFound("no fundamentals for this symbol")
    sd = result.get("summaryDetail") or {}
    ks = result.get("defaultKeyStatistics") or {}
    fd = result.get("financialData") or {}
    pr = result.get("price") or {}

    out = {
        "market_cap_cr": _scale(_first(pr.get("marketCap"), sd.get("marketCap")), 1e-7),
        "pe": _first(sd.get("trailingPE"), ks.get("trailingPE")),
        "pb": _first(ks.get("priceToBook")),
        "eps": _first(ks.get("trailingEps")),
        "div_yield": _scale(_first(sd.get("dividendYield"), sd.get("trailingAnnualDividendYield")), 100),
        "roe": _scale(_first(fd.get("returnOnEquity")), 100),
        "roa": _scale(_first(fd.get("returnOnAssets")), 100),
        "profit_margin": _scale(_first(fd.get("profitMargins")), 100),
        "op_margin": _scale(_first(fd.get("operatingMargins")), 100),
        "debt_equity": _scale(_first(fd.get("debtToEquity")), 0.01),  # Yahoo gives it as a percent
        "rev_growth": _scale(_first(fd.get("revenueGrowth")), 100),
        "earn_growth": _scale(_first(fd.get("earningsGrowth")), 100),
    }
    return {k: _round(v) for k, v in out.items()}


# ---------------------------------------------------------------------- client
class Client:
    """Holds the cookie + crumb Yahoo wants and fetches one symbol at a time."""

    def __init__(self, session: Optional[requests.Session] = None):
        self.session = session or requests.Session()
        self.session.headers.update(YAHOO_HEADERS)
        self.crumb: Optional[str] = None

    def _get(self, url: str, **kw):
        try:
            return self.session.get(url, timeout=15, **kw)
        except requests.RequestException as exc:
            raise HistoryError(f"cannot reach the data source ({exc.__class__.__name__})", transient=True) from None

    def login(self) -> str:
        cookie = _cookie_url()
        if cookie:
            self._get(cookie)  # sets the cookie; the page itself may answer 404, that is fine
        r = self._get(f"{_base()}/v1/test/getcrumb")
        if r.status_code == 429:
            raise RateLimited("rate limited while getting a session (HTTP 429)")
        text = (r.text or "").strip()
        if r.status_code != 200 or not text or "<" in text or " " in text:
            raise HistoryError(f"could not get a session crumb (HTTP {r.status_code})", transient=True)
        self.crumb = text
        return text

    def fetch(self, yahoo: str, _retry: bool = True) -> dict:
        if not self.crumb:
            self.login()
        r = self._get(f"{_base()}/v10/finance/quoteSummary/{yahoo}", params={"modules": MODULES, "crumb": self.crumb})
        if r.status_code in (401, 403) and _retry:
            self.crumb = None
            return self.fetch(yahoo, _retry=False)
        if r.status_code == 404:
            raise NotFound(f"{yahoo}: no fundamentals")
        if r.status_code == 429:
            raise RateLimited(f"{yahoo}: rate limited by the data source (HTTP 429)")
        if r.status_code != 200:
            raise HistoryError(f"{yahoo}: data source answered HTTP {r.status_code}", transient=r.status_code >= 500)
        try:
            payload = r.json()
        except ValueError:
            raise HistoryError(f"{yahoo}: unreadable answer", transient=True) from None
        try:
            return parse(payload)
        except NotFound:
            raise NotFound(f"{yahoo}: no fundamentals") from None


def probe(client: Optional[Client] = None, yahoo: str = "RELIANCE.NS") -> list[tuple[str, bool, str]]:
    """Step-by-step check of the fundamentals source: [(label, ok, detail)]."""
    client = client or Client()
    steps: list[tuple[str, bool, str]] = []
    try:
        client.login()
        steps.append(("session (cookie + crumb)", True, "ok"))
    except HistoryError as exc:
        steps.append(("session (cookie + crumb)", False, str(exc)))
        return steps
    try:
        values = client.fetch(yahoo)
        have = sorted(k for k, v in values.items() if v is not None)
        steps.append((f"fundamentals for {yahoo}", bool(have), f"{len(have)} of {len(values)} fields: {', '.join(have)}"))
    except HistoryError as exc:
        steps.append((f"fundamentals for {yahoo}", False, str(exc)))
    return steps


# --------------------------------------------------------------------- storage
def _now() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")


def save(conn: sqlite3.Connection, symbol_id: int, values: dict, now: Optional[str] = None) -> None:
    cols = ["symbol_id", "fetched_at"] + FUND_COLUMNS
    row = [symbol_id, now or _now()] + [values.get(c) for c in FUND_COLUMNS]
    with conn:
        conn.execute(
            f"INSERT OR REPLACE INTO fundamentals ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", row
        )


def get(conn: sqlite3.Connection, symbol_id: int) -> Optional[dict]:
    r = conn.execute("SELECT * FROM fundamentals WHERE symbol_id = ?", (symbol_id,)).fetchone()
    return dict(r) if r else None


def coverage(conn: sqlite3.Connection) -> dict:
    one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    return {
        "stored": one("SELECT COUNT(*) FROM fundamentals"),
        "active": one("SELECT COUNT(*) FROM symbols WHERE active = 1"),
        "oldest": one("SELECT MIN(fetched_at) FROM fundamentals"),
        "newest": one("SELECT MAX(fetched_at) FROM fundamentals"),
        "with_pe": one("SELECT COUNT(*) FROM fundamentals WHERE pe IS NOT NULL"),
    }


@dataclass
class RefreshResult:
    ok: int = 0
    skipped: int = 0  # fetched recently, left alone
    empty: int = 0  # Yahoo has nothing for these
    failed: list = field(default_factory=list)  # (ticker, message)
    aborted: bool = False


def _wanted(conn, tickers: Optional[Iterable[str]]) -> list:
    if tickers is None:
        return conn.execute("SELECT id, ticker, yahoo FROM symbols WHERE active = 1 ORDER BY ticker").fetchall()
    names = [t.upper() for t in tickers]
    rows = []
    for t in names:
        r = conn.execute("SELECT id, ticker, yahoo FROM symbols WHERE ticker = ?", (t,)).fetchone()
        if r:
            rows.append(r)
    return rows


def refresh(
    conn: sqlite3.Connection,
    tickers: Optional[Iterable[str]] = None,
    client: Optional[Client] = None,
    force: bool = False,
    max_age_days: int = MAX_AGE_DAYS,
    delay: float = 0.8,
    cooldown: float = 10.0,
    sleep: Callable[[float], None] = time.sleep,
    progress: Optional[Callable[[int, int, str], None]] = None,
    now: Optional[datetime] = None,
) -> RefreshResult:
    """Download fundamentals for `tickers` (None = every active symbol).
    Stocks fetched within `max_age_days` are skipped unless `force`. Three transient
    failures in a row stop the run (the source is refusing us)."""
    client = client or Client()
    now = (now or datetime.now(IST)).replace(tzinfo=None)
    cutoff = (now - timedelta(days=max_age_days)).strftime("%Y-%m-%d %H:%M:%S")
    rows = _wanted(conn, tickers)
    res = RefreshResult()
    strikes = 0
    for i, r in enumerate(rows, 1):
        if progress:
            progress(i, len(rows), r["ticker"])
        if not force:
            have = conn.execute("SELECT fetched_at FROM fundamentals WHERE symbol_id = ?", (r["id"],)).fetchone()
            if have and have[0] >= cutoff:
                res.skipped += 1
                continue
        try:
            try:
                values = client.fetch(r["yahoo"])
            except RateLimited:
                sleep(cooldown)
                values = client.fetch(r["yahoo"])  # one more try after cooling down
            strikes = 0
        except NotFound:
            save(conn, r["id"], {}, now.strftime("%Y-%m-%d %H:%M:%S"))  # remembered, so not re-asked all week
            res.empty += 1
            strikes = 0
            continue
        except HistoryError as exc:
            res.failed.append((r["ticker"], str(exc)))
            strikes += 1
            if strikes >= 3:
                res.aborted = True
                break
            continue
        save(conn, r["id"], values, now.strftime("%Y-%m-%d %H:%M:%S"))
        res.ok += 1
        if i < len(rows):
            sleep(delay)
    return res
