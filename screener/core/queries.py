"""Read helpers over the stored data: lookup, history, stats, watchlist, status."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .quote import IST
from .updater import expected_last_session


# --------------------------------------------------------------------- symbols
def normalize(text: str) -> str:
    t = text.strip().upper()
    for suffix in (".NS", ".BO"):
        if t.endswith(suffix):
            return t[: -len(suffix)]
    return t


def get_symbol(conn: sqlite3.Connection, text: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM symbols WHERE ticker = ?", (normalize(text),)).fetchone()


def find(conn: sqlite3.Connection, text: str, limit: int = 20) -> list[sqlite3.Row]:
    like = f"%{text.strip()}%"
    return conn.execute(
        """
        SELECT s.ticker, s.name, s.industry,
               (SELECT MAX(date) FROM prices_daily p WHERE p.symbol_id = s.id) AS last_date
        FROM symbols s
        WHERE s.active = 1 AND (s.ticker LIKE ? OR s.name LIKE ?)
        ORDER BY (s.ticker = ? COLLATE NOCASE) DESC,
                 (s.ticker LIKE ? ) DESC, s.ticker
        LIMIT ?
        """,
        (like, like, text.strip(), f"{text.strip()}%", limit),
    ).fetchall()


# --------------------------------------------------------------------- history
def history(conn: sqlite3.Connection, symbol_id: int, days: int) -> list[sqlite3.Row]:
    """Last `days` sessions, oldest first, each with change vs the previous close."""
    rows = conn.execute(
        """SELECT date, open, high, low, close, volume FROM prices_daily
           WHERE symbol_id = ? ORDER BY date DESC LIMIT ?""",
        (symbol_id, days + 1),
    ).fetchall()
    rows = list(reversed(rows))
    out = []
    for i, r in enumerate(rows):
        prev = rows[i - 1]["close"] if i else None
        chg = (r["close"] / prev - 1) * 100 if prev else None
        out.append({**dict(r), "chg_pct": chg})
    return out[1:] if len(rows) > days else out


# ----------------------------------------------------------------------- stats
@dataclass
class Stats:
    last_date: str
    close: float
    chg: dict  # period label -> % change or None
    high52: float
    low52: float
    from_high_pct: float
    from_low_pct: float
    range_pos: float  # 0..1 position inside the 52 week range
    avg_vol20: Optional[float]
    last_vol: Optional[int]
    sessions: int


PERIODS = [("1D", 1), ("1W", 5), ("1M", 21), ("6M", 126), ("1Y", 252)]


def stats(conn: sqlite3.Connection, symbol_id: int) -> Optional[Stats]:
    rows = conn.execute(
        """SELECT date, high, low, close, volume FROM prices_daily
           WHERE symbol_id = ? ORDER BY date DESC LIMIT 253""",
        (symbol_id,),
    ).fetchall()
    if not rows:
        return None
    close = rows[0]["close"]
    chg = {}
    for label, n in PERIODS:
        chg[label] = (close / rows[n]["close"] - 1) * 100 if len(rows) > n else None

    window = rows[:252]
    high52 = max((r["high"] if r["high"] is not None else r["close"]) for r in window)
    low52 = min((r["low"] if r["low"] is not None else r["close"]) for r in window)
    span = high52 - low52
    vols = [r["volume"] for r in rows[:20] if r["volume"] is not None]
    return Stats(
        last_date=rows[0]["date"],
        close=close,
        chg=chg,
        high52=high52,
        low52=low52,
        from_high_pct=(close / high52 - 1) * 100,
        from_low_pct=(close / low52 - 1) * 100 if low52 else 0.0,
        range_pos=(close - low52) / span if span else 0.5,
        avg_vol20=sum(vols) / len(vols) if vols else None,
        last_vol=rows[0]["volume"],
        sessions=len(rows),
    )


# ------------------------------------------------------------------- watchlist
def watch_add(conn: sqlite3.Connection, tickers: list[str]) -> list[str]:
    now = datetime.now(IST).isoformat(timespec="seconds")
    added = []
    with conn:
        for t in tickers:
            t = t.strip().upper()
            if not t:
                continue
            cur = conn.execute("INSERT OR IGNORE INTO watchlist (ticker, added_at) VALUES (?, ?)", (t, now))
            if cur.rowcount:
                added.append(t)
    return added


def watch_remove(conn: sqlite3.Connection, tickers: list[str]) -> list[str]:
    removed = []
    with conn:
        for t in tickers:
            t = t.strip().upper()
            if conn.execute("DELETE FROM watchlist WHERE ticker = ?", (t,)).rowcount:
                removed.append(t)
    return removed


def watch_list(conn: sqlite3.Connection) -> list[str]:
    return [r["ticker"] for r in conn.execute("SELECT ticker FROM watchlist ORDER BY rowid")]


# ---------------------------------------------------------------------- status
def status(conn: sqlite3.Connection, now: Optional[datetime] = None) -> dict:
    target = expected_last_session(now).isoformat()
    one = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731

    active = one("SELECT COUNT(*) FROM symbols WHERE active = 1")
    with_data = one("SELECT COUNT(DISTINCT symbol_id) FROM prices_daily")
    behind = one(
        """SELECT COUNT(*) FROM symbols s WHERE s.active = 1 AND s.no_data_since IS NULL AND
           COALESCE((SELECT MAX(date) FROM prices_daily p WHERE p.symbol_id = s.id), '') < ?""",
        target,
    )
    stale = one(
        """SELECT COUNT(*) FROM (SELECT symbol_id, MAX(date) AS d FROM prices_daily GROUP BY symbol_id) m
           JOIN symbols s ON s.id = m.symbol_id AND s.active = 1
           LEFT JOIN indicators i ON i.symbol_id = m.symbol_id
           WHERE i.symbol_id IS NULL OR i.as_of < m.d"""
    )
    last_log = conn.execute("SELECT * FROM update_log ORDER BY id DESC LIMIT 1").fetchone()
    path = conn.execute("PRAGMA database_list").fetchone()["file"]
    return {
        "path": path,
        "size_bytes": os.path.getsize(path) if path and os.path.exists(path) else 0,
        "symbols": active,
        "with_data": with_data,
        "price_rows": one("SELECT COUNT(*) FROM prices_daily"),
        "first_date": one("SELECT MIN(date) FROM prices_daily"),
        "latest_date": one("SELECT MAX(date) FROM prices_daily"),
        "expected_date": target,
        "behind": behind,
        "unavailable": one("SELECT COUNT(*) FROM symbols WHERE active = 1 AND no_data_since IS NOT NULL"),
        "indicators_stale": stale,
        "watchlist": one("SELECT COUNT(*) FROM watchlist"),
        "last_update": dict(last_log) | {"failures": json.loads(last_log["failures"] or "[]")} if last_log else None,
    }
