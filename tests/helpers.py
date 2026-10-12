"""Shared test helpers: synthetic Yahoo-style payloads and a series generator."""

from datetime import date, datetime, timedelta

from screener.core.history import HistoryResult, parse_history
from screener.core.quote import IST


def business_days(end: date, n: int) -> list[date]:
    days, d = [], end
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return list(reversed(days))


def series(end: date, n: int, base: float = 100.0, step: float = 1.0):
    """n business days ending on `end`; close rises by `step` each day."""
    out = []
    for i, d in enumerate(business_days(end, n)):
        c = round(base + i * step, 2)
        out.append((d.isoformat(), c - 0.5, c + 1.0, c - 1.0, c, c, 1_000_000 + i))
    return out


def payload(rows, splits=None, gmtoffset=19800, symbol="TEST.NS"):
    """Build a chart-endpoint style response from rows (as made by series())."""
    stamps = []
    for r in rows:
        y, m, dd = map(int, r[0].split("-"))
        stamps.append(int(datetime(y, m, dd, 9, 15, tzinfo=IST).timestamp()))
    body = {
        "chart": {
            "result": [
                {
                    "meta": {"symbol": symbol, "gmtoffset": gmtoffset},
                    "timestamp": stamps,
                    "indicators": {
                        "quote": [
                            {
                                "open": [r[1] for r in rows],
                                "high": [r[2] for r in rows],
                                "low": [r[3] for r in rows],
                                "close": [r[4] for r in rows],
                                "volume": [r[6] for r in rows],
                            }
                        ],
                        "adjclose": [{"adjclose": [r[5] for r in rows]}],
                    },
                }
            ],
            "error": None,
        }
    }
    if splits:
        body["chart"]["result"][0]["events"] = {
            "splits": {
                str(i): {
                    "date": int(datetime(*map(int, s.split("-")), 9, 15, tzinfo=IST).timestamp()),
                    "numerator": 2,
                    "denominator": 1,
                }
                for i, s in enumerate(splits)
            }
        }
    return body


class FakeFetch:
    """Stands in for history.fetch_history and records how it was called."""

    def __init__(self, data: dict, splits: dict | None = None, fail: dict | None = None):
        self.data = data  # yahoo ticker -> rows
        self.splits = splits or {}
        self.fail = fail or {}
        self.calls = []

    def __call__(self, ticker, start=None, years=2, session=None, **kw):
        self.calls.append((ticker, start, years))
        if ticker in self.fail:
            from screener.core.history import HistoryError

            err = self.fail[ticker]
            raise err if isinstance(err, Exception) else HistoryError(err)
        rows = self.data.get(ticker, [])
        if start is not None:
            rows = [r for r in rows if r[0] >= start.isoformat()]
        return HistoryResult(ticker=ticker, rows=list(rows), splits=list(self.splits.get(ticker, [])))


def mk_rows(closes, vols=None, end=None):
    """Price rows (date, open, high, low, close, adj_close, volume) ending on `end`."""
    from datetime import date as _date

    end = end or _date(2026, 10, 9)
    days = business_days(end, len(closes))
    out = []
    for i, (d, c) in enumerate(zip(days, closes)):
        v = vols[i] if vols else 1_000_000
        out.append((d.isoformat(), c, round(c * 1.01, 4), round(c * 0.99, 4), c, c, v))
    return out


def add_stock(conn, ticker, closes, vols=None, industry="Test", name=None, end=None):
    """Insert a symbol with prices; returns its id."""
    from screener.core import updater

    with conn:
        conn.execute(
            "INSERT INTO symbols (ticker, yahoo, name, industry, exchange, active, added_at) "
            "VALUES (?, ?, ?, ?, 'NSE', 1, '2026-10-09')",
            (ticker, f"{ticker}.NS", name or f"{ticker} Ltd", industry),
        )
    sid = conn.execute("SELECT id FROM symbols WHERE ticker = ?", (ticker,)).fetchone()[0]
    updater.store_rows(conn, sid, mk_rows(closes, vols, end))
    return sid
