"""stk command line interface (Termux friendly).

    stk init                      set up the database and Nifty 500 list
    stk update                    download / refresh daily prices
    stk status                    what is stored, and how fresh it is
    stk quote RELIANCE            latest price (live, a few minutes delayed)
    stk history RELIANCE -d 30    stored daily prices
    stk info RELIANCE             52-week range, returns, RSI, trend, volume
    stk screen                    list screens; `stk screen oversold` runs one
    stk screen --rsi-below 30 --above-sma 200     your own filters
    stk find tata                 search symbols and company names
    stk watch add TCS INFY        manage the watchlist; `stk watch` shows prices
    stk alert add TCS above 2200  alerts; `stk alert check` tests them
    stk telegram setup            get alerts and daily summaries on Telegram
    stk daily / stk schedule      automatic daily run
    stk doctor                    check setup and connectivity
    stk                           home screen with a menu
"""

from __future__ import annotations

import argparse
import csv
import json
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
from ..core import db, fundamentals, indicators, probe, queries, screens, symbols, updater
from . import style as sty
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
    return Text(f"{v:+.2f}%", style=sty.pct_style(v))


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
    style = sty.pct_style(q.change_pct)
    style = style if style.startswith("bold") else f"bold {style}".strip()
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
    t.add_row("Price", Text(f"₹{q.price:,.2f}", style=sty.price_style(q.change)))
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
            Text(f"{q.price:,.2f}", style=sty.price_style(q.change)),
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


LIVE_MIN, LIVE_DEFAULT = 3, 5  # seconds between live refreshes


def show_quotes(symbols_: list, bse: bool = False, watch_secs: int = 0) -> int:
    def fetch():
        return get_quotes(symbols_, prefer_bse=bse, use_cache=not watch_secs)

    if watch_secs:
        base = max(watch_secs, LIVE_MIN)
        interval = base
        try:
            with Live(render(fetch()), console=console, refresh_per_second=2) as live:
                while True:
                    time.sleep(interval)
                    quotes = fetch()
                    got = any(isinstance(q, Quote) and q.source == "live" for q in quotes)
                    # if the source pushes back, slow down instead of hammering it
                    interval = base if got else min(interval * 2, 60)
                    body = render(quotes)
                    if interval > base:
                        body = Group(body, Text(f"source not answering - retrying every {interval}s", style="yellow"))
                    else:
                        body = Group(body, Text(f"refreshing every {base}s - Ctrl+C to stop", style="dim"))
                    live.update(body)
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
            retry_unavailable=args.retry_missing,
        )

    secs = (datetime.fromisoformat(result.finished) - datetime.fromisoformat(result.started)).total_seconds()
    left = f" · left {result.not_done}" if result.not_done else ""
    unavailable = len(result.unavailable) + result.unavailable_skipped
    gone = f" · not on Yahoo {unavailable}" if unavailable else ""
    console.print(
        f"[green]✓[/green] updated {result.ok} · already current {result.skipped} · "
        f"failed {len(result.failures)}{left}{gone} · {result.rows:,} rows · {secs:.0f}s"
    )
    if result.unavailable:
        console.print(
            f"[dim]Yahoo has no data for: {', '.join(result.unavailable)}. "
            "They won't be asked about again for 30 days (or use --retry-missing).[/dim]"
        )
    for ticker, msg in result.failures[:5]:
        console.print(f"  [red]✗[/red] {msg}")
    if len(result.failures) > 5:
        console.print(f"  ...and {len(result.failures) - 5} more (see stk status)")
    if any("rate limited" in m for _, m in result.failures):
        console.print("[dim]Some stocks were rate limited. Run stk update again to retry just those.[/dim]")

    if result.interrupted:
        console.print("[yellow]Stopped early. Progress is saved; run stk update to continue.[/yellow]")

    _refresh_indicators(conn, result.updated_ids)

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


def _refresh_indicators(conn: sqlite3.Connection, extra_ids=()) -> int:
    """Recalculate indicators for stocks with new prices and any that are stale."""
    ids = set(extra_ids) | set(indicators.stale_symbol_ids(conn))
    if not ids:
        return 0
    if len(ids) > 20:
        console.print(f"Calculating indicators for {len(ids)} stocks...")
    n = indicators.refresh(conn, ids)
    console.print(f"[green]✓[/green] indicators ready for {n} stocks")
    return n


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
    if s["unavailable"]:
        t.add_row("Not on Yahoo", f'{s["unavailable"]} (skipped on update)')
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
    if s["with_data"]:
        if s["indicators_stale"] == 0:
            t.add_row("Indicators", "[green]✓ up to date[/green]")
        else:
            t.add_row("Indicators", f'[yellow]{s["indicators_stale"]} to recalculate[/yellow] - run stk update')
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
    _vs = [r["volume"] for r in rows if r["volume"] is not None]
    avg_vol = sum(_vs) / len(_vs) if _vs else None
    for r in rows:
        cells = [_date(r["date"])]
        if args.ohlc:
            cells += [_money(r["open"]), _money(r["high"]), _money(r["low"])]
        ratio = (r["volume"] / avg_vol) if (r["volume"] is not None and avg_vol) else None
        vol = Text(_volume(r["volume"]), style=sty.volume_style(ratio, r["chg_pct"]))
        cells += [Text(_money(r["close"]), style=sty.pct_style(r["chg_pct"]).replace("bold ", "")), _pct(r["chg_pct"]), vol]
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


def _ago(days: int) -> str:
    return "today" if days == 0 else f"{days}d ago"


def _trend_text(ind: dict) -> str:
    above = None
    if ind.get("sma50") and ind.get("sma200"):
        above = ind["sma50"] > ind["sma200"]
    state = "50d above 200d" if above else "50d below 200d" if above is not None else ""
    if ind.get("golden_cross_days") is not None:
        return f'golden cross {_ago(ind["golden_cross_days"])}' + (f" ({state})" if state else "")
    if ind.get("death_cross_days") is not None:
        return f'death cross {_ago(ind["death_cross_days"])}' + (f" ({state})" if state else "")
    return state


def _macd_text(ind: dict) -> str:
    hist = ind.get("macd_hist")
    if hist is None:
        return ""
    text = "bullish" if hist > 0 else "bearish"
    if ind.get("macd_cross_up_days") is not None:
        text += f', crossed up {_ago(ind["macd_cross_up_days"])}'
    elif ind.get("macd_cross_down_days") is not None:
        text += f', crossed down {_ago(ind["macd_cross_down_days"])}'
    return text


FUND_ROWS = [
    ("market_cap_cr", "Market cap (Cr)"), ("pe", "P/E"), ("pb", "P/B"), ("eps", "EPS"), ("div_yield", "Dividend %"),
    ("roe", "ROE"), ("roa", "ROA"), ("profit_margin", "Net margin"), ("op_margin", "Operating margin"),
    ("debt_equity", "Debt / equity"), ("rev_growth", "Revenue growth"), ("earn_growth", "Earnings growth"),
]


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
    t.add_row(
        "In range",
        Text(f"{_bar(st.range_pos)} {st.range_pos * 100:.0f}%", style="green" if st.range_pos >= 0.66 else "red" if st.range_pos <= 0.33 else ""),
    )
    t.add_row("From high", _pct(st.from_high_pct))
    t.add_row("From low", _pct(st.from_low_pct))
    ind = indicators.compute_symbol(conn, sym["id"])
    if ind:
        t.add_row("", "")
        rsi = ind["rsi14"]
        if rsi is not None:
            tag = " (oversold)" if rsi < 30 else " (overbought)" if rsi > 70 else ""
            t.add_row("RSI (14)", Text(f"{rsi:.1f}{tag}", style=sty.rsi_style(rsi)))
        for n in (50, 200):
            avg = ind[f"sma{n}"]
            if avg:
                t.add_row(f"vs {n}-day avg", _pct((ind["close"] / avg - 1) * 100))
        trend = _trend_text(ind)
        if trend:
            t.add_row("Trend", trend)
        macd = _macd_text(ind)
        if macd:
            t.add_row("MACD", macd)
        if ind["vol_ratio"] is not None:
            t.add_row(
                "Volume",
                Text(f'{_volume(ind["volume"])} ({ind["vol_ratio"]:.1f}x avg)', style=sty.volume_style(ind["vol_ratio"], ind["chg_1d"])),
            )

    fund = fundamentals.get(conn, sym["id"])
    if fund and any(fund[c] is not None for c in indicators.FUND_COLUMNS):
        t.add_row("", "")
        for name, label in FUND_ROWS:
            if fund.get(name) is not None:
                t.add_row(label, _fmt_field(name, fund[name]))
    title = f'[bold]{sym["ticker"]}[/bold]  {sym["name"] or ""}'
    console.print(Panel(t, title=title, subtitle=sym["industry"] or None, box=box.ROUNDED, expand=False))
    if fund is None:
        console.print(f'[dim]No fundamentals yet. Run: stk fundamentals update {sym["ticker"]}[/dim]')
    elif all(fund[c] is None for c in indicators.FUND_COLUMNS):
        console.print("[dim]The data source has no fundamentals for this stock.[/dim]")
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
    if args.once or not sys.stdout.isatty():
        return show_quotes(items)
    return show_quotes(items, watch_secs=args.every or LIVE_DEFAULT)


def cmd_fundamentals(args: argparse.Namespace) -> int:
    conn = db.connect()
    action = args.action or "status"
    if action == "probe":
        console.print("[bold]Checking the fundamentals source...[/bold]")
        steps = fundamentals.probe()
        for label, ok, detail in steps:
            console.print(f'{"[green]✓[/green]" if ok else "[red]✗[/red]"} {label}  [dim]{detail}[/dim]')
        if not all(ok for _, ok, _ in steps):
            console.print("\n[bold]Copy everything above and send it back.[/bold]")
            return 1
        return 0
    if action == "status":
        cov = fundamentals.coverage(conn)
        if not cov["stored"]:
            console.print("No fundamentals yet. Try: [bold]stk fundamentals probe[/bold], then [bold]stk fundamentals update[/bold]")
            return 0
        console.print(f"Fundamentals stored for {cov['stored']} of {cov['active']} stocks ({cov['with_pe']} with a P/E).")
        console.print(f"Oldest {cov['oldest'][:10]} · newest {cov['newest'][:10]}. Refresh: stk fundamentals update --all")
        return 0

    if args.symbols:
        names = args.symbols
    elif args.all:
        names = None
    else:
        names = queries.watch_list(conn)
        if not names:
            console.print("Your watchlist is empty. Give symbols (stk fundamentals update TCS INFY) or use --all.")
            return 1
    missing = [n for n in (names or []) if not conn.execute("SELECT 1 FROM symbols WHERE ticker = ?", (n.upper(),)).fetchone()]
    if missing:
        console.print(f"[yellow]Not in your database (skipped): {', '.join(missing)}[/yellow]")
    with Progress(SpinnerColumn(), TextColumn("{task.description}"), BarColumn(), TaskProgressColumn(), console=console, transient=True) as bar:
        task = bar.add_task("fundamentals", total=None)

        def tick(i: int, n: int, ticker: str) -> None:
            bar.update(task, total=n, completed=i - 1, description=f"fundamentals {ticker}")

        res = fundamentals.refresh(conn, names, force=args.force, delay=args.delay, progress=tick)
    console.print(
        f"[green]✓[/green] downloaded {res.ok} · recent already {res.skipped} · no data at source {res.empty} · failed {len(res.failed)}"
    )
    for t, msg in res.failed[:8]:
        console.print(f"  [red]✗[/red] {msg}")
    if res.aborted:
        console.print("[yellow]Stopped early: the data source kept refusing. Wait a few minutes and run it again (finished stocks are kept).[/yellow]")
        console.print("Run [bold]stk fundamentals probe[/bold] to see what is wrong.")
        return 1
    return 0


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


# ------------------------------------------------------------- screen command
def _fmt_field(name: str, v) -> Text:
    if v is None:
        return Text("-", style="dim")
    kind = indicators.FIELD_BY_NAME[name].kind
    if kind == "pct":
        return Text(f"{v:+.1f}%", style=sty.pct_style(v))
    if kind == "price":
        return Text(f"{v:,.1f}")
    if kind == "rsi":
        return Text(f"{v:.1f}", style=sty.rsi_style(v))
    if kind == "macd":
        return Text(f"{v:+.3f}", style="green" if v > 0 else "red" if v < 0 else "")
    if kind == "ratio":
        return Text(f"{v:.1f}x", style=sty.volume_style(v))
    if kind == "num":
        return Text(f"{v:,.1f}" if abs(v) >= 10 or name == "pe" else f"{v:,.2f}")
    if kind == "pct0":
        return Text(f"{v:.0f}%", style="green" if v >= 15 else "red" if v < 0 else "")
    if kind == "days":
        return Text(f"{int(v)}d")
    if kind == "cr":
        return Text(f"{v:,.1f}Cr" if v < 10 else f"{v:,.0f}Cr")
    if kind == "int":
        return Text(_volume(v) if name in ("volume", "avg_vol20") else f"{int(v):,}")
    return Text(str(v))


def render_screen(res: screens.ScreenResult, show_names: bool = False) -> Table:
    t = Table(box=box.SIMPLE_HEAD, header_style="bold", pad_edge=False)
    t.add_column("Symbol")
    if show_names:
        t.add_column("Name", no_wrap=True, overflow="ellipsis", max_width=16)
    t.add_column("Close", justify="right")
    for f in res.show:
        t.add_column(indicators.FIELD_BY_NAME[f].short, justify="right")
    for r in res.rows:
        cells = [r["ticker"]]
        if show_names:
            cells.append(r["name"] or "-")
        cells.append(_fmt_field("close", r["close"]))
        cells += [_fmt_field(f, r.get(f)) for f in res.show]
        t.add_row(*cells)
    return t


def _screen_list(conn: sqlite3.Connection) -> int:
    t = Table(box=box.SIMPLE_HEAD, header_style="bold", pad_edge=False)
    t.add_column("Screen")
    t.add_column("Finds")
    for it in screens.list_all(conn):
        t.add_row(it["name"] + ("*" if it["source"] == "saved" else ""), it["description"])
    console.print(t)
    console.print("[dim]* = your saved screens. Screens flag candidates from end-of-day data; not advice.[/dim]")
    console.print("Run one:   [bold]stk screen oversold[/bold]")
    console.print("Your own:  [bold]stk screen --rsi-below 30 --above-sma 200[/bold]")
    console.print("Save it:   [bold]stk screen save mine --rsi-below 30 --above-sma 200[/bold]")
    console.print("Fields:    [bold]stk screen fields[/bold]")
    return 0


def _screen_fields() -> int:
    t = Table(box=box.SIMPLE_HEAD, header_style="bold", pad_edge=False)
    t.add_column("Field")
    t.add_column("Meaning")
    for f in indicators.FIELDS:
        t.add_row(f.name, f.help)
    t.add_row("ticker, name, industry", "Text. Match part of it with ~, e.g. industry~bank")
    console.print(t)
    console.print("Use with [bold]--where[/bold], for example:")
    console.print('  stk screen --where "rsi14<35" --where "close>sma200" --where "vol_ratio>=1.5"')
    console.print('  stk screen --where "close>sma50*1.05" --industry bank')
    return 0


def _screen_csv(path: str, res: screens.ScreenResult) -> str:
    out = Path(path).expanduser()
    cols = ["ticker", "name", "industry"] + indicators.COLUMNS
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for r in res.all_rows:
            w.writerow([r.get(c) for c in cols])
    return str(out)


def _screen(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    words = list(args.words or [])
    action = words[0].lower() if words and words[0].lower() in screens.RESERVED else None
    extra = screens.conditions_from_options(
        rsi_below=args.rsi_below,
        rsi_above=args.rsi_above,
        above_sma=args.above_sma,
        below_sma=args.below_sma,
        min_volume_ratio=args.min_volume_ratio,
        min_turnover=args.min_turnover,
        industry=args.industry,
        where=args.where,
    )
    order = "asc" if args.asc else "desc" if args.desc else None

    if action == "fields":
        return _screen_fields()
    if action == "list" or (not words and not extra):
        return _screen_list(conn)

    if action in ("show", "delete", "save"):
        if len(words) != 2:
            console.print(f"Usage: stk screen {action} NAME" + (" [filters]" if action == "save" else ""))
            return 1
        name = words[1]
        if action == "delete":
            if screens.delete(conn, name):
                console.print(f"[green]✓[/green] deleted {name.lower()}")
                return 0
            console.print(f'No saved screen called "{name}".')
            return 1
        if action == "show":
            defn, source = screens.get(conn, name)
            console.print(f"[bold]{name.lower()}[/bold] ({source})")
            if defn.get("description"):
                console.print(defn["description"])
            console.print(f"Conditions: {screens.describe(defn)}")
            console.print(json.dumps(defn, indent=2), markup=False, highlight=False)
            return 0
        if not extra:
            console.print("Add some filters to save, for example:")
            console.print(f"  [bold]stk screen save {name.lower()} --rsi-below 30 --above-sma 200[/bold]")
            return 1
        defn = screens.merge(None, extra, sort=args.sort, order=order, limit=args.limit)
        verb = screens.save(conn, name, defn)
        console.print(f"[green]✓[/green] {verb} screen [bold]{name.lower()}[/bold]: {screens.describe(defn)}")
        console.print(f"Run it with: [bold]stk screen {name.lower()}[/bold]")
        return 0

    base, title = None, "custom screen"
    if words:
        if len(words) > 1:
            raise screens.ScreenError("Give one screen name, for example: stk screen oversold")
        base, _source = screens.get(conn, words[0])
        title = words[0].lower()
    defn = screens.merge(base, extra, sort=args.sort, order=order, limit=args.limit)
    screens.validate(defn)

    if conn.execute("SELECT 1 FROM prices_daily LIMIT 1").fetchone() is None:
        console.print("No prices stored yet. Run [bold]stk init[/bold], then [bold]stk update[/bold].")
        return 1
    _refresh_indicators(conn)
    if screens.uses_fundamentals(defn) and fundamentals.coverage(conn)["with_pe"] == 0:
        console.print("[yellow]No fundamentals downloaded yet.[/yellow] Run: [bold]stk fundamentals update --all[/bold] (about 7 minutes)")
        return 1
    res = screens.run(conn, defn)

    head = f"[bold]{title}[/bold] · {res.total} match" + ("" if res.total == 1 else "es")
    if res.as_of:
        head += f" · data to {_date(res.as_of)}"
    console.print(head)
    console.print(f"[dim]{screens.describe(defn)}[/dim]")
    if res.as_of and res.as_of < updater.expected_last_session().isoformat():
        console.print("[yellow]⚠ stored prices are behind; run stk update for the latest[/yellow]")
    if screens.uses_fundamentals(defn):
        cov = fundamentals.coverage(conn)
        console.print(f"[dim]Fundamentals for {cov['with_pe']} of {cov['active']} stocks (updated {cov['newest'][:10]}); stocks without the data are skipped.[/dim]")
    if res.total == 0:
        console.print("No stocks match right now.")
    else:
        console.print(render_screen(res, show_names=args.names))
        if res.total > len(res.rows):
            console.print(f"[dim]Showing {len(res.rows)} of {res.total}. More: --limit 100, or --csv FILE[/dim]")
    if args.csv:
        console.print(f"[green]✓[/green] saved {res.total} rows to {_screen_csv(args.csv, res)}")
    return 0


def cmd_screen(args: argparse.Namespace) -> int:
    conn = db.connect()
    try:
        return _screen(conn, args)
    except screens.ScreenError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1


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


def cmd_home(_: argparse.Namespace) -> int:
    from . import home

    return home.run(console, main)


# ------------------------------------------------------- alerts & telegram (4B)
def _notify(text: str) -> None:
    """Send to Telegram if it is set up; say so (never crash) if it fails."""
    from ..core import notify

    if not notify.configured():
        return
    try:
        notify.send(text)
        console.print("[dim]sent to Telegram[/dim]")
    except notify.NotifyError as exc:
        console.print(f"[yellow]Telegram: {exc}[/yellow]")


def _print_fired(fired: list) -> None:
    for a in fired:
        console.print(f"[bold yellow]🔔[/bold yellow] {a['message']}")


def _alert_table(rows: list) -> Table:
    from ..core import alerts

    t = Table(box=box.SIMPLE_HEAD, header_style="bold", pad_edge=False)
    for c in ("ID", "Alert", "Status"):
        t.add_column(c)
    for a in rows:
        if a["active"]:
            status = Text("watching", style="green")
        else:
            status = Text(f"fired {a['triggered_at'][:16]}", style="yellow")
        t.add_row(str(a["id"]), alerts.describe(a), status)
    return t


def cmd_alert(args: argparse.Namespace) -> int:
    from ..core import alerts

    conn = db.connect()
    action, words = args.action or "list", args.words
    try:
        if action == "add":
            if len(words) != 3:
                console.print("Usage: [bold]stk alert add SYMBOL TYPE VALUE[/bold]")
                for kind, (_, meaning, example) in alerts.KINDS.items():
                    console.print(f"  {example:<34}[dim]{meaning}[/dim]")
                return 1
            aid = alerts.add(conn, words[0], words[1], words[2])
            a = [x for x in alerts.list_all(conn) if x["id"] == aid][0]
            console.print(f"[green]✓[/green] alert {aid}: {alerts.describe(a)}")
            return 0
        if action in ("remove", "reset"):
            ids = [int(w) for w in words if w.isdigit()]
            if not ids:
                console.print(f"Usage: stk alert {action} ID [ID ...]")
                return 1
            n = (alerts.remove if action == "remove" else alerts.reset)(conn, ids)
            console.print(f"[green]✓[/green] {action}d {n} alert(s)" if n else "No alert with that ID.")
            return 0
        if action == "check":
            return _alert_check(conn, args.loop, args.no_notify)
    except alerts.AlertError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1
    rows = alerts.list_all(conn)
    if not rows:
        console.print("No alerts yet. Example: [bold]stk alert add TCS above 2200[/bold]")
        return 0
    console.print(_alert_table(rows))
    return 0


def _alert_check(conn, loop: int, no_notify: bool) -> int:
    from ..core import alerts

    def once() -> None:
        fired = alerts.check(conn)
        if fired:
            _print_fired(fired)
            if not no_notify:
                _notify("🔔 stk alerts\n" + "\n".join(f"• {a['message']}" for a in fired))
        else:
            console.print(f"[dim]{datetime.now(IST).strftime('%H:%M')} no alerts fired ({len(alerts.list_all(conn, True))} watching)[/dim]")

    if not loop:
        once()
        return 0
    interval = max(loop, 60)  # don't hammer the data source
    console.print(f"Checking every {interval}s - Ctrl+C to stop.")
    try:
        while True:
            once()
            if not alerts.list_all(conn, True):
                console.print("All alerts have fired.")
                return 0
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0


def cmd_telegram(args: argparse.Namespace) -> int:
    from ..core import notify

    if args.action == "test":
        try:
            notify.send("✓ stk can reach you on Telegram.")
        except notify.NotifyError as exc:
            console.print(f"[red]{exc}[/red]")
            return 1
        console.print("[green]✓[/green] test message sent")
        return 0
    if args.action == "status":
        console.print("Telegram: [green]set up[/green]" if notify.configured() else "Telegram: not set up (stk telegram setup)")
        return 0
    console.print(
        "1. In Telegram open [bold]@BotFather[/bold] and send /newbot, then follow the steps.\n"
        "2. Copy the token it gives you (looks like 123456:ABC...).\n"
        "3. Open your new bot and send it any message, e.g. hello."
    )
    token = input("Paste the token here > ").strip()
    if not token:
        return 1
    input("Press Enter after you have messaged the bot > ")
    try:
        chat = notify.find_chat_id(token)
    except notify.NotifyError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1
    if not chat:
        console.print("[red]The bot has no messages yet.[/red] Send it one, then run: stk telegram setup")
        return 1
    notify.save(token, chat)
    console.print("[green]✓[/green] saved")
    return cmd_telegram(argparse.Namespace(action="test"))


def cmd_daily(args: argparse.Namespace) -> int:
    """Update prices, check alerts, send the summary. Made for a scheduler."""
    from ..core import alerts, daily

    rc = cmd_update(
        argparse.Namespace(symbols=[], full=False, years=2, workers=2, delay=0.8, retry_missing=False)
    )
    conn = db.connect()
    try:  # keep the watchlist's fundamentals fresh (weekly); never let this break the run
        fundamentals.refresh(conn, queries.watch_list(conn))
    except Exception as exc:  # noqa: BLE001
        console.print(f"[dim]fundamentals skipped: {exc}[/dim]")
    fired = alerts.check(conn)
    text = daily.summary(conn, fired)
    console.print(text)
    if not args.no_notify:
        _notify(text)
    return rc


SCHEDULE_HELP = """[bold]Run stk by itself every day (Termux)[/bold]

One-time setup:
  pkg install cronie termux-services
  (close and reopen Termux)
  sv-enable crond
  termux-wake-lock          [dim]# keeps Termux alive in the background[/dim]

Then open the schedule with [bold]crontab -e[/bold] and add:
  30 16 * * 1-5  stk daily                [dim]# 4:30 pm Mon-Fri: update + alerts + summary[/dim]
  */15 9-15 * * 1-5  stk alert check      [dim]# every 15 min in market hours[/dim]

Android may stop background apps to save battery. If nothing arrives, set
Termux to "unrestricted" in the phone's battery settings.
Check that Telegram works first: [bold]stk telegram test[/bold]"""


def cmd_schedule(_: argparse.Namespace) -> int:
    console.print(SCHEDULE_HELP)
    return 0


# ------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="stk", description="Personal NSE/BSE stock screener")
    p.add_argument("--version", action="version", version=f"stk {__version__}")
    p.add_argument("--no-color", action="store_true", help="plain output (also honours the NO_COLOR variable)")
    p.add_argument("--no-splash", action="store_true", help="print help instead of the home screen")
    sub = p.add_subparsers(dest="command")

    q = sub.add_parser("quote", help="latest price (no names = your watchlist)")
    q.add_argument("symbols", nargs="*", help="e.g. RELIANCE TCS, RELIANCE.BO, 500325, ^NSEI")
    q.add_argument("--bse", action="store_true", help="look up on BSE first")
    q.add_argument("--watch", type=int, nargs="?", const=LIVE_DEFAULT, metavar="SECS", help="live view, refreshing every SECS seconds (default 5, min 3)")
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
    u.add_argument("--retry-missing", action="store_true", help="ask again about stocks Yahoo had no data for")
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
    w.add_argument("--once", action="store_true", help="print the prices once instead of a live view")
    w.add_argument("--every", type=int, metavar="SECS", help="live refresh interval (default 5, min 3)")
    w.set_defaults(func=cmd_watch)

    sc = sub.add_parser("screen", help="find stocks: built-in screens, your own filters, saved screens")
    sc.add_argument("words", nargs="*", help="a screen name, or: list | fields | show NAME | save NAME | delete NAME")
    sc.add_argument("--rsi-below", type=float, metavar="N", help="RSI under N")
    sc.add_argument("--rsi-above", type=float, metavar="N", help="RSI over N")
    sc.add_argument("--above-sma", type=int, metavar="N", help="price above its N-day average (20, 50 or 200)")
    sc.add_argument("--below-sma", type=int, metavar="N", help="price below its N-day average (20, 50 or 200)")
    sc.add_argument("--min-volume-ratio", type=float, metavar="X", help="volume at least X times its 20-day average")
    sc.add_argument("--min-turnover", type=float, metavar="CR", help="average daily traded value of at least CR crore")
    sc.add_argument("--industry", metavar="TEXT", help="industry contains TEXT")
    sc.add_argument("--where", action="append", metavar="EXPR", help='any condition, e.g. "chg_1m<-5" (repeatable)')
    sc.add_argument("--sort", metavar="FIELD", help="sort by this field")
    sc.add_argument("--asc", action="store_true", help="smallest first")
    sc.add_argument("--desc", action="store_true", help="largest first")
    sc.add_argument("--limit", type=int, metavar="N", help="rows to show (default 25)")
    sc.add_argument("--names", action="store_true", help="show company names")
    sc.add_argument("--csv", metavar="FILE", help="also save every match to a CSV file")
    sc.set_defaults(func=cmd_screen)

    al = sub.add_parser("alert", help="alerts: add, list, remove, reset, check")
    al.add_argument("action", nargs="?", choices=["add", "list", "remove", "reset", "check"])
    al.add_argument("words", nargs="*", help="add: SYMBOL TYPE VALUE (e.g. TCS above 2200); remove/reset: IDs")
    al.add_argument("--loop", type=int, metavar="SECS", help="check: repeat every SECS seconds (min 60)")
    al.add_argument("--no-notify", action="store_true", help="check: do not send to Telegram")
    al.set_defaults(func=cmd_alert)

    tg = sub.add_parser("telegram", help="set up and test Telegram messages")
    tg.add_argument("action", nargs="?", choices=["setup", "test", "status"], default="setup")
    tg.set_defaults(func=cmd_telegram)

    dl = sub.add_parser("daily", help="update prices, check alerts and send the summary (for a scheduler)")
    dl.add_argument("--no-notify", action="store_true", help="do not send to Telegram")
    dl.set_defaults(func=cmd_daily)

    sch = sub.add_parser("schedule", help="how to run stk automatically every day")
    sch.set_defaults(func=cmd_schedule)

    hm = sub.add_parser("home", help="home screen with menu (also: just run stk)")
    hm.set_defaults(func=cmd_home)

    fu = sub.add_parser("fundamentals", aliases=["fund"], help="P/E, ROE, margins, growth: download and check")
    fu.add_argument("action", nargs="?", choices=["update", "status", "probe"])
    fu.add_argument("symbols", nargs="*")
    fu.add_argument("--all", action="store_true", help="every stock in the database (about 7 minutes)")
    fu.add_argument("--force", action="store_true", help="download again even if fetched this week")
    fu.add_argument("--delay", type=float, default=0.8, help="seconds between requests (default 0.8)")
    fu.set_defaults(func=cmd_fundamentals)

    d = sub.add_parser("doctor", help="check setup and connectivity")
    d.set_defaults(func=cmd_doctor)

    pr = sub.add_parser("probe", help="diagnose which requests the data source refuses (429 errors)")
    pr.set_defaults(func=cmd_probe)
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.no_color:
        console.no_color = True
    if not getattr(args, "func", None):
        if sys.stdout.isatty() and not args.no_splash:
            return cmd_home(args)
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
