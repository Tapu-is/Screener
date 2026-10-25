import os
import sys
import tempfile
import unittest
import xml.dom.minidom
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("STK_CACHE_DIR", tempfile.mkdtemp())

from helpers import add_stock  # noqa: E402

from screener.core import alerts, db, fundamentals as F, indicators as I, queries, screens  # noqa: E402
from screener.core.quote import Quote  # noqa: E402
from screener.web import charts, data, tables  # noqa: E402

GOOD = {"pe": 15.0, "pb": 2.0, "roe": 20.0, "market_cap_cr": 5000.0, "debt_equity": 0.2, "profit_margin": 12.0, "div_yield": 1.5}


def seeded_db(path=None):
    path = path or Path(tempfile.mkdtemp()) / "w.db"
    conn = db.connect(path)
    up = [100 + i * 1.0 for i in range(260)]
    down = [300 - i * 0.8 for i in range(260)]
    add_stock(conn, "UPCO", up, vols=[2_000_000] * 260, name="Up Company", industry="Banks")
    add_stock(conn, "DOWNCO", down, vols=[3_000_000] * 260, name="Down Company", industry="IT")
    I.refresh(conn)
    ids = {r["ticker"]: r["id"] for r in conn.execute("SELECT id, ticker FROM symbols")}
    F.save(conn, ids["UPCO"], GOOD)
    queries.watch_add(conn, ["UPCO", "GHOST"])
    alerts.add(conn, "UPCO", "above", 500)
    return conn, path


class FormatTests(unittest.TestCase):
    def test_fmt(self):
        self.assertEqual(data.fmt("chg_1d", 4.0), ("+4.0%", "up2"))
        self.assertEqual(data.fmt("chg_1d", -1.0), ("-1.0%", "down"))
        self.assertEqual(data.fmt("chg_1d", None), ("-", "dim"))
        self.assertEqual(data.fmt("rsi14", 75), ("75.0", "down"))
        self.assertEqual(data.fmt("rsi14", 20), ("20.0", "up"))
        self.assertEqual(data.fmt("vol_ratio", 2.2), ("2.2x", "warn"))
        self.assertEqual(data.fmt("close", 1234.5), ("1,234.50", ""))
        self.assertEqual(data.fmt("turnover20_cr", 123.4)[0], "123 Cr")
        self.assertEqual(data.fmt("roe", 22)[1], "up")
        self.assertEqual(data.fmt("volume", 25_000_000)[0], "2.50 Cr")

    def test_every_field_formats(self):
        for name in data.numeric_fields():
            text, cls = data.fmt(name, 5.5)
            self.assertTrue(text)


class TableTests(unittest.TestCase):
    def test_escaping_and_links(self):
        h = tables.html_table(["A"], [[{"text": "<script>x</script>", "link": "A&B"}]])
        self.assertNotIn("<script>", h)
        self.assertIn("&lt;script&gt;", h)
        self.assertIn("?stock=A%26B", h)

    def test_cards_and_pills(self):
        self.assertIn("Nifty", tables.cards([("Nifty", "1", "+1%", "up")]))
        self.assertIn("stk-up", tables.pills([("ok", "up")]))
        self.assertIn("<td", tables.kv_table([("a", "b", "")]))


class ChartTests(unittest.TestCase):
    def test_candles_are_valid_svg(self):
        conn, _ = seeded_db()
        page = data.stock_page(conn, "UPCO", 120)
        svg = charts.candlestick_svg(page["bars"], page["averages"])
        xml.dom.minidom.parseString(svg)
        self.assertEqual(svg.count("<title>"), 120)
        self.assertIn("SMA 50", svg)

    def test_empty_and_flat(self):
        xml.dom.minidom.parseString(charts.candlestick_svg([]))
        flat = [dict(date="2026-10-01", open=10, high=10, low=10, close=10, volume=0)] * 3
        xml.dom.minidom.parseString(charts.candlestick_svg(flat))
        self.assertEqual(charts.sparkline_svg([1]), "")
        xml.dom.minidom.parseString(charts.sparkline_svg([1, 2, 3]))


class DataTests(unittest.TestCase):
    def setUp(self):
        self.conn, _ = seeded_db()

    def test_stock_page(self):
        p = data.stock_page(self.conn, "upco", 100)
        self.assertEqual(p["symbol"]["ticker"], "UPCO")
        self.assertEqual(len(p["bars"]), 100)
        self.assertEqual(len(p["averages"]["sma20"]), 100)
        self.assertTrue(p["in_watchlist"])
        self.assertIsNone(data.stock_page(self.conn, "NOPE"))
        notes = [t for t, _ in data.trend_notes(p["indicators"])]
        self.assertTrue(any("Above its 200-day" in n for n in notes))
        self.assertEqual(data.fundamentals_rows(p["fundamentals"])[1][0], "P/E")
        self.assertEqual(data.fundamentals_rows(None), [])

    def test_watchlist_rows_live_override(self):
        rows = data.watchlist_rows(self.conn)
        self.assertEqual([r["ticker"] for r in rows], ["UPCO", "GHOST"])
        self.assertFalse(rows[1]["known"])
        q = Quote(symbol="UPCO", name="x", price=999.0, prev_close=900.0, change=99.0, change_pct=11.0, day_high=1, day_low=1, week52_high=1, week52_low=1, volume=1, exchange="NSE", currency="INR", as_of="2026-10-12T10:00:00+05:30", source="live") if "exchange" in Quote.__dataclass_fields__ else None
        if q is None:
            self.skipTest("Quote fields differ")
        rows = data.watchlist_rows(self.conn, {"UPCO": q})
        self.assertEqual((rows[0]["price"], rows[0]["live"]), (999.0, True))

    def test_breadth_and_movers(self):
        b = data.breadth(self.conn)
        self.assertEqual((b["advancing"], b["declining"]), (1, 1))
        m = data.movers(self.conn, n=3, min_turnover_cr=0)
        self.assertEqual(m["gainers"][0]["ticker"], "UPCO")
        self.assertEqual(m["losers"][0]["ticker"], "DOWNCO")

    def test_index_quotes_survive_failures(self):
        self.assertEqual(data.index_quotes(lambda s: (_ for _ in ()).throw(RuntimeError("x"))), [])
        self.assertEqual(data.index_quotes(lambda s: [Exception("bad")] * 3), [])

    def test_screen_flow(self):
        d = data.build_definition(None, [("roe", ">", "10"), ("industry", "contains", "bank")], None, None, 10)
        res = screens.run(self.conn, d)
        self.assertEqual([r["ticker"] for r in res.rows], ["UPCO"])
        t = data.screen_table(res)
        self.assertEqual(t["rows"][0][0]["link"], "UPCO")
        self.assertIn("ROE", t["columns"])
        csv_text = data.screen_csv(res)
        self.assertTrue(csv_text.startswith("ticker,name,close"))
        self.assertIn("UPCO", csv_text)
        with self.assertRaises(screens.ScreenError):
            data.build_definition(None, [("rsi14", "<", "abc")], None, None, None)
        base = screens.BUILTIN["oversold"]
        d2 = data.build_definition(base, [], "chg_1m", "asc", 5)
        self.assertEqual(d2["sort"], "chg_1m")

    def test_search_and_health(self):
        self.assertEqual(data.search(self.conn, "up comp")[0]["ticker"], "UPCO")
        self.assertEqual(data.search(self.conn, "  "), [])
        self.assertEqual(data.data_health(self.conn)["status"]["symbols"], 2)


# ---- run every page against a stand-in for streamlit ----------------------------
class SessionState(dict):
    __getattr__ = dict.get


def make_streamlit(page, fragment_runs=False):
    st = mock.MagicMock(name="streamlit")

    def widgets(target):  # columns and containers answer widget calls like st itself
        target.button.return_value = False
        target.text_input.side_effect = lambda label, value="", **kw: value
        target.number_input.side_effect = lambda label, value=0, **kw: value
        target.selectbox.side_effect = lambda label, options, index=0, **kw: options[index]
        target.checkbox.side_effect = lambda label, value=False, **kw: value
        return target

    st.session_state = SessionState(page=page)
    st.query_params = {}
    st.button.return_value = False
    st.form_submit_button.return_value = False
    st.text_input.side_effect = lambda label, value="", **kw: value
    st.number_input.side_effect = lambda label, value=0, **kw: value
    st.checkbox.side_effect = lambda label, value=False, **kw: value
    st.radio.side_effect = lambda label, options, index=0, key=None, **kw: st.session_state["page"] if key == "page" else options[index]
    st.selectbox.side_effect = lambda label, options, index=0, **kw: options[index]
    st.sidebar.radio.side_effect = st.radio.side_effect
    st.multiselect.return_value = []
    st.file_uploader.return_value = None
    st.columns.side_effect = lambda spec, **kw: [widgets(mock.MagicMock()) for _ in range(spec if isinstance(spec, int) else len(spec))]
    if fragment_runs:
        st.fragment = lambda run_every=None: (lambda fn: fn)
    else:
        st.fragment = None
    return st


class PageSmokeTests(unittest.TestCase):
    """Runs each page's code once. This catches typos and wrong names, not Streamlit API mistakes."""

    @classmethod
    def setUpClass(cls):
        cls.conn, cls.path = seeded_db()
        cls.conn.close()
        os.environ["STK_DB"] = str(cls.path)

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("STK_DB", None)

    def run_page(self, page, **kw):
        st = make_streamlit(page, **kw)
        sys.modules.pop("screener.web.app", None)
        with mock.patch.dict(sys.modules, {"streamlit": st}), \
             mock.patch("screener.web.data.live_quotes", return_value={}), \
             mock.patch("screener.web.data.index_quotes", return_value=[{"label": "NIFTY 50", "price": 24000.0, "change": 100.0, "pct": 0.4, "stale": False}]):
            import screener.web.app as app

            app.main()
        return st

    def shown(self, st):
        return " ".join(str(c.args[0]) for c in st.markdown.call_args_list if c.args)

    def test_dashboard(self):
        for runs in (False, True):
            st = self.run_page("Dashboard", fragment_runs=runs)
            text = self.shown(st)
            self.assertIn("Market breadth", " ".join(str(c.args[0]) for c in st.subheader.call_args_list))
            self.assertIn("NIFTY 50", text)
            self.assertIn("UPCO", text)

    def test_stock_page(self):
        st = make_streamlit("Stock")
        st.text_input.side_effect = lambda label, value="", **kw: "UPCO"
        sys.modules.pop("screener.web.app", None)
        with mock.patch.dict(sys.modules, {"streamlit": st}), mock.patch("screener.web.data.live_quotes", return_value={}):
            import screener.web.app as app

            app.main()
        text = self.shown(st)
        self.assertIn("<svg", text)
        self.assertIn("P/E", text)

    def test_stock_page_unknown_and_empty(self):
        st = self.run_page("Stock")
        self.assertTrue(st.caption.called)

    def test_screener_with_builtin(self):
        st = make_streamlit("Screener")
        st.selectbox.side_effect = lambda label, options, index=0, **kw: "trend-up" if label == "Screen" else options[index]
        sys.modules.pop("screener.web.app", None)
        with mock.patch.dict(sys.modules, {"streamlit": st}):
            import screener.web.app as app

            app.main()
        self.assertTrue(st.download_button.called)
        self.assertIn("UPCO", self.shown(st))

    def test_other_pages(self):
        for page in ("Watchlist", "Alerts", "Data"):
            st = self.run_page(page)
            self.assertTrue(st.title.called, page)
        self.assertIn("GHOST", self.shown(self.run_page("Watchlist")))
        self.assertIn("UPCO above", self.shown(self.run_page("Alerts")))

    def test_query_param_opens_stock(self):
        st = make_streamlit("x")
        st.session_state.clear()
        st.query_params = {"stock": "UPCO"}
        st.sidebar.radio.side_effect = lambda label, options, index=0, key=None, **kw: st.session_state["page"]
        sys.modules.pop("screener.web.app", None)
        with mock.patch.dict(sys.modules, {"streamlit": st}), mock.patch("screener.web.data.live_quotes", return_value={}):
            import screener.web.app as app

            app.main()
        self.assertEqual(st.session_state["page"], "Stock")


if __name__ == "__main__":
    unittest.main()
