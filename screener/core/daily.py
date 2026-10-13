"""Text summary sent after the daily update."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from .quote import IST


def summary(conn: sqlite3.Connection, fired: list | None = None, top: int = 5, min_turnover_cr: float = 5.0) -> str:
    latest = conn.execute("SELECT MAX(as_of) FROM indicators").fetchone()[0]
    if not latest:
        return "stk: no data yet."
    lines = [f"stk daily · {datetime.strptime(latest, '%Y-%m-%d').strftime('%a %d %b %Y')}"]

    if fired:
        lines += ["", "🔔 Alerts"] + [f"• {a['message']}" for a in fired]

    base = """SELECT s.ticker, i.close, i.chg_1d FROM indicators i JOIN symbols s ON s.id = i.symbol_id
              WHERE i.as_of = ? AND i.chg_1d IS NOT NULL AND i.turnover20_cr >= ?"""
    gain = conn.execute(base + " ORDER BY i.chg_1d DESC LIMIT ?", (latest, min_turnover_cr, top)).fetchall()
    lose = conn.execute(base + " ORDER BY i.chg_1d ASC LIMIT ?", (latest, min_turnover_cr, top)).fetchall()
    fmt = lambda r: f"• {r['ticker']} {r['close']:,.2f} ({r['chg_1d']:+.1f}%)"  # noqa: E731
    if gain:
        lines += ["", "▲ Top gainers"] + [fmt(r) for r in gain]
    if lose:
        lines += ["", "▼ Top losers"] + [fmt(r) for r in lose]

    one = lambda sql: conn.execute(sql, (latest,)).fetchone()[0]  # noqa: E731
    lines += [
        "",
        f"Oversold (RSI<30): {one('SELECT COUNT(*) FROM indicators WHERE as_of = ? AND rsi14 < 30')}"
        f" · Overbought (RSI>70): {one('SELECT COUNT(*) FROM indicators WHERE as_of = ? AND rsi14 > 70')}",
        f"Volume spikes (2x+): {one('SELECT COUNT(*) FROM indicators WHERE as_of = ? AND vol_ratio >= 2')}",
    ]

    w = conn.execute(
        """SELECT s.ticker, i.close, i.chg_1d FROM watchlist w JOIN symbols s ON s.ticker = w.ticker
           JOIN indicators i ON i.symbol_id = s.id WHERE i.chg_1d IS NOT NULL ORDER BY w.rowid"""
    ).fetchall()
    if w:
        lines += ["", "★ Watchlist"] + [fmt(r) for r in w]
    return "\n".join(lines)
