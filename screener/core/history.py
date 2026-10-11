"""Daily price history (OHLCV) from Yahoo Finance's chart endpoint."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional

import requests

from .quote import IST, TIMEOUT, YAHOO_HEADERS, chart_url

# (date, open, high, low, close, adj_close, volume)
Row = tuple


class HistoryError(Exception):
    """transient=True means retrying later may work (network, rate limit)."""

    def __init__(self, message: str, transient: bool = False):
        super().__init__(message)
        self.transient = transient


class RateLimited(HistoryError):
    """The data source answered HTTP 429: it wants us to slow down."""

    def __init__(self, message: str):
        super().__init__(message, transient=True)


@dataclass
class HistoryResult:
    ticker: str
    rows: list[Row] = field(default_factory=list)
    splits: list[str] = field(default_factory=list)  # ISO dates of stock splits/bonuses


def _r(v) -> Optional[float]:
    return round(float(v), 4) if isinstance(v, (int, float)) else None


def parse_history(payload: dict, ticker: str) -> HistoryResult:
    chart = payload.get("chart") or {}
    if chart.get("error"):
        err = chart["error"]
        raise HistoryError(f'{ticker}: {err.get("description") or err.get("code")}')
    results = chart.get("result") or []
    if not results:
        raise HistoryError(f"{ticker}: no data returned")

    res = results[0]
    meta = res.get("meta") or {}
    offset = meta.get("gmtoffset")
    offset = offset if isinstance(offset, (int, float)) else 19800  # IST

    stamps = res.get("timestamp") or []
    try:
        q = res["indicators"]["quote"][0]
    except (KeyError, IndexError, TypeError):
        q = {}
    try:
        adj = res["indicators"]["adjclose"][0]["adjclose"]
    except (KeyError, IndexError, TypeError):
        adj = []

    def at(series, i):
        return series[i] if series and i < len(series) else None

    rows: list[Row] = []
    for i, ts in enumerate(stamps):
        close = _r(at(q.get("close"), i))
        if close is None:  # holiday / missing candle
            continue
        day = datetime.fromtimestamp(ts + offset, timezone.utc).date().isoformat()
        vol = at(q.get("volume"), i)
        rows.append(
            (
                day,
                _r(at(q.get("open"), i)),
                _r(at(q.get("high"), i)),
                _r(at(q.get("low"), i)),
                close,
                _r(at(adj, i)) or close,
                int(vol) if isinstance(vol, (int, float)) else None,
            )
        )

    # A symbol sometimes repeats the latest day; keep the last candle per date.
    rows = list({r[0]: r for r in rows}.values())
    rows.sort(key=lambda r: r[0])

    splits = []
    for ev in ((res.get("events") or {}).get("splits") or {}).values():
        ts = ev.get("date")
        if isinstance(ts, (int, float)):
            splits.append(datetime.fromtimestamp(ts + offset, timezone.utc).date().isoformat())

    return HistoryResult(ticker=ticker, rows=rows, splits=sorted(splits))


def fetch_history(
    ticker: str,
    start: Optional[date] = None,
    years: int = 2,
    session: Optional[requests.Session] = None,
    retries: int = 3,
    sleep: Callable[[float], None] = time.sleep,
) -> HistoryResult:
    """Download daily candles. start=None fetches `years` of history."""
    params = {"interval": "1d", "events": "div,splits"}
    if start is None:
        params["range"] = f"{years}y"
    else:
        p1 = datetime(start.year, start.month, start.day, tzinfo=IST)
        params["period1"] = str(int(p1.timestamp()))
        params["period2"] = str(int(time.time()) + 86400)

    http = session or requests
    url = chart_url(ticker)
    alt_tried = False
    last: Optional[HistoryError] = None
    attempt = 0
    while attempt < retries:
        if attempt:
            sleep(2 ** attempt)  # 2s, 4s
        try:
            resp = http.get(url, params=params, headers=YAHOO_HEADERS, timeout=TIMEOUT)
        except requests.RequestException as exc:
            last = HistoryError(f"{ticker}: network error ({exc.__class__.__name__})", transient=True)
            attempt += 1
            continue

        code = resp.status_code
        if code == 404:
            raise HistoryError(f"{ticker}: symbol not found")
        if code == 429:
            # Yahoo has two equivalent hosts with separate limits; try the other once.
            if not alt_tried and "//query1." in url:
                url, alt_tried = url.replace("//query1.", "//query2."), True
                continue
            raise RateLimited(f"{ticker}: rate limited by the data source (HTTP 429)")
        if code >= 500:
            last = HistoryError(f"{ticker}: data source error (HTTP {code})", transient=True)
            attempt += 1
            continue
        if code >= 400:
            raise HistoryError(f"{ticker}: data source refused (HTTP {code})")
        try:
            return parse_history(resp.json(), ticker)
        except ValueError:
            last = HistoryError(f"{ticker}: invalid response", transient=True)
            attempt += 1
    raise last or HistoryError(f"{ticker}: failed")
