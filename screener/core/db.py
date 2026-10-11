"""SQLite storage for stk.

The database lives in ~/.local/share/stk/stk.db. Override the folder with
STK_HOME or the exact file with STK_DB.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Optional

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS symbols (
    id        INTEGER PRIMARY KEY,
    ticker    TEXT NOT NULL UNIQUE,          -- NSE symbol, e.g. RELIANCE
    yahoo     TEXT NOT NULL,                 -- data-source ticker, e.g. RELIANCE.NS
    name      TEXT,
    industry  TEXT,
    isin      TEXT,
    exchange  TEXT NOT NULL DEFAULT 'NSE',
    active    INTEGER NOT NULL DEFAULT 1,
    added_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prices_daily (
    symbol_id INTEGER NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
    date      TEXT NOT NULL,                 -- YYYY-MM-DD (IST trading day)
    open      REAL,
    high      REAL,
    low       REAL,
    close     REAL NOT NULL,
    adj_close REAL,
    volume    INTEGER,
    PRIMARY KEY (symbol_id, date)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS watchlist (
    ticker   TEXT PRIMARY KEY,
    added_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS update_log (
    id              INTEGER PRIMARY KEY,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    mode            TEXT,
    symbols_total   INTEGER,
    symbols_ok      INTEGER,
    symbols_skipped INTEGER,
    symbols_failed  INTEGER,
    rows_written    INTEGER,
    interrupted     INTEGER NOT NULL DEFAULT 0,
    failures        TEXT                      -- JSON list of {ticker, error}
);
"""


def data_dir() -> Path:
    base = os.environ.get("STK_HOME")
    path = Path(base) if base else Path.home() / ".local" / "share" / "stk"
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    override = os.environ.get("STK_DB")
    if override:
        p = Path(override)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p
    return data_dir() / "stk.db"


def connect(path: Optional[os.PathLike] = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path or db_path()))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.DatabaseError:
        pass  # some filesystems refuse WAL; the default mode still works
    conn.execute("PRAGMA foreign_keys=ON")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= SCHEMA_VERSION:
        return
    conn.executescript(SCHEMA)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
