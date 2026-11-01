"""A bracket's figures (pure): its net after every fee, and its BTC stop by side.

One rule, used by every screen, message and summary (review fix plan 2026-10-31, P2 "DRY"; before this, six copies
summed or rounded differently):

* `net(b)` = proceeds - entry_cost - tp_fees - exit_fees (a missing figure is $0), rounded to $0.0001: the stored
  figures are 4-dp dollars, so the rounding only removes float noise (1.1 - 1.0 - 0.1 is 8.3e-17, not a win).
  A win is a net above $0. Totals add the per-bracket nets. A display in whole cents rounds this net
  (`net_cents_display`), never the unrounded difference.
* A bracket stops on the BTC index: UP when the index is at or below `stop_index_below`, DOWN at or above
  `stop_index_above` (application/trading.py). `stop_key(side)` names the field, `stop_of(row)` reads it,
  `stop_fields(side, level)` writes both.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

UP, DOWN = "UP", "DOWN"
STOP_BELOW, STOP_ABOVE = "stop_index_below", "stop_index_above"


def _f(v: Any) -> float:
    return float(v or 0.0)


def net(b: Mapping[str, Any]) -> float:
    """A bracket's net after every fee, to the $0.0001 (module docstring)."""
    return round(_f(b.get("proceeds")) - _f(b.get("entry_cost")) - _f(b.get("tp_fees")) - _f(b.get("exit_fees")), 4)


def net_cents_display(b: Mapping[str, Any]) -> float:
    """The net in whole cents for a sentence ("+$0.56"): `net(b)` rounded to $0.01."""
    return round(net(b), 2)


def total(brackets: Iterable[Mapping[str, Any]]) -> float:
    """The sum of these brackets' nets, to the $0.0001."""
    return round(sum(net(b) for b in brackets), 4)


def closed_filled(b: Mapping[str, Any]) -> bool:
    """Closed and bought something: a bracket with a result."""
    return b.get("state") == "closed" and _f(b.get("filled")) > 0


def stop_key(side: str | None) -> str:
    """The stop field for this side: UP stops below, anything else (DOWN) above."""
    return STOP_BELOW if side == UP else STOP_ABOVE


def stop_of(row: Mapping[str, Any], side: str | None = None) -> float | None:
    """The BTC stop level for the row's side (or `side`), None when not set."""
    return row.get(stop_key(row.get("side") if side is None else side))


def stop_fields(side: str | None, level: float | None) -> dict[str, float | None]:
    """Both stop fields for a side: the side's own one set to `level`, the other None (no stop for an unknown side)."""
    return {STOP_BELOW: level if side == UP else None, STOP_ABOVE: level if side == DOWN else None}
