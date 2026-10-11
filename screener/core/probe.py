"""Diagnostics: find out which kind of request the data source refuses.

`stk probe` sends a handful of small, spaced-out requests that differ in one
detail each (history length, the `events` option, host, User-Agent) and
reports the HTTP status of each. If the `curl` program is installed it also
makes the same request with curl, to tell a blocked IP from a blocked client.
"""

from __future__ import annotations

import platform
import shutil
import ssl
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import urlencode

import requests

from .quote import YAHOO_HEADERS

DESKTOP_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
BASE = "https://{host}.finance.yahoo.com/v8/finance/chart/{symbol}"
SYMBOL = "TCS.NS"


@dataclass
class ProbeResult:
    label: str
    status: Optional[int]  # None = no response
    detail: str = ""


def _cases() -> list[tuple[str, str, dict, dict]]:
    ours = dict(YAHOO_HEADERS)
    desktop = {"User-Agent": DESKTOP_UA}
    day = {"interval": "1d"}
    full = {**day, "range": "2y", "events": "div,splits"}
    return [
        ("5 days (what `quote` uses)", "query1", {**day, "range": "5d"}, ours),
        ("1 year, no events", "query1", {**day, "range": "1y"}, ours),
        ("2 years, no events", "query1", {**day, "range": "2y"}, ours),
        ("2 years + events (what `update` uses)", "query1", full, ours),
        ("same, other host (query2)", "query2", full, ours),
        ("same, browser-style User-Agent (often refused)", "query1", full, desktop),
    ]


def _candles(resp) -> str:
    try:
        n = len(resp.json()["chart"]["result"][0].get("timestamp") or [])
        return f"{n} candles"
    except (ValueError, KeyError, IndexError, TypeError):
        return "unreadable body"


def run(
    pause: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
    on_result: Optional[Callable[[ProbeResult], None]] = None,
) -> list[ProbeResult]:
    out: list[ProbeResult] = []

    def add(r: ProbeResult) -> None:
        out.append(r)
        if on_result:
            on_result(r)

    for i, (label, host, params, headers) in enumerate(_cases()):
        if i:
            sleep(pause)
        url = BASE.format(host=host, symbol=SYMBOL)
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=15)
        except requests.RequestException as exc:
            add(ProbeResult(label, None, f"no response ({exc.__class__.__name__})"))
            continue
        if resp.status_code == 200:
            detail = _candles(resp)
        else:
            retry = resp.headers.get("Retry-After")
            detail = (resp.text or "").strip().replace("\n", " ")[:60]
            if retry:
                detail += f" (Retry-After {retry}s)"
        add(ProbeResult(label, resp.status_code, detail))

    curl = shutil.which("curl")
    if curl:
        sleep(pause)
        params = {"interval": "1d", "range": "2y", "events": "div,splits"}
        ua = YAHOO_HEADERS["User-Agent"]
        url = BASE.format(host="query1", symbol=SYMBOL) + "?" + urlencode(params, safe=",")
        try:
            p = subprocess.run(
                [curl, "-s", "-o", "/dev/null", "-w", "%{http_code}", "-m", "20", "-A", ua, url],
                capture_output=True,
                text=True,
                timeout=30,
            )
            code = int(p.stdout.strip() or 0) or None
            add(ProbeResult("same request made with curl instead of python", code, ""))
        except (subprocess.SubprocessError, ValueError, OSError) as exc:
            add(ProbeResult("same request made with curl instead of python", None, f"curl failed ({exc.__class__.__name__})"))
    return out


def environment() -> str:
    return (
        f"Python {platform.python_version()} · requests {requests.__version__} · "
        f"{ssl.OPENSSL_VERSION} · {platform.system()} {platform.machine()}"
    )
