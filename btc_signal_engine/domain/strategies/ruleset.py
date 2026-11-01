"""Immutable, hash-identified rulesets (KTD10).

A ruleset is filters (strategy D) + ordered signal rules + per-horizon params. Its identity
is a SHA-256 over the canonical spec plus every rule's RULE_VERSION and FEATURE_LIB_VERSION,
so a change to params, rule logic or feature code always yields a new version.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from btc_signal_engine.domain.features import FEATURE_LIB_VERSION
from btc_signal_engine.domain.models.decisions import Call, Vote
from btc_signal_engine.domain.strategies.base import Context, Features
from btc_signal_engine.domain.strategies.rules import FILTERS, SIGNAL_RULES

COMBINE = ("first_match", "unanimous")


@dataclass(frozen=True, slots=True)
class Evaluation:
    call: Call
    explanation: str
    conditions: tuple[str, ...]
    flags: tuple[str, ...]


def _resolve_params(rule, given: dict[str, Any], horizon: int) -> dict[str, Any]:
    out = {}
    for name, spec in rule.params.items():
        out[name] = spec.validate(name, given[name]) if name in given else spec.default_for(horizon)
    unknown = set(given) - set(rule.params)
    if unknown:
        raise ValueError(f"unknown parameter(s) for {rule.name}: {sorted(unknown)}")
    return out


def build_spec(name: str, signals: list[str], horizons: list[int], combine: str = "first_match",
               params: dict[str, dict[str, dict[str, Any]]] | None = None) -> dict:
    """Complete, validated spec with every param resolved per horizon (stable hashing input)."""
    if not signals:
        raise ValueError("a ruleset needs at least one signal rule")
    for s in signals:
        if s not in SIGNAL_RULES:
            raise ValueError(f"unknown rule {s!r}; choose from {sorted(SIGNAL_RULES)}")
    if combine not in COMBINE:
        raise ValueError(f"combine must be one of {COMBINE}")
    params = params or {}
    resolved: dict[str, dict[str, dict[str, Any]]] = {}
    for rname in [*signals, "filters"]:
        rule = FILTERS if rname == "filters" else SIGNAL_RULES[rname]
        resolved[rname] = {str(h): _resolve_params(rule, params.get(rname, {}).get(str(h), {}), h) for h in horizons}
    return {"name": name, "signals": list(signals), "combine": combine, "horizons": sorted(horizons), "params": resolved}


def spec_hash(spec: dict) -> str:
    body = {k: spec[k] for k in ("signals", "combine", "horizons", "params")}
    body["rule_versions"] = {r: (FILTERS if r == "filters" else SIGNAL_RULES[r]).RULE_VERSION
                             for r in [*spec["signals"], "filters"]}
    body["feature_version"] = FEATURE_LIB_VERSION
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def clone_spec(spec: dict, overrides: dict[str, dict[str, dict[str, Any]]]) -> dict:
    """New spec with overrides {rule: {horizon: {param: value}}}; validated like a fresh build."""
    params = copy.deepcopy(spec["params"])
    for rname, by_h in overrides.items():
        if rname not in params:
            raise ValueError(f"ruleset has no rule {rname!r}")
        for h, values in by_h.items():
            if str(h) not in params[rname]:
                raise ValueError(f"ruleset does not cover horizon {h}")
            params[rname][str(h)].update(values)
    return build_spec(spec["name"], spec["signals"], spec["horizons"], spec["combine"], params)


def required_features(spec: dict, horizon: int) -> set[str]:
    req: set[str] = set()
    for s in spec["signals"]:
        req |= SIGNAL_RULES[s].required(spec["params"][s][str(horizon)], horizon)
    return req


def evaluate(spec: dict, f: Features, avail: dict[str, str], ctx: Context) -> Evaluation:
    h = str(ctx.horizon)
    required = required_features(spec, ctx.horizon)
    veto, sentence, flags = FILTERS.check(f, avail, required, spec["params"]["filters"][h], ctx)
    if veto:
        return Evaluation(Call.SKIP, f"Sitting out: {sentence}", (f"skip: {veto}",), (veto,))
    votes = []
    notes: list[str] = []
    for s in spec["signals"]:
        rv = SIGNAL_RULES[s].evaluate(f, spec["params"][s][h], ctx)
        votes.append(rv)
        notes.extend(rv.notes)
    directional = [v for v in votes if v.vote is not Vote.ABSTAIN]
    if spec["combine"] == "first_match":
        chosen = directional[0] if directional else None
    else:
        chosen = directional[0] if directional and len(directional) == len(votes) \
            and len({v.vote for v in directional}) == 1 else None
    if chosen is None:
        reason = votes[0].sentence if len(votes) == 1 else "The rules did not agree on a direction."
        return Evaluation(Call.SKIP, f"No call: {reason}", tuple(notes), tuple(flags))
    return Evaluation(Call(chosen.vote.value), chosen.sentence, chosen.conditions + tuple(notes), tuple(flags))
