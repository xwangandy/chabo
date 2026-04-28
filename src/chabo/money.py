from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP


def money_to_cents(value: str | int | float | Decimal) -> int:
    amount = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return int(amount * 100)


def cents_to_money(cents: int) -> str:
    return f"{Decimal(cents) / Decimal(100):.2f}"


def bps_amount(amount_cents: int, bps: int) -> int:
    return (amount_cents * bps) // 10_000
