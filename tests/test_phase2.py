import os
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("STK_CACHE_DIR", tempfile.mkdtemp())

from helpers import FakeFetch, payload, series  # noqa: E402

from screener.core import db, queries, symbols, updater  # noqa: E402
from screener.core.history import HistoryError, RateLimited, fetch_history, parse_history  # noqa: E402
from screener.core.quote import IST  # noqa: E402
from screener.core.updater import Pacer, paced  # noqa: E402
from unittest import mock  # noqa: E402


def fast():
    """A pacer that never really sleeps."""
    return Pacer(interval=0, cooldown=0, sleep=lambda s: None)

CSV = (
    "﻿Company Name,Industry,Symbol,Series,ISIN Code\n"
    "Reliance Industries Ltd.,Oil Gas & Consumable Fuels,RELIANCE,EQ,INE002A01018\n"
    "Tata Consultancy Services Ltd.,Information Technology,TCS,EQ,INE467B01029\n"
    "Mahindra & Mahindra Ltd.,Automobile,M&M,EQ,INE101A01026\n"
    "Reliance Industries Ltd.,Oil Gas & Consumable Fuels,RELIANCE,EQ,INE002A01018\n"
)


def at(y, m, d, h, mi):
    return datetime(y, m, d, h, mi, tzinfo=IST)


def fresh_db():
    return db.connect(Path(tempfile.mkdtemp()) / "t.db")


class ParseHistoryTests(unittest.TestCase):
    def test_dates_are_ist_trading_days_and_sorted(self):
        rows = series(date(2026, 10, 9), 5)
        res = parse_history(payload(rows), "X.NS")
        self.assertEqual([r[0] for r in res.rows], [r[0] for r in rows])
        self.assertEqual(res.rows[-1][0], "2026-10-09")

    def test_missing_close_candles_dropped(self):
        rows = series(date(2026, 10, 9), 5)
        body = payload(rows)
        body["chart"]["result"][0]["indicators"]["quote"][0]["close"][2] = None
        self.assertEqual(len(parse_history(body, "X.NS").rows), 4)

    def test_adjclose_falls_back_to_close(self):
        body = payload(series(date(2026, 10, 9), 3))
        del body["chart"]["result"][0]["indicators"]["adjclose"]
        res = parse_history(body, "X.NS")
        self.assertEqual(res.rows[0][5], res.rows[0][4])

    def test_splits_reported(self):
        body = payload(series(date(2026, 10, 9), 3), splits=["2026-10-08"])
        self.assertEqual(parse_history(body, "X.NS").splits, ["2026-10-08"])

    def test_error_and_empty(self):
        with self.assertRaises(HistoryError):
            parse_history({"chart": {"result": None, "error": {"description": "No data found"}}}, "X.NS")
        with self.assertRaises(HistoryError):
            parse_history({"chart": {"result": [], "error": None}}, "X.NS")

    def test_no_timestamps_gives_no_rows(self):
        res = parse_history({"chart": {"result": [{"meta": {}}], "error": None}}, "X.NS")
        self.assertEqual(res.rows, [])


class SymbolTests(unittest.TestCase):
    def test_parse_csv_handles_bom_dupes_and_ampersand(self):
        rows = symbols.parse_csv(CSV)
        self.assertEqual([r["ticker"] for r in rows], ["RELIANCE", "TCS", "M&M"])
        self.assertEqual(rows[0]["isin"], "INE002A01018")

    def test_parse_csv_minimal_file(self):
        rows = symbols.parse_csv("symbol\nitc\n")
        self.assertEqual(rows[0]["ticker"], "ITC")
        self.assertIsNone(rows[0]["name"])

    def test_parse_csv_errors(self):
        for bad in ("", "Name,Industry\nA,B\n", "Symbol\n"):
            with self.assertRaises(symbols.SymbolListError):
                symbols.parse_csv(bad)

    def test_load_counts_and_yahoo_ticker(self):
        conn = fresh_db()
        rows = symbols.parse_csv(CSV)
        self.assertEqual(symbols.load(conn, rows), (3, 0))
        self.assertEqual(symbols.load(conn, rows), (0, 3))
        y = conn.execute("SELECT yahoo FROM symbols WHERE ticker='M&M'").fetchone()[0]
        self.assertEqual(y, "M&M.NS")

    def test_deactivate_missing(self):
        conn = fresh_db()
        symbols.load(conn, symbols.parse_csv(CSV))
        symbols.load(conn, symbols.parse_csv("Symbol\nTCS\n"), deactivate_missing=True)
        active = [r[0] for r in conn.execute("SELECT ticker FROM symbols WHERE active=1")]
        self.assertEqual(active, ["TCS"])


class TradingDayTests(unittest.TestCase):
    def test_expected_last_session(self):
        # Fri 9 Oct 2026
        self.assertEqual(updater.expected_last_session(at(2026, 10, 9, 16, 0)), date(2026, 10, 9))
        self.assertEqual(updater.expected_last_session(at(2026, 10, 9, 12, 0)), date(2026, 10, 8))
        self.assertEqual(updater.expected_last_session(at(2026, 10, 10, 10, 0)), date(2026, 10, 9))  # Sat
        self.assertEqual(updater.expected_last_session(at(2026, 10, 12, 8, 0)), date(2026, 10, 9))  # Mon early

    def test_today_incomplete(self):
        self.assertTrue(updater.today_is_incomplete(at(2026, 10, 9, 12, 0)))
        self.assertFalse(updater.today_is_incomplete(at(2026, 10, 9, 15, 45)))
        self.assertFalse(updater.today_is_incomplete(at(2026, 10, 10, 12, 0)))


class UpdateTests(unittest.TestCase):
    NOW = at(2026, 10, 9, 18, 0)  # Fri evening, session finished

    def setUp(self):
        self.conn = fresh_db()
        symbols.load(self.conn, symbols.parse_csv(CSV))

    def run_update(self, fetch, **kw):
        kw.setdefault("now", self.NOW)
        kw.setdefault("pacer", fast())
        return updater.update(self.conn, fetch=fetch, workers=1, **kw)

    def data(self, n=40):
        return {f"{t}.NS": series(date(2026, 10, 9), n) for t in ("RELIANCE", "TCS", "M&M")}

    def count(self):
        return self.conn.execute("SELECT COUNT(*) FROM prices_daily").fetchone()[0]

    def test_backfill_writes_everything(self):
        f = FakeFetch(self.data())
        res = self.run_update(f)
        self.assertEqual((res.total, res.ok, res.skipped, len(res.failures)), (3, 3, 0, 0))
        self.assertEqual(self.count(), 120)
        self.assertTrue(all(c[1] is None for c in f.calls))  # full-range requests

    def test_second_run_is_skipped_without_network(self):
        self.run_update(FakeFetch(self.data()))
        f = FakeFetch(self.data())
        res = self.run_update(f)
        self.assertEqual((res.ok, res.skipped), (0, 3))
        self.assertEqual(f.calls, [])

    def test_incremental_fetches_only_recent_window(self):
        old = {k: v[:-3] for k, v in self.data().items()}  # data ends 3 sessions ago
        self.run_update(FakeFetch(old), now=at(2026, 10, 9, 18, 0))
        f = FakeFetch(self.data())
        res = self.run_update(f)
        self.assertEqual(res.ok, 3)
        self.assertEqual(self.count(), 120)
        start = f.calls[0][1]
        last_old = date.fromisoformat(old["TCS.NS"][-1][0])
        self.assertEqual((last_old - start).days, updater.OVERLAP_DAYS)

    def test_split_triggers_full_redownload(self):
        old = {k: v[:-2] for k, v in self.data().items()}
        self.run_update(FakeFetch(old))
        f = FakeFetch(self.data(), splits={"TCS.NS": ["2026-10-08"]})
        self.run_update(f)
        tcs_calls = [c for c in f.calls if c[0] == "TCS.NS"]
        self.assertEqual(len(tcs_calls), 2)
        self.assertIsNotNone(tcs_calls[0][1])  # incremental first
        self.assertIsNone(tcs_calls[1][1])  # then full

    def test_forming_candle_not_stored_during_market_hours(self):
        data = self.data(10)
        res = self.run_update(FakeFetch(data), now=at(2026, 10, 9, 12, 0))
        self.assertEqual(res.ok, 3)
        newest = self.conn.execute("SELECT MAX(date) FROM prices_daily").fetchone()[0]
        self.assertEqual(newest, "2026-10-08")

    def test_failure_is_recorded_and_others_continue(self):
        f = FakeFetch(self.data(), fail={"TCS.NS": "TCS.NS: symbol not found"})
        res = self.run_update(f)
        self.assertEqual((res.ok, len(res.failures)), (2, 1))
        log = self.conn.execute("SELECT symbols_failed, failures FROM update_log").fetchone()
        self.assertEqual(log[0], 1)
        self.assertIn("TCS", log[1])

    def test_empty_history_on_first_download_is_a_failure(self):
        res = self.run_update(FakeFetch({"RELIANCE.NS": [], "TCS.NS": series(date(2026, 10, 9), 5)}),
                              tickers=["RELIANCE", "TCS"])
        self.assertEqual((res.ok, len(res.failures)), (1, 1))

    def test_specific_tickers_and_full(self):
        self.run_update(FakeFetch(self.data()))
        f = FakeFetch(self.data())
        res = self.run_update(f, tickers=["tcs"], full=True)
        self.assertEqual(res.total, 1)
        self.assertEqual([c[0] for c in f.calls], ["TCS.NS"])
        self.assertEqual(self.count(), 120)  # replaced, not duplicated

    def test_progress_callback_reaches_total(self):
        seen = []
        self.run_update(FakeFetch(self.data()), progress=lambda d, t, s: seen.append((d, t)))
        self.assertEqual(seen[-1], (3, 3))


class QueryTests(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_db()
        symbols.load(self.conn, symbols.parse_csv(CSV))
        data = {"RELIANCE.NS": series(date(2026, 10, 9), 300), "TCS.NS": series(date(2026, 10, 9), 10)}
        updater.update(self.conn, fetch=FakeFetch(data), workers=1, pacer=fast(), now=at(2026, 10, 9, 18, 0))
        self.rel = queries.get_symbol(self.conn, "reliance.ns")
        self.tcs = queries.get_symbol(self.conn, "TCS")

    def test_get_symbol_normalises(self):
        self.assertEqual(self.rel["ticker"], "RELIANCE")
        self.assertIsNone(queries.get_symbol(self.conn, "NOPE"))

    def test_history_oldest_first_with_change(self):
        rows = queries.history(self.conn, self.rel["id"], 5)
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[-1]["date"], "2026-10-09")
        self.assertAlmostEqual(rows[-1]["chg_pct"], (rows[-1]["close"] / rows[-2]["close"] - 1) * 100)
        self.assertIsNotNone(rows[0]["chg_pct"])

    def test_history_shorter_than_requested(self):
        rows = queries.history(self.conn, self.tcs["id"], 30)
        self.assertEqual(len(rows), 10)
        self.assertIsNone(rows[0]["chg_pct"])

    def test_stats_full_year(self):
        st = queries.stats(self.conn, self.rel["id"])
        self.assertEqual(st.close, 399.0)  # base 100 + 299
        self.assertAlmostEqual(st.chg["1D"], (399 / 398 - 1) * 100)
        self.assertAlmostEqual(st.chg["1Y"], (399 / (399 - 252) - 1) * 100)
        self.assertEqual(st.high52, 400.0)
        self.assertGreater(st.range_pos, 0.9)
        self.assertLessEqual(st.from_high_pct, 0)

    def test_stats_short_history_has_gaps(self):
        st = queries.stats(self.conn, self.tcs["id"])
        self.assertIsNotNone(st.chg["1W"])
        self.assertIsNone(st.chg["1M"])
        self.assertIsNone(st.chg["1Y"])

    def test_stats_no_data(self):
        mm = queries.get_symbol(self.conn, "M&M")
        self.assertIsNone(queries.stats(self.conn, mm["id"]))

    def test_find_ranks_exact_and_prefix_first(self):
        names = [r["ticker"] for r in queries.find(self.conn, "tc")]
        self.assertEqual(names[0], "TCS")
        self.assertEqual([r["ticker"] for r in queries.find(self.conn, "mahindra")], ["M&M"])
        self.assertEqual(queries.find(self.conn, "zzz"), [])

    def test_watchlist(self):
        self.assertEqual(queries.watch_add(self.conn, ["tcs", "INFY", "TCS"]), ["TCS", "INFY"])
        self.assertEqual(queries.watch_list(self.conn), ["TCS", "INFY"])
        self.assertEqual(queries.watch_remove(self.conn, ["tcs", "nope"]), ["TCS"])
        self.assertEqual(queries.watch_list(self.conn), ["INFY"])

    def test_status(self):
        s = queries.status(self.conn, now=at(2026, 10, 9, 18, 0))
        self.assertEqual((s["symbols"], s["with_data"]), (3, 2))
        self.assertEqual(s["behind"], 1)  # M&M has no data
        self.assertEqual(s["latest_date"], "2026-10-09")
        self.assertEqual(s["last_update"]["symbols_ok"], 2)
        self.assertGreater(s["size_bytes"], 0)


class FakeResp:
    def __init__(self, code=200, body=None):
        self.status_code, self._body = code, body

    def json(self):
        if self._body is None:
            raise ValueError("bad json")
        return self._body


class FetchHistoryTests(unittest.TestCase):
    OK = payload(series(date(2026, 10, 9), 3))

    def test_429_switches_to_alternate_host_once(self):
        responses = [FakeResp(429), FakeResp(200, self.OK)]
        with mock.patch("requests.get", side_effect=responses) as g:
            res = fetch_history("TCS.NS", sleep=lambda s: None)
        self.assertEqual(len(res.rows), 3)
        urls = [c.args[0] for c in g.call_args_list]
        self.assertIn("//query1.", urls[0])
        self.assertIn("//query2.", urls[1])

    def test_429_on_both_hosts_raises_rate_limited(self):
        with mock.patch("requests.get", return_value=FakeResp(429)):
            with self.assertRaises(RateLimited) as cm:
                fetch_history("TCS.NS", sleep=lambda s: None)
        self.assertTrue(cm.exception.transient)

    def test_server_error_retries_then_succeeds(self):
        sleeps = []
        responses = [FakeResp(503), FakeResp(200, self.OK)]
        with mock.patch("requests.get", side_effect=responses):
            res = fetch_history("TCS.NS", sleep=sleeps.append)
        self.assertEqual(len(res.rows), 3)
        self.assertEqual(sleeps, [2])

    def test_not_found_is_permanent(self):
        with mock.patch("requests.get", return_value=FakeResp(404)):
            with self.assertRaises(HistoryError) as cm:
                fetch_history("NOPE.NS", sleep=lambda s: None)
        self.assertFalse(cm.exception.transient)

    def test_incremental_request_uses_period_params(self):
        with mock.patch("requests.get", return_value=FakeResp(200, self.OK)) as g:
            fetch_history("TCS.NS", start=date(2026, 10, 1), sleep=lambda s: None)
        params = g.call_args.kwargs["params"]
        self.assertIn("period1", params)
        self.assertNotIn("range", params)


class PacerTests(unittest.TestCase):
    def make(self, **kw):
        self.now = 100.0
        self.sleeps = []

        def sleep(s):
            self.sleeps.append(round(s, 3))
            self.now += s

        return Pacer(clock=lambda: self.now, sleep=sleep, **kw)

    def test_requests_are_spaced_by_interval(self):
        p = self.make(interval=1.0, cooldown=10)
        for _ in range(3):
            p.wait()
        self.assertEqual(self.sleeps, [1.0, 1.0])

    def test_rate_limit_doubles_interval_and_pauses(self):
        p = self.make(interval=1.0, cooldown=10)
        self.assertEqual(p.rate_limited(), 10)
        self.assertEqual(p.interval, 2.0)
        self.assertEqual(p.rate_limited(), 20)
        for _ in range(5):
            p.rate_limited()
        self.assertEqual(p.interval, 6.0)  # capped
        p.wait()
        self.assertGreaterEqual(self.sleeps[0], 30)  # paused for the latest cooldown

    def test_ok_resets_pause_but_keeps_slower_interval(self):
        p = self.make(interval=1.0, cooldown=10)
        p.rate_limited()
        p.ok()
        self.assertEqual(p.hits, 0)
        self.assertEqual(p.interval, 2.0)

    def test_stop_releases_workers(self):
        from screener.core.updater import Stopped

        p = self.make(interval=1.0)
        p.stop()
        with self.assertRaises(Stopped):
            p.wait()

    def test_paced_retries_once_after_429(self):
        calls = []

        def flaky(*a, **k):
            calls.append(1)
            if len(calls) == 1:
                raise RateLimited("x")
            return "data"

        p = self.make(interval=0, cooldown=5)
        self.assertEqual(paced(flaky, p)(), "data")
        self.assertEqual(len(calls), 2)
        self.assertEqual(p.hits, 0)

    def test_paced_gives_up_after_second_429(self):
        def always(*a, **k):
            raise RateLimited("x")

        with self.assertRaises(RateLimited):
            paced(always, self.make(interval=0, cooldown=5))()


class RateLimitUpdateTests(unittest.TestCase):
    NOW = at(2026, 10, 9, 18, 0)
    TICKERS = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH"]

    def setUp(self):
        self.conn = fresh_db()
        symbols.load(self.conn, symbols.parse_csv("Symbol\n" + "\n".join(self.TICKERS) + "\n"))
        self.data = {f"{t}.NS": series(date(2026, 10, 9), 10) for t in self.TICKERS}

    def run_update(self, fetch):
        return updater.update(self.conn, fetch=fetch, workers=1, pacer=fast(), now=self.NOW)

    def test_persistent_429_stops_early_without_listing_every_symbol_as_failed(self):
        fail = {k: RateLimited("429") for k in self.data}
        res = self.run_update(FakeFetch(self.data, fail=fail))
        self.assertEqual(res.aborted, "rate_limit")
        self.assertEqual((res.ok, res.failures), (0, []))
        self.assertEqual(res.not_done, 8)
        log = self.conn.execute("SELECT interrupted, symbols_failed FROM update_log").fetchone()
        self.assertEqual(tuple(log), (1, 0))

    def test_network_outage_aborts_with_network_reason(self):
        err = HistoryError("net", transient=True)
        res = self.run_update(FakeFetch(self.data, fail={k: err for k in self.data}))
        self.assertEqual(res.aborted, "network")

    def test_progress_before_the_block_is_kept_and_resumable(self):
        # first three symbols succeed, the rest are rate limited
        fail = {f"{t}.NS": RateLimited("429") for t in self.TICKERS[3:]}
        res = self.run_update(FakeFetch(self.data, fail=fail))
        self.assertEqual((res.ok, res.aborted), (3, "rate_limit"))
        self.assertEqual(res.not_done, 5)
        # next run, source is healthy again: only the missing five are fetched
        f = FakeFetch(self.data)
        res2 = self.run_update(f)
        self.assertEqual((res2.ok, res2.skipped, res2.aborted), (5, 3, ""))
        self.assertEqual(len(f.calls), 5)

    def test_isolated_transient_failure_is_a_normal_failure(self):
        fail = {"BBB.NS": HistoryError("blip", transient=True)}
        res = self.run_update(FakeFetch(self.data, fail=fail))
        self.assertEqual((res.ok, res.aborted), (7, ""))
        self.assertEqual([t for t, _ in res.failures], ["BBB"])

    def test_isolated_failure_at_end_of_run_is_still_reported(self):
        fail = {"HHH.NS": HistoryError("blip", transient=True)}
        res = self.run_update(FakeFetch(self.data, fail=fail))
        self.assertEqual([t for t, _ in res.failures], ["HHH"])

    def test_single_429_is_retried_and_recovers(self):
        class OnceLimited(FakeFetch):
            def __call__(self, ticker, *a, **k):
                if not self.calls:
                    self.calls.append(("first", None, None))
                    raise RateLimited("429")
                return super().__call__(ticker, *a, **k)

        res = self.run_update(OnceLimited(self.data))
        self.assertEqual((res.ok, res.aborted, res.failures), (8, "", []))


class ProbeTests(unittest.TestCase):
    def test_probe_reports_each_variant_and_survives_failures(self):
        from screener.core import probe

        body = payload(series(date(2026, 10, 9), 4))

        def fake_get(url, params=None, headers=None, timeout=None):
            if params.get("range") == "2y" and "events" in params and "query1" in url:
                resp = FakeResp(429, None)
                resp.text, resp.headers = "Too Many Requests", {"Retry-After": "30"}
                return resp
            resp = FakeResp(200, body)
            resp.text, resp.headers = "", {}
            return resp

        seen = []
        with mock.patch("requests.get", side_effect=fake_get), mock.patch("shutil.which", return_value=None):
            results = probe.run(pause=0, sleep=lambda s: None, on_result=seen.append)
        by = {r.label: r for r in results}
        self.assertEqual(len(results), 6)
        self.assertEqual(by["5 days (what `quote` uses)"].status, 200)
        self.assertEqual(by["5 days (what `quote` uses)"].detail, "4 candles")
        r = by["2 years + events (what `update` uses)"]
        self.assertEqual(r.status, 429)
        self.assertIn("Retry-After 30s", r.detail)
        self.assertEqual(by["same, other host (query2)"].status, 200)
        self.assertEqual(len(seen), 6)

    def test_every_yahoo_request_uses_the_accepted_user_agent(self):
        from screener.core.quote import YAHOO_HEADERS

        self.assertEqual(YAHOO_HEADERS["User-Agent"], "Mozilla/5.0")
        with mock.patch("requests.get", return_value=FakeResp(200, payload(series(date(2026, 10, 9), 3)))) as g:
            fetch_history("TCS.NS", sleep=lambda s: None)
        self.assertEqual(g.call_args.kwargs["headers"], YAHOO_HEADERS)

    def test_probe_handles_network_failure(self):
        import requests as rq

        from screener.core import probe

        with mock.patch("requests.get", side_effect=rq.ConnectionError("down")), mock.patch("shutil.which", return_value=None):
            results = probe.run(pause=0, sleep=lambda s: None)
        self.assertTrue(all(r.status is None for r in results))


if __name__ == "__main__":
    unittest.main()
