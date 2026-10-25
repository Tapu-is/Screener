"""Charts as plain inline SVG: no plotting library, works in any theme.

Text and grid lines use `currentColor`, so they follow the page's light or dark
text colour. Candles are green when the close is at or above the open, else red.
Hovering a candle shows its date, prices and volume.
"""

from __future__ import annotations

from datetime import datetime
from html import escape
from typing import Optional, Sequence

UP, DOWN = "#16a34a", "#dc2626"
SMA_STYLE = {"sma20": ("SMA 20", "#f59e0b"), "sma50": ("SMA 50", "#3b82f6"), "sma200": ("SMA 200", "#a855f7")}


def _nice(v: float) -> str:
    if abs(v) >= 1000:
        return f"{v:,.0f}"
    return f"{v:,.1f}" if abs(v) >= 100 else f"{v:,.2f}"


def _short_date(iso: str) -> str:
    return datetime.strptime(iso, "%Y-%m-%d").strftime("%d %b")


def _vol(v) -> str:
    if v is None:
        return "-"
    if v >= 1e7:
        return f"{v / 1e7:.2f} Cr"
    if v >= 1e5:
        return f"{v / 1e5:.2f} L"
    return f"{int(v):,}"


def candlestick_svg(
    rows: Sequence[dict],
    averages: Optional[dict] = None,
    width: int = 900,
    height: int = 460,
) -> str:
    """Candles + volume bars + optional moving-average lines.

    rows: dicts with date, open, high, low, close, volume (oldest first).
    averages: {"sma20": [values aligned with rows, None where unknown], ...}
    """
    rows = [r for r in rows if None not in (r.get("open"), r.get("high"), r.get("low"), r.get("close"))]
    if not rows:
        return _empty(width, 160, "No price history stored for this stock yet.")
    averages = averages or {}
    n = len(rows)
    left, right, top = 56, 12, 24
    axis_h = 22
    vol_h = int(height * 0.18)
    gap = 10
    price_h = height - top - axis_h - vol_h - gap
    plot_w = width - left - right

    hi = max(r["high"] for r in rows)
    lo = min(r["low"] for r in rows)
    for vals in averages.values():
        for v in vals:
            if v is not None:
                hi, lo = max(hi, v), min(lo, v)
    pad = (hi - lo) * 0.04 or hi * 0.02 or 1
    hi, lo = hi + pad, lo - pad
    y = lambda p: top + (hi - p) / (hi - lo) * price_h  # noqa: E731
    step = plot_w / n
    x = lambda i: left + step * (i + 0.5)  # noqa: E731
    body = max(min(step * 0.7, 14), 1)

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="100%" '
        f'style="max-width:{width}px;height:auto;font-family:system-ui,sans-serif" role="img" '
        f'aria-label="Price chart">'
    ]
    # grid + price labels
    for k in range(5):
        p = lo + (hi - lo) * k / 4
        yy = y(p)
        out.append(f'<line x1="{left}" x2="{width - right}" y1="{yy:.1f}" y2="{yy:.1f}" stroke="currentColor" stroke-opacity="0.12"/>')
        out.append(f'<text x="{left - 6}" y="{yy + 4:.1f}" text-anchor="end" font-size="11" fill="currentColor" fill-opacity="0.65">{escape(_nice(p))}</text>')
    # date labels
    ticks = sorted({0, n - 1, *(round(n * k / 5) for k in range(1, 5))})
    for i in ticks:
        if 0 <= i < n:
            anchor = "start" if i == 0 else "end" if i == n - 1 else "middle"
            xx = x(i) if anchor == "middle" else (left if anchor == "start" else width - right)
            out.append(f'<text x="{xx:.1f}" y="{height - 6}" text-anchor="{anchor}" font-size="11" fill="currentColor" fill-opacity="0.65">{escape(_short_date(rows[i]["date"]))}</text>')

    # moving averages (drawn under the candles)
    legend_x = left
    for key, (label, colour) in SMA_STYLE.items():
        vals = averages.get(key)
        if not vals:
            continue
        pts, segs = [], []
        for i, v in enumerate(vals):
            if v is None:
                if pts:
                    segs.append(pts)
                    pts = []
            else:
                pts.append(f"{x(i):.1f},{y(v):.1f}")
        if pts:
            segs.append(pts)
        for s in segs:
            if len(s) > 1:
                out.append(f'<polyline points="{" ".join(s)}" fill="none" stroke="{colour}" stroke-width="1.4"/>')
        out.append(f'<line x1="{legend_x}" x2="{legend_x + 14}" y1="12" y2="12" stroke="{colour}" stroke-width="2"/>')
        out.append(f'<text x="{legend_x + 18}" y="16" font-size="11" fill="currentColor" fill-opacity="0.75">{label}</text>')
        legend_x += 78

    # candles
    vmax = max((r.get("volume") or 0) for r in rows) or 1
    vtop = top + price_h + gap
    for i, r in enumerate(rows):
        colour = UP if r["close"] >= r["open"] else DOWN
        xx = x(i)
        tip = (
            f'{r["date"]}  O {_nice(r["open"])}  H {_nice(r["high"])}  L {_nice(r["low"])}  '
            f'C {_nice(r["close"])}  Vol {_vol(r.get("volume"))}'
        )
        y_open, y_close = y(r["open"]), y(r["close"])
        top_b, h_b = min(y_open, y_close), max(abs(y_open - y_close), 1)
        out.append(
            f'<g><title>{escape(tip)}</title>'
            f'<line x1="{xx:.1f}" x2="{xx:.1f}" y1="{y(r["high"]):.1f}" y2="{y(r["low"]):.1f}" stroke="{colour}" stroke-width="1"/>'
            f'<rect x="{xx - body / 2:.1f}" y="{top_b:.1f}" width="{body:.1f}" height="{h_b:.1f}" fill="{colour}"/>'
        )
        v = r.get("volume") or 0
        if v:
            vh = v / vmax * vol_h
            out.append(f'<rect x="{xx - body / 2:.1f}" y="{vtop + vol_h - vh:.1f}" width="{body:.1f}" height="{vh:.1f}" fill="{colour}" fill-opacity="0.45"/>')
        out.append("</g>")
    out.append(f'<text x="{left - 6}" y="{vtop + 10}" text-anchor="end" font-size="10" fill="currentColor" fill-opacity="0.55">Vol</text>')
    out.append("</svg>")
    return "".join(out)


def sparkline_svg(values: Sequence[Optional[float]], width: int = 110, height: int = 30) -> str:
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1
    pts = " ".join(f"{i * (width - 2) / (len(vals) - 1) + 1:.1f},{height - 3 - (v - lo) / span * (height - 6):.1f}" for i, v in enumerate(vals))
    colour = UP if vals[-1] >= vals[0] else DOWN
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}" role="img">'
        f'<polyline points="{pts}" fill="none" stroke="{colour}" stroke-width="1.6" stroke-linejoin="round"/></svg>'
    )


def range_bar_html(low: float, high: float, price: float, label_low: str = "52w low", label_high: str = "52w high") -> str:
    """A thin bar showing where the price sits between its low and high."""
    if high <= low:
        return ""
    pos = min(max((price - low) / (high - low), 0), 1) * 100
    return (
        '<div style="margin:4px 0 10px">'
        '<div style="position:relative;height:6px;border-radius:3px;background:linear-gradient(90deg,#dc2626,#f59e0b,#16a34a);opacity:.85">'
        f'<div style="position:absolute;left:{pos:.1f}%;top:-4px;width:3px;height:14px;background:currentColor;border-radius:2px"></div></div>'
        '<div style="display:flex;justify-content:space-between;font-size:12px;opacity:.7;margin-top:6px">'
        f"<span>{escape(label_low)} {_nice(low)}</span><span>{escape(label_high)} {_nice(high)}</span></div></div>"
    )


def _empty(width: int, height: int, text: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="100%" style="max-width:{width}px">'
        f'<text x="{width / 2}" y="{height / 2}" text-anchor="middle" font-size="14" fill="currentColor" fill-opacity="0.6">{escape(text)}</text></svg>'
    )
