"""Color rules shared by every command. Pure functions: value in, Rich style out."""

from __future__ import annotations



def pct_style(v) -> str:
    """Green up / red down; bold for big moves (3% or more)."""
    if v is None:
        return "dim"
    if v >= 3:
        return "bold green"
    if v > 0:
        return "green"
    if v <= -3:
        return "bold red"
    if v < 0:
        return "red"
    return ""


def pct_text(v, digits: int = 2):
    from rich.text import Text

    if v is None:
        return Text("-", style="dim")
    return Text(f"{v:+.{digits}f}%", style=pct_style(v))


def volume_style(ratio, direction=None) -> str:
    """Volume vs its average: dim when quiet, yellow when busy, bold when a spike
    (green/red by price direction when known)."""
    if ratio is None:
        return ""
    if ratio >= 2:
        return "bold red" if (direction is not None and direction < 0) else "bold green" if direction else "bold yellow"
    if ratio >= 1.5:
        return "yellow"
    if ratio < 0.7:
        return "dim"
    return ""


def rsi_style(v) -> str:
    if v is None:
        return "dim"
    if v >= 70:
        return "red"
    if v <= 30:
        return "green"
    return ""


def price_style(change) -> str:
    """Style for a price shown next to its day change."""
    if change is None or change == 0:
        return "bold"
    return "bold green" if change > 0 else "bold red"


def side_style(above: bool) -> str:
    return "green" if above else "red"
