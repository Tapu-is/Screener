import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("STK_CACHE_DIR", tempfile.mkdtemp())

from helpers import add_stock  # noqa: E402

from screener.core import db, indicators as I, screens as S  # noqa: E402


def fresh_db():
    return db.connect(Path(tempfile.mkdtemp()) / "t.db")


def put(conn, ticker, industry="Test", **vals):
    """Insert a symbol plus a hand-written indicators row."""
    with conn:
        conn.execute(
            "INSERT INTO symbols (ticker, yahoo, name, industry, exchange, active, added_at) "
            "VALUES (?, ?, ?, ?, 'NSE', 1, '2026-10-09')",
            (ticker, f"{ticker}.NS", f"{ticker} Ltd", industry),
        )
        sid = conn.execute("SELECT id FROM symbols WHERE ticker = ?", (ticker,)).fetchone()[0]
        vals = {"as_of": "2026-10-09", "close": 100.0, "turnover20_cr": 10.0, **vals}
        I.save(conn, sid, vals)
    return sid


class ValidateTests(unittest.TestCase):
    def test_all_builtins_are_valid(self):
        for name, defn in S.BUILTIN.items():
            S.validate(defn)
            self.assertTrue(defn["description"], name)
            self.assertLessEqual(len(defn["show"]), 3)

    def test_errors_are_helpful(self):
        cases = [
            ({}, "at least one condition"),
            ({"all": []}, "non-empty"),
            ({"all": [{"field": "rsi", "op": "<", "value": 3}]}, 'Did you mean "rsi14"'),
            ({"all": [{"field": "rsi14", "op": "=<", "value": 3}]}, "Unknown operator"),
            ({"all": [{"field": "rsi14", "op": "<", "value": "x"}]}, "number"),
            ({"all": [{"field": "rsi14", "op": "<", "value": True}]}, "number"),
            ({"all": [{"field": "close", "op": ">", "ref": "nope"}]}, "Unknown field"),
            ({"all": [{"field": "industry", "op": "<", "value": "a"}]}, "text"),
            ({"all": [{"field": "rsi14", "op": "<", "value": 3}], "sort": "nope"}, "Unknown field"),
            ({"all": [{"field": "rsi14", "op": "<", "value": 3}], "order": "up"}, "asc"),
            ({"all": [{"field": "rsi14", "op": "<", "value": 3}], "limit": 0}, "limit"),
            ({"all": [{"field": "rsi14", "op": "<", "value": 3}], "show": ["a", "b", "c", "d"]}, "at most 3"),
            ([], "JSON object"),
        ]
        for defn, fragment in cases:
            with self.assertRaises(S.ScreenError, msg=str(defn)) as cm:
                S.validate(defn)
            self.assertIn(fragment, str(cm.exception), str(defn))


class EvaluateTests(unittest.TestCase):
    row = {"ticker": "ABC", "name": "ABC Bank Ltd", "industry": "Banks", "close": 110.0,
           "sma50": 100.0, "sma200": 120.0, "rsi14": 25.0, "vol_ratio": None}

    def ev(self, node):
        S.validate({"all": [node]})
        return S.matches(self.row, {"all": [node]})

    def test_value_comparisons(self):
        self.assertTrue(self.ev({"field": "rsi14", "op": "<", "value": 30}))
        self.assertFalse(self.ev({"field": "rsi14", "op": ">", "value": 30}))
        self.assertTrue(self.ev({"field": "rsi14", "op": "<=", "value": 25}))
        self.assertTrue(self.ev({"field": "rsi14", "op": "==", "value": 25}))
        self.assertTrue(self.ev({"field": "rsi14", "op": "!=", "value": 26}))

    def test_field_to_field_and_multiplier(self):
        self.assertTrue(self.ev({"field": "close", "op": ">", "ref": "sma50"}))
        self.assertFalse(self.ev({"field": "close", "op": ">", "ref": "sma200"}))
        self.assertTrue(self.ev({"field": "close", "op": ">", "ref": "sma50", "mult": 1.05}))
        self.assertFalse(self.ev({"field": "close", "op": ">", "ref": "sma50", "mult": 1.2}))

    def test_missing_values_never_match(self):
        self.assertFalse(self.ev({"field": "vol_ratio", "op": ">", "value": 0}))
        self.assertFalse(self.ev({"field": "vol_ratio", "op": "<", "value": 99}))
        self.assertFalse(self.ev({"field": "vol_ratio", "op": "!=", "value": 3}))
        self.assertFalse(self.ev({"field": "close", "op": ">", "ref": "vol_ratio"}))

    def test_text_fields_ignore_case(self):
        self.assertTrue(self.ev({"field": "industry", "op": "contains", "value": "bank"}))
        self.assertTrue(self.ev({"field": "ticker", "op": "==", "value": "abc"}))
        self.assertFalse(self.ev({"field": "name", "op": "contains", "value": "steel"}))

    def test_nesting(self):
        good, bad = {"field": "rsi14", "op": "<", "value": 30}, {"field": "rsi14", "op": ">", "value": 30}
        self.assertTrue(S.matches(self.row, {"all": [good], "any": [bad, good]}))
        self.assertFalse(S.matches(self.row, {"all": [good], "any": [bad]}))
        self.assertTrue(S.matches(self.row, {"all": [{"any": [bad, good]}, good]}))
        self.assertFalse(S.matches(self.row, {"all": [good, bad]}))

    def test_describe(self):
        d = {"all": [{"field": "rsi14", "op": "<", "value": 30},
                     {"field": "close", "op": ">", "ref": "sma50", "mult": 1.05},
                     {"field": "industry", "op": "contains", "value": "bank"}]}
        self.assertEqual(S.describe(d), 'rsi14 < 30 and close > sma50 x 1.05 and industry contains "bank"')


class ParseTests(unittest.TestCase):
    def test_parse_where(self):
        self.assertEqual(S.parse_where("rsi14<30"), {"field": "rsi14", "op": "<", "value": 30})
        self.assertEqual(S.parse_where(" rsi14 >= 55.5 "), {"field": "rsi14", "op": ">=", "value": 55.5})
        self.assertEqual(S.parse_where("chg_1m<-5"), {"field": "chg_1m", "op": "<", "value": -5})
        self.assertEqual(S.parse_where("close>sma200"), {"field": "close", "op": ">", "ref": "sma200"})
        self.assertEqual(S.parse_where("CLOSE > SMA50 * 1.02"),
                         {"field": "close", "op": ">", "ref": "sma50", "mult": 1.02})
        self.assertEqual(S.parse_where("industry~Bank"), {"field": "industry", "op": "contains", "value": "Bank"})
        self.assertEqual(S.parse_where('name == "TCS"')["value"], "TCS")

    def test_parse_where_errors(self):
        for bad, fragment in [("rsi<30", "rsi14"), ("rsi14", "Could not read"), ("rsi14<abc", "Unknown field"),
                              ("rsi14<nan", "usable number"), ("rsi14~3", "text fields"), ("", "Could not read"),
                              ("industry<3", "text")]:
            with self.assertRaises(S.ScreenError, msg=bad) as cm:
                S.parse_where(bad)
            self.assertIn(fragment, str(cm.exception), bad)

    def test_conditions_from_options(self):
        c = S.conditions_from_options(rsi_below=30, rsi_above=10, above_sma=200, below_sma=20,
                                      min_volume_ratio=1.5, min_turnover=5, industry="bank", where=["chg_1m<-5"])
        self.assertEqual(len(c), 8)
        self.assertIn({"field": "close", "op": ">", "ref": "sma200"}, c)
        self.assertIn({"field": "close", "op": "<", "ref": "sma20"}, c)
        self.assertEqual(S.conditions_from_options(), [])
        with self.assertRaises(S.ScreenError):
            S.conditions_from_options(above_sma=100)

    def test_merge(self):
        base = S.BUILTIN["oversold"]
        merged = S.merge(base, [S.parse_where("close>sma200")], sort="chg_1m", order="desc", limit=5)
        self.assertEqual(len(merged["all"]), 2)
        self.assertEqual((merged["sort"], merged["order"], merged["limit"]), ("chg_1m", "desc", 5))
        self.assertEqual(len(base["all"]), 1)  # the original is untouched
        self.assertEqual(S.merge(None, [S.parse_where("rsi14<30")])["all"][0]["field"], "rsi14")


class RunTests(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_db()
        put(self.conn, "LOW", rsi14=20.0, chg_1d=1.0, golden_cross_days=3, close=50.0, sma200=40.0, sma50=45.0,
            vol_ratio=2.5, prior_high52=49.0, turnover20_cr=50.0, industry="Banks")
        put(self.conn, "MID", rsi14=29.0, chg_1d=-2.0, death_cross_days=2, close=60.0, sma200=70.0, sma50=65.0,
            vol_ratio=0.8, turnover20_cr=0.5, industry="IT")
        put(self.conn, "HIGH", rsi14=75.0, chg_1d=3.0, golden_cross_days=40, close=90.0, sma200=60.0, sma50=80.0,
            vol_ratio=1.0, macd_cross_up_days=1, turnover20_cr=5.0, industry="Auto")
        put(self.conn, "EMPTY", chg_1d=None, turnover20_cr=None)  # missing values everywhere
        with self.conn:
            self.conn.execute("INSERT INTO symbols (ticker, yahoo, exchange, active, added_at) "
                              "VALUES ('OFF', 'OFF.NS', 'NSE', 0, 'x')")

    def tickers(self, name, **kw):
        defn = S.merge(S.BUILTIN[name], [], **kw)
        return [r["ticker"] for r in S.run(self.conn, defn).all_rows]

    def test_builtins_pick_the_expected_stocks(self):
        self.assertEqual(self.tickers("oversold"), ["LOW", "MID"])  # RSI ascending
        self.assertEqual(self.tickers("overbought"), ["HIGH"])
        self.assertEqual(self.tickers("golden-cross"), ["LOW"])  # HIGH crossed 40 sessions ago
        self.assertEqual(self.tickers("death-cross"), ["MID"])
        self.assertEqual(self.tickers("breakout"), ["LOW"])  # only one with a prior high below its close
        self.assertEqual(self.tickers("volume-surge"), ["LOW"])
        self.assertEqual(self.tickers("macd-bullish"), ["HIGH"])
        self.assertEqual(sorted(self.tickers("trend-up")), ["HIGH", "LOW"])
        self.assertEqual(self.tickers("gainers"), ["LOW", "HIGH"][::-1])  # HIGH +3 first, LOW +1
        self.assertEqual(self.tickers("losers"), [])  # MID is down but illiquid (0.5 Cr)

    def test_inactive_and_empty_rows_never_appear(self):
        for name in S.BUILTIN:
            self.assertNotIn("OFF", self.tickers(name))
            self.assertNotIn("EMPTY", self.tickers(name))

    def test_sort_limit_and_missing_values_last(self):
        defn = {"all": [{"field": "close", "op": ">", "value": 0}], "sort": "rsi14", "order": "asc", "limit": 3}
        res = S.run(self.conn, defn)
        self.assertEqual([r["ticker"] for r in res.rows], ["LOW", "MID", "HIGH"])
        self.assertEqual(res.total, 4)
        self.assertEqual(res.all_rows[-1]["ticker"], "EMPTY")  # empty RSI sorts last
        defn["order"] = "desc"
        self.assertEqual([r["ticker"] for r in S.run(self.conn, defn).all_rows][:3], ["HIGH", "MID", "LOW"])

    def test_default_sort_is_most_traded_and_show_gets_the_sort_field(self):
        res = S.run(self.conn, {"all": [{"field": "close", "op": ">", "value": 0}]})
        self.assertEqual([r["ticker"] for r in res.all_rows], ["LOW", "HIGH", "MID", "EMPTY"])
        self.assertEqual(res.sort, "turnover20_cr")
        self.assertEqual(res.show, ["chg_1d", "rsi14", "turnover20_cr"])
        self.assertEqual(res.as_of, "2026-10-09")

    def test_text_filter_and_extra_conditions(self):
        defn = S.merge(S.BUILTIN["oversold"], S.conditions_from_options(industry="bank"))
        self.assertEqual([r["ticker"] for r in S.run(self.conn, defn).all_rows], ["LOW"])

    def test_default_limit(self):
        for i in range(40):
            put(self.conn, f"X{i:02d}", rsi14=10.0)
        res = S.run(self.conn, {"all": [{"field": "rsi14", "op": "<", "value": 30}]})
        self.assertEqual(len(res.rows), S.DEFAULT_LIMIT)
        self.assertEqual(res.total, 42)

    def test_no_stocks_at_all(self):
        res = S.run(fresh_db(), S.BUILTIN["oversold"])
        self.assertEqual((res.total, res.rows, res.as_of), (0, [], None))


class SavedScreenTests(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_db()
        self.defn = S.merge(None, S.conditions_from_options(rsi_below=30, above_sma=200), sort="rsi14", order="asc")

    def test_save_get_list_delete(self):
        self.assertEqual(S.save(self.conn, "My-Dip", self.defn), "created")
        self.assertEqual(S.save(self.conn, "my-dip", self.defn), "updated")
        defn, source = S.get(self.conn, "MY-DIP")
        self.assertEqual((defn, source), (self.defn, "saved"))
        listing = {i["name"]: i for i in S.list_all(self.conn)}
        self.assertEqual(listing["my-dip"]["source"], "saved")
        self.assertIn("rsi14 < 30", listing["my-dip"]["description"])
        self.assertIn("oversold", listing)
        self.assertTrue(S.delete(self.conn, "my-dip"))
        self.assertFalse(S.delete(self.conn, "my-dip"))

    def test_get_builtin_returns_a_copy(self):
        defn, source = S.get(self.conn, "Oversold")
        self.assertEqual(source, "built-in")
        defn["all"].append({"field": "close", "op": ">", "value": 1})
        self.assertEqual(len(S.BUILTIN["oversold"]["all"]), 1)

    def test_unknown_name_suggests(self):
        with self.assertRaises(S.ScreenError) as cm:
            S.get(self.conn, "oversol")
        self.assertIn("oversold", str(cm.exception))

    def test_name_rules(self):
        for bad in ("", "Has Space", "-start", "x" * 31, "oversold", "save", "list", "fields"):
            with self.assertRaises(S.ScreenError, msg=bad):
                S.save(self.conn, bad, self.defn)
        with self.assertRaises(S.ScreenError):
            S.delete(self.conn, "oversold")

    def test_invalid_definition_is_not_saved(self):
        with self.assertRaises(S.ScreenError):
            S.save(self.conn, "bad", {"all": [{"field": "zzz", "op": "<", "value": 1}]})
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM saved_screens").fetchone()[0], 0)

    def test_damaged_saved_screen_is_reported(self):
        with self.conn:
            self.conn.execute("INSERT INTO saved_screens VALUES ('broken', '{not json', 'x')")
        with self.assertRaises(S.ScreenError) as cm:
            S.get(self.conn, "broken")
        self.assertIn("damaged", str(cm.exception))
        self.assertTrue(any(i["name"] == "broken" for i in S.list_all(self.conn)))


class EndToEndTests(unittest.TestCase):
    """Real prices -> indicators -> screens."""

    def test_prices_flow_into_the_right_screens(self):
        conn = fresh_db()
        add_stock(conn, "DROP", [100.0] * 270 + [100 * 0.98 ** (i + 1) for i in range(30)])
        add_stock(conn, "RISE", [100 + 0.5 * i for i in range(300)])
        add_stock(conn, "SPIKE", [100.0] * 300, vols=[1_000_000] * 299 + [4_000_000])
        add_stock(conn, "BREAK", [100.0] * 299 + [106.0])
        I.refresh(conn)

        def names(defn):
            return [r["ticker"] for r in S.run(conn, S.merge(defn, [])).all_rows]

        self.assertEqual(names(S.BUILTIN["oversold"]), ["DROP"])
        # a flat stock that suddenly jumps (BREAK) also has a maxed-out RSI; ties sort by ticker
        self.assertEqual(names(S.BUILTIN["overbought"]), ["BREAK", "RISE"])
        self.assertEqual(names(S.BUILTIN["volume-surge"]), ["SPIKE"])
        self.assertEqual(names(S.BUILTIN["breakout"]), ["BREAK"])
        # BREAK's jump lifts its 50-day average above the 200-day too; DROP and SPIKE are not in an uptrend
        self.assertEqual(sorted(names(S.BUILTIN["trend-up"])), ["BREAK", "RISE"])
        self.assertEqual(names(S.BUILTIN["losers"]), ["DROP"])
        self.assertEqual(names(S.BUILTIN["gainers"]), ["BREAK", "RISE"])
        self.assertEqual(names(S.merge(None, [S.parse_where("chg_1m<-20")])), ["DROP"])


if __name__ == "__main__":
    unittest.main()
