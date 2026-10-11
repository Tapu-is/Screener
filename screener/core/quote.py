"""Latest-price lookup for NSE/BSE symbols.

Data source: Yahoo Finance's public chart endpoint, called directly with
`requests`. That keeps Termux installs light (no pandas/yfinance needed for
this feature). Prices are typically delayed by a few minutes.

Symbol rules
------------
RELIANCE        -> tries RELIANCE.NS (NSE) first, then RELIANCE.BO (BSE)
RELIANCE.BO     -> used exactly as given
500325          -> all-digit BSE scrip code, becomes 500325.BO
^NSEI, ^BSESN   -> indices, used as given
"""

from __future__ import annotations

import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from urllib.parse import quote as urlquote

import requests

from . import cache

IST = timezone(timedelta(hours=5, minutes=30))
DEFAULT_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
# Yahoo refuses browser-style User-Agents sent by non-browser clients (HTTP 429)
# but accepts this plain one. Found with `stk probe`.
YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0"}
# Browser-style headers, used for the NSE / niftyindices symbol list download.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36"
    )
}
CACHE_TTL = 45  # seconds a fresh lookup is reused
TIMEOUT = 10


class QuoteError(Exception):
    """Raised when a symbol cannot be resolved or fetched."""


@dataclass
class Quote:
    symbol: str
    name: str
    exchange: str
    currency: str
    price: float
    prev_close: Optional[float]
    change: Optional[float]
    change_pct: Optional[float]
    day_high: Optional[float]
    day_low: Optional[float]
    volume: Optional[int]
    week52_high: Optional[float]
    week52_low: Optional[float]
    as_of: str  # ISO timestamp (IST) of the price itself
    source: str = "live"  # live | cache | stale
    age_seconds: int = 0  # how old the data is when shown from cache

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- market hours
def is_market_open(now: Optional[datetime] = None) -> bool:
    """True during NSE/BSE regular hours (Mon-Fri, 09:15-15:30 IST).

    Exchange holidays are not modelled yet, so on a holiday this can say
    "open" while the price simply isn't moving.
    """
    now = (now or datetime.now(IST)).astimezone(IST)
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return 9 * 60 + 15 <= minutes <= 15 * 60 + 30


# ---------------------------------------------------------- symbol resolution
def candidates(symbol: str, prefer_bse: bool = False) -> list[str]:
    """Yahoo tickers to try, in order, for what the user typed."""
    s = symbol.strip().upper()
    if not s:
        raise QuoteError("empty symbol")
    if s.startswith("^") or s.endswith((".NS", ".BO")):
        return [s]
    if s.isdigit():  # BSE scrip code
        return [f"{s}.BO"]
    order = [f"{s}.BO", f"{s}.NS"] if prefer_bse else [f"{s}.NS", f"{s}.BO"]
    return order


# ------------------------------------------------------------------- parsing
def _num(value) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) else None


def parse_chart(payload: dict, symbol: str) -> Quote:
    """Turn a Yahoo chart response into a Quote. Raises QuoteError if unusable."""
    chart = payload.get("chart") or {}
    if chart.get("error"):
        desc = chart["error"].get("description") or chart["error"].get("code")
        raise QuoteError(f"{symbol}: {desc}")
    results = chart.get("result") or []
    if not results:
        raise QuoteError(f"{symbol}: no data returned")

    result = results[0]
    meta = result.get("meta") or {}
    price = _num(meta.get("regularMarketPrice"))
    if price is None:
        raise QuoteError(f"{symbol}: no price available")

    # Previous close = the candle before the latest one. (meta.chartPreviousClose
    # is the close *before the requested range*, so it is wrong for range=5d.)
    closes = []
    try:
        closes = [
            c for c in result["indicators"]["quote"][0].get("close", []) if c is not None
        ]
    except (KeyError, IndexError, TypeError):
        pass
    prev_close = closes[-2] if len(closes) >= 2 else _num(meta.get("chartPreviousClose"))

    change = change_pct = None
    if prev_close:
        change = price - prev_close
        change_pct = change / prev_close * 100

    ts = meta.get("regularMarketTime")
    as_of = (
        datetime.fromtimestamp(ts, IST).isoformat(timespec="seconds")
        if isinstance(ts, (int, float))
        else datetime.now(IST).isoformat(timespec="seconds")
    )

    volume = meta.get("regularMarketVolume")
    return Quote(
        symbol=meta.get("symbol", symbol),
        name=meta.get("longName") or meta.get("shortName") or symbol,
        exchange=meta.get("fullExchangeName") or meta.get("exchangeName") or "",
        currency=meta.get("currency") or "INR",
        price=price,
        prev_close=prev_close,
        change=change,
        change_pct=change_pct,
        day_high=_num(meta.get("regularMarketDayHigh")),
        day_low=_num(meta.get("regularMarketDayLow")),
        volume=int(volume) if isinstance(volume, (int, float)) else None,
        week52_high=_num(meta.get("fiftyTwoWeekHigh")),
        week52_low=_num(meta.get("fiftyTwoWeekLow")),
        as_of=as_of,
    )


# ------------------------------------------------------------------- fetching
def chart_url(ticker: str) -> str:
    """Chart endpoint for a ticker. STK_CHART_URL overrides the host (used by tests)."""
    template = os.environ.get("STK_CHART_URL", DEFAULT_CHART_URL)
    return template.format(symbol=urlquote(ticker, safe="^"))


def _fetch(ticker: str) -> Quote:
    try:
        resp = requests.get(
            chart_url(ticker),
            params={"interval": "1d", "range": "5d"},
            headers=YAHOO_HEADERS,
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        raise ConnectionError(f"network error for {ticker}: {exc.__class__.__name__}") from exc

    if resp.status_code == 404:
        raise QuoteError(f"{ticker}: symbol not found")
    if resp.status_code == 429:
        raise ConnectionError("rate limited by data source, try again in a minute")
    if resp.status_code >= 400:
        raise ConnectionError(f"data source returned HTTP {resp.status_code}")
    try:
        return parse_chart(resp.json(), ticker)
    except ValueError as exc:
        raise ConnectionError("data source returned invalid data") from exc


def get_quote(symbol: str, prefer_bse: bool = False, use_cache: bool = True) -> Quote:
    """Latest quote for a symbol.

    Order of attempts: fresh cache (<45s) -> live fetch (NSE then BSE) ->
    stale cache labelled as such. Raises QuoteError if nothing works.
    """
    tickers = candidates(symbol, prefer_bse)

    if use_cache:
        for t in tickers:
            hit = cache.get(t, max_age=CACHE_TTL)
            if hit:
                q = Quote(**hit)
                q.source = "cache"
                return q

    last_error: Optional[Exception] = None
    network_down = False
    for t in tickers:
        try:
            q = _fetch(t)
        except QuoteError as exc:  # symbol not on this exchange, try the next
            last_error = exc
            continue
        except ConnectionError as exc:  # network trouble, stop trying new tickers
            last_error = exc
            network_down = True
            break
        cache.put(t, q.to_dict())
        return q

    # Live fetch failed: fall back to whatever we saved last time.
    if network_down:
        for t in tickers:
            entry = None
            try:
                entry = cache.get(t, max_age=None)
            except Exception:
                pass
            if entry:
                q = Quote(**entry)
                q.source = "stale"
                try:
                    saved = datetime.fromisoformat(q.as_of)
                    q.age_seconds = int((datetime.now(IST) - saved).total_seconds())
                except ValueError:
                    pass
                return q

    raise QuoteError(str(last_error) if last_error else f"{symbol}: not found")


def get_quotes(symbols: list[str], prefer_bse: bool = False) -> list:
    """Quotes for several symbols. Failures come back as QuoteError objects
    in place of a Quote, so one bad ticker never hides the rest."""
    out = []
    for i, sym in enumerate(symbols):
        try:
            out.append(get_quote(sym, prefer_bse))
        except QuoteError as exc:
            out.append(exc)
        if i < len(symbols) - 1:
            time.sleep(0.15)  # be polite to the data source
    return out
