import contextlib
import csv
import io
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("STK_CACHE_DIR", tempfile.mkdtemp())

from helpers import FakeFetch, add_stock, series  # noqa: E402

from screener.core import db, indicators as I, queries, symbols, updater  # noqa: E402
from screener.core.history import NotFound  # noqa: E402
from screener.core.quote import IST  # noqa: E402
from screener.core.updater import Pacer  # noqa: E402

try:
    from screener.cli import main as cli

    HAVE_RICH = True
except ImportError:  # the terminal library is not installed
    HAVE_RICH = False


def at(y, m, d, h, mi):
    return datetime(y, m, d, h, mi, tzinfo=IST)


def fast():
    return Pacer(interval=0, cooldown=0, sleep=lambda s: None)


def fresh_db():
    return db.connect(Path(tempfile.mkdtemp()) / "t.db")


V1_SCHEMA = """
CREATE TABLE symbols (
    id INTEGER PRIMARY KEY, ticker TEXT NOT NULL UNIQUE, yahoo TEXT NOT NULL, name TEXT, industry TEXT,
    isin TEXT, exchange TEXT NOT NULL DEFAULT 'NSE', active INTEGER NOT NULL DEFAULT 1, added_at TEXT NOT NULL);
CREATE TABLE prices_daily (
    symbol_id INTEGER NOT NULL REFERENCES symbols(id) ON DELETE CASCADE, date TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL NOT NULL, adj_close REAL, volume INTEGER,
    PRIMARY KEY (symbol_id, date)) WITHOUT ROWID;
CREATE TABLE watchlist (ticker TEXT PRIMARY KEY, added_at TEXT NOT NULL);
CREATE TABLE update_log (
    id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT, mode TEXT, symbols_total INTEGER,
    symbols_ok INTEGER, symbols_skipped INTEGER, symbols_failed INTEGER, rows_written INTEGER,
    interrupted INTEGER NOT NULL DEFAULT 0, failures TEXT);
"""


class MigrationTests(unittest.TestCase):
    def test_existing_phase2_database_keeps_its_data(self):
        path = Path(tempfile.mkdtemp()) / "old.db"
        raw = sqlite3.connect(path)
        raw.executescript(V1_SCHEMA)
        raw.execute("INSERT INTO symbols (id, ticker, yahoo, name, added_at) VALUES (1, 'TCS', 'TCS.NS', 'Tata', 'x')")
        raw.execute("INSERT INTO prices_daily VALUES (1, '2026-10-09', 1, 2, 0.5, 1.5, 1.5, 1000)")
        raw.execute("INSERT INTO watchlist VALUES ('TCS', 'x')")
        raw.execute("PRAGMA user_version = 1")
        raw.commit()
        raw.close()

        conn = db.connect(path)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(symbols)")]
        self.assertIn("no_data_since", cols)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"indicators", "saved_screens"} <= tables)
        self.assertEqual(conn.execute("SELECT close FROM prices_daily").fetchone()[0], 1.5)
        self.assertEqual(conn.execute("SELECT ticker FROM watchlist").fetchone()[0], "TCS")
        self.assertIsNone(conn.execute("SELECT no_data_since FROM symbols").fetchone()[0])
        conn.close()
        db.connect(path).close()  # opening an upgraded database again is harmless

    def test_fresh_database_is_current(self):
        conn = fresh_db()
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
        self.assertIn("no_data_since", [r[1] for r in conn.execute("PRAGMA table_info(symbols)")])


class UnavailableTests(unittest.TestCase):
    NOW = at(2026, 10, 9, 18, 0)

    def setUp(self):
        self.conn = fresh_db()
        symbols.load(self.conn, symbols.parse_csv("Symbol\nGOOD\nBADCO\n"))
        self.data = {"GOOD.NS": series(date(2026, 10, 9), 10)}
        self.fail = {"BADCO.NS": NotFound("BADCO.NS: symbol not found")}

    def run_update(self, fetch=None, now=None, **kw):
        fetch = fetch or FakeFetch(self.data, fail=self.fail)
        res = updater.update(self.conn, fetch=fetch, workers=1, pacer=fast(), now=now or self.NOW, **kw)
        return res, fetch

    def flag(self):
        return self.conn.execute("SELECT no_data_since FROM symbols WHERE ticker = 'BADCO'").fetchone()[0]

    def test_unknown_symbol_is_flagged_not_failed(self):
        res, _ = self.run_update()
        self.assertEqual(res.unavailable, ["BADCO"])
        self.assertEqual((res.ok, res.failures, res.not_done), (1, [], 0))
        self.assertEqual(self.flag(), "2026-10-09")
        log = self.conn.execute("SELECT symbols_failed FROM update_log").fetchone()[0]
        self.assertEqual(log, 0)

    def test_flagged_symbol_is_not_asked_about_again(self):
        self.run_update()
        res, fetch = self.run_update(now=at(2026, 10, 10, 12, 0))
        self.assertEqual((res.unavailable, res.unavailable_skipped), ([], 1))
        self.assertNotIn("BADCO.NS", [c[0] for c in fetch.calls])

    def test_it_is_retried_after_30_days(self):
        self.run_update()
        later = self.NOW + timedelta(days=31)
        _, fetch = self.run_update(now=later)
        self.assertIn("BADCO.NS", [c[0] for c in fetch.calls])

    def test_retry_flag_and_naming_the_symbol_force_a_retry(self):
        self.run_update()
        _, fetch = self.run_update(retry_unavailable=True)
        self.assertIn("BADCO.NS", [c[0] for c in fetch.calls])
        _, fetch = self.run_update(tickers=["badco"])
        self.assertEqual([c[0] for c in fetch.calls], ["BADCO.NS"])

    def test_flag_clears_when_the_source_knows_the_symbol_again(self):
        self.run_update()
        data = {**self.data, "BADCO.NS": series(date(2026, 10, 9), 10)}
        res, _ = self.run_update(FakeFetch(data), retry_unavailable=True)
        self.assertEqual((res.ok, res.unavailable), (1, []))
        self.assertIsNone(self.flag())

    def test_a_symbol_that_had_prices_is_reported_as_a_failure_instead(self):
        old = {"BADCO.NS": series(date(2026, 10, 2), 10), "GOOD.NS": self.data["GOOD.NS"]}
        self.run_update(FakeFetch(old))
        res, _ = self.run_update()
        self.assertEqual([t for t, _ in res.failures], ["BADCO"])
        self.assertEqual(res.unavailable, [])
        self.assertIsNone(self.flag())

    def test_status_does_not_call_flagged_symbols_behind(self):
        self.run_update()
        s = queries.status(self.conn, now=self.NOW)
        self.assertEqual((s["symbols"], s["with_data"], s["behind"], s["unavailable"]), (2, 1, 0, 1))

    def test_not_found_error_codes_are_recognised(self):
        from screener.core.history import HistoryError, parse_history

        body = {"chart": {"result": None, "error": {"code": "Not Found", "description": "No data found, symbol may be delisted"}}}
        with self.assertRaises(NotFound):
            parse_history(body, "X.NS")
        with self.assertRaises(HistoryError) as cm:
            parse_history({"chart": {"result": None, "error": {"code": "Internal", "description": "boom"}}}, "X.NS")
        self.assertNotIsInstance(cm.exception, NotFound)


class StatusIndicatorTests(unittest.TestCase):
    def test_stale_indicator_count(self):
        conn = fresh_db()
        add_stock(conn, "AAA", [100 + i for i in range(80)])
        add_stock(conn, "BBB", [100 + i for i in range(80)])
        self.assertEqual(queries.status(conn)["indicators_stale"], 2)
        I.refresh(conn)
        self.assertEqual(queries.status(conn)["indicators_stale"], 0)


@unittest.skipUnless(HAVE_RICH, "the rich library is not installed")
class CliTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        patch = mock.patch.dict(os.environ, {"STK_HOME": self.home})
        patch.start()
        self.addCleanup(patch.stop)
        conn = db.connect()
        add_stock(conn, "DROP", [100.0] * 270 + [100 * 0.98 ** (i + 1) for i in range(30)], industry="Banks", name="Drop Bank")
        add_stock(conn, "RISE", [100 + 0.5 * i for i in range(300)], industry="IT")
        add_stock(conn, "SPIKE", [100.0] * 300, vols=[1_000_000] * 299 + [4_000_000], industry="Auto")
        conn.close()

    def cli(self, *argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli_main(list(argv))
        return code, buf.getvalue()

    def test_screen_list_and_fields(self):
        code, out = self.cli("screen")
        self.assertEqual(code, 0)
        for name in ("oversold", "golden-cross", "breakout", "volume-surge", "gainers", "losers"):
            self.assertIn(name, out)
        code, out = self.cli("screen", "fields")
        self.assertEqual(code, 0)
        self.assertIn("rsi14", out)
        self.assertIn("turnover20_cr", out)

    def test_builtin_screens(self):
        code, out = self.cli("screen", "oversold")
        self.assertEqual(code, 0)
        self.assertIn("DROP", out)
        self.assertNotIn("RISE", out)
        self.assertIn("1 match", out)
        self.assertIn("indicators ready for 3 stocks", out)  # calculated on first use
        code, out = self.cli("screen", "overbought")
        self.assertIn("RISE", out)
        self.assertNotIn("indicators ready", out)  # already fresh now

    def test_custom_filters(self):
        _, out = self.cli("screen", "--rsi-below", "30", "--below-sma", "200")
        self.assertIn("DROP", out)
        self.assertIn("custom screen", out)
        _, out = self.cli("screen", "--where", "vol_ratio>=3")
        self.assertIn("SPIKE", out)
        self.assertNotIn("RISE", out)
        _, out = self.cli("screen", "--where", "chg_1d>-50", "--industry", "bank", "--names")
        self.assertIn("DROP", out)
        self.assertIn("Drop Bank", out)
        self.assertNotIn("SPIKE", out)
        _, out = self.cli("screen", "oversold", "--where", "close>1000")
        self.assertIn("0 matches", out)
        self.assertIn("No stocks match", out)

    def test_sort_and_limit(self):
        _, out = self.cli("screen", "--where", "close>0", "--sort", "chg_1m", "--asc", "--limit", "1")
        self.assertIn("DROP", out)
        self.assertNotIn("SPIKE", out)
        self.assertIn("Showing 1 of 3", out)

    def test_save_show_run_and_delete(self):
        code, out = self.cli("screen", "save", "My-Dip", "--rsi-below", "30")
        self.assertEqual(code, 0)
        self.assertIn("created screen", out)
        _, out = self.cli("screen")
        self.assertIn("my-dip*", out)
        _, out = self.cli("screen", "my-dip")
        self.assertIn("DROP", out)
        code, out = self.cli("screen", "show", "my-dip")
        self.assertEqual(code, 0)
        self.assertIn('"rsi14"', out)
        code, out = self.cli("screen", "delete", "my-dip")
        self.assertEqual(code, 0)
        code, out = self.cli("screen", "my-dip")
        self.assertEqual(code, 1)
        self.assertIn("No screen called", out)

    def test_friendly_errors(self):
        code, out = self.cli("screen", "--where", "rsi<30")
        self.assertEqual(code, 1)
        self.assertIn('Did you mean "rsi14"', out)
        code, out = self.cli("screen", "oversol")
        self.assertEqual(code, 1)
        self.assertIn("oversold", out)
        code, out = self.cli("screen", "oversold", "gainers")
        self.assertEqual(code, 1)
        self.assertIn("one screen name", out)
        self.assertEqual(self.cli("screen", "save")[0], 1)
        code, out = self.cli("screen", "save", "empty")
        self.assertEqual(code, 1)
        self.assertIn("filters", out)
        code, out = self.cli("screen", "save", "oversold", "--rsi-below", "30")
        self.assertEqual(code, 1)
        self.assertIn("taken", out)
        self.assertEqual(self.cli("screen", "--above-sma", "100")[0], 1)

    def test_csv_export_has_every_match(self):
        path = os.path.join(self.home, "out.csv")
        code, out = self.cli("screen", "--where", "close>0", "--limit", "1", "--csv", path)
        self.assertEqual(code, 0)
        with open(path, newline="") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(sorted(r["ticker"] for r in rows), ["DROP", "RISE", "SPIKE"])
        self.assertIn("rsi14", rows[0])

    def test_screen_with_no_prices(self):
        with mock.patch.dict(os.environ, {"STK_HOME": tempfile.mkdtemp()}):
            code, out = self.cli("screen", "oversold")
        self.assertEqual(code, 1)
        self.assertIn("No prices stored yet", out)

    def test_info_shows_technicals_and_status_shows_indicators(self):
        code, out = self.cli("info", "DROP")
        self.assertEqual(code, 0)
        self.assertIn("RSI (14)", out)
        self.assertIn("oversold", out)
        self.assertIn("MACD", out)
        code, out = self.cli("status")
        self.assertIn("Indicators", out)
        self.assertIn("to recalculate", out)  # nothing has calculated them yet
        self.cli("screen", "oversold")
        _, out = self.cli("status")
        self.assertIn("up to date", out)


if HAVE_RICH:
    cli_main = cli.main


if __name__ == "__main__":
    unittest.main()
