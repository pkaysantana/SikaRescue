"""Server-side formatting, so the frontend never does financial arithmetic."""

from __future__ import annotations

from sikarescue.models import Currency, Money


def money(m: Money) -> str:
    symbol = "£" if m.currency is Currency.GBP else f"{m.currency.value} "
    return f"{symbol}{m.amount:,.2f}"


def pct(p: float | None) -> str | None:
    return None if p is None else f"{p * 100:.1f}%"
