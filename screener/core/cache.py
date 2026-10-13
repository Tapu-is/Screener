"""Tiny JSON file cache so repeated lookups don't hammer the data source.

Stored at ~/.cache/stk/quotes.json (override the folder with STK_CACHE_DIR).
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional


def cache_dir() -> Path:
    base = os.environ.get("STK_CACHE_DIR")
    path = Path(base) if base else Path.home() / ".cache" / "stk"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _file() -> Path:
    return cache_dir() / "quotes.json"


def _load() -> dict:
    try:
        return json.loads(_file().read_text())
    except (OSError, ValueError):
        return {}


def get(key: str, max_age: Optional[float] = None) -> Optional[dict[str, Any]]:
    """Return the cached entry, or None.

    max_age=None returns the entry regardless of age (used as a last-resort
    fallback when the network fails).
    """
    entry = _load().get(key)
    if not entry:
        return None
    if max_age is not None and time.time() - entry.get("_saved", 0) > max_age:
        return None
    return entry["data"]


_LOCK = threading.Lock()


def put(key: str, data: dict[str, Any]) -> None:
    with _LOCK:  # several quotes may be fetched at once
        store = _load()
        store[key] = {"_saved": time.time(), "data": data}
        try:
            _file().write_text(json.dumps(store))
        except OSError:
            pass  # cache is best-effort
