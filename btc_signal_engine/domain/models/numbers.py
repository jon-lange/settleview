"""Parse numbers as Kalshi sends them: floats, or strings that may carry thousands separators ("77,362.10")."""

from __future__ import annotations


def to_float(v) -> float | None:
    if v is None or v == "":
        return None
    if isinstance(v, str):
        v = v.replace(",", "").strip()
    return float(v)
