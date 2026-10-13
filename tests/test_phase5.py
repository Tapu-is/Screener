import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("STK_CACHE_DIR", tempfile.mkdtemp())
os.environ["STK_FUND_URL"] = "http://fund.test"

from helpers import add_stock  # noqa: E402

from screener.cli import home  # noqa: E402
from screener.core import db, fundamentals as F, indicators as I, quote, screens  # noqa: E402
from screener.core.history import HistoryError, NotFound, RateLimited  # noqa: E402
from screener.core.quote import IST  # noqa: E402


def payload(**over):
    body = {
        "price": {"marketCap": {"raw": 2.0e13}},
        "summaryDetail": {"trailingPE": {"raw": 24.5, "fmt": "24.5"}, "dividendYield": {"raw": 0.0123}},
        "defaultKeyStatistics": {"priceToBook": {"raw": 2.34567}, "trailingEps": {"raw": 99.9}},
        "financialData": {
            "returnOnEquity": {"raw": 0.1234}, "returnOnAssets": {"raw": 0.05}, "profitMargins": {"raw": 0.0987},
            "operatingMargins": {"raw": 0.2}, "debtToEquity": {"raw": 45.0},
            "revenueGrowth": {"raw": 0.11}, "earningsGrowth": {},
        },
    }
    body.update(over)
    return {"quoteSummary": {"result": [body], "error": None}}


class Resp:
    def __init__(self, status=200, body=None, text=""):
        self.status_code, self._body, self.text = status, body, text

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeSession:
    def __init__(self, routes):
        self.routes = routes  # substring -> Resp or list of Resp (consumed in order)
        self.headers = {}
        self.calls = []

    def get(self, url, **kw):
        self.calls.append((url, kw.get("params")))
        for key, val in self.routes.items():
            if key in url:
                if isinstance(val, list):
                    return val.pop(0) if len(val) > 1 else val[0]
                return val
        return Resp(404)


class ParseTests(unittest.TestCase):
    def test_parse_units(self):
        v = F.parse(payload())
        self.assertEqual(v["market_cap_cr"], 2000000.0)  # 2e13 rupees = 20 lakh crore
        self.assertEqual(v["pe"], 24.5)
        self.assertEqual(v["pb"], 2.35)
        self.assertEqual(v["div_yield"], 1.23)
        self.assertEqual(v["roe"], 12.34)
        self.assertEqual(v["debt_equity"], 0.45)
        self.assertIsNone(v["earn_growth"])  # empty object = no data

    def test_missing_modules_are_none(self):
        v = F.parse({"quoteSummary": {"result": [{"price": {}}], "error": None}})
        self.assertTrue(all(x is None for x in v.values()))

    def test_not_found(self):
        with self.assertRaises(NotFound):
            F.parse({"quoteSummary": {"result": None, "error": {"code": "Not Found", "description": "x"}}})
        with self.assertRaises(NotFound):
            F.parse({"quoteSummary": {"result": [], "error": None}})
        with self.assertRaises(HistoryError):
            F.parse({"quoteSummary": {"result": None, "error": {"code": "Internal", "description": "boom"}}})


class ClientTests(unittest.TestCase):
    def test_fetch_gets_crumb_once(self):
        s = FakeSession({"getcrumb": Resp(200, text="abc123"), "quoteSummary": Resp(200, payload())})
        c = F.Client(s)
        c.fetch("TCS.NS")
        c.fetch("INFY.NS")
        crumbs = [u for u, _ in s.calls if "getcrumb" in u]
        self.assertEqual(len(crumbs), 1)
        self.assertEqual(s.calls[-1][1]["crumb"], "abc123")

    def test_expired_crumb_is_renewed_once(self):
        s = FakeSession({
            "getcrumb": [Resp(200, text="old"), Resp(200, text="new")],
            "quoteSummary": [Resp(401), Resp(200, payload())],
        })
        v = F.Client(s).fetch("TCS.NS")
        self.assertEqual(v["pe"], 24.5)
        self.assertEqual(s.calls[-1][1]["crumb"], "new")

    def test_errors(self):
        for status, exc in ((404, NotFound), (429, RateLimited), (500, HistoryError)):
            s = FakeSession({"getcrumb": Resp(200, text="c"), "quoteSummary": Resp(status)})
            with self.assertRaises(exc):
                F.Client(s).fetch("X.NS")

    def test_bad_crumb(self):
        s = FakeSession({"getcrumb": Resp(200, text="<html>nope</html>")})
        with self.assertRaises(HistoryError):
            F.Client(s).login()

    def test_probe_reports_each_step(self):
        s = FakeSession({"getcrumb": Resp(200, text="c"), "quoteSummary": Resp(200, payload())})
        steps = F.probe(F.Client(s))
        self.assertTrue(all(ok for _, ok, _ in steps))
        s = FakeSession({"getcrumb": Resp(429)})
        steps = F.probe(F.Client(s))
        self.assertEqual(len(steps), 1)
        self.assertFalse(steps[0][1])


class FakeClient:
    def __init__(self, data):
        self.data, self.calls = data, []

    def fetch(self, yahoo):
        self.calls.append(yahoo)
        r = self.data[yahoo]
        if isinstance(r, Exception):
            raise r
        return r


NOW = datetime(2026, 10, 13, 12, 0, tzinfo=IST)
GOOD = {"pe": 15.0, "pb": 2.0, "roe": 20.0, "market_cap_cr": 5000.0, "debt_equity": 0.2, "profit_margin": 12.0}


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(Path(tempfile.mkdtemp()) / "t.db")
        for t in ("AAA", "BBB", "CCC"):
            add_stock(self.conn, t, [100 + i for i in range(30)])
        self.nosleep = lambda s: None

    def run_refresh(self, client, **kw):
        return F.refresh(self.conn, client=client, sleep=self.nosleep, now=NOW, **kw)

    def test_downloads_and_stores(self):
        c = FakeClient({"AAA.NS": GOOD, "BBB.NS": GOOD, "CCC.NS": GOOD})
        res = self.run_refresh(c)
        self.assertEqual(res.ok, 3)
        sid = self.conn.execute("SELECT id FROM symbols WHERE ticker='AAA'").fetchone()[0]
        self.assertEqual(F.get(self.conn, sid)["pe"], 15.0)

    def test_recent_are_skipped_unless_forced(self):
        c = FakeClient({"AAA.NS": GOOD, "BBB.NS": GOOD, "CCC.NS": GOOD})
        self.run_refresh(c)
        c2 = FakeClient({"AAA.NS": GOOD, "BBB.NS": GOOD, "CCC.NS": GOOD})
        res = self.run_refresh(c2)
        self.assertEqual((res.ok, res.skipped), (0, 3))
        self.assertEqual(c2.calls, [])
        res = self.run_refresh(c2, force=True)
        self.assertEqual(res.ok, 3)

    def test_only_named_tickers(self):
        c = FakeClient({"AAA.NS": GOOD})
        res = self.run_refresh(c, tickers=["aaa", "nope"])
        self.assertEqual((res.ok, c.calls), (1, ["AAA.NS"]))

    def test_not_found_is_remembered(self):
        c = FakeClient({"AAA.NS": NotFound("x"), "BBB.NS": GOOD, "CCC.NS": GOOD})
        res = self.run_refresh(c)
        self.assertEqual((res.ok, res.empty), (2, 1))
        self.assertEqual(self.run_refresh(FakeClient({})).skipped, 3)  # not asked again this week

    def test_aborts_after_three_transient_failures(self):
        for t in ("DDD", "EEE"):
            add_stock(self.conn, t, [100 + i for i in range(30)])
        err = HistoryError("down", transient=True)
        c = FakeClient({f"{t}.NS": err for t in ("AAA", "BBB", "CCC", "DDD", "EEE")})
        res = self.run_refresh(c)
        self.assertTrue(res.aborted)
        self.assertEqual(len(res.failed), 3)
        self.assertEqual(len(c.calls), 3)

    def test_rate_limit_cools_down_then_retries(self):
        sleeps = []

        class Flaky:
            n = 0

            def fetch(self, y):
                Flaky.n += 1
                if Flaky.n == 1:
                    raise RateLimited("429")
                return GOOD

        res = F.refresh(self.conn, ["AAA"], client=Flaky(), sleep=sleeps.append, now=NOW, cooldown=7)
        self.assertEqual(res.ok, 1)
        self.assertIn(7, sleeps)


class ScreenTests(unittest.TestCase):
    def test_value_screen_uses_fundamentals(self):
        conn = db.connect(Path(tempfile.mkdtemp()) / "s.db")
        for t in ("CHEAP", "PRICEY", "NODATA"):
            add_stock(conn, t, [100 + i for i in range(30)], vols=[2_000_000] * 30)
        I.refresh(conn)
        ids = {r["ticker"]: r["id"] for r in conn.execute("SELECT id, ticker FROM symbols")}
        F.save(conn, ids["CHEAP"], GOOD)
        F.save(conn, ids["PRICEY"], dict(GOOD, pe=80.0))
        res = screens.run(conn, screens.BUILTIN["value"])
        self.assertEqual([r["ticker"] for r in res.rows], ["CHEAP"])  # NODATA never matches
        self.assertTrue(screens.uses_fundamentals(screens.BUILTIN["value"]))
        self.assertFalse(screens.uses_fundamentals(screens.BUILTIN["oversold"]))
        # any custom condition works too
        d = screens.merge(None, [{"field": "roe", "op": ">", "value": 10}], sort=None, order=None, limit=None)
        self.assertEqual(screens.run(conn, d).total, 2)

    def test_coverage(self):
        conn = db.connect(Path(tempfile.mkdtemp()) / "c.db")
        add_stock(conn, "ONE", [100, 101])
        self.assertEqual(F.coverage(conn)["stored"], 0)
        F.save(conn, 1, GOOD)
        cov = F.coverage(conn)
        self.assertEqual((cov["stored"], cov["with_pe"]), (1, 1))


class LiveQuoteTests(unittest.TestCase):
    def test_get_quotes_keeps_order_and_isolates_failures(self):
        def fake(sym, prefer_bse=False, use_cache=True):
            if sym == "BAD":
                raise quote.QuoteError("nope")
            return sym

        with mock.patch.object(quote, "get_quote", fake):
            out = quote.get_quotes(["A", "BAD", "C", "D", "E"])
        self.assertEqual([o if isinstance(o, str) else "ERR" for o in out], ["A", "ERR", "C", "D", "E"])

    def test_no_cache_flag_is_passed_through(self):
        seen = []
        with mock.patch.object(quote, "get_quote", lambda s, b=False, use_cache=True: seen.append(use_cache) or s):
            quote.get_quotes(["A", "B"], use_cache=False)
        self.assertEqual(seen, [False, False])


class LogoTests(unittest.TestCase):
    def test_eight_candles_same_space(self):
        self.assertEqual(len(home.CANDLE_SPEC), 8)
        self.assertEqual(home.CANDLE_ROWS, 6)
        self.assertEqual(home.CANDLE_WIDTH, 15)
        lines = home.logo_lines(color=False)
        self.assertEqual(len(lines), 6)
        self.assertLessEqual(max(len(l) for l in lines), home.LOGO_WIDTH)
        joined = "".join(lines)
        self.assertGreaterEqual(joined.count("█"), 10)

    def test_colors_per_candle(self):
        colored = "".join(home.logo_lines(color=True))
        self.assertIn("[red]", colored)
        self.assertIn("[green]", colored)


if __name__ == "__main__":
    unittest.main()
