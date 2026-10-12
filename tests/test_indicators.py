import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("STK_CACHE_DIR", tempfile.mkdtemp())

from helpers import add_stock, mk_rows  # noqa: E402

from screener.core import db, indicators as I  # noqa: E402


def rows(closes, vols=None):
    """(date, high, low, close, volume) tuples as compute() expects."""
    return [
        (f"2026-01-{i:04d}", c * 1.01, c * 0.99, c, vols[i] if vols else 1_000_000)
        for i, c in enumerate(closes)
    ]


def fresh_db():
    return db.connect(Path(tempfile.mkdtemp()) / "t.db")


class SeriesTests(unittest.TestCase):
    def test_sma(self):
        self.assertEqual(I.sma_series([1, 2, 3, 4, 5], 3), [None, None, 2.0, 3.0, 4.0])
        self.assertEqual(I.sma_series([1, 2], 3), [None, None])

    def test_ema_hand_checked(self):
        self.assertEqual(I.ema_series([1, 2, 3, 4, 5], 3), [None, None, 2.0, 3.0, 4.0])

    def test_ema_skips_leading_none(self):
        out = I.ema_series([None, None, 1, 2, 3, 4, 5], 3)
        self.assertEqual(out, [None, None, None, None, 2.0, 3.0, 4.0])

    def test_rsi_hand_checked(self):
        r = I.rsi_series([10, 11, 12, 11, 12], 3)
        self.assertAlmostEqual(r[3], 66.6667, places=3)  # gains 2, losses 1 -> RS 2
        self.assertAlmostEqual(r[4], 77.7778, places=3)  # smoothed: RS 3.5

    def test_rsi_published_example(self):
        # Wilder's classic 14-period example (also used by StockCharts)
        closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89,
                  46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64]
        r = I.rsi_series(closes, 14)
        self.assertAlmostEqual(r[14], 70.46, delta=0.05)

    def test_rsi_extremes(self):
        self.assertEqual(I.rsi_series(list(range(1, 30)), 14)[-1], 100.0)
        self.assertLess(I.rsi_series(list(range(30, 1, -1)), 14)[-1], 0.01)
        self.assertEqual(I.rsi_series([5.0] * 30, 14)[-1], 50.0)  # no movement at all

    def test_rsi_needs_enough_data(self):
        self.assertTrue(all(v is None for v in I.rsi_series([1, 2, 3], 14)))

    def test_last_cross(self):
        self.assertEqual(I.last_cross([1, 2, 3, 2, 1], [2, 2, 2, 2, 2]), (4, "down"))
        self.assertEqual(I.last_cross([1, 2, 3], [2, 2, 2]), (2, "up"))
        self.assertIsNone(I.last_cross([3, 3, 3], [1, 1, 1]))
        self.assertIsNone(I.last_cross([None, None], [None, None]))


class ComputeTests(unittest.TestCase):
    def test_too_little_data(self):
        self.assertIsNone(I.compute([]))
        self.assertIsNone(I.compute(rows([100])))

    def test_rising_series_values(self):
        v = I.compute([(f"d{i}", i + 1.0, i - 1.0, float(i), 1_000_000) for i in range(1, 301)])
        self.assertEqual(v["close"], 300.0)
        self.assertEqual(v["sma20"], 290.5)
        self.assertEqual(v["sma50"], 275.5)
        self.assertEqual(v["sma200"], 200.5)
        self.assertEqual(v["rsi14"], 100.0)
        self.assertEqual(v["high52"], 301.0)
        self.assertEqual(v["low52"], 48.0)  # lowest low of the last 252 sessions (49 - 1)
        self.assertAlmostEqual(v["chg_1d"], (300 / 299 - 1) * 100, places=3)
        self.assertAlmostEqual(v["chg_1y"], (300 / 48 - 1) * 100, places=3)
        self.assertGreater(v["macd"], 0)
        self.assertEqual(v["sessions"], 300)
        self.assertEqual(v["as_of"], "d300")

    def test_short_history_leaves_long_fields_empty(self):
        v = I.compute(rows([100 + i for i in range(30)]))
        self.assertIsNone(v["sma50"])
        self.assertIsNone(v["sma200"])
        self.assertIsNone(v["chg_6m"])
        self.assertIsNone(v["prior_high52"])
        self.assertIsNone(v["macd_signal"])
        self.assertIsNotNone(v["rsi14"])
        self.assertIsNotNone(v["macd"])
        self.assertIsNone(v["golden_cross_days"])

    def test_golden_cross_after_a_v_shaped_recovery(self):
        down = [200 - 100 * i / 249 for i in range(250)]
        up = [100 + 120 * (i + 1) / 120 for i in range(120)]
        v = I.compute(rows(down + up))
        self.assertIsNotNone(v["golden_cross_days"])
        self.assertGreater(v["golden_cross_days"], 0)
        self.assertIsNone(v["death_cross_days"])
        self.assertGreater(v["sma50"], v["sma200"])

    def test_death_cross_after_a_peak(self):
        up = [100 + 100 * i / 249 for i in range(250)]
        down = [200 - 110 * (i + 1) / 120 for i in range(120)]
        v = I.compute(rows(up + down))
        self.assertIsNotNone(v["death_cross_days"])
        self.assertIsNone(v["golden_cross_days"])
        self.assertLess(v["sma50"], v["sma200"])

    def test_oversold_after_a_sharp_drop(self):
        closes = [100.0] * 270 + [100 * 0.98 ** (i + 1) for i in range(30)]
        v = I.compute(rows(closes))
        self.assertLess(v["rsi14"], 30)
        self.assertAlmostEqual(v["chg_1d"], -2.0, places=2)
        self.assertLess(v["macd_hist"], 0)
        self.assertIsNotNone(v["macd_cross_down_days"])
        self.assertIsNone(v["macd_cross_up_days"])
        self.assertLess(v["from_high_pct"], -40)

    def test_breakout_close_above_prior_high(self):
        v = I.compute(rows([100.0] * 299 + [105.0]))
        self.assertEqual(v["prior_high52"], 101.0)
        self.assertGreater(v["close"], v["prior_high52"])
        v = I.compute(rows([100.0] * 300))
        self.assertLess(v["close"], v["prior_high52"] + 1e-9)

    def test_volume_ratio_excludes_the_latest_day(self):
        v = I.compute(rows([100.0] * 300, [1_000_000] * 299 + [3_000_000]))
        self.assertEqual(v["vol_ratio"], 3.0)
        self.assertEqual(v["avg_vol20"], 1_000_000)
        self.assertEqual(v["turnover20_cr"], 11.0)  # (19*1e6 + 3e6) * 100 / 20 / 1e7

    def test_missing_volumes_do_not_crash(self):
        v = I.compute(rows([100.0] * 60, [None] * 60))
        self.assertIsNone(v["vol_ratio"])
        self.assertIsNone(v["turnover20_cr"])
        self.assertIsNone(v["volume"])

    def test_missing_high_low_fall_back_to_close(self):
        data = [(f"d{i}", None, None, 100.0 + i, 1000) for i in range(80)]
        v = I.compute(data)
        self.assertEqual(v["high52"], 179.0)
        self.assertEqual(v["from_high_pct"], 0.0)

    def test_every_field_is_present_in_the_result(self):
        v = I.compute(rows([100.0 + (i % 7) for i in range(300)]))
        for f in I.FIELDS:
            self.assertIn(f.name, v)


class StorageTests(unittest.TestCase):
    def test_fields_match_table_columns(self):
        conn = fresh_db()
        cols = [r[1] for r in conn.execute("PRAGMA table_info(indicators)")]
        self.assertEqual(sorted(cols), sorted(["symbol_id"] + I.COLUMNS))

    def test_refresh_and_stale_detection(self):
        conn = fresh_db()
        a = add_stock(conn, "AAA", [100 + i for i in range(80)])
        b = add_stock(conn, "BBB", [200 - i for i in range(80)])
        self.assertEqual(sorted(I.stale_symbol_ids(conn)), [a, b])
        self.assertEqual(I.refresh(conn), 2)
        self.assertEqual(I.stale_symbol_ids(conn), [])
        row = conn.execute("SELECT close, as_of, rsi14 FROM indicators WHERE symbol_id = ?", (a,)).fetchone()
        self.assertEqual((row["close"], row["as_of"]), (179.0, "2026-10-09"))
        self.assertEqual(row["rsi14"], 100.0)

        # a newer price makes only that stock stale
        from screener.core import updater

        updater.store_rows(conn, a, mk_rows([300.0], end=date(2026, 10, 12)))
        self.assertEqual(I.stale_symbol_ids(conn), [a])
        I.refresh(conn)
        self.assertEqual(conn.execute("SELECT close FROM indicators WHERE symbol_id = ?", (a,)).fetchone()[0], 300.0)

    def test_refresh_given_ids_recomputes_even_if_not_stale(self):
        conn = fresh_db()
        a = add_stock(conn, "AAA", [100 + i for i in range(80)])
        I.refresh(conn)
        conn.execute("UPDATE indicators SET close = 1 WHERE symbol_id = ?", (a,))
        I.refresh(conn, [a])
        self.assertEqual(conn.execute("SELECT close FROM indicators WHERE symbol_id = ?", (a,)).fetchone()[0], 179.0)

    def test_inactive_symbols_are_ignored(self):
        conn = fresh_db()
        a = add_stock(conn, "AAA", [100 + i for i in range(80)])
        with conn:
            conn.execute("UPDATE symbols SET active = 0 WHERE id = ?", (a,))
        self.assertEqual(I.stale_symbol_ids(conn), [])

    def test_compute_symbol_matches_stored_row(self):
        conn = fresh_db()
        a = add_stock(conn, "AAA", [100 + (i % 9) for i in range(150)])
        I.refresh(conn)
        live = I.compute_symbol(conn, a)
        row = conn.execute("SELECT * FROM indicators WHERE symbol_id = ?", (a,)).fetchone()
        for f in I.FIELDS:
            self.assertEqual(live[f.name], row[f.name], f.name)


if __name__ == "__main__":
    unittest.main()
