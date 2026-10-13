import os
import re
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("STK_CACHE_DIR", tempfile.mkdtemp())

from helpers import add_stock  # noqa: E402

from screener.cli import home, style as sty  # noqa: E402
from screener.core import db, queries  # noqa: E402
from screener.core.quote import IST  # noqa: E402


def plain(s):
    return re.sub(r"\[/?[a-z ]+\]", "", s)


class StyleTests(unittest.TestCase):
    def test_pct(self):
        self.assertEqual(sty.pct_style(3.5), "bold green")
        self.assertEqual(sty.pct_style(0.4), "green")
        self.assertEqual(sty.pct_style(-4), "bold red")
        self.assertEqual(sty.pct_style(-1), "red")
        self.assertEqual(sty.pct_style(None), "dim")

    def test_volume_and_rsi(self):
        self.assertEqual(sty.volume_style(0.5), "dim")
        self.assertEqual(sty.volume_style(1.0), "")
        self.assertEqual(sty.volume_style(1.7), "yellow")
        self.assertEqual(sty.volume_style(2.5, 1), "bold green")
        self.assertEqual(sty.volume_style(2.5, -1), "bold red")
        self.assertEqual(sty.rsi_style(75), "red")
        self.assertEqual(sty.rsi_style(25), "green")
        self.assertEqual(sty.rsi_style(50), "")


class LogoTests(unittest.TestCase):
    def test_logo_shape(self):
        lines = home.logo_lines(color=False)
        self.assertEqual(len(lines), 6)
        self.assertLessEqual(max(len(l) for l in lines), home.LOGO_WIDTH)
        self.assertIn("███████ ████████ ██  ██", lines[1])
        self.assertIn("│", lines[0])

    def test_logo_colored_has_markup(self):
        self.assertIn("[red]", home.logo_lines(True)[3])
        self.assertEqual(len(home.CANDLES), len(home.LETTERS))


class MarketTests(unittest.TestCase):
    def test_open_closed(self):
        mon_open = datetime(2026, 10, 12, 11, 0, tzinfo=IST)
        self.assertIn("open", home.market_line(mon_open))
        sat = datetime(2026, 10, 10, 11, 0, tzinfo=IST)
        self.assertIn("Mon 09:15", plain(home.market_line(sat)))
        early = datetime(2026, 10, 12, 8, 0, tzinfo=IST)
        self.assertIn("opens 09:15", plain(home.market_line(early)))
        fri_late = datetime(2026, 10, 9, 18, 0, tzinfo=IST)
        self.assertEqual(home.next_open(fri_late).weekday(), 0)


class MenuTests(unittest.TestCase):
    def ask(self, *answers):
        it = iter(answers)
        return lambda prompt: next(it)

    def test_simple_choices(self):
        f = home.command_for
        self.assertEqual(f("1", self.ask()), ["watch"])
        self.assertEqual(f("6", self.ask()), ["update"])
        self.assertEqual(f("7", self.ask()), ["status"])
        self.assertEqual(f("0", self.ask()), "quit")
        self.assertEqual(f("q", self.ask()), "quit")
        self.assertIsNone(f("", self.ask()))

    def test_followups(self):
        f = home.command_for
        self.assertEqual(f("2", self.ask("1")), ["screen", home.screen_names()[0]])
        self.assertEqual(f("2", self.ask("")), ["screen"])
        self.assertEqual(f("2", self.ask("oversold")), ["screen", "oversold"])
        self.assertEqual(f("3", self.ask("TCS")), ["info", "TCS"])
        self.assertEqual(f("3", self.ask("tata motors")), ["find", "tata", "motors"])
        self.assertEqual(f("4", self.ask("TCS", "90")), ["history", "TCS", "-d", "90"])
        self.assertEqual(f("4", self.ask("TCS", "")), ["history", "TCS", "-d", "30"])
        self.assertIsNone(f("4", self.ask("")))

    def test_typed_commands(self):
        f = home.command_for
        self.assertEqual(f("quote TCS", self.ask()), ["quote", "TCS"])
        self.assertEqual(f("stk screen oversold", self.ask()), ["screen", "oversold"])

    def test_menu_has_every_key(self):
        text = plain("\n".join(home.menu_lines()))
        for k, label in home.MENU:
            self.assertIn(label, text)


class InfoTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(Path(tempfile.mkdtemp()) / "t.db")

    def test_empty(self):
        text = plain("\n".join(home.info_lines(self.conn)))
        self.assertIn("No data yet", text)
        self.assertIn("Watchlist empty", text)

    def test_watch_counts_and_data(self):
        add_stock(self.conn, "UPCO", [100, 101, 103])
        add_stock(self.conn, "DOWNCO", [100, 99, 95])
        queries.watch_add(self.conn, ["UPCO", "DOWNCO", "GHOST"])
        self.assertEqual(home.watch_counts(self.conn), (3, 1, 1))
        text = plain("\n".join(home.info_lines(self.conn)))
        self.assertIn("2 stocks", text)
        self.assertIn("Watchlist 3", text)


if __name__ == "__main__":
    unittest.main()
