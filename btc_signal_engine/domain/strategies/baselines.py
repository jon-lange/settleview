"""Baselines every result is compared against (KTD15). They decide every window and are never champions."""

from __future__ import annotations

from btc_signal_engine.domain.models.decisions import Call
from btc_signal_engine.domain.strategies.base import Context, Features

BASELINES = ("known_component", "repeat_previous", "momentum_60s")


def baseline_hash(name: str) -> str:
    return f"baseline:{name}"


def baseline_call(name: str, f: Features, ctx: Context) -> tuple[Call, str]:
    if name == "known_component":
        kc = f.get("known_component")
        if kc is None:
            return Call.SKIP, "Not enough price history yet."
        # A tie settles UP under Kalshi's rule, so zero counts as UP.
        return (Call.UP if kc >= 0 else Call.DOWN), (
            f"Price is {'at or above' if kc >= 0 else 'below'} the starting average by ${abs(kc):.2f}, "
            "which the scoring rule already partly locks in.")
    if name == "repeat_previous":
        if ctx.prev_actual not in ("UP", "DOWN"):
            return Call.SKIP, "Previous window not settled."
        return Call(ctx.prev_actual), f"The previous window went {ctx.prev_actual}."
    if name == "momentum_60s":
        m = f.get("momentum_60")
        if m is None:
            return Call.SKIP, "Not enough price history yet."
        return (Call.UP if m >= 0 else Call.DOWN), f"Price {'rose' if m >= 0 else 'fell'} over the last minute."
    raise ValueError(f"unknown baseline {name}")
