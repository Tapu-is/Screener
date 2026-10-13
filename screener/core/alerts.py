"""Price / indicator alerts. An alert fires once, then switches itself off
(re-arm it with `stk alert reset ID`)."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Callable, Optional

from .quote import IST, Quote, get_quotes

# kind -> (needs a live quote, what the number means, example)
KINDS = {
    "above": (True, "price at or above", "stk alert add TCS above 2200"),
    "below": (True, "price at or below", "stk alert add TCS below 2000"),
    "rsi-below": (False, "RSI(14) at or below", "stk alert add TCS rsi-below 30"),
    "rsi-above": (False, "RSI(14) at or above", "stk alert add TCS rsi-above 70"),
    "move": (True, "today's move of at least +/- N percent", "stk alert add TCS move 5"),
    "volume": (True, "volume at least N times its 20-day average", "stk alert add TCS volume 2"),
}


class AlertError(ValueError):
    pass


def _now() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")


def add(conn: sqlite3.Connection, ticker: str, kind: str, value) -> int:
    kind = kind.lower()
    if kind not in KINDS:
        raise AlertError(f"unknown alert type '{kind}'. Choose from: {', '.join(KINDS)}")
    try:
        value = float(str(value).replace(",", ""))
    except ValueError:
        raise AlertError(f"'{value}' is not a number") from None
    if value <= 0:
        raise AlertError("the value must be above zero")
    if kind.startswith("rsi") and value > 100:
        raise AlertError("RSI is between 0 and 100")
    ticker = ticker.strip().upper()
    if not ticker:
        raise AlertError("give a symbol")
    with conn:
        cur = conn.execute(
            "INSERT INTO alerts (ticker, kind, value, created_at) VALUES (?, ?, ?, ?)",
            (ticker, kind, value, _now()),
        )
    return cur.lastrowid


def list_all(conn: sqlite3.Connection, active_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM alerts" + (" WHERE active = 1" if active_only else "") + " ORDER BY id"
    return [dict(r) for r in conn.execute(sql)]


def remove(conn: sqlite3.Connection, ids: list[int]) -> int:
    with conn:
        n = 0
        for i in ids:
            n += conn.execute("DELETE FROM alerts WHERE id = ?", (i,)).rowcount
    return n


def reset(conn: sqlite3.Connection, ids: list[int]) -> int:
    with conn:
        n = 0
        for i in ids:
            n += conn.execute(
                "UPDATE alerts SET active = 1, triggered_at = NULL, triggered_note = NULL WHERE id = ?", (i,)
            ).rowcount
    return n


def describe(a: dict) -> str:
    v = a["value"]
    k = a["kind"]
    if k in ("above", "below"):
        return f"{a['ticker']} {k} ₹{v:,.2f}"
    if k == "rsi-below":
        return f"{a['ticker']} RSI at or below {v:g}"
    if k == "rsi-above":
        return f"{a['ticker']} RSI at or above {v:g}"
    if k == "move":
        return f"{a['ticker']} moves ±{v:g}% in a day"
    return f"{a['ticker']} volume {v:g}x average"


def evaluate(a: dict, quote: Optional[Quote], rsi: Optional[float], avg_vol20: Optional[float]) -> Optional[str]:
    """A message if the alert condition is met right now, otherwise None.
    Missing data never triggers."""
    k, v, t = a["kind"], a["value"], a["ticker"]
    if k in ("above", "below", "move", "volume") and quote is None:
        return None
    if k == "above" and quote.price >= v:
        return f"{t} ₹{quote.price:,.2f} is at or above ₹{v:,.2f}"
    if k == "below" and quote.price <= v:
        return f"{t} ₹{quote.price:,.2f} is at or below ₹{v:,.2f}"
    if k == "move" and quote.change_pct is not None and abs(quote.change_pct) >= v:
        return f"{t} moved {quote.change_pct:+.1f}% today (₹{quote.price:,.2f})"
    if k == "volume" and quote.volume and avg_vol20:
        ratio = quote.volume / avg_vol20
        if ratio >= v:
            return f"{t} volume is {ratio:.1f}x its 20-day average"
    if k == "rsi-below" and rsi is not None and rsi <= v:
        return f"{t} RSI is {rsi:.1f} (at or below {v:g})"
    if k == "rsi-above" and rsi is not None and rsi >= v:
        return f"{t} RSI is {rsi:.1f} (at or above {v:g})"
    return None


def _indicator_row(conn, ticker):
    return conn.execute(
        """SELECT i.rsi14, i.avg_vol20 FROM indicators i JOIN symbols s ON s.id = i.symbol_id
           WHERE s.ticker = ?""",
        (ticker,),
    ).fetchone()


def check(conn: sqlite3.Connection, quotes_fn: Callable = get_quotes) -> list[dict]:
    """Test every active alert, switch off the ones that fire, return them
    as dicts with a 'message'."""
    active = list_all(conn, active_only=True)
    if not active:
        return []
    live_tickers = sorted({a["ticker"] for a in active if KINDS[a["kind"]][0]})
    quotes = {}
    if live_tickers:
        for t, q in zip(live_tickers, quotes_fn(live_tickers)):
            if isinstance(q, Quote):
                quotes[t] = q
    fired = []
    for a in active:
        row = _indicator_row(conn, a["ticker"])
        msg = evaluate(a, quotes.get(a["ticker"]), row["rsi14"] if row else None, row["avg_vol20"] if row else None)
        if msg:
            with conn:
                conn.execute(
                    "UPDATE alerts SET active = 0, triggered_at = ?, triggered_note = ? WHERE id = ?",
                    (_now(), msg, a["id"]),
                )
            fired.append({**a, "message": msg})
    return fired
