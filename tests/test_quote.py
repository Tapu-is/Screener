import os
import tempfile
import unittest
from datetime import datetime
from unittest import mock

os.environ["STK_CACHE_DIR"] = tempfile.mkdtemp()

from screener.core import cache, quote  # noqa: E402
from screener.core.quote import IST, QuoteError  # noqa: E402

SAMPLE = {
    "chart": {
        "result": [
            {
                "meta": {
                    "symbol": "RELIANCE.NS",
                    "longName": "Reliance Industries Limited",
                    "fullExchangeName": "NSE",
                    "currency": "INR",
                    "regularMarketPrice": 1550.0,
                    "regularMarketDayHigh": 1560.0,
                    "regularMarketDayLow": 1530.0,
                    "regularMarketVolume": 5_200_000,
                    "fiftyTwoWeekHigh": 1608.0,
                    "fiftyTwoWeekLow": 1115.0,
                    "chartPreviousClose": 1400.0,  # deliberately wrong for a 5d range
                    "regularMarketTime": 1760000000,
                },
                "indicators": {"quote": [{"close": [1500.0, 1520.0, 1540.0, None, 1550.0]}]},
            }
        ],
        "error": None,
    }
}


class FakeResp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("bad json")
        return self._body


class ParseTests(unittest.TestCase):
    def test_parse_fields(self):
        q = quote.parse_chart(SAMPLE, "RELIANCE.NS")
        self.assertEqual(q.price, 1550.0)
        self.assertEqual(q.name, "Reliance Industries Limited")
        self.assertEqual(q.volume, 5_200_000)

    def test_prev_close_uses_previous_candle_not_range_start(self):
        q = quote.parse_chart(SAMPLE, "RELIANCE.NS")
        self.assertEqual(q.prev_close, 1540.0)
        self.assertAlmostEqual(q.change, 10.0)
        self.assertAlmostEqual(q.change_pct, 10 / 1540 * 100)

    def test_error_payload(self):
        bad = {"chart": {"result": None, "error": {"code": "Not Found", "description": "No data found"}}}
        with self.assertRaises(QuoteError):
            quote.parse_chart(bad, "NOPE.NS")

    def test_missing_price(self):
        bad = {"chart": {"result": [{"meta": {}}], "error": None}}
        with self.assertRaises(QuoteError):
            quote.parse_chart(bad, "X.NS")


class SymbolTests(unittest.TestCase):
    def test_bare_symbol_tries_nse_then_bse(self):
        self.assertEqual(quote.candidates("reliance"), ["RELIANCE.NS", "RELIANCE.BO"])

    def test_prefer_bse(self):
        self.assertEqual(quote.candidates("TCS", prefer_bse=True), ["TCS.BO", "TCS.NS"])

    def test_explicit_suffix_and_index_and_code(self):
        self.assertEqual(quote.candidates("tcs.bo"), ["TCS.BO"])
        self.assertEqual(quote.candidates("^NSEI"), ["^NSEI"])
        self.assertEqual(quote.candidates("500325"), ["500325.BO"])

    def test_empty(self):
        with self.assertRaises(QuoteError):
            quote.candidates("  ")


class MarketHoursTests(unittest.TestCase):
    def at(self, y, m, d, h, mi):
        return datetime(y, m, d, h, mi, tzinfo=IST)

    def test_open_midday(self):  # Mon 12 Oct 2026
        self.assertTrue(quote.is_market_open(self.at(2026, 10, 12, 12, 0)))

    def test_boundaries(self):
        self.assertFalse(quote.is_market_open(self.at(2026, 10, 12, 9, 14)))
        self.assertTrue(quote.is_market_open(self.at(2026, 10, 12, 9, 15)))
        self.assertTrue(quote.is_market_open(self.at(2026, 10, 12, 15, 30)))
        self.assertFalse(quote.is_market_open(self.at(2026, 10, 12, 15, 31)))

    def test_weekend(self):  # Sat 10 Oct 2026
        self.assertFalse(quote.is_market_open(self.at(2026, 10, 10, 12, 0)))


class FetchTests(unittest.TestCase):
    def setUp(self):
        cache._file().unlink(missing_ok=True)

    def test_live_then_cache(self):
        with mock.patch("requests.get", return_value=FakeResp(200, SAMPLE)) as g:
            first = quote.get_quote("RELIANCE")
            second = quote.get_quote("RELIANCE")
        self.assertEqual(first.source, "live")
        self.assertEqual(second.source, "cache")
        self.assertEqual(g.call_count, 1)

    def test_falls_back_to_bse_when_not_on_nse(self):
        bse = {"chart": {"result": [{"meta": {**SAMPLE["chart"]["result"][0]["meta"], "symbol": "XYZ.BO"},
                                     "indicators": SAMPLE["chart"]["result"][0]["indicators"]}], "error": None}}
        responses = [FakeResp(404), FakeResp(200, bse)]
        with mock.patch("requests.get", side_effect=responses):
            q = quote.get_quote("XYZ")
        self.assertEqual(q.symbol, "XYZ.BO")

    def test_unknown_symbol(self):
        with mock.patch("requests.get", return_value=FakeResp(404)):
            with self.assertRaises(QuoteError):
                quote.get_quote("NOSUCHSTOCK")

    def test_offline_uses_stale_cache(self):
        with mock.patch("requests.get", return_value=FakeResp(200, SAMPLE)):
            quote.get_quote("RELIANCE")
        import requests

        with mock.patch("requests.get", side_effect=requests.ConnectionError("down")):
            q = quote.get_quote("RELIANCE", use_cache=False)
        self.assertEqual(q.source, "stale")
        self.assertEqual(q.price, 1550.0)

    def test_offline_with_no_cache_raises(self):
        import requests

        with mock.patch("requests.get", side_effect=requests.ConnectionError("down")):
            with self.assertRaises(QuoteError):
                quote.get_quote("RELIANCE", use_cache=False)

    def test_many_keeps_going_after_one_failure(self):
        def fake(url, **kw):
            return FakeResp(200, SAMPLE) if "RELIANCE" in url else FakeResp(404)

        with mock.patch("requests.get", side_effect=fake):
            out = quote.get_quotes(["RELIANCE", "NOSUCH"])
        self.assertIsInstance(out[0], quote.Quote)
        self.assertIsInstance(out[1], QuoteError)


if __name__ == "__main__":
    unittest.main()
