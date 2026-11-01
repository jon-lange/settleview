"""Live lean (U17, R27): what the champion would call right now, recomputed every second.

Leans are display-only. They are never written as decisions or scored.
"""

from __future__ import annotations

from btc_signal_engine.domain.features.state import FeatureState
from btc_signal_engine.domain.strategies.base import Context
from btc_signal_engine.domain.strategies.ruleset import evaluate
from btc_signal_engine.domain.strategies.rules import SIGNAL_RULES


def compute_lean(state: FeatureState, spec: dict, horizon: int, at: int, flags: frozenset[str] = frozenset()) -> dict:
    """`at` must be a whole second; uses only data before it (KTD5)."""
    features, availability = state.snapshot(at)
    ev = evaluate(spec, features, availability, Context(horizon=horizon, flags=flags))
    checks = []
    for name in spec["signals"]:
        rule = SIGNAL_RULES[name]
        for text, up, down in rule.checklist(features, spec["params"][name][str(horizon)]):
            checks.append({"rule": name, "condition": text, "up": up, "down": down})
    total = len(checks)
    up_met = sum(c["up"] for c in checks)
    down_met = sum(c["down"] for c in checks)
    side = "UP" if up_met >= down_met else "DOWN"
    return {
        "at": at,
        "call": ev.call.value,
        "sentence": ev.explanation,
        "side": side,
        "strength": (max(up_met, down_met) / total) if total else 0.0,
        "up_met": up_met,
        "down_met": down_met,
        "total": total,
        "checks": checks,
        "known_component": features.get("known_component"),
        "open_avg": features.get("open_avg"),
    }
