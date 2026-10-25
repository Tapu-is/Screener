"""Small HTML pieces shared by the pages (tables, cards, CSS). Pure strings."""

from __future__ import annotations

from html import escape
from urllib.parse import quote

CSS = """
<style>
.stk-up{color:#16a34a}.stk-up2{color:#16a34a;font-weight:700}
.stk-down{color:#dc2626}.stk-down2{color:#dc2626;font-weight:700}
.stk-warn{color:#d97706}.stk-dim{opacity:.55}
.stk-table{width:100%;border-collapse:collapse;font-size:14px}
.stk-table th{text-align:right;font-weight:600;font-size:12px;opacity:.7;padding:6px 10px;border-bottom:1px solid rgba(128,128,128,.35);white-space:nowrap}
.stk-table td{text-align:right;padding:7px 10px;border-bottom:1px solid rgba(128,128,128,.15);white-space:nowrap;font-variant-numeric:tabular-nums}
.stk-table th:first-child,.stk-table td:first-child{text-align:left}
.stk-table tr:hover td{background:rgba(128,128,128,.08)}
.stk-table a{color:inherit;text-decoration:none;font-weight:600;border-bottom:1px dotted rgba(128,128,128,.6)}
.stk-table .stk-l{text-align:left}
.stk-wrap{overflow-x:auto}
.stk-cards{display:flex;gap:12px;flex-wrap:wrap;margin:6px 0 14px}
.stk-card{flex:1 1 150px;border:1px solid rgba(128,128,128,.3);border-radius:10px;padding:10px 14px}
.stk-card .k{font-size:12px;opacity:.65}.stk-card .v{font-size:22px;font-weight:700;line-height:1.3}.stk-card .s{font-size:13px}
.stk-pill{display:inline-block;padding:2px 10px;border-radius:999px;border:1px solid rgba(128,128,128,.35);font-size:12px;margin:2px 6px 2px 0}
.stk-kv{width:100%;border-collapse:collapse;font-size:14px}.stk-kv td{padding:5px 0;border-bottom:1px solid rgba(128,128,128,.15)}.stk-kv td:last-child{text-align:right;font-weight:600}
.stk-banner{border:1px solid rgba(217,119,6,.5);background:rgba(217,119,6,.08);border-radius:8px;padding:8px 12px;font-size:14px;margin:6px 0}
</style>
"""


def stock_link(ticker: str) -> str:
    return f"?stock={quote(ticker)}"


def cell(c, header: bool = False) -> str:
    """c is a plain string or {"text", "cls", "link", "bold", "dim", "html"}."""
    if not isinstance(c, dict):
        c = {"text": c}
    inner = c["html"] if "html" in c else escape(str(c.get("text", "")))
    if c.get("link"):
        inner = f'<a href="{stock_link(c["link"])}" target="_self">{inner}</a>'
    cls = []
    if c.get("cls"):
        cls.append(f"stk-{c['cls']}")
    if c.get("dim"):
        cls.append("stk-dim")
    if c.get("left"):
        cls.append("stk-l")
    attr = f' class="{" ".join(cls)}"' if cls else ""
    return f"<td{attr}>{inner}</td>"


LEFT_COLUMNS = {"Name", "Alert"}


def html_table(columns: list, rows: list) -> str:
    head = "".join(f'<th class="stk-l">{escape(str(c))}</th>' if str(c) in LEFT_COLUMNS else f"<th>{escape(str(c))}</th>" for c in columns)
    body = "".join("<tr>" + "".join(cell(c) for c in r) + "</tr>" for r in rows)
    return f'<div class="stk-wrap"><table class="stk-table"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def cards(items: list) -> str:
    """items: (label, value, sub_text, css_class)"""
    out = []
    for label, value, sub, cls in items:
        sub_html = f'<div class="s stk-{cls}">{escape(sub)}</div>' if sub and cls else (f'<div class="s">{escape(sub)}</div>' if sub else "")
        out.append(f'<div class="stk-card"><div class="k">{escape(label)}</div><div class="v">{escape(str(value))}</div>{sub_html}</div>')
    return f'<div class="stk-cards">{"".join(out)}</div>'


def pills(items: list) -> str:
    return "".join(f'<span class="stk-pill stk-{cls}">{escape(text)}</span>' if cls else f'<span class="stk-pill">{escape(text)}</span>' for text, cls in items)


def kv_table(rows: list) -> str:
    """rows: (label, text, css_class)"""
    body = "".join(
        f'<tr><td>{escape(label)}</td><td class="stk-{cls}">{escape(text)}</td></tr>' if cls else f"<tr><td>{escape(label)}</td><td>{escape(text)}</td></tr>"
        for label, text, cls in rows
    )
    return f'<table class="stk-kv">{body}</table>'


def banner(text: str) -> str:
    return f'<div class="stk-banner">{escape(text)}</div>'
