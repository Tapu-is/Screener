"""stk command line interface (Termux friendly).

Usage:
    stk quote RELIANCE
    stk quote TCS INFY HDFCBANK
    stk quote RELIANCE --bse
    stk quote RELIANCE --watch 30
    stk doctor
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime

from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .. import __version__
from ..core.quote import IST, Quote, QuoteError, get_quotes, is_market_open

console = Console()


# ---------------------------------------------------------------- formatting
def _money(v) -> str:
    return "-" if v is None else f"{v:,.2f}"


def _volume(v) -> str:
    if v is None:
        return "-"
    if v >= 10_000_000:
        return f"{v / 10_000_000:.2f} Cr"
    if v >= 100_000:
        return f"{v / 100_000:.2f} L"
    return f"{v:,}"


def _change(q: Quote) -> Text:
    if q.change is None or q.change_pct is None:
        return Text("-")
    up = q.change >= 0
    arrow = "▲" if up else "▼"
    style = "bold green" if up else "bold red"
    return Text(f"{arrow} {q.change:+,.2f} ({q.change_pct:+.2f}%)", style=style)


def _freshness(q: Quote) -> str:
    when = datetime.fromisoformat(q.as_of).strftime("%d %b %H:%M IST")
    if q.source == "stale":
        mins = max(q.age_seconds // 60, 1)
        return f"[yellow]offline - last saved price, {mins} min old ({when})[/yellow]"
    if q.source == "cache":
        return f"[dim]{when} (cached)[/dim]"
    return f"[dim]{when}[/dim]"


def _status_line() -> str:
    now = datetime.now(IST).strftime("%a %d %b, %H:%M IST")
    if is_market_open():
        return f"[green]● Market open[/green]  [dim]{now}[/dim]"
    return f"[red]● Market closed[/red] - showing last close  [dim]{now}[/dim]"


def render_single(q: Quote) -> Panel:
    t = Table.grid(padding=(0, 2))
    t.add_column(style="dim")
    t.add_column(justify="right")
    t.add_row("Price", Text(f"₹{q.price:,.2f}", style="bold"))
    t.add_row("Change", _change(q))
    t.add_row("Prev close", _money(q.prev_close))
    t.add_row("Day range", f"{_money(q.day_low)} - {_money(q.day_high)}")
    t.add_row("52w range", f"{_money(q.week52_low)} - {_money(q.week52_high)}")
    t.add_row("Volume", _volume(q.volume))
    t.add_row("As of", _freshness(q))
    title = f"[bold]{q.symbol}[/bold]  {q.name}"
    subtitle = q.exchange or None
    return Panel(t, title=title, subtitle=subtitle, box=box.ROUNDED, expand=False)


def render_many(quotes: list) -> Table:
    t = Table(box=box.SIMPLE_HEAD, header_style="bold")
    t.add_column("Symbol")
    t.add_column("Price", justify="right")
    t.add_column("Change", justify="right")
    t.add_column("Day range", justify="right")
    t.add_column("Vol", justify="right")
    for q in quotes:
        if isinstance(q, QuoteError):
            t.add_row(Text(str(q), style="red"), "", "", "", "")
            continue
        sym = q.symbol + ("*" if q.source == "stale" else "")
        t.add_row(
            sym,
            f"{q.price:,.2f}",
            _change(q),
            f"{_money(q.day_low)}-{_money(q.day_high)}",
            _volume(q.volume),
        )
    return t


def render(quotes: list):
    body = render_single(quotes[0]) if len(quotes) == 1 and isinstance(quotes[0], Quote) else render_many(quotes)
    parts = [Text.from_markup(_status_line()), body]
    if any(isinstance(q, Quote) and q.source == "stale" for q in quotes):
        parts.append(Text.from_markup("[yellow]* offline - showing last saved price[/yellow]"))
    return Group(*parts)


# ------------------------------------------------------------------ commands
def cmd_quote(args: argparse.Namespace) -> int:
    symbols = args.symbols

    def fetch():
        return get_quotes(symbols, prefer_bse=args.bse)

    if args.watch:
        interval = max(args.watch, 15)  # don't hammer the source
        try:
            with Live(render(fetch()), console=console, refresh_per_second=2) as live:
                while True:
                    time.sleep(interval)
                    live.update(render(fetch()))
        except KeyboardInterrupt:
            return 0

    quotes = fetch()
    console.print(render(quotes))
    return 0 if any(isinstance(q, Quote) for q in quotes) else 1


def cmd_doctor(_: argparse.Namespace) -> int:
    """Quick self-check, handy when something doesn't work on the phone."""
    import platform

    import requests

    console.print(f"stk {__version__}  |  Python {platform.python_version()}  |  {platform.system()}")
    ok = True
    try:
        r = requests.get("https://query1.finance.yahoo.com/v8/finance/chart/RELIANCE.NS",
                         params={"range": "1d"}, timeout=10,
                         headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            console.print("[green]✓[/green] data source reachable")
        else:
            ok = False
            console.print(f"[red]✗[/red] data source answered HTTP {r.status_code}")
    except requests.RequestException as exc:
        ok = False
        console.print(f"[red]✗[/red] cannot reach data source ({exc.__class__.__name__}) - check internet")
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="stk", description="Personal NSE/BSE stock screener")
    p.add_argument("--version", action="version", version=f"stk {__version__}")
    sub = p.add_subparsers(dest="command")

    q = sub.add_parser("quote", help="latest price for one or more stocks")
    q.add_argument("symbols", nargs="+", help="e.g. RELIANCE TCS, RELIANCE.BO, 500325, ^NSEI")
    q.add_argument("--bse", action="store_true", help="look up on BSE first")
    q.add_argument("--watch", type=int, metavar="SECS", help="refresh every SECS seconds (min 15)")
    q.set_defaults(func=cmd_quote)

    d = sub.add_parser("doctor", help="check setup and connectivity")
    d.set_defaults(func=cmd_doctor)
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
