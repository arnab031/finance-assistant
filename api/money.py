"""
Rupee formatting, in one place.

The ledger is Indian - IFSC codes, UPI and NEFT rails, accounts at Indian banks
- so every figure it holds is rupees. Rendering those with a dollar sign is not
a cosmetic slip: it misstates the unit on a finance answer, which is the same
class of wrongness as misstating the digits.

Indian digit grouping (last three, then twos: 1,69,299.00) rather than Western
thousands, so the written figure reads the way a reader of this ledger expects.
`str.format`'s "," gives Western grouping only, hence the hand-rolled pass.
"""

from __future__ import annotations

from decimal import Decimal

SYMBOL = "₹"  # ₹


def group_indian(digits: str) -> str:
    """169299 -> 1,69,299. Digits only, no sign and no decimal part."""
    if len(digits) <= 3:
        return digits
    head, tail = digits[:-3], digits[-3:]
    parts: list[str] = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join([*parts, tail])


def fmt_inr(value: Decimal | int | float, places: int = 2) -> str:
    """₹1,69,299.00 - the canonical way this app writes money."""
    text = f"{Decimal(str(value)):.{places}f}"
    sign, text = ("-", text[1:]) if text.startswith("-") else ("", text)
    whole, _, frac = text.partition(".")
    grouped = group_indian(whole)
    return f"{sign}{SYMBOL}{grouped}.{frac}" if frac else f"{sign}{SYMBOL}{grouped}"


def fmt_count(value: int) -> str:
    """Counts share the ledger's grouping so a page never mixes conventions."""
    n = int(value)
    return f"{'-' if n < 0 else ''}{group_indian(str(abs(n)))}"
