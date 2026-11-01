"""Money, cents and side words as every screen and message writes them (pure; web/fmt.js is the browser's copy).

One style (review fix plan 2026-10-31, P2 "DRY"; before this a dozen copies differed in sign and separators):

* Dollars: `usd(-1234.5)` "−$1,234.50" (thousands separators, the sign before the $). Negative amounts use the
  real minus sign U+2212 (`MINUS`), never a hyphen. `dp=4` for exact fees and P&L ("$0.0123").
* Signed: `signed_usd(x)` "+$0.56" / "−$0.40", rounded first so a tiny loss reads "+$0.00", never "−$0.00".
* Whole dollars when exact: `usd_short(1)` "$1", `usd_short(0.86)` "$0.86" (Telegram's short lines).
* Cents: `cents(0.43)` "43¢" (rounded to the cent, half to even like Python's round); None is "—" (or `missing`).
* Sides: `side_word("UP")` "Up", "DOWN" "Down", anything else as given.
"""

from __future__ import annotations

MINUS = "−"  # "−": negative amounts
DASH = "—"  # "—": a figure that is not known


def usd(x: float, *, dp: int = 2) -> str:
    """Dollars with separators, the minus sign in front: "$0.40", "−$1,234.50"."""
    x = float(x)
    return (MINUS if x < 0 else "") + f"${abs(x):,.{dp}f}"


def signed_usd(x: float, *, dp: int = 2) -> str:
    """Always a sign: "+$0.56", "−$0.40", "+$0.00" (the amount is rounded to `dp` before its sign is chosen)."""
    r = round(float(x), dp)
    return ("+" if r >= 0 else MINUS) + f"${abs(r):,.{dp}f}"


def usd_short(x: float) -> str:
    """Dollars, whole ones without cents: "$1", "$0.86", "−$0.40" (a loss under half a cent shows no sign)."""
    r = round(abs(float(x)), 2)
    s = f"${r:,.0f}" if abs(r - round(r)) < 1e-9 else f"${r:,.2f}"
    return (MINUS if x < -0.004 else "") + s


def cents(p: float | None, *, missing: str = DASH) -> str:
    """A price in whole cents: "43¢"; `missing` when unknown."""
    return missing if p is None else f"{round(p * 100):.0f}¢"


def side_word(side: str | None) -> str:
    """"Up" / "Down" for a bracket side; anything else as given."""
    return "Up" if side == "UP" else "Down" if side == "DOWN" else str(side)
