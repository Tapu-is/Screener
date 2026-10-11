"""stk command line interface (Termux friendly).

    stk init                      set up the database and Nifty 500 list
    stk update                    download / refresh daily prices
    stk status                    what is stored, and how fresh it is
    stk quote RELIANCE            latest price (live, a few minutes delayed)
    stk history RELIANCE -d 30    stored daily prices
    stk info RELIANCE             52-week range, returns, volume
    stk find tata                 search symbols and company names
    stk watch add TCS INFY        manage the watchlist; `stk watch` shows prices
    stk doctor                    check setup and connectivity
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn, TimeRemainingColumn
from rich.table import Table
from rich.text import Text

from .. import __version__
from ..core import db, probe, queries, symbols, updater
from ..core.quote import IST, YAHOO_HEADERS, Quote, QuoteError, get_quotes, is_market_open

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
    return f"{int(v):,}"


def _pct(v, plain: bool = False) -> Text:
    if v is None:
        return Text("-", style="dim")
    style = "green" if v >= 0 else "red"
    return Text(f"{v:+.2f}%", style=style)


def _bar(pos: float, width: int = 12) -> str:
    pos = min(max(pos, 0.0), 1.0)
    filled = round(pos * width)
    return "█" * filled + "░" * (width - filled)


def _date(iso: str) -> str:
    return datetime.strptime(iso, "%Y-%m-%d").strftime("%d %b %y")


def _size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


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


# ------------------------------------------------------------- quote rendering
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
    return Panel(t, title=title, subtitle=q.exchange or None, box=box.ROUNDED, expand=False)


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


def show_quotes(symbols_: list, bse: bool = False, watch_secs: int = 0) -> int:
    def fetch():
        return get_quotes(symbols_, prefer_bse=bse)

    if watch_secs:
        interval = max(watch_secs, 15)  # don't hammer the source
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


# ------------------------------------------------------------------ commands
def cmd_quote(args: argparse.Namespace) -> int:
    names = args.symbols
    if not names:
        names = queries.watch_list(db.connect())
        if not names:
            console.print("No symbols given and your watchlist is empty.")
            console.print("Try: [bold]stk quote RELIANCE[/bold]  or  [bold]stk watch add TCS INFY[/bold]")
            return 1
    return show_quotes(names, bse=args.bse, watch_secs=args.watch or 0)


def cmd_init(args: argparse.Namespace) -> int:
    conn = db.connect()
    try:
        if args.file:
            path = Path(args.file).expanduser()
            if not path.is_file():
                console.print(f"[red]File not found:[/red] {path}")
                return 1
            rows = symbols.parse_csv(path.read_text(encoding="utf-8-sig"))
            added, updated = symbols.load(conn, rows)
            source = path.name
        else:
            console.print("Downloading the Nifty 500 list...")
            rows = symbols.parse_csv(symbols.download())
            added, updated = symbols.load(conn, rows, deactivate_missing=len(rows) >= 100)
            source = "Nifty 500 list"
    except symbols.SymbolListError as exc:
        console.print(f"[red]Could not load symbols:[/red] {exc}")
        if not args.file:
            console.print(
                "\nIf you're offline or NSE blocked the request, download the CSV in your browser\n"
                "from niftyindices.com (Indices > Nifty 500 > constituents), then run:\n"
                "  [bold]stk init --file ~/storage/downloads/ind_nifty500list.csv[/bold]"
            )
        return 1

    console.print(f"[green]✓[/green] {source}: {len(rows)} symbols ({added} new, {updated} already known)")
    console.print(f"[dim]Database: {db.db_path()}[/dim]")
    console.print("Next: [bold]stk update[/bold]  (first run downloads 2 years of prices; use Wi-Fi)")
    return 0


def _symbol_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM symbols WHERE active = 1").fetchone()[0]


def cmd_update(args: argparse.Namespace) -> int:
    conn = db.connect()
    if _symbol_count(conn) == 0:
        console.print("No symbols yet, setting up first.")
        rc = cmd_init(argparse.Namespace(file=None))
        if rc:
            return rc
        conn = db.connect()

    tickers = None
    if args.symbols:
        tickers = [queries.normalize(t) for t in args.symbols]
        unknown = [t for t in tickers if not queries.get_symbol(conn, t)]
        if unknown:
            console.print(f"[yellow]Not in your database, skipped:[/yellow] {', '.join(unknown)}")
            console.print("[dim]Add other stocks with: stk init --file yourlist.csv (needs a Symbol column)[/dim]")
        tickers = [t for t in tickers if t not in unknown]
        if not tickers:
            return 1

    first_run = conn.execute("SELECT COUNT(*) FROM prices_daily").fetchone()[0] == 0
    if first_run or args.full:
        console.print(
            f"Downloading {args.years} years of daily prices. This takes a few minutes the first time.\n"
            "[dim]Safe to stop with Ctrl+C, and run stk update again to continue.[/dim]"
        )

    with Progress(
        SpinnerColumn(),
        TextColumn("{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeRemainingColumn(),
        console=console,
    ) as prog:
        task = prog.add_task("Updating", total=None)

        def on_progress(done: int, total: int, ticker: str) -> None:
            prog.update(task, total=total, completed=done, description=f"{ticker:<12}")

        result = updater.update(
            conn,
            tickers=tickers,
            full=args.full,
            years=args.years,
            workers=args.workers,
            delay=args.delay,
            progress=on_progress,
        )

    secs = (datetime.fromisoformat(result.finished) - datetime.fromisoformat(result.started)).total_seconds()
    left = f" · left {result.not_done}" if result.not_done else ""
    console.print(
        f"[green]✓[/green] updated {result.ok} · already current {result.skipped} · "
        f"failed {len(result.failures)}{left} · {result.rows:,} rows · {secs:.0f}s"
    )
    for ticker, msg in result.failures[:5]:
        console.print(f"  [red]✗[/red] {msg}")
    if len(result.failures) > 5:
        console.print(f"  ...and {len(result.failures) - 5} more (see stk status)")
    if any("rate limited" in m for _, m in result.failures):
        console.print("[dim]Some stocks were rate limited. Run stk update again to retry just those.[/dim]")

    if result.interrupted:
        console.print("[yellow]Stopped early. Progress is saved; run stk update to continue.[/yellow]")
    if result.aborted == "rate_limit":
        console.print(
            "\n[yellow]The data source (Yahoo) is limiting requests from this connection (HTTP 429), "
            "so I stopped instead of making it worse.[/yellow]\n"
            f"Saved so far: {result.ok} stocks. Still to do: {result.not_done}.\n"
            "What to try:\n"
            "  1. Wait 30-60 minutes, then run [bold]stk update[/bold] again. It resumes where it stopped.\n"
            "  2. Switch between Wi-Fi and mobile data (limits are per connection).\n"
            "  3. If it keeps happening, slow it down: [bold]stk update --delay 2 --workers 1[/bold]"
        )
        return 1
    if result.aborted == "network":
        console.print(
            "\n[yellow]Lost contact with the data source, so I stopped.[/yellow] "
            f"Saved so far: {result.ok} stocks. Still to do: {result.not_done}.\n"
            "Check your connection with [bold]stk doctor[/bold], then run [bold]stk update[/bold] to continue."
        )
        return 1
    if result.total and result.ok == 0 and result.skipped == 0 and result.failures:
        console.print("[red]Nothing could be downloaded.[/red] Check your connection with: stk doctor")
        return 1
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    conn = db.connect()
    s = queries.status(conn)
    if s["symbols"] == 0:
        console.print("Not set up yet. Run: [bold]stk init[/bold]")
        return 0

    t = Table.grid(padding=(0, 2))
    t.add_column(style="dim")
    t.add_column()
    t.add_row("Database", f'{s["path"]} ({_size(s["size_bytes"])})')
    t.add_row("Symbols", f'{s["symbols"]} ({s["with_data"]} with prices)')
    t.add_row("Price rows", f'{s["price_rows"]:,}')
    if s["latest_date"]:
        t.add_row("Data range", f'{_date(s["first_date"])} to {_date(s["latest_date"])}')
        if s["behind"] == 0:
            fresh = f'[green]✓ up to date[/green] (through {_date(s["expected_date"])})'
        else:
            word = "symbol" if s["behind"] == 1 else "symbols"
            fresh = f'[yellow]⚠ {s["behind"]} {word} behind[/yellow] - run stk update'
        t.add_row("Freshness", fresh)
    else:
        t.add_row("Freshness", "[yellow]no prices yet[/yellow] - run stk update")
    t.add_row("Watchlist", str(s["watchlist"]))
    lu = s["last_update"]
    if lu:
        when = datetime.fromisoformat(lu["started_at"]).strftime("%d %b %H:%M")
        extra = " (stopped early)" if lu["interrupted"] else ""
        t.add_row(
            "Last update",
            f'{when} · ok {lu["symbols_ok"]} · current {lu["symbols_skipped"]} · '
            f'failed {lu["symbols_failed"]} · {lu["rows_written"]:,} rows{extra}',
        )
    console.print(Panel(t, title="[bold]stk status[/bold]", box=box.ROUNDED, expand=False))
    if lu and lu["failures"]:
        console.print("[bold]Last failures[/bold]")
        for f in lu["failures"][:10]:
            console.print(f'  [red]✗[/red] {f["error"]}')
        if len(lu["failures"]) > 10:
            console.print(f'  ...and {len(lu["failures"]) - 10} more')
    return 0


def _need_symbol(conn: sqlite3.Connection, text: str):
    sym = queries.get_symbol(conn, text)
    if sym is None:
        console.print(f"[red]{text.upper()} is not in your database.[/red]")
        near = queries.find(conn, text, limit=5)
        if near:
            console.print("Did you mean: " + ", ".join(r["ticker"] for r in near) + "?")
        else:
            console.print("Search with: [bold]stk find <name>[/bold]  (run stk init first if this is a fresh install)")
    return sym


def cmd_history(args: argparse.Namespace) -> int:
    conn = db.connect()
    sym = _need_symbol(conn, args.symbol)
    if sym is None:
        return 1
    rows = queries.history(conn, sym["id"], max(args.days, 1))
    if not rows:
        console.print(f'No stored prices for {sym["ticker"]}. Run: [bold]stk update {sym["ticker"]}[/bold]')
        return 1

    t = Table(box=box.SIMPLE_HEAD, header_style="bold", pad_edge=False)
    t.add_column("Date")
    if args.ohlc:
        for h in ("Open", "High", "Low"):
            t.add_column(h, justify="right")
    t.add_column("Close", justify="right")
    t.add_column("Chg", justify="right")
    t.add_column("Vol", justify="right")
    for r in rows:
        cells = [_date(r["date"])]
        if args.ohlc:
            cells += [_money(r["open"]), _money(r["high"]), _money(r["low"])]
        cells += [_money(r["close"]), _pct(r["chg_pct"]), _volume(r["volume"])]
        t.add_row(*cells)

    first, last = rows[0], rows[-1]
    period = (last["close"] / first["close"] - 1) * 100 if first["close"] else None
    highs = [r["high"] if r["high"] is not None else r["close"] for r in rows]
    lows = [r["low"] if r["low"] is not None else r["close"] for r in rows]
    vols = [r["volume"] for r in rows if r["volume"] is not None]

    console.print(Text.from_markup(f'[bold]{sym["ticker"]}[/bold]  {sym["name"] or ""}'))
    console.print(t)
    summary = Text.assemble(
        f"Since {_date(first['date'])}: ", _pct(period), f"   High {_money(max(highs))}   Low {_money(min(lows))}"
    )
    if vols:
        summary.append(f"   Avg vol {_volume(sum(vols) / len(vols))}")
    console.print(summary)
    return 0


def cmd_find(args: argparse.Namespace) -> int:
    conn = db.connect()
    rows = queries.find(conn, " ".join(args.query), limit=args.limit)
    if not rows:
        console.print("No matches." + (" Run [bold]stk init[/bold] first." if _symbol_count(conn) == 0 else ""))
        return 1
    t = Table(box=box.SIMPLE_HEAD, header_style="bold")
    t.add_column("Symbol")
    t.add_column("Name", no_wrap=True, overflow="ellipsis", max_width=34)
    t.add_column("Data", justify="center")
    for r in rows:
        t.add_row(r["ticker"], r["name"] or "-", "✓" if r["last_date"] else "-")
    console.print(t)
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    conn = db.connect()
    sym = _need_symbol(conn, args.symbol)
    if sym is None:
        return 1
    st = queries.stats(conn, sym["id"])
    if st is None:
        console.print(f'No stored prices for {sym["ticker"]}. Run: [bold]stk update {sym["ticker"]}[/bold]')
        return 1

    t = Table.grid(padding=(0, 2))
    t.add_column(style="dim")
    t.add_column(justify="right")
    t.add_row("Last close", Text(f"₹{st.close:,.2f}", style="bold"))
    t.add_row("Date", _date(st.last_date))
    for label in ("1D", "1W", "1M", "6M", "1Y"):
        t.add_row(f"Change {label}", _pct(st.chg[label]))
    t.add_row("52w low", _money(st.low52))
    t.add_row("52w high", _money(st.high52))
    t.add_row("In range", f"{_bar(st.range_pos)} {st.range_pos * 100:.0f}%")
    t.add_row("From high", _pct(st.from_high_pct))
    t.add_row("From low", _pct(st.from_low_pct))
    if st.avg_vol20:
        ratio = (st.last_vol or 0) / st.avg_vol20
        t.add_row("Volume", f"{_volume(st.last_vol)} ({ratio:.1f}x 20d avg)")

    title = f'[bold]{sym["ticker"]}[/bold]  {sym["name"] or ""}'
    console.print(Panel(t, title=title, subtitle=sym["industry"] or None, box=box.ROUNDED, expand=False))
    target = updater.expected_last_session().isoformat()
    if st.last_date < target:
        console.print(f"[yellow]⚠ stored data ends {_date(st.last_date)}; run stk update[/yellow]")
    if st.sessions < 253:
        console.print("[dim]1Y change needs about a year of stored history; some figures may show '-'.[/dim]")
    console.print("[dim]Based on stored end-of-day prices. Use stk quote for the latest price.[/dim]")
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    conn = db.connect()
    action = args.action
    names = args.symbols

    if action in ("add", "remove"):
        if not names:
            console.print(f"Usage: stk watch {action} SYMBOL [SYMBOL ...]")
            return 1
        if action == "add":
            added = queries.watch_add(conn, names)
            console.print(f"[green]✓[/green] added: {', '.join(added)}" if added else "Already on your watchlist.")
        else:
            removed = queries.watch_remove(conn, names)
            console.print(f"[green]✓[/green] removed: {', '.join(removed)}" if removed else "None of those were on your watchlist.")
        return 0

    if names:
        console.print("Use: stk watch add|remove SYMBOL ...   (or just: stk watch)")
        return 1

    items = queries.watch_list(conn)
    if not items:
        console.print("Your watchlist is empty. Add stocks with: [bold]stk watch add TCS INFY[/bold]")
        return 0
    if action == "list":
        console.print(", ".join(items))
        return 0
    return show_quotes(items)


def cmd_doctor(_: argparse.Namespace) -> int:
    """Quick self-check, handy when something doesn't work on the phone."""
    import platform

    import requests

    console.print(f"stk {__version__}  |  Python {platform.python_version()}  |  {platform.system()}")
    ok = True
    try:
        r = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/RELIANCE.NS",
            params={"range": "1d"},
            timeout=10,
            headers=YAHOO_HEADERS,
        )
        if r.status_code == 200:
            console.print("[green]✓[/green] data source reachable")
        elif r.status_code == 429:
            ok = False
            console.print(
                "[red]✗[/red] data source is rate limiting this connection (HTTP 429).\n"
                "  Wait 30-60 minutes, or switch between Wi-Fi and mobile data."
            )
        else:
            ok = False
            console.print(f"[red]✗[/red] data source answered HTTP {r.status_code}")
    except requests.RequestException as exc:
        ok = False
        console.print(f"[red]✗[/red] cannot reach data source ({exc.__class__.__name__}) - check internet")

    try:
        conn = db.connect()
        n = _symbol_count(conn)
        console.print(f"[green]✓[/green] database ok ({db.db_path()}), {n} symbols")
    except sqlite3.Error as exc:
        ok = False
        console.print(f"[red]✗[/red] database problem: {exc}")
    return 0 if ok else 1


def cmd_probe(_: argparse.Namespace) -> int:
    """Find out which requests the data source refuses (for 429 problems)."""
    console.print("Testing the data source with a few small requests (about 20 seconds)...")
    console.print(f"[dim]{probe.environment()}[/dim]\n")
    results = probe.run(on_result=lambda r: console.print(
        f'{"✓" if r.status == 200 else "✗"} {r.status or "---"}  {r.label}'
        + (f"  [dim]{r.detail}[/dim]" if r.detail else "")
    ))
    console.print("\n[bold]Copy everything above and send it back.[/bold]")
    return 0 if any(r.status == 200 for r in results) else 1


# ------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="stk", description="Personal NSE/BSE stock screener")
    p.add_argument("--version", action="version", version=f"stk {__version__}")
    sub = p.add_subparsers(dest="command")

    q = sub.add_parser("quote", help="latest price (no names = your watchlist)")
    q.add_argument("symbols", nargs="*", help="e.g. RELIANCE TCS, RELIANCE.BO, 500325, ^NSEI")
    q.add_argument("--bse", action="store_true", help="look up on BSE first")
    q.add_argument("--watch", type=int, metavar="SECS", help="refresh every SECS seconds (min 15)")
    q.set_defaults(func=cmd_quote)

    i = sub.add_parser("init", help="create the database and load the Nifty 500 list")
    i.add_argument("--file", metavar="CSV", help="load symbols from your own CSV (needs a Symbol column)")
    i.set_defaults(func=cmd_init)

    u = sub.add_parser("update", help="download or refresh daily prices")
    u.add_argument("symbols", nargs="*", help="only these symbols (default: all)")
    u.add_argument("--full", action="store_true", help="re-download everything from scratch")
    u.add_argument("--years", type=int, choices=[1, 2, 5, 10], default=2, help="history to download (default 2)")
    u.add_argument("--workers", type=int, default=2, help="parallel downloads (default 2)")
    u.add_argument("--delay", type=float, default=0.8, help="seconds between requests (default 0.8)")
    u.set_defaults(func=cmd_update)

    s = sub.add_parser("status", help="what is stored and how fresh it is")
    s.set_defaults(func=cmd_status)

    h = sub.add_parser("history", help="stored daily prices for a stock")
    h.add_argument("symbol")
    h.add_argument("-d", "--days", type=int, default=30, help="number of sessions (default 30)")
    h.add_argument("--ohlc", action="store_true", help="also show open, high, low")
    h.set_defaults(func=cmd_history)

    f = sub.add_parser("find", help="search symbols and company names")
    f.add_argument("query", nargs="+")
    f.add_argument("--limit", type=int, default=20)
    f.set_defaults(func=cmd_find)

    n = sub.add_parser("info", help="52-week range, returns and volume for a stock")
    n.add_argument("symbol")
    n.set_defaults(func=cmd_info)

    w = sub.add_parser("watch", help="watchlist: add, remove, list, or show live prices")
    w.add_argument("action", nargs="?", choices=["add", "remove", "list"])
    w.add_argument("symbols", nargs="*")
    w.set_defaults(func=cmd_watch)

    d = sub.add_parser("doctor", help="check setup and connectivity")
    d.set_defaults(func=cmd_doctor)

    pr = sub.add_parser("probe", help="diagnose which requests the data source refuses (429 errors)")
    pr.set_defaults(func=cmd_probe)
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        return args.func(args)
    except KeyboardInterrupt:
        console.print("\nStopped.")
        return 130
    except sqlite3.Error as exc:
        console.print(f"[red]Database error:[/red] {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
