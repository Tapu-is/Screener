"""Technical indicators computed from stored daily prices.

Pure Python (no numpy/pandas) so it installs everywhere, Termux included.

What is calculated, per stock, from the latest ~420 stored sessions:

* RSI(14) using Wilder's smoothing
* SMA 20 / 50 / 200 and the 50/200 "golden" and "death" crosses
* MACD (12, 26, 9) and its crossovers with the signal line
* 52-week high / low (252 sessions) and the prior high (for breakouts)
* volume versus the previous 20-session average, and traded value
* percentage change over 1 day, 1 week, 1 month, 6 months and 1 year

Prices are the split-adjusted closes stored by `stk update`. Results go to the
`indicators` table (one row per stock), so screens are a simple read.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable, Optional, Sequence

from .quote import IST

LOOKBACK = 420  # sessions loaded per stock: warm-up for the EMAs plus a trading year


@dataclass(frozen=True)
class Field:
    name: str
    kind: str  # price | pct | rsi | macd | ratio | days | cr | int
    short: str  # table header
    help: str


FIELDS = (
    Field("close", "price", "Close", "Latest closing price (₹)"),
    Field("volume", "int", "Volume", "Shares traded in the latest session"),
    Field("chg_1d", "pct", "1D", "% change over 1 session"),
    Field("chg_1w", "pct", "1W", "% change over 5 sessions (a week)"),
    Field("chg_1m", "pct", "1M", "% change over 21 sessions (a month)"),
    Field("chg_6m", "pct", "6M", "% change over 126 sessions"),
    Field("chg_1y", "pct", "1Y", "% change over 252 sessions (a year)"),
    Field("rsi14", "rsi", "RSI", "RSI(14). Below 30 is called oversold, above 70 overbought"),
    Field("sma20", "price", "SMA20", "20-session simple moving average"),
    Field("sma50", "price", "SMA50", "50-session simple moving average"),
    Field("sma200", "price", "SMA200", "200-session simple moving average"),
    Field("macd", "macd", "MACD", "MACD line (12-session EMA minus 26-session EMA)"),
    Field("macd_signal", "macd", "Signal", "MACD signal line (9-session EMA of MACD)"),
    Field("macd_hist", "macd", "Hist", "MACD minus its signal line; above 0 is bullish"),
    Field("macd_cross_up_days", "days", "MACD up", "Sessions since MACD crossed above its signal (empty if it last crossed down)"),
    Field("macd_cross_down_days", "days", "MACD dn", "Sessions since MACD crossed below its signal (empty if it last crossed up)"),
    Field("golden_cross_days", "days", "Golden", "Sessions since the 50-day average crossed above the 200-day (empty if it is below now)"),
    Field("death_cross_days", "days", "Death", "Sessions since the 50-day average crossed below the 200-day (empty if it is above now)"),
    Field("high52", "price", "52w H", "Highest high of the last 252 sessions"),
    Field("low52", "price", "52w L", "Lowest low of the last 252 sessions"),
    Field("prior_high52", "price", "Prior H", "Highest high of the 251 sessions before the latest (close above it = breakout)"),
    Field("from_high_pct", "pct", "vs High", "% below the 52-week high (0 = at the high)"),
    Field("from_low_pct", "pct", "vs Low", "% above the 52-week low"),
    Field("avg_vol20", "int", "AvgVol", "Average volume of the 20 sessions before the latest"),
    Field("vol_ratio", "ratio", "Vol x", "Latest volume divided by that average (2 = double the usual)"),
    Field("turnover20_cr", "cr", "Value", "Average daily traded value over 20 sessions, in ₹ crore (liquidity)"),
    Field("sessions", "int", "Days", "Number of stored sessions for the stock"),
)
FUND_FIELDS = (
    Field("market_cap_cr", "cr", "MCap", "Market value of the company in ₹ crore"),
    Field("pe", "num", "P/E", "Price to earnings (trailing). Lower can mean cheaper; negative means losses"),
    Field("pb", "num", "P/B", "Price to book value"),
    Field("eps", "num", "EPS", "Earnings per share (₹, trailing)"),
    Field("div_yield", "num", "Div%", "Dividend yield, percent"),
    Field("roe", "pct0", "ROE", "Return on equity, percent"),
    Field("roa", "pct0", "ROA", "Return on assets, percent"),
    Field("profit_margin", "pct0", "Margin", "Net profit margin, percent"),
    Field("op_margin", "pct0", "OpMrg", "Operating margin, percent"),
    Field("debt_equity", "num", "D/E", "Debt to equity ratio (0.5 = half as much debt as equity)"),
    Field("rev_growth", "pct", "RevGr", "Revenue growth, latest year on year, percent"),
    Field("earn_growth", "pct", "EarnGr", "Earnings growth, latest year on year, percent"),
)
FUND_COLUMNS = [f.name for f in FUND_FIELDS]
FIELD_BY_NAME = {f.name: f for f in FIELDS + FUND_FIELDS}
COLUMNS = ["as_of", "computed_at"] + [f.name for f in FIELDS]


# --------------------------------------------------------------------- series
def sma_series(values: Sequence[float], n: int) -> list:
    out: list = [None] * len(values)
    if n <= 0 or len(values) < n:
        return out
    total = sum(values[:n])
    out[n - 1] = total / n
    for i in range(n, len(values)):
        total += values[i] - values[i - n]
        out[i] = total / n
    return out


def ema_series(values: Sequence[Optional[float]], n: int) -> list:
    """Exponential average seeded with the simple average of the first n values.

    Leading None values are skipped, so it can be applied to a MACD series.
    """
    out: list = [None] * len(values)
    start = next((i for i, v in enumerate(values) if v is not None), None)
    if start is None or len(values) - start < n:
        return out
    k = 2.0 / (n + 1)
    seed = start + n - 1
    prev = sum(values[start : seed + 1]) / n
    out[seed] = prev
    for i in range(seed + 1, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def rsi_series(closes: Sequence[float], period: int = 14) -> list:
    """Wilder's RSI: first averages are simple means, then smoothed."""
    out: list = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        if d > 0:
            gains += d
        else:
            losses -= d
    avg_gain, avg_loss = gains / period, losses / period
    out[period] = _rsi_value(avg_gain, avg_loss)
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + (d if d > 0 else 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + (-d if d < 0 else 0.0)) / period
        out[i] = _rsi_value(avg_gain, avg_loss)
    return out


def last_cross(fast: Sequence, slow: Sequence) -> Optional[tuple]:
    """Most recent crossing of `fast` over/under `slow`: (index, "up"|"down")."""
    for i in range(len(fast) - 1, 0, -1):
        a0, b0, a1, b1 = fast[i - 1], slow[i - 1], fast[i], slow[i]
        if a0 is None or b0 is None or a1 is None or b1 is None:
            continue
        if a0 <= b0 and a1 > b1:
            return i, "up"
        if a0 >= b0 and a1 < b1:
            return i, "down"
    return None


# -------------------------------------------------------------------- compute
def _r(x: Optional[float], digits: int = 4) -> Optional[float]:
    return None if x is None else round(float(x), digits)


def _chg(closes: Sequence[float], k: int) -> Optional[float]:
    if len(closes) > k and closes[-1 - k]:
        return (closes[-1] / closes[-1 - k] - 1) * 100
    return None


def compute(rows: Sequence) -> Optional[dict]:
    """Indicators from price rows (date, high, low, close, volume), oldest first."""
    n = len(rows)
    if n < 2:
        return None
    closes = [float(r[3]) for r in rows]
    highs = [float(r[1]) if r[1] is not None else closes[i] for i, r in enumerate(rows)]
    lows = [float(r[2]) if r[2] is not None else closes[i] for i, r in enumerate(rows)]
    vols = [r[4] for r in rows]
    last = closes[-1]

    out: dict = {
        "as_of": rows[-1][0],
        "sessions": n,
        "close": _r(last),
        "volume": int(vols[-1]) if vols[-1] is not None else None,
    }
    for name, k in (("chg_1d", 1), ("chg_1w", 5), ("chg_1m", 21), ("chg_6m", 126), ("chg_1y", 252)):
        out[name] = _r(_chg(closes, k))

    out["rsi14"] = _r(rsi_series(closes, 14)[-1])

    s20, s50, s200 = sma_series(closes, 20), sma_series(closes, 50), sma_series(closes, 200)
    out["sma20"], out["sma50"], out["sma200"] = _r(s20[-1]), _r(s50[-1]), _r(s200[-1])
    out["golden_cross_days"] = out["death_cross_days"] = None
    cross = last_cross(s50, s200)
    if cross:
        idx, direction = cross
        out["golden_cross_days" if direction == "up" else "death_cross_days"] = n - 1 - idx

    e12, e26 = ema_series(closes, 12), ema_series(closes, 26)
    macd = [a - b if a is not None and b is not None else None for a, b in zip(e12, e26)]
    signal = ema_series(macd, 9)
    out["macd"] = _r(macd[-1], 6)
    out["macd_signal"] = _r(signal[-1], 6)
    out["macd_hist"] = _r(macd[-1] - signal[-1], 6) if macd[-1] is not None and signal[-1] is not None else None
    out["macd_cross_up_days"] = out["macd_cross_down_days"] = None
    cross = last_cross(macd, signal)
    if cross:
        idx, direction = cross
        out["macd_cross_up_days" if direction == "up" else "macd_cross_down_days"] = n - 1 - idx

    window_hi, window_lo = highs[-252:], lows[-252:]
    hi, lo = max(window_hi), min(window_lo)
    out["high52"], out["low52"] = _r(hi), _r(lo)
    out["prior_high52"] = _r(max(highs[-252:-1])) if n >= 61 else None
    out["from_high_pct"] = _r((last / hi - 1) * 100) if hi else None
    out["from_low_pct"] = _r((last / lo - 1) * 100) if lo > 0 else None

    prev = [v for v in vols[-21:-1] if v is not None]
    avg = sum(prev) / len(prev) if len(prev) >= 10 else None
    out["avg_vol20"] = _r(avg, 0)
    out["vol_ratio"] = _r(vols[-1] / avg, 2) if avg and vols[-1] is not None else None
    traded = [c * v for c, v in zip(closes[-20:], vols[-20:]) if v is not None]
    out["turnover20_cr"] = _r(sum(traded) / len(traded) / 1e7, 2) if len(traded) >= 10 else None
    return out


# -------------------------------------------------------------------- storage
def load_rows(conn: sqlite3.Connection, symbol_id: int, limit: int = LOOKBACK) -> list:
    rows = conn.execute(
        """SELECT date, high, low, close, volume FROM prices_daily
           WHERE symbol_id = ? ORDER BY date DESC LIMIT ?""",
        (symbol_id, limit),
    ).fetchall()
    rows.reverse()
    return rows


def compute_symbol(conn: sqlite3.Connection, symbol_id: int) -> Optional[dict]:
    """Fresh indicators for one stock straight from its stored prices."""
    return compute(load_rows(conn, symbol_id))


def save(conn: sqlite3.Connection, symbol_id: int, values: dict) -> None:
    values = dict(values)
    values["computed_at"] = datetime.now(IST).isoformat(timespec="seconds")
    cols = ["symbol_id"] + COLUMNS
    conn.execute(
        f"INSERT OR REPLACE INTO indicators ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
        [symbol_id] + [values.get(c) for c in COLUMNS],
    )


def stale_symbol_ids(conn: sqlite3.Connection) -> list:
    """Active stocks with prices whose indicators are missing or older than their prices."""
    return [
        r[0]
        for r in conn.execute(
            """SELECT m.symbol_id FROM
                 (SELECT symbol_id, MAX(date) AS d FROM prices_daily GROUP BY symbol_id) m
               JOIN symbols s ON s.id = m.symbol_id AND s.active = 1
               LEFT JOIN indicators i ON i.symbol_id = m.symbol_id
               WHERE i.symbol_id IS NULL OR i.as_of < m.d"""
        )
    ]


def refresh(
    conn: sqlite3.Connection,
    symbol_ids: Optional[Iterable[int]] = None,
    progress: Optional[Callable[[int, int], None]] = None,
) -> int:
    """Recompute indicators for the given stocks (default: only the stale ones).

    Returns how many stocks now have indicators.
    """
    ids = sorted(set(stale_symbol_ids(conn) if symbol_ids is None else symbol_ids))
    done = 0
    for i, sid in enumerate(ids, 1):
        values = compute(load_rows(conn, sid))
        if values is None:
            conn.execute("DELETE FROM indicators WHERE symbol_id = ?", (sid,))
        else:
            save(conn, sid, values)
            done += 1
        if i % 100 == 0:
            conn.commit()
        if progress:
            progress(i, len(ids))
    conn.commit()
    return done
