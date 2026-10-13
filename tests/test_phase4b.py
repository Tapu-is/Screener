import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ["STK_HOME"] = tempfile.mkdtemp()
for k in ("STK_TELEGRAM_TOKEN", "STK_TELEGRAM_CHAT_ID"):
    os.environ.pop(k, None)

from helpers import add_stock  # noqa: E402

from screener.cli import home  # noqa: E402
from screener.core import alerts, daily, db, indicators, notify, queries  # noqa: E402
from screener.core.quote import Quote, QuoteError  # noqa: E402


def quote(sym, price, pct=0.0, vol=1_000_000):
    return Quote(sym, sym, "NSE", "INR", price, price, 0.0, pct, price, price, vol, None, None, "2026-10-12T10:00:00+05:30")


def conn_with(*stocks):
    conn = db.connect(Path(tempfile.mkdtemp()) / "t.db")
    for t, closes in stocks:
        add_stock(conn, t, closes)
    indicators.refresh(conn)
    return conn


class AlertTests(unittest.TestCase):
    def test_add_validation(self):
        c = conn_with()
        self.assertEqual(alerts.add(c, "tcs", "above", "2,200"), 1)
        self.assertEqual(alerts.list_all(c)[0]["ticker"], "TCS")
        for bad in (("TCS", "sideways", 1), ("TCS", "above", "abc"), ("TCS", "above", 0), ("TCS", "rsi-below", 150)):
            with self.assertRaises(alerts.AlertError):
                alerts.add(c, *bad)

    def test_evaluate(self):
        a = lambda k, v: {"ticker": "X", "kind": k, "value": v}  # noqa: E731
        q = quote("X", 105, pct=-6, vol=3_000_000)
        self.assertIn("at or above", alerts.evaluate(a("above", 100), q, None, None))
        self.assertIsNone(alerts.evaluate(a("above", 110), q, None, None))
        self.assertIn("at or below", alerts.evaluate(a("below", 110), q, None, None))
        self.assertIn("-6.0%", alerts.evaluate(a("move", 5), q, None, None))
        self.assertIsNone(alerts.evaluate(a("move", 7), q, None, None))
        self.assertIn("3.0x", alerts.evaluate(a("volume", 2), q, None, 1_000_000))
        self.assertIn("RSI", alerts.evaluate(a("rsi-below", 30), None, 25.0, None))
        self.assertIsNone(alerts.evaluate(a("rsi-below", 30), None, None, None))
        self.assertIsNone(alerts.evaluate(a("above", 1), None, None, None))

    def test_check_fires_once_and_resets(self):
        c = conn_with(("UPCO", [100] * 30))
        alerts.add(c, "UPCO", "above", 120)
        alerts.add(c, "UPCO", "below", 50)
        calls = []

        def fake(tickers):
            calls.append(tickers)
            return [quote(t, 125) for t in tickers]

        fired = alerts.check(c, fake)
        self.assertEqual([f["kind"] for f in fired], ["above"])
        self.assertEqual(calls, [["UPCO"]])
        self.assertEqual(alerts.check(c, fake)[0:0], [])  # nothing left to fire twice
        self.assertEqual(len(alerts.list_all(c, active_only=True)), 1)
        self.assertEqual(alerts.reset(c, [1]), 1)
        self.assertEqual(len(alerts.check(c, fake)), 1)

    def test_rsi_alert_needs_no_network(self):
        closes = [100 - i for i in range(40)]  # steady fall -> RSI near 0
        c = conn_with(("FALL", closes))
        alerts.add(c, "FALL", "rsi-below", 30)

        def boom(_):
            raise AssertionError("no live quote should be fetched")

        self.assertEqual(len(alerts.check(c, boom)), 1)

    def test_failed_quote_does_not_fire(self):
        c = conn_with()
        alerts.add(c, "GHOST", "above", 1)
        self.assertEqual(alerts.check(c, lambda ts: [QuoteError("nope") for _ in ts]), [])
        self.assertEqual(len(alerts.list_all(c, True)), 1)

    def test_remove(self):
        c = conn_with()
        alerts.add(c, "A", "above", 1)
        self.assertEqual(alerts.remove(c, [1, 99]), 1)
        self.assertEqual(alerts.list_all(c), [])


class MigrationTests(unittest.TestCase):
    def test_v2_database_gains_alerts(self):
        path = Path(tempfile.mkdtemp()) / "old.db"
        raw = sqlite3.connect(path)
        raw.executescript(db.SCHEMA.replace("CREATE TABLE IF NOT EXISTS alerts", "CREATE TABLE IF NOT EXISTS _unused"))
        raw.execute("DROP TABLE IF EXISTS alerts")
        raw.execute("PRAGMA user_version = 2")
        raw.commit()
        raw.close()
        conn = db.connect(path)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
        self.assertEqual(alerts.list_all(conn), [])


class FakeResp:
    def __init__(self, status=200, body=None):
        self.status_code, self._b = status, body or {}

    def json(self):
        return self._b


class FakeSession:
    def __init__(self, status=200, body=None):
        self.status, self.body, self.posts = status, body, []

    def get(self, url, **kw):
        return FakeResp(self.status, self.body)

    def post(self, url, json=None, **kw):
        self.posts.append((url, json))
        return FakeResp(self.status)


class NotifyTests(unittest.TestCase):
    def setUp(self):
        os.environ["STK_HOME"] = tempfile.mkdtemp()

    def test_save_load_and_send(self):
        self.assertFalse(notify.configured())
        with self.assertRaises(notify.NotifyError):
            notify.send("x")
        notify.save("123:ABC", 555)
        self.assertEqual(notify.load(), {"token": "123:ABC", "chat_id": "555"})
        s = FakeSession()
        notify.send("hello", session=s)
        url, body = s.posts[0]
        self.assertIn("bot123:ABC/sendMessage", url)
        self.assertEqual(body["chat_id"], "555")

    def test_http_error(self):
        notify.save("t", 1)
        with self.assertRaises(notify.NotifyError):
            notify.send("x", session=FakeSession(status=400))

    def test_find_chat_id(self):
        body = {"result": [{"message": {"chat": {"id": 42}}}, {"message": {"chat": {"id": 77}}}]}
        self.assertEqual(notify.find_chat_id("t", FakeSession(body=body)), "77")
        self.assertIsNone(notify.find_chat_id("t", FakeSession(body={"result": []})))
        with self.assertRaises(notify.NotifyError):
            notify.find_chat_id("t", FakeSession(status=401))

    def test_chunks(self):
        text = "\n".join(f"line {i}" for i in range(1500))
        parts = notify.chunks(text, 1000)
        self.assertTrue(all(len(p) <= 1000 for p in parts))
        self.assertEqual("\n".join(parts), text)
        self.assertEqual(notify.chunks("short"), ["short"])


class DailyTests(unittest.TestCase):
    def test_summary(self):
        c = conn_with(("UP", [100] * 30 + [110]), ("DOWN", [100] * 30 + [90]))
        queries.watch_add(c, ["UP"])
        c.execute("UPDATE indicators SET turnover20_cr = 50")
        text = daily.summary(c, [{"message": "UP hit target"}], min_turnover_cr=1)
        self.assertIn("🔔 Alerts", text)
        self.assertIn("UP hit target", text)
        self.assertIn("Top gainers", text)
        self.assertIn("UP", text.split("Top losers")[0])
        self.assertIn("DOWN", text.split("Top losers")[1])
        self.assertIn("Watchlist", text)

    def test_empty(self):
        self.assertIn("no data", daily.summary(conn_with()))


class HomeAlertTests(unittest.TestCase):
    def test_menu_five(self):
        f = home.command_for
        ask = lambda *a: (lambda p, it=iter(a): next(it))  # noqa: E731
        self.assertEqual(f("5", ask("")), ["alert", "list"])
        self.assertEqual(f("5", ask("c")), ["alert", "check"])
        self.assertEqual(f("5", ask("n", "TCS above 2200")), ["alert", "add", "TCS", "above", "2200"])

    def test_info_line(self):
        c = conn_with()
        self.assertIn("Alerts    none", "\n".join(home.info_lines(c)))
        alerts.add(c, "TCS", "above", 1)
        self.assertIn("1 watching", "\n".join(home.info_lines(c)))


if __name__ == "__main__":
    unittest.main()
