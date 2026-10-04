"""Exact rational concentration parsing and formatting.

Concentrations are handled as exact rationals (:class:`fractions.Fraction`).
Accepted input forms:

* integer            -> ``3``
* JSON number        -> ``0.25`` (converted through its decimal text, never binary float math)
* decimal string     -> ``"0.25"``, ``"1e-3"``
* fraction string    -> ``"1/4"``
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Any


def parse_concentration(value: Any) -> Fraction:
    """Parse a user supplied concentration into an exact non-negative Fraction."""
    if isinstance(value, bool):
        raise ValueError("concentration must be a number or string, not a boolean")
    if isinstance(value, int):
        frac = Fraction(value)
    elif isinstance(value, float):
        # Route through the decimal text so 0.1 means exactly 1/10.
        frac = Fraction(str(value))
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("concentration string is empty")
        if "/" in text:
            parts = text.split("/")
            if len(parts) != 2:
                raise ValueError(f"invalid fraction string: {value!r}")
            try:
                num = int(parts[0].strip())
                den = int(parts[1].strip())
            except ValueError:
                raise ValueError(f"invalid fraction string: {value!r}")
            if den == 0:
                raise ValueError("concentration denominator must be non-zero")
            frac = Fraction(num, den)
        else:
            try:
                frac = Fraction(Decimal(text))
            except (InvalidOperation, ValueError):
                raise ValueError(f"invalid concentration: {value!r}")
    else:
        raise ValueError(f"unsupported concentration type: {type(value).__name__}")
    if frac < 0:
        raise ValueError("concentration must be non-negative")
    return frac


def format_concentration(frac: Fraction) -> str:
    """Canonical string form ``"<numerator>/<denominator>"`` (always reduced)."""
    return f"{frac.numerator}/{frac.denominator}"
