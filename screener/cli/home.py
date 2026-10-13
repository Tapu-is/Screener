"""The `stk` home screen: logo, a short info block and a numbered menu.

Everything that decides *what* to show or run is a plain function (easy to test);
only `run` talks to the terminal.
"""

from __future__ import annotations

import shlex
from datetime import datetime, timedelta
from typing import Callable, Optional

from .. import __version__
from ..core import queries, screens
from ..core.quote import IST, Quote, get_quotes, is_market_open

# Eight small candles rising into the STK letters, in the same 15 x 6 space the
# three big ones used. Each is (colour, [(row, character), ...]); column = 2 * index.
_W, _B = "│", "█"
CANDLE_SPEC = [
    ("red", [(3, _W), (4, _B), (5, _W)]),
    ("green", [(2, _W), (3, _B), (4, _B), (5, _W)]),
    ("red", [(2, _W), (3, _B), (4, _W)]),
    ("green", [(1, _W), (2, _B), (3, _B), (4, _W)]),
    ("green", [(1, _W), (2, _B), (3, _W)]),
    ("red", [(2, _W), (3, _B), (4, _W)]),
    ("green", [(0, _W), (1, _B), (2, _B), (3, _W)]),
    ("green", [(0, _W), (1, _B), (2, _B), (3, _W)]),
]
CANDLE_ROWS, CANDLE_WIDTH = 6, 15


def _candle_grid() -> list[list[tuple[str, str]]]:
    grid = [[(" ", "") for _ in range(CANDLE_WIDTH)] for _ in range(CANDLE_ROWS)]
    for i, (colour, cells) in enumerate(CANDLE_SPEC):
        for row, ch in cells:
            grid[row][i * 2] = (ch, colour)
    return grid


CANDLES = ["".join(ch for ch, _ in row).rstrip() for row in _candle_grid()]
LETTERS = [
    "                       ",
    "███████ ████████ ██  ██",
    "██         ██    ██ ██ ",
    "███████    ██    ████  ",
    "     ██    ██    ██ ██ ",
    "███████    ██    ██  ██",
]
LOGO_WIDTH = CANDLE_WIDTH + 3 + 23


def logo_lines(color: bool = True) -> list[str]:
    """Rich-markup lines for the logo (plain text when color is off)."""
    out = []
    for candle, cells, letters in zip(CANDLES, _candle_grid(), LETTERS):
        candle = candle.ljust(CANDLE_WIDTH)
        if color:
            candle = "".join(f"[{c}]{ch}[/{c}]" if c else ch for ch, c in cells)
            letters = f"[bold cyan]{letters}[/bold cyan]"
        out.append((candle + "   " + letters).rstrip())
    return out


def next_open(now: Optional[datetime] = None) -> datetime:
    now = (now or datetime.now(IST)).astimezone(IST)
    d = now.replace(hour=9, minute=15, second=0, microsecond=0)
    if now >= d:
        d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def market_line(now: Optional[datetime] = None) -> str:
    now = (now or datetime.now(IST)).astimezone(IST)
    if is_market_open(now):
        return "[green]● Market open[/green] [dim]· closes 15:30[/dim]"
    nxt = next_open(now)
    when = nxt.strftime("%H:%M") if nxt.date() == now.date() else nxt.strftime("%a %H:%M")
    return f"[red]● Market closed[/red] [dim]· opens {when}[/dim]"


def index_line(label: str, q) -> Optional[str]:
    if not isinstance(q, Quote) or q.change_pct is None:
        return None
    up = q.change_pct >= 0
    colour = "green" if up else "red"
    arrow = "▲" if up else "▼"
    return f"{label:<10}{q.price:>10,.0f}  [{colour}]{arrow} {q.change_pct:+.2f}%[/{colour}]"


def watch_counts(conn) -> tuple[int, int, int]:
    """(total, up, down) from the last two stored closes of each watchlist stock."""
    names = queries.watch_list(conn)
    up = down = 0
    for t in names:
        rows = conn.execute(
            """SELECT p.close FROM prices_daily p JOIN symbols s ON s.id = p.symbol_id
               WHERE s.ticker = ? ORDER BY p.date DESC LIMIT 2""",
            (t,),
        ).fetchall()
        if len(rows) == 2 and rows[0][0] and rows[1][0]:
            if rows[0][0] > rows[1][0]:
                up += 1
            elif rows[0][0] < rows[1][0]:
                down += 1
    return len(names), up, down


def info_lines(conn, indices=None, now=None) -> list[str]:
    lines = [f"[bold]stk[/bold] [dim]v{__version__}[/dim]", "[dim]─────────────────────────────[/dim]", market_line(now)]
    for label, q in indices or []:
        s = index_line(label, q)
        if s:
            lines.append(s)
    st = queries.status(conn)
    if st["with_data"]:
        lines.append(f"Data      {st['with_data']} stocks · to {datetime.strptime(st['latest_date'], '%Y-%m-%d').strftime('%d %b %y')}")
        if st["behind"] or st["indicators_stale"]:
            lines.append("[yellow]Data is behind - run Update (6)[/yellow]")
    else:
        lines.append("[yellow]No data yet - run: stk init, then stk update[/yellow]")
    total, up, down = watch_counts(conn)
    if total:
        lines.append(f"Watchlist {total} · [green]{up}▲[/green] [red]{down}▼[/red] [dim](last close)[/dim]")
    else:
        lines.append("Watchlist empty [dim](stk watch add TCS)[/dim]")
    n_alerts = conn.execute("SELECT COUNT(*) FROM alerts WHERE active = 1").fetchone()[0]
    lines.append(f"Alerts    {n_alerts} watching" if n_alerts else "Alerts    none [dim](stk alert add TCS above 2200)[/dim]")
    return lines


MENU = [
    ("1", "Watchlist & quotes"),
    ("2", "Screens"),
    ("3", "Find / Info"),
    ("4", "History"),
    ("5", "Alerts"),
    ("6", "Update data"),
    ("7", "Status & doctor"),
    ("0", "Quit"),
]


def menu_lines() -> list[str]:
    half = (len(MENU) + 1) // 2
    left, right = MENU[:half], MENU[half:]
    out = []
    for i, (k, label) in enumerate(left):
        cell = f"[bold cyan]{k}[/bold cyan] {label}".ljust(0)
        pad = " " * (24 - len(k) - 1 - len(label))
        r = f"[bold cyan]{right[i][0]}[/bold cyan] {right[i][1]}" if i < len(right) else ""
        out.append(f" {cell}{pad}{r}")
    return out


def screen_names() -> list[str]:
    return list(screens.BUILTIN)


def command_for(choice: str, ask: Callable[[str], str]) -> Optional[list]:
    """Turn what was typed at the menu into an argv list for stk.

    Returns None for "nothing to do", the string "quit" to leave, or a list of
    arguments such as ["screen", "oversold"]. `ask` prompts for follow-up input.
    """
    c = choice.strip()
    if not c:
        return None
    if c.lower() in ("0", "q", "quit", "exit"):
        return "quit"
    if c == "1":
        return ["watch"]
    if c == "2":
        names = screen_names()
        for i, n in enumerate(names, 1):
            print(f"  {i:>2}  {n}")
        pick = ask("Screen number or name (Enter = all) > ").strip()
        if not pick:
            return ["screen"]
        if pick.isdigit() and 1 <= int(pick) <= len(names):
            return ["screen", names[int(pick) - 1]]
        return ["screen", pick]
    if c == "3":
        q = ask("Symbol or name > ").strip()
        if not q:
            return None
        return ["info", q] if len(q.split()) == 1 and q.isalnum() and q.upper() == q else ["find", *q.split()]
    if c == "4":
        s = ask("Symbol > ").strip()
        if not s:
            return None
        d = ask("Days (Enter = 30) > ").strip()
        return ["history", s, "-d", d if d.isdigit() else "30"]
    if c == "5":
        pick = ask("a = list, c = check now, n = new alert (Enter = list) > ").strip().lower()
        if pick in ("c", "check"):
            return ["alert", "check"]
        if pick in ("n", "new", "add"):
            text = ask("SYMBOL TYPE VALUE (e.g. TCS above 2200) > ").strip()
            return ["alert", "add", *text.split()] if text else None
        return ["alert", "list"]
    if c == "6":
        return ["update"]
    if c == "7":
        return ["status"]
    try:
        argv = shlex.split(c)
    except ValueError:
        return None
    return argv[1:] if argv and argv[0] == "stk" else argv


def _indices() -> list:
    try:
        qs = get_quotes(["^NSEI", "^BSESN"])
    except Exception:  # noqa: BLE001 - the home screen must always open
        return []
    return list(zip(["NIFTY 50", "SENSEX"], qs))


def render(console, conn) -> None:
    color = not console.no_color
    logo = logo_lines(color)
    info = info_lines(conn, _indices())
    console.print()
    for line in logo:
        console.print(line, highlight=False)
    console.print()
    for line in info:
        console.print(" " + line, highlight=False)
    console.print()
    for line in menu_lines():
        console.print(line, highlight=False)
    console.print()


def run(console, main_fn) -> int:
    from ..core import db

    conn = db.connect()
    render(console, conn)
    ask = lambda prompt: input(prompt)  # noqa: E731
    while True:
        try:
            choice = ask("[stk] > ")
        except (EOFError, KeyboardInterrupt):
            console.print()
            return 0
        try:
            argv = command_for(choice, ask)
        except (EOFError, KeyboardInterrupt):
            console.print()
            continue
        if argv == "quit":
            return 0
        if not argv:
            continue
        console.print()
        try:
            main_fn(argv)
        except SystemExit:
            pass  # argparse complaining about a typo should not close the menu
        console.print()
