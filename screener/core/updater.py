"""Backfill and incremental update of stored daily prices.

How it works
------------
* First run per symbol: download `years` of history (default 2).
* Later runs: download only from a few days before the last stored date, so
  each daily update is small. The overlap picks up corrected candles.
* If a split or bonus happened in that window, the symbol's history is
  re-downloaded in full, because older prices must be re-adjusted.
* Today's candle is never stored while it is still forming (before 15:45 IST
  on a weekday), so the database only ever holds finished days.
* A symbol already up to date is skipped without any network call.
* Every symbol is committed on its own, so Ctrl+C or a dropped connection
  loses nothing; just run `stk update` again to continue.

Being polite to the data source
-------------------------------
Requests are spaced out by a shared Pacer. When the source answers HTTP 429
("slow down") the Pacer doubles the spacing and pauses every worker. If
several symbols in a row fail that way (or the network drops), the run stops
early with a clear reason instead of hammering the source. Nothing is lost:
finished symbols are already saved.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable, Optional

import requests

from .history import HistoryError, HistoryResult, RateLimited, fetch_history
from .quote import IST

OVERLAP_DAYS = 7
CLOSE_BUFFER = (15, 45)  # a session counts as finished after 15:45 IST
ABORT_STREAK = 3  # stop after this many transient failures in a row
DEFAULT_DELAY = 0.8  # seconds between requests


# ----------------------------------------------------------------- trading days
def _now(now: Optional[datetime]) -> datetime:
    return (now or datetime.now(IST)).astimezone(IST)


def expected_last_session(now: Optional[datetime] = None) -> date:
    """Most recent finished trading day, ignoring exchange holidays."""
    now = _now(now)
    d = now.date()
    if d.weekday() < 5 and (now.hour, now.minute) >= CLOSE_BUFFER:
        return d
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def today_is_incomplete(now: Optional[datetime] = None) -> bool:
    """True while today's candle may still be forming."""
    now = _now(now)
    return now.weekday() < 5 and (now.hour, now.minute) < CLOSE_BUFFER


# ------------------------------------------------------------------------ pacing
class Stopped(Exception):
    """Raised inside workers when the run has been cancelled."""


class Pacer:
    """Spaces requests apart and backs off when the source says slow down."""

    def __init__(
        self,
        interval: float = DEFAULT_DELAY,
        cooldown: Optional[float] = None,
        sleep: Optional[Callable[[float], None]] = None,
        clock: Callable[[], float] = time.monotonic,
        max_interval: float = 6.0,
    ):
        self.interval = interval
        self.cooldown = float(os.environ.get("STK_COOLDOWN", 10)) if cooldown is None else cooldown
        self.max_interval = max_interval
        self.hits = 0
        self._clock = clock
        self._stop = threading.Event()
        self._sleep = sleep or (lambda secs: self._stop.wait(secs))
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        """Block until this worker may send its next request."""
        if self._stop.is_set():
            raise Stopped()
        with self._lock:
            now = self._clock()
            start = max(self._next, now)
            self._next = start + self.interval
        if start > now:
            self._sleep(start - now)
        if self._stop.is_set():
            raise Stopped()

    def rate_limited(self) -> float:
        """Source said 429: slow down and pause everyone. Returns the pause."""
        with self._lock:
            self.hits += 1
            self.interval = min(max(self.interval, 0.5) * 2, self.max_interval)
            pause = self.cooldown * min(self.hits, 3)
            self._next = max(self._next, self._clock() + pause)
        return pause

    def ok(self) -> None:
        with self._lock:
            self.hits = 0  # pauses reset, but the slower interval is kept for this run

    def stop(self) -> None:
        self._stop.set()


def paced(fetch: Callable, pacer: Pacer) -> Callable:
    """Wrap a fetch function so every call goes through the pacer.

    A 429 is retried once after the pacer's pause; a second one is raised.
    """

    def inner(*args, **kwargs):
        for attempt in range(2):
            pacer.wait()
            try:
                result = fetch(*args, **kwargs)
            except RateLimited:
                pacer.rate_limited()
                if attempt == 1:
                    raise
                continue
            pacer.ok()
            return result

    return inner


# ----------------------------------------------------------------------- storage
def store_rows(conn: sqlite3.Connection, symbol_id: int, rows: list[tuple], replace_all: bool = False) -> int:
    with conn:
        if replace_all:
            conn.execute("DELETE FROM prices_daily WHERE symbol_id = ?", (symbol_id,))
        conn.executemany(
            """INSERT OR REPLACE INTO prices_daily
               (symbol_id, date, open, high, low, close, adj_close, volume)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [(symbol_id, *r) for r in rows],
        )
    return len(rows)


# ------------------------------------------------------------------------ update
@dataclass
class UpdateResult:
    total: int = 0
    ok: int = 0
    skipped: int = 0
    rows: int = 0
    failures: list = field(default_factory=list)  # [(ticker, message)]
    interrupted: bool = False
    aborted: str = ""  # "" | "rate_limit" | "network"
    not_done: int = 0  # symbols left untried or unfinished after an early stop
    started: str = ""
    finished: str = ""


def update(
    conn: sqlite3.Connection,
    tickers: Optional[list[str]] = None,
    full: bool = False,
    years: int = 2,
    workers: int = 2,
    progress: Optional[Callable[[int, int, str], None]] = None,
    now: Optional[datetime] = None,
    fetch: Callable = fetch_history,
    delay: float = DEFAULT_DELAY,
    pacer: Optional[Pacer] = None,
) -> UpdateResult:
    now = _now(now)
    result = UpdateResult(started=now.isoformat(timespec="seconds"))
    pacer = pacer or Pacer(interval=delay)
    fetch = paced(fetch, pacer)

    sql = "SELECT id, ticker, yahoo FROM symbols WHERE active = 1"
    params: list = []
    if tickers:
        marks = ",".join("?" * len(tickers))
        sql += f" AND ticker IN ({marks})"
        params = [t.upper() for t in tickers]
    symbols = conn.execute(sql + " ORDER BY ticker", params).fetchall()
    result.total = len(symbols)

    last_dates = {
        r["symbol_id"]: r["d"]
        for r in conn.execute("SELECT symbol_id, MAX(date) AS d FROM prices_daily GROUP BY symbol_id")
    }
    target = expected_last_session(now)
    drop_today = today_is_incomplete(now)
    today_iso = now.date().isoformat()

    todo = []
    done = 0
    for s in symbols:
        last = last_dates.get(s["id"])
        if not full and last and date.fromisoformat(last) >= target:
            result.skipped += 1
            done += 1
            if progress:
                progress(done, result.total, s["ticker"])
        else:
            todo.append((s, last))

    local = threading.local()

    def work(item):
        s, last = item
        session = getattr(local, "session", None)
        if session is None:
            session = local.session = requests.Session()
        incremental = bool(last) and not full
        start = date.fromisoformat(last) - timedelta(days=OVERLAP_DAYS) if incremental else None
        res: HistoryResult = fetch(s["yahoo"], start=start, years=years, session=session)
        refetched = False
        if incremental and res.splits:
            res = fetch(s["yahoo"], start=None, years=years, session=session)
            refetched = True
        return res, (not incremental) or refetched

    pool = ThreadPoolExecutor(max_workers=max(1, workers))
    futures = {pool.submit(work, item): item[0] for item in todo}
    streak: list = []  # transient failures in a row: (ticker, message, was_rate_limit)
    try:
        for fut in as_completed(futures):
            s = futures[fut]
            try:
                res, replace_all = fut.result()
                rows = [r for r in res.rows if not (drop_today and r[0] >= today_iso)]
                if rows:
                    result.rows += store_rows(conn, s["id"], rows, replace_all=replace_all)
                elif replace_all:
                    raise HistoryError(f'{s["ticker"]}: no price history returned')
                result.ok += 1
                # isolated blips before this success were real, individual failures
                result.failures.extend((t, m) for t, m, _ in streak)
                streak.clear()
            except Stopped:
                continue
            except HistoryError as exc:
                if exc.transient:
                    streak.append((s["ticker"], str(exc), isinstance(exc, RateLimited)))
                    if len(streak) >= ABORT_STREAK:
                        result.aborted = "rate_limit" if any(rl for _, _, rl in streak) else "network"
                        break
                else:
                    result.failures.append((s["ticker"], str(exc)))
            except requests.RequestException as exc:
                streak.append((s["ticker"], f'{s["ticker"]}: network error ({exc.__class__.__name__})', False))
                if len(streak) >= ABORT_STREAK:
                    result.aborted = "network"
                    break
            done += 1
            if progress:
                progress(done, result.total, s["ticker"])
    except KeyboardInterrupt:
        result.interrupted = True

    if result.aborted or result.interrupted:
        pacer.stop()
        pool.shutdown(wait=False, cancel_futures=True)
    else:
        pool.shutdown(wait=True)
        result.failures.extend((t, m) for t, m, _ in streak)  # run ended on isolated failures
        streak.clear()

    result.not_done = max(result.total - result.skipped - result.ok - len(result.failures), 0)
    result.finished = datetime.now(IST).isoformat(timespec="seconds")
    _log(conn, result, "full" if full else "update")
    return result


def _log(conn: sqlite3.Connection, r: UpdateResult, mode: str) -> None:
    with conn:
        conn.execute(
            """INSERT INTO update_log
               (started_at, finished_at, mode, symbols_total, symbols_ok, symbols_skipped,
                symbols_failed, rows_written, interrupted, failures)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                r.started,
                r.finished,
                mode,
                r.total,
                r.ok,
                r.skipped,
                len(r.failures),
                r.rows,
                int(r.interrupted or bool(r.aborted)),
                json.dumps([{"ticker": t, "error": e} for t, e in r.failures]),
            ),
        )
