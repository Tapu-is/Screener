"""Everything the web pages show, as plain Python (no Streamlit): easy to test.

Functions take a database connection and return dicts / lists. Colours are
returned as CSS class names (see tables.CSS), never as raw colours.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable, Optional

from ..core import fundamentals, indicators, queries, screens, updater
from ..core.quote import IST, Quote, get_quotes, is_market_open

INDEX_SYMBOLS = [("NIFTY 50", "^NSEI"), ("SENSEX", "^BSESN"), ("BANK NIFTY", "^NSEBANK")]


# ------------------------------------------------------------------ formatting
def pct_class(v) -> str:
    if v is None:
        return "dim"
    if v >= 3:
        return "up2"
    if v > 0:
        return "up"
    if v <= -3:
        return "down2"
    if v < 0:
        return "down"
    return ""


def fmt(name: str, v) -> tuple[str, str]:
    """(text, css class) for one value of a screen field."""
    if v is None:
        return "-", "dim"
    kind = indicators.FIELD_BY_NAME[name].kind if name in indicators.FIELD_BY_NAME else "num"
    if kind == "pct":
        return f"{v:+.1f}%", pct_class(v)
    if kind == "price":
        return f"{v:,.2f}", ""
    if kind == "rsi":
        return f"{v:.1f}", "down" if v >= 70 else "up" if v <= 30 else ""
    if kind == "macd":
        return f"{v:+.3f}", "up" if v > 0 else "down" if v < 0 else ""
    if kind == "ratio":
        return f"{v:.1f}x", "warn" if v >= 1.5 else "dim" if v < 0.7 else ""
    if kind == "days":
        return f"{int(v)}d", ""
    if kind == "cr":
        return (f"{v:,.1f} Cr" if v < 10 else f"{v:,.0f} Cr"), ""
    if kind == "int":
        return _volume(v) if name in ("volume", "avg_vol20") else f"{int(v):,}", ""
    if kind == "pct0":
        return f"{v:.0f}%", "up" if v >= 15 else "down" if v < 0 else ""
    if kind == "num":
        return (f"{v:,.1f}" if abs(v) >= 10 or name == "pe" else f"{v:,.2f}"), ""
    return str(v), ""


def _volume(v) -> str:
    if v is None:
        return "-"
    if v >= 1e7:
        return f"{v / 1e7:.2f} Cr"
    if v >= 1e5:
        return f"{v / 1e5:.2f} L"
    return f"{int(v):,}"


def field_label(name: str) -> str:
    f = indicators.FIELD_BY_NAME.get(name)
    return f.short if f else name


# ------------------------------------------------------------------- dashboard
def market_status(conn, now: Optional[datetime] = None) -> dict:
    st = queries.status(conn, now)
    return {
        "open": is_market_open(now),
        "latest_date": st["latest_date"],
        "expected_date": st["expected_date"],
        "behind": st["behind"],
        "symbols": st["symbols"],
        "with_data": st["with_data"],
        "indicators_stale": st["indicators_stale"],
        "watchlist": st["watchlist"],
        "last_update": st["last_update"],
    }


def index_quotes(quotes_fn: Callable = get_quotes) -> list[dict]:
    try:
        quotes = quotes_fn([s for _, s in INDEX_SYMBOLS])
    except Exception:  # noqa: BLE001 - the page must still open offline
        return []
    out = []
    for (label, _), q in zip(INDEX_SYMBOLS, quotes):
        if isinstance(q, Quote) and q.change_pct is not None:
            out.append({"label": label, "price": q.price, "change": q.change, "pct": q.change_pct, "stale": q.source == "stale"})
    return out


def breadth(conn) -> dict:
    latest = conn.execute("SELECT MAX(as_of) FROM indicators").fetchone()[0]
    if not latest:
        return {}
    one = lambda sql: conn.execute(sql, (latest,)).fetchone()[0]  # noqa: E731
    return {
        "as_of": latest,
        "advancing": one("SELECT COUNT(*) FROM indicators WHERE as_of = ? AND chg_1d > 0"),
        "declining": one("SELECT COUNT(*) FROM indicators WHERE as_of = ? AND chg_1d < 0"),
        "oversold": one("SELECT COUNT(*) FROM indicators WHERE as_of = ? AND rsi14 < 30"),
        "overbought": one("SELECT COUNT(*) FROM indicators WHERE as_of = ? AND rsi14 > 70"),
        "volume_spikes": one("SELECT COUNT(*) FROM indicators WHERE as_of = ? AND vol_ratio >= 2"),
        "near_high": one("SELECT COUNT(*) FROM indicators WHERE as_of = ? AND from_high_pct >= -2"),
    }


def movers(conn, n: int = 8, min_turnover_cr: float = 5.0) -> dict:
    latest = conn.execute("SELECT MAX(as_of) FROM indicators").fetchone()[0]
    if not latest:
        return {"gainers": [], "losers": []}
    base = """SELECT s.ticker, s.name, i.close, i.chg_1d, i.vol_ratio FROM indicators i
              JOIN symbols s ON s.id = i.symbol_id
              WHERE i.as_of = ? AND i.chg_1d IS NOT NULL AND i.turnover20_cr >= ?"""
    g = conn.execute(base + " ORDER BY i.chg_1d DESC LIMIT ?", (latest, min_turnover_cr, n)).fetchall()
    l = conn.execute(base + " ORDER BY i.chg_1d ASC LIMIT ?", (latest, min_turnover_cr, n)).fetchall()
    return {"gainers": [dict(r) for r in g], "losers": [dict(r) for r in l]}


def watchlist_rows(conn, live: Optional[dict] = None, spark_days: int = 30) -> list[dict]:
    """Watchlist from stored data; `live` ({ticker: Quote}) overrides price and change."""
    rows = []
    for t in queries.watch_list(conn):
        sym = queries.get_symbol(conn, t)
        row = {"ticker": t, "name": None, "price": None, "pct": None, "spark": [], "live": False, "known": sym is not None}
        if sym is not None:
            row["name"] = sym["name"]
            hist = queries.history(conn, sym["id"], spark_days)
            row["spark"] = [h["close"] for h in hist]
            if hist:
                row["price"], row["pct"] = hist[-1]["close"], hist[-1]["chg_pct"]
            ind = conn.execute("SELECT rsi14, vol_ratio FROM indicators WHERE symbol_id = ?", (sym["id"],)).fetchone()
            row["rsi"] = ind["rsi14"] if ind else None
            row["vol_ratio"] = ind["vol_ratio"] if ind else None
        q = (live or {}).get(t)
        if isinstance(q, Quote):
            row.update(price=q.price, pct=q.change_pct, live=q.source == "live")
        rows.append(row)
    return rows


def live_quotes(tickers: list[str], quotes_fn: Callable = get_quotes) -> dict:
    if not tickers:
        return {}
    try:
        return dict(zip(tickers, quotes_fn(tickers, use_cache=False)))
    except Exception:  # noqa: BLE001
        return {}


# ----------------------------------------------------------------- stock page
def search(conn, text: str, limit: int = 12) -> list[dict]:
    return [dict(r) for r in queries.find(conn, text, limit)] if text.strip() else []


def stock_page(conn, text: str, days: int = 180) -> Optional[dict]:
    sym = queries.get_symbol(conn, text)
    if sym is None:
        return None
    full = conn.execute(
        "SELECT date, open, high, low, close, volume FROM prices_daily WHERE symbol_id = ? ORDER BY date DESC LIMIT 400",
        (sym["id"],),
    ).fetchall()
    full = [dict(r) for r in reversed(full)]
    closes = [r["close"] for r in full]
    avgs = {f"sma{n}": indicators.sma_series(closes, n) for n in (20, 50, 200)}
    cut = max(len(full) - days, 0)
    bars = full[cut:]
    averages = {k: v[cut:] for k, v in avgs.items()}
    st = queries.stats(conn, sym["id"])
    ind = conn.execute("SELECT * FROM indicators WHERE symbol_id = ?", (sym["id"],)).fetchone()
    return {
        "symbol": dict(sym),
        "bars": bars,
        "averages": averages,
        "stats": st,
        "indicators": dict(ind) if ind else None,
        "fundamentals": fundamentals.get(conn, sym["id"]),
        "in_watchlist": sym["ticker"] in queries.watch_list(conn),
        "has_data": bool(full),
    }


def trend_notes(ind: Optional[dict]) -> list[tuple[str, str]]:
    """Short plain-language readings of the indicators: [(text, css class)]."""
    if not ind or ind.get("close") is None:
        return []
    out = []
    rsi = ind.get("rsi14")
    if rsi is not None:
        if rsi < 30:
            out.append((f"RSI {rsi:.0f}: oversold", "up"))
        elif rsi > 70:
            out.append((f"RSI {rsi:.0f}: overbought", "down"))
        else:
            out.append((f"RSI {rsi:.0f}: neutral", ""))
    for n in (50, 200):
        avg = ind.get(f"sma{n}")
        if avg:
            above = ind["close"] >= avg
            out.append((f"{'Above' if above else 'Below'} its {n}-day average", "up" if above else "down"))
    if ind.get("golden_cross_days") is not None:
        out.append((f"Golden cross {ind['golden_cross_days']} sessions ago", "up"))
    if ind.get("death_cross_days") is not None:
        out.append((f"Death cross {ind['death_cross_days']} sessions ago", "down"))
    if ind.get("macd_hist") is not None:
        out.append(("MACD above its signal line", "up") if ind["macd_hist"] > 0 else ("MACD below its signal line", "down"))
    if ind.get("vol_ratio") is not None and ind["vol_ratio"] >= 1.5:
        out.append((f"Volume {ind['vol_ratio']:.1f}x the usual", "warn"))
    return out


FUND_ROWS = [
    ("market_cap_cr", "Market cap"), ("pe", "P/E"), ("pb", "P/B"), ("eps", "EPS"), ("div_yield", "Dividend yield %"),
    ("roe", "ROE"), ("roa", "ROA"), ("profit_margin", "Net margin"), ("op_margin", "Operating margin"),
    ("debt_equity", "Debt / equity"), ("rev_growth", "Revenue growth"), ("earn_growth", "Earnings growth"),
]


def fundamentals_rows(fund: Optional[dict]) -> list[tuple[str, str, str]]:
    if not fund:
        return []
    return [(label,) + fmt(name, fund[name]) for name, label in FUND_ROWS if fund.get(name) is not None]


# -------------------------------------------------------------------- screens
def numeric_fields() -> list[str]:
    return [f.name for f in indicators.FIELDS + indicators.FUND_FIELDS if f.name not in ("sessions",)]


def screen_choices(conn) -> list[dict]:
    return screens.list_all(conn)


def condition_from_row(field: str, op: str, value: str) -> dict:
    """Build one screen condition from the three form boxes (raises ScreenError)."""
    op = "~" if op == "contains" else op
    return screens.parse_where(f"{field}{op}{str(value).strip()}")


def build_definition(base: Optional[dict], rows: list[tuple], sort: Optional[str], order: Optional[str], limit: Optional[int]) -> dict:
    conds = [condition_from_row(*r) for r in rows if str(r[2]).strip() != ""]
    defn = screens.merge(base, conds, sort=sort or None, order=order or None, limit=limit or None)
    if not (base or {}).get("show") and conds:  # custom screen: show the columns it filters on
        used = [c["field"] for c in conds if c["field"] in screens.NUMERIC and c["field"] != "close"]
        show = list(dict.fromkeys(used + screens.DEFAULT_SHOW))[:3]
        defn["show"] = show
    return defn


def screen_table(res: screens.ScreenResult, show_names: bool = True) -> dict:
    """Columns and formatted rows for tables.html_table."""
    cols = ["Symbol"] + (["Name"] if show_names else []) + ["Close"] + [field_label(f) for f in res.show]
    rows = []
    for r in res.rows:
        cells = [{"text": r["ticker"], "link": r["ticker"], "bold": True}]
        if show_names:
            cells.append({"text": r.get("name") or "-", "dim": True, "left": True})
        cells.append({"text": fmt("close", r.get("close"))[0]})
        for f in res.show:
            text, cls = fmt(f, r.get(f))
            cells.append({"text": text, "cls": cls})
        rows.append(cells)
    return {"columns": cols, "rows": rows}


def screen_csv(res: screens.ScreenResult) -> str:
    import csv
    import io

    buf = io.StringIO()
    w = csv.writer(buf)
    fields = ["ticker", "name", "close"] + list(res.show)
    w.writerow(fields)
    for r in res.all_rows:
        w.writerow([r.get(f, "") if r.get(f) is not None else "" for f in fields])
    return buf.getvalue()


# ----------------------------------------------------------------------- data
def data_health(conn) -> dict:
    st = queries.status(conn)
    cov = fundamentals.coverage(conn)
    return {"status": st, "fundamentals": cov, "expected": updater.expected_last_session().isoformat()}
