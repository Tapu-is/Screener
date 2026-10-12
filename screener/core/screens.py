"""Screen engine: filter the stored indicators with simple conditions.

A screen is a small JSON-friendly dict:

    {
      "description": "what it finds",              # optional
      "all": [ {"field": "rsi14", "op": "<", "value": 30},
               {"field": "close", "op": ">", "ref": "sma200"} ],
      "any": [ ... ],                               # optional, at least one must hold
      "sort": "rsi14", "order": "asc",              # optional
      "limit": 25,                                  # optional
      "show": ["rsi14", "chg_1m"]                   # optional extra columns (max 3)
    }

A condition compares a field with a number ("value"), another field ("ref",
optionally scaled by "mult"), or, for text fields (ticker, name, industry),
uses ==, != or contains. Conditions can be nested with "all"/"any".
A stock with an empty (missing) field never matches a condition on it.
"""

from __future__ import annotations

import copy
import difflib
import json
import math
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .indicators import FIELDS
from .quote import IST

NUMERIC = {f.name for f in FIELDS}
TEXT_FIELDS = {"ticker", "name", "industry"}
NUM_OPS = ("<", "<=", ">", ">=", "==", "!=")
TEXT_OPS = ("==", "!=", "contains")
RESERVED = {"save", "delete", "show", "list", "fields"}
DEFAULT_SORT = "turnover20_cr"  # most actively traded first
DEFAULT_SHOW = ["chg_1d", "rsi14"]
DEFAULT_LIMIT = 25
MAX_LIMIT = 500
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,29}$")


class ScreenError(ValueError):
    """A screen definition or request that cannot be used (message is user-facing)."""


# ----------------------------------------------------------------- built-ins
def _c(field_: str, op: str, value=None, ref: Optional[str] = None) -> dict:
    node = {"field": field_, "op": op}
    if ref is not None:
        node["ref"] = ref
    else:
        node["value"] = value
    return node


BUILTIN: dict = {
    "oversold": {
        "description": "RSI below 30: sold off hard. May bounce, or keep falling.",
        "all": [_c("rsi14", "<", 30)],
        "sort": "rsi14", "order": "asc", "show": ["rsi14", "chg_1m"],
    },
    "overbought": {
        "description": "RSI above 70: rallied hard. May pause or pull back.",
        "all": [_c("rsi14", ">", 70)],
        "sort": "rsi14", "order": "desc", "show": ["rsi14", "chg_1m"],
    },
    "golden-cross": {
        "description": "50-day average crossed above the 200-day in the last 10 sessions.",
        "all": [_c("golden_cross_days", "<=", 10)],
        "sort": "golden_cross_days", "order": "asc", "show": ["golden_cross_days", "chg_1m"],
    },
    "death-cross": {
        "description": "50-day average crossed below the 200-day in the last 10 sessions.",
        "all": [_c("death_cross_days", "<=", 10)],
        "sort": "death_cross_days", "order": "asc", "show": ["death_cross_days", "chg_1m"],
    },
    "breakout": {
        "description": "Closed above the highest high of the previous 251 sessions.",
        "all": [_c("close", ">", ref="prior_high52")],
        "sort": "vol_ratio", "order": "desc", "show": ["vol_ratio", "chg_1d"],
    },
    "near-high": {
        "description": "Within 3% of the 52-week high.",
        "all": [_c("from_high_pct", ">=", -3)],
        "sort": "from_high_pct", "order": "desc", "show": ["from_high_pct", "chg_1m"],
    },
    "near-low": {
        "description": "Within 5% of the 52-week low.",
        "all": [_c("from_low_pct", "<=", 5)],
        "sort": "from_low_pct", "order": "asc", "show": ["from_low_pct", "rsi14"],
    },
    "volume-surge": {
        "description": "Volume at least 2x the 20-session average (liquid stocks only).",
        "all": [_c("vol_ratio", ">=", 2), _c("turnover20_cr", ">=", 1)],
        "sort": "vol_ratio", "order": "desc", "show": ["vol_ratio", "chg_1d"],
    },
    "macd-bullish": {
        "description": "MACD crossed above its signal line in the last 3 sessions.",
        "all": [_c("macd_cross_up_days", "<=", 3)],
        "sort": "macd_cross_up_days", "order": "asc", "show": ["macd_cross_up_days", "rsi14"],
    },
    "macd-bearish": {
        "description": "MACD crossed below its signal line in the last 3 sessions.",
        "all": [_c("macd_cross_down_days", "<=", 3)],
        "sort": "macd_cross_down_days", "order": "asc", "show": ["macd_cross_down_days", "rsi14"],
    },
    "trend-up": {
        "description": "Price above the 50-day average, which is above the 200-day.",
        "all": [_c("close", ">", ref="sma50"), _c("sma50", ">", ref="sma200")],
        "sort": "chg_1m", "order": "desc", "show": ["chg_1m", "rsi14"],
    },
    "gainers": {
        "description": "Biggest one-day gains (liquid stocks only).",
        "all": [_c("chg_1d", ">", 0), _c("turnover20_cr", ">=", 1)],
        "sort": "chg_1d", "order": "desc", "limit": 20, "show": ["chg_1d", "vol_ratio"],
    },
    "losers": {
        "description": "Biggest one-day losses (liquid stocks only).",
        "all": [_c("chg_1d", "<", 0), _c("turnover20_cr", ">=", 1)],
        "sort": "chg_1d", "order": "asc", "limit": 20, "show": ["chg_1d", "vol_ratio"],
    },
}


# ---------------------------------------------------------------- validation
def _check_field(name, numeric_only: bool = False) -> None:
    allowed = NUMERIC if numeric_only else NUMERIC | TEXT_FIELDS
    if isinstance(name, str) and name in allowed:
        return
    hint = ""
    if isinstance(name, str):
        near = difflib.get_close_matches(name, sorted(allowed), n=1)
        if near:
            hint = f' Did you mean "{near[0]}"?'
    raise ScreenError(f'Unknown field "{name}".{hint} List fields with: stk screen fields')


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _validate_node(node) -> None:
    if not isinstance(node, dict):
        raise ScreenError("Each condition must be an object with field, op and value")
    for key in ("all", "any"):
        if key in node:
            kids = node[key]
            if not isinstance(kids, list) or not kids:
                raise ScreenError(f'"{key}" needs a non-empty list of conditions')
            for kid in kids:
                _validate_node(kid)
            return
    name, op = node.get("field"), node.get("op")
    _check_field(name)
    if name in TEXT_FIELDS:
        if op not in TEXT_OPS:
            raise ScreenError(f'"{name}" is text: use ==, != or contains (~ on the command line)')
        if "ref" in node or not isinstance(node.get("value"), str) or not node["value"].strip():
            raise ScreenError(f'"{name}" needs a text value, for example industry~bank')
        return
    if op == "contains":
        raise ScreenError('"~" only works on text fields (ticker, name, industry)')
    if op not in NUM_OPS:
        raise ScreenError(f'Unknown operator "{op}". Use < <= > >= == !=')
    if "ref" in node:
        _check_field(node["ref"], numeric_only=True)
        if "mult" in node and not _is_number(node["mult"]):
            raise ScreenError('"mult" must be a number')
    elif not _is_number(node.get("value")):
        raise ScreenError(f'"{name}" needs a number to compare with')


def validate(defn) -> dict:
    """Check a screen definition. Returns it unchanged, or raises ScreenError."""
    if not isinstance(defn, dict):
        raise ScreenError("A screen must be a JSON object")
    found = False
    for key in ("all", "any"):
        if key in defn:
            _validate_node({key: defn[key]})
            found = True
    if not found:
        raise ScreenError("A screen needs at least one condition. Example: --rsi-below 30")
    sort = defn.get("sort")
    if sort is not None and sort not in NUMERIC:
        _check_field(sort, numeric_only=True)
    if defn.get("order") not in (None, "asc", "desc"):
        raise ScreenError('"order" must be "asc" or "desc"')
    limit = defn.get("limit")
    if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_LIMIT):
        raise ScreenError(f"limit must be a whole number from 1 to {MAX_LIMIT}")
    show = defn.get("show")
    if show is not None:
        if not isinstance(show, list) or len(show) > 3:
            raise ScreenError('"show" must be a list of at most 3 fields')
        for f_ in show:
            _check_field(f_, numeric_only=True)
    return defn


# ---------------------------------------------------------------- evaluation
def _compare(left, op: str, right) -> bool:
    if op == "<":
        return left < right
    if op == "<=":
        return left <= right
    if op == ">":
        return left > right
    if op == ">=":
        return left >= right
    if op == "==":
        return left == right
    return left != right


def evaluate(node: dict, row: dict) -> bool:
    if "all" in node:
        return all(evaluate(k, row) for k in node["all"])
    if "any" in node:
        return any(evaluate(k, row) for k in node["any"])
    left = row.get(node["field"])
    if left is None:
        return False
    if node["field"] in TEXT_FIELDS:
        a, b = str(left).lower(), str(node["value"]).lower()
        if node["op"] == "contains":
            return b in a
        return _compare(a, node["op"], b)
    if "ref" in node:
        right = row.get(node["ref"])
        if right is None:
            return False
        right = right * node.get("mult", 1)
    else:
        right = node["value"]
    return _compare(left, node["op"], right)


def matches(row: dict, defn: dict) -> bool:
    if "all" in defn and not all(evaluate(k, row) for k in defn["all"]):
        return False
    if "any" in defn and not any(evaluate(k, row) for k in defn["any"]):
        return False
    return True


def _describe_node(node: dict) -> str:
    if "all" in node or "any" in node:
        key = "all" if "all" in node else "any"
        joiner = " and " if key == "all" else " or "
        inner = joiner.join(_describe_node(k) for k in node[key])
        return inner if len(node[key]) == 1 else f"({inner})"
    op = "contains" if node["op"] == "contains" else node["op"]
    if "ref" in node:
        mult = node.get("mult")
        right = node["ref"] + (f" x {mult:g}" if mult not in (None, 1) else "")
    else:
        right = f'{node["value"]:g}' if _is_number(node["value"]) else f'"{node["value"]}"'
    return f'{node["field"]} {op} {right}'


def describe(defn: dict) -> str:
    parts = []
    if "all" in defn:
        parts += [_describe_node(k) for k in defn["all"]]
    if "any" in defn:
        parts.append("(" + " or ".join(_describe_node(k) for k in defn["any"]) + ")")
    return " and ".join(parts)


# ------------------------------------------------------------------- running
@dataclass
class ScreenResult:
    rows: list  # shown rows (after limit)
    all_rows: list  # every match, sorted
    total: int
    sort: str
    order: str
    show: list
    as_of: Optional[str] = None


def load_universe(conn: sqlite3.Connection) -> list:
    cur = conn.execute(
        """SELECT s.ticker, s.name, s.industry, i.*
           FROM indicators i JOIN symbols s ON s.id = i.symbol_id
           WHERE s.active = 1"""
    )
    return [dict(r) for r in cur]


def run(conn: sqlite3.Connection, defn: dict) -> ScreenResult:
    validate(defn)
    universe = load_universe(conn)
    matched = [r for r in universe if matches(r, defn)]

    sort = defn.get("sort") or DEFAULT_SORT
    order = defn.get("order") or "desc"
    keyed = sorted((r for r in matched if r.get(sort) is not None), key=lambda r: r["ticker"])
    keyed.sort(key=lambda r: r[sort], reverse=(order == "desc"))
    rest = sorted((r for r in matched if r.get(sort) is None), key=lambda r: r["ticker"])
    ordered = keyed + rest

    show = list(defn.get("show") or DEFAULT_SHOW)
    if sort not in show and sort != "close":
        show = (show + [sort])[:3]
    limit = defn.get("limit") or DEFAULT_LIMIT
    return ScreenResult(
        rows=ordered[:limit],
        all_rows=ordered,
        total=len(ordered),
        sort=sort,
        order=order,
        show=show,
        as_of=max((r["as_of"] for r in universe), default=None),
    )


def merge(
    base: Optional[dict],
    extra: list,
    sort: Optional[str] = None,
    order: Optional[str] = None,
    limit: Optional[int] = None,
) -> dict:
    """A copy of `base` narrowed by extra conditions, with optional overrides."""
    out = copy.deepcopy(base) if base else {}
    if extra:
        out["all"] = list(out.get("all", [])) + list(extra)
    if sort:
        out["sort"] = sort
    if order:
        out["order"] = order
    if limit:
        out["limit"] = limit
    return out


# ------------------------------------------------------ building conditions
_WHERE = re.compile(r"^\s*([A-Za-z_][A-Za-z_0-9]*)\s*(<=|>=|==|!=|<|>|~)\s*(.+?)\s*$")
_REF = re.compile(r"^([A-Za-z_][A-Za-z_0-9]*)(?:\s*\*\s*([0-9]*\.?[0-9]+))?$")


def parse_where(expr: str) -> dict:
    """Turn "rsi14<30", "close>sma50*1.02" or "industry~bank" into a condition."""
    m = _WHERE.match(expr or "")
    if not m:
        raise ScreenError(
            f'Could not read "{expr}". Use a form like  rsi14<30   close>sma200   or   industry~bank'
        )
    name, op, rhs = m.group(1).lower(), m.group(2), m.group(3)
    if op == "~":
        op = "contains"
    if name in TEXT_FIELDS:
        node = {"field": name, "op": op, "value": rhs.strip("\"'")}
    else:
        try:
            num = float(rhs)
        except ValueError:
            ref = _REF.match(rhs)
            if not ref:
                raise ScreenError(f'Could not read the right side of "{expr}". Use a number or a field name') from None
            node = {"field": name, "op": op, "ref": ref.group(1).lower()}
            if ref.group(2):
                node["mult"] = float(ref.group(2))
        else:
            if not math.isfinite(num):
                raise ScreenError(f'"{rhs}" is not a usable number')
            node = {"field": name, "op": op, "value": int(num) if num.is_integer() else num}
    _validate_node(node)
    return node


def conditions_from_options(
    rsi_below: Optional[float] = None,
    rsi_above: Optional[float] = None,
    above_sma: Optional[int] = None,
    below_sma: Optional[int] = None,
    min_volume_ratio: Optional[float] = None,
    min_turnover: Optional[float] = None,
    industry: Optional[str] = None,
    where=(),
) -> list:
    conds = []
    if rsi_below is not None:
        conds.append(_c("rsi14", "<", rsi_below))
    if rsi_above is not None:
        conds.append(_c("rsi14", ">", rsi_above))
    for n, op in ((above_sma, ">"), (below_sma, "<")):
        if n is not None:
            if n not in (20, 50, 200):
                raise ScreenError("Moving averages available: 20, 50 or 200")
            conds.append(_c("close", op, ref=f"sma{n}"))
    if min_volume_ratio is not None:
        conds.append(_c("vol_ratio", ">=", min_volume_ratio))
    if min_turnover is not None:
        conds.append(_c("turnover20_cr", ">=", min_turnover))
    if industry:
        conds.append({"field": "industry", "op": "contains", "value": industry})
    for expr in where or ():
        conds.append(parse_where(expr))
    return conds


# ------------------------------------------------------------- saved screens
def get(conn: sqlite3.Connection, name: str) -> tuple:
    """(definition, "built-in" | "saved") or ScreenError with suggestions."""
    key = name.strip().lower()
    if key in BUILTIN:
        return copy.deepcopy(BUILTIN[key]), "built-in"
    row = conn.execute("SELECT definition FROM saved_screens WHERE name = ?", (key,)).fetchone()
    if row:
        try:
            return json.loads(row[0]), "saved"
        except ValueError:
            raise ScreenError(f'The saved screen "{key}" is damaged. Delete it with: stk screen delete {key}') from None
    names = list(BUILTIN) + [r[0] for r in conn.execute("SELECT name FROM saved_screens")]
    near = difflib.get_close_matches(key, names, n=3)
    hint = f' Did you mean: {", ".join(near)}?' if near else ""
    raise ScreenError(f'No screen called "{name}".{hint} See them all with: stk screen')


def list_all(conn: sqlite3.Connection) -> list:
    items = [{"name": k, "description": v["description"], "source": "built-in"} for k, v in BUILTIN.items()]
    for r in conn.execute("SELECT name, definition FROM saved_screens ORDER BY name"):
        try:
            defn = json.loads(r[1])
            text = defn.get("description") or describe(defn)
        except (ValueError, KeyError, TypeError):
            text = "(damaged)"
        items.append({"name": r[0], "description": text, "source": "saved"})
    return items


def save(conn: sqlite3.Connection, name: str, defn: dict) -> str:
    """Store a screen. Returns "created" or "updated"."""
    key = (name or "").strip().lower()
    if not NAME_RE.match(key):
        raise ScreenError("Screen names use lowercase letters, numbers, - and _ (up to 30 characters)")
    if key in BUILTIN or key in RESERVED:
        raise ScreenError(f'"{key}" is taken. Choose another name')
    validate(defn)
    existed = conn.execute("SELECT 1 FROM saved_screens WHERE name = ?", (key,)).fetchone() is not None
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO saved_screens (name, definition, created_at) VALUES (?, ?, ?)",
            (key, json.dumps(defn, sort_keys=True), datetime.now(IST).isoformat(timespec="seconds")),
        )
    return "updated" if existed else "created"


def delete(conn: sqlite3.Connection, name: str) -> bool:
    key = name.strip().lower()
    if key in BUILTIN:
        raise ScreenError(f'"{key}" is built in and cannot be deleted')
    with conn:
        return conn.execute("DELETE FROM saved_screens WHERE name = ?", (key,)).rowcount > 0
