"""Symbol list handling: download the Nifty 500 constituents and store them.

You can also load your own list with `stk init --file my.csv`. The CSV needs a
"Symbol" column; "Company Name", "Industry" and "ISIN Code" are optional.
"""

from __future__ import annotations

import csv
import io
import sqlite3
from datetime import datetime
from typing import Optional

import requests

from .quote import HEADERS, IST

SOURCES = [
    "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv",
    "https://archives.nseindia.com/content/indices/ind_nifty500list.csv",
    "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv",
]


class SymbolListError(Exception):
    pass


def _clean_header(h: str) -> str:
    return h.replace("﻿", "").strip().lower()


def parse_csv(text: str) -> list[dict]:
    """Parse a constituents CSV into [{ticker, name, industry, isin}, ...]."""
    reader = csv.reader(io.StringIO(text.replace("﻿", "")))
    try:
        header = next(reader)
    except StopIteration:
        raise SymbolListError("the file is empty") from None

    cols = {_clean_header(h): i for i, h in enumerate(header)}
    if "symbol" not in cols:
        raise SymbolListError('no "Symbol" column found in the file')

    def pick(row, *names) -> Optional[str]:
        for n in names:
            i = cols.get(n)
            if i is not None and i < len(row) and row[i].strip():
                return row[i].strip()
        return None

    out, seen = [], set()
    for row in reader:
        if not row:
            continue
        ticker = (pick(row, "symbol") or "").upper()
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        out.append(
            {
                "ticker": ticker,
                "name": pick(row, "company name", "name"),
                "industry": pick(row, "industry", "sector"),
                "isin": pick(row, "isin code", "isin"),
            }
        )
    if not out:
        raise SymbolListError("no symbols found in the file")
    return out


def download(timeout: int = 20) -> str:
    """Fetch the official Nifty 500 CSV, trying each mirror in turn."""
    last = "no source reachable"
    for url in SOURCES:
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout)
        except requests.RequestException as exc:
            last = f"{exc.__class__.__name__} for {url.split('/')[2]}"
            continue
        if r.status_code == 200 and "symbol" in r.text[:400].lower():
            return r.text
        last = f"HTTP {r.status_code} from {url.split('/')[2]}"
    raise SymbolListError(last)


def load(conn: sqlite3.Connection, rows: list[dict], deactivate_missing: bool = False) -> tuple[int, int]:
    """Insert or update symbols. Returns (added, updated)."""
    now = datetime.now(IST).isoformat(timespec="seconds")
    existing = {r["ticker"] for r in conn.execute("SELECT ticker FROM symbols")}
    added = updated = 0
    with conn:
        for r in rows:
            conn.execute(
                """
                INSERT INTO symbols (ticker, yahoo, name, industry, isin, exchange, active, added_at)
                VALUES (?, ?, ?, ?, ?, 'NSE', 1, ?)
                ON CONFLICT(ticker) DO UPDATE SET
                    name     = COALESCE(excluded.name, symbols.name),
                    industry = COALESCE(excluded.industry, symbols.industry),
                    isin     = COALESCE(excluded.isin, symbols.isin),
                    active   = 1
                """,
                (r["ticker"], f'{r["ticker"]}.NS', r["name"], r["industry"], r["isin"], now),
            )
            if r["ticker"] in existing:
                updated += 1
            else:
                added += 1
        if deactivate_missing:
            tickers = [r["ticker"] for r in rows]
            marks = ",".join("?" * len(tickers))
            conn.execute(f"UPDATE symbols SET active = 0 WHERE ticker NOT IN ({marks})", tickers)
    return added, updated
