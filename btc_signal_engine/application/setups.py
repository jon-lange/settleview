"""Setup lights (Ask Settleview, Manual level; 2026-10-30): one light per ACTIVE tested rule on a Play market.

States: "grey" (not in play), "amber" (setting up: within reach, the light says what is missing and how far along it
is, `progress` 0..1) and "green" (ready). Green is exactly the agent runner's entry decision on the same view: the
rule's own match function (`agent_rules.evaluate`) plus the runner's time floor (`Rule.min_left_ms`); nothing here
re-decides it. The runner's session guards (one entry per window, none while the session holds the market) are about
an agent session, not about the user, and do not apply to a light.

Amber needs the rules' thresholds to say what is missing ("BTC needs $28 more"). agent_rules.py keeps them as
literals inside each match function, so they are mirrored in `THRESHOLDS` for the WORDS only (never for green);
tests/application/test_setups.py probes every mirrored number against the rule function itself, so a rule that
changes without this table fails the suite. Every registered rule must have an amber definition (checked at import:
a rule added without one is an error, never a light that silently stays grey).

A retired rule (rule_scores.RuleBook status) has no light at all. A rule that does not trade the market has none.
Each light carries the rule's live record (rule_scores.tally over the agent's rule entries, all markets) and its
backtest reference (rule_scores.BACKTEST), for the tap-to-expand details.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

from btc_signal_engine.application.agent_proposals import APPROVE_SLACK
from btc_signal_engine.application.agent_rules import RULES, _base, _edges, _fee, _inputs, evaluate

GREY, AMBER, GREEN = "grey", "amber", "green"
STATES = (GREY, AMBER, GREEN)
EPS = 1e-9


@dataclass(frozen=True)
class Thresholds:
    """The rule's numbers as agent_rules.py decides them (mirrored for the words; see the module doc)."""
    left_s: tuple[float, float]  # the entry window, seconds left (both ends included)
    gap_usd: float | None = None  # BTC at least this far past the strike (CUSHION100)
    gap_pct: float | None = None  # |BTC - strike| at least this share of the strike (H4)
    near_pct: float | None = None  # |BTC - strike| at most this share of the strike (HPANIC)
    ask_lo: float | None = None  # the side's ask at least this
    ask_hi: float | None = None  # the side's ask at most this (the rule's ask_cap)
    edge: float | None = None  # model fair - ask - fee at least this (LATERESID; HPANIC: more than this)


THRESHOLDS: dict[str, Thresholds] = {
    "H4": Thresholds(left_s=(600, 720), gap_pct=0.004, ask_hi=RULES["H4"].ask_cap),
    "CUSHION100": Thresholds(left_s=(300, 600), gap_usd=100.0, ask_lo=0.70, ask_hi=RULES["CUSHION100"].ask_cap),
    "HPANIC": Thresholds(left_s=(0, 240), near_pct=0.001, ask_hi=RULES["HPANIC"].ask_cap, edge=0.03),
    "LATERESID": Thresholds(left_s=(180, 240), ask_hi=RULES["LATERESID"].ask_cap, edge=0.05),
}


def ready(rule_id: str, market: str, view: dict) -> dict | None:
    """The agent runner's entry decision on this view: the rule's match, when the runner's time floor allows it."""
    m = evaluate(rule_id, market, view)
    if m is None:
        return None
    floor = RULES[rule_id].min_left_ms
    if floor is not None and int(view["window"]["end"]) - int(view["now"]) < floor:
        return None
    return m


# -- words ------------------------------------------------------------------------------------------------------------
def clock(s: float) -> str:
    s = max(0, int(math.ceil(s - EPS)))
    return f"{s // 60}:{s % 60:02d}"


def cents(p: float | None) -> str:
    return "—" if p is None else f"{round(p * 100):.0f}¢"


def side_word(side: str) -> str:
    return "Up" if side == "UP" else "Down"


def money(x: float) -> str:
    return f"${x:,.0f}"


def _time_missing(t: Thresholds, left: float) -> str | None:
    hi = t.left_s[1]
    return f"its window opens in {clock(left - hi)}" if left > hi + EPS else None


def _price_missing(t: Thresholds, side: str, ask: float | None) -> str | None:
    if ask is None:
        return f"no {side_word(side)} price yet"
    if t.ask_hi is not None and ask > t.ask_hi + EPS:
        return f"{side_word(side)} is {cents(ask)}, over its {cents(t.ask_hi)} cap"
    if t.ask_lo is not None and ask < t.ask_lo - EPS:
        return f"{side_word(side)} is {cents(ask)}, under its {cents(t.ask_lo)} floor"
    return None


def _window_words(t: Thresholds) -> str:
    lo, hi = t.left_s
    return f"{clock(lo)}–{clock(hi)} left" if lo > 0 else f"at most {clock(hi)} left"


def _price_words(t: Thresholds, side: str) -> str:
    if t.ask_lo is not None:
        return f"{side_word(side)} at {cents(t.ask_lo)}–{cents(t.ask_hi)}"
    return f"{side_word(side)} at {cents(t.ask_hi)} or less"


# -- amber, per rule ----------------------------------------------------------------------------------------------------
# Each takes (view, market) and returns None (grey) or {progress, missing: [phrases], needs, have, side, ask}.
def amber_h4(v: dict, market: str) -> dict | None:
    """H4 amber: up to 2:00 before its window (12:00-10:00 left) or inside it, with BTC on one side at least half
    the 0.4% gap (0.2% of the strike). Progress = the gap / the 0.4% gap."""
    t = THRESHOLDS["H4"]
    x = _inputs(v, market)
    if x is None or not t.left_s[0] - EPS <= x["left_s"] <= t.left_s[1] + 120 + EPS:
        return None
    need = t.gap_pct * x["strike"]
    if abs(x["gap"]) < need / 2 - EPS:
        return None
    missing = [m for m in (_time_missing(t, x["left_s"]),
                           f"BTC needs {money(math.ceil(need - abs(x['gap'])))} more" if abs(x["gap"]) < need - EPS
                           else None, _price_missing(t, x["side"], x["ask"])) if m]
    return {"progress": min(1.0, abs(x["gap"]) / need), "missing": missing, "side": x["side"], "ask": x["ask"],
            "needs": f"Needs BTC {t.gap_pct * 100:.1f}% ({money(need)}) past the strike, "
                     f"{_price_words(t, x['side'])}, {_window_words(t)}.",
            "have": f"Have: {money(abs(x['gap']))}, {cents(x['ask'])}, {clock(x['left_s'])} left."}


def amber_cushion100(v: dict, market: str) -> dict | None:
    """CUSHION100 amber: up to 2:00 before its window (12:00-10:00 left) or inside it (10:00-5:00 left), with BTC on
    one side at least $50 past the strike (half its $100). Progress = the gap / $100."""
    t = THRESHOLDS["CUSHION100"]
    x = _inputs(v, market)
    if x is None or not t.left_s[0] - EPS <= x["left_s"] <= t.left_s[1] + 120 + EPS:
        return None
    if abs(x["gap"]) < t.gap_usd / 2 - EPS:
        return None
    short = t.gap_usd - abs(x["gap"])
    missing = [m for m in (_time_missing(t, x["left_s"]),
                           f"BTC needs {money(math.ceil(short - EPS))} more" if short > EPS else None,
                           _price_missing(t, x["side"], x["ask"])) if m]
    return {"progress": min(1.0, abs(x["gap"]) / t.gap_usd), "missing": missing, "side": x["side"], "ask": x["ask"],
            "needs": f"Needs BTC {money(t.gap_usd)} past the strike, {_price_words(t, x['side'])}, "
                     f"{_window_words(t)}.",
            "have": f"Have: {money(abs(x['gap']))}, {cents(x['ask'])}, {clock(x['left_s'])} left."}


def _best(e: dict) -> str:
    """LATERESID's side: the larger edge (a tie, or no DOWN ask, is UP)."""
    return "UP" if e["DOWN"] is None or (e["UP"] is not None and e["UP"][2] >= e["DOWN"][2]) else "DOWN"


def amber_lateresid(v: dict, market: str) -> dict | None:
    """LATERESID amber: up to 1:00 before its window (5:00-4:00 left) or inside it (4:00-3:00 left), with the
    larger-edge side's model fair - ask - fee at least half its 5c (2.5c). Progress = that edge / 5c."""
    t = THRESHOLDS["LATERESID"]
    x = _base(v, market)
    if x is None or not t.left_s[0] - EPS <= x["left_s"] <= t.left_s[1] + 60 + EPS:
        return None
    e = _edges(v, x, market)
    if e is None or (e["UP"] is None and e["DOWN"] is None):
        return None
    side = _best(e)
    ask, _fee_c, edge = e[side]
    if edge < t.edge / 2 - EPS:
        return None
    missing = [m for m in (_time_missing(t, x["left_s"]),
                           f"its edge is {edge * 100:.1f}¢ of {t.edge * 100:.0f}¢" if edge < t.edge - EPS else None,
                           _price_missing(t, side, ask)) if m]
    return {"progress": max(0.0, min(1.0, edge / t.edge)), "missing": missing, "side": side, "ask": ask,
            "needs": f"Needs model fair − price − fee of {t.edge * 100:.0f}¢ or more, {_price_words(t, side)}, "
                     f"{_window_words(t)}.",
            "have": f"Have: {edge * 100:.1f}¢ on {side_word(side)}, {cents(ask)}, {clock(x['left_s'])} left."}


def amber_hpanic(v: dict, market: str) -> dict | None:
    """HPANIC amber (only while it is active; it is retired since 2026-10-30): up to 1:00 before its window (5:00-4:00
    left) or inside it, more than the market's entry cutoff left, BTC within 0.2% of the strike (twice its 0.1%).
    Progress = 1 at 0.1% or nearer, falling to 0 at 0.2%."""
    t = THRESHOLDS["HPANIC"]
    x = _base(v, market)
    cut = int(v["window"].get("entry_cutoff_ms") or 0) / 1000
    if x is None or not cut < x["left_s"] <= t.left_s[1] + 60 + EPS:
        return None
    band = t.near_pct * x["strike"]
    dist = abs(x["btc"] - x["strike"])
    if dist > 2 * band + EPS:
        return None
    e = _edges(v, x, market)
    cheap = None
    if e is not None:
        ok = [s for s in ("UP", "DOWN") if e[s] is not None and e[s][0] <= t.ask_hi + EPS]
        cheap = min(ok, key=lambda s: e[s][0]) if ok else None
    missing = [m for m in (_time_missing(t, x["left_s"]),
                           f"BTC is {money(dist)} from the strike (needs {money(band)} or less)" if dist > band + EPS
                           else None,
                           None if cheap is not None and e[cheap][2] > t.edge + EPS
                           else f"no side at {cents(t.ask_hi)} or less with over {t.edge * 100:.0f}¢ of edge") if m]
    side = cheap or ("UP" if x["gap"] < 0 else "DOWN")
    return {"progress": max(0.0, min(1.0, (2 * band - dist) / band)), "missing": missing, "side": side,
            "ask": e[cheap][0] if cheap else None,
            "needs": f"Needs BTC within {money(band)} of the strike, a side at {cents(t.ask_hi)} or less with over "
                     f"{t.edge * 100:.0f}¢ of edge, {_window_words(t)}.",
            "have": f"Have: {money(dist)} from the strike, {clock(x['left_s'])} left."}


AMBERS: dict[str, Callable[[dict, str], dict | None]] = {
    "H4": amber_h4, "CUSHION100": amber_cushion100, "LATERESID": amber_lateresid, "HPANIC": amber_hpanic,
}
if set(AMBERS) != set(RULES) or set(THRESHOLDS) != set(RULES):
    raise ValueError("setups: every agent rule needs an amber definition and its thresholds "
                     f"(rules {sorted(RULES)}, amber {sorted(AMBERS)}, thresholds {sorted(THRESHOLDS)})")


# -- the record words ---------------------------------------------------------------------------------------------------
def record_words(t: dict | None) -> str:
    """The rule's live record in one honest sentence (rule_scores.tally)."""
    if not t or not t.get("trades"):
        return "No live record yet."
    n, w = t["trades"], t["wins"]
    be = "" if t.get("break_even_pct") is None else f", break-even {t['break_even_pct']:.0f}%"
    net = t.get("net_usd") or 0.0
    head = f"Live record {w} of {n}{be}, net {'−' if net < 0 else '+'}${abs(net):.2f}"
    return head + (": anecdotal, not proven." if n < 5 else ".")


def backtest_words(b: dict | None) -> str | None:
    if not b or not b.get("n"):
        return None
    return (f"Backtest holdout: {b['n']} trades, {b['won_pct']:.0f}% won vs {b['break_even_pct']:.1f}% "
            f"break-even.")


def break_even(v: dict, market: str, ask: float | None) -> float | None:
    """The hold-to-settlement break-even win rate at this ask: (ask + the venue's fee for one contract) per $1."""
    if ask is None:
        return None
    return round((ask + _fee(v, market, ask)) * 100, 1)


# -- one light ----------------------------------------------------------------------------------------------------------
def light(rule_id: str, market: str, view: dict, record: dict | None = None, backtest: dict | None = None) -> dict:
    """{rule, market, state, progress, side, ask, line, details, match (green only), record, backtest}."""
    if rule_id not in RULES:
        raise ValueError(f"unknown rule {rule_id!r}; rules are {', '.join(RULES)}")
    rule = RULES[rule_id]
    base = {"rule": rule_id, "market": market, "label": rule.label, "record": record, "backtest": backtest,
            "record_text": record_words(record), "backtest_text": backtest_words(backtest)}
    m = ready(rule_id, market, view)
    if m is not None:
        stop = m["stop_index_below"] if m["side"] == "UP" else m["stop_index_above"]
        be = break_even(view, market, m["ask"])
        details = [f"BTC {money(abs(m['gap']))} past the strike with {clock(m['left_s'])} left, "
                   f"{side_word(m['side'])} at {cents(m['ask'])}.",
                   base["record_text"] + ("" if be is None else f" Break-even at this price: {be:.0f}%."),
                   "Its stop: BTC " + (f"at {money(stop)}." if stop is not None else "none; it rides to settlement.")]
        if base["backtest_text"]:
            details.append(base["backtest_text"])
        return {**base, "state": GREEN, "progress": 1.0, "side": m["side"], "ask": m["ask"],
                "line": f"{rule_id} fits this window.", "details": details,
                "match": {"side": m["side"], "ask": m["ask"],
                          # the price you were shown + 2c, never above the rule's cap (as Assist approves)
                          "max_price": min(rule.ask_cap, round(float(m["ask"]) + APPROVE_SLACK, 2)),
                          "stop_index_below": m["stop_index_below"], "stop_index_above": m["stop_index_above"],
                          "stop": stop, "window_end": m["window_end"], "ticker": m["ticker"],
                          "break_even_pct": be}}
    a = AMBERS[rule_id](view, market)
    if a is None:
        return {**base, "state": GREY, "progress": 0.0, "side": None, "ask": None, "line": None, "details": [],
                "match": None}
    what = " and ".join(a["missing"]) if a["missing"] else "waiting for the rule's other inputs"
    details = [a["needs"], a["have"], base["record_text"]]
    if base["backtest_text"]:
        details.append(base["backtest_text"])
    return {**base, "state": AMBER, "progress": round(a["progress"], 3), "side": a["side"], "ask": a["ask"],
            "line": f"{rule_id} is setting up: {what}.", "details": details, "match": None}


def lights(market: str, view: dict, statuses: dict[str, dict], records: dict[str, dict],
           backtests: dict[str, dict]) -> list[dict]:
    """A light for every active rule that trades this market (RULES order). `statuses` is RuleBook.statuses():
    a rule missing from it is an error (never assumed active)."""
    out = []
    for rid, rule in RULES.items():
        if market not in rule.markets:
            continue
        if rid not in statuses:
            raise ValueError(f"no status for rule {rid!r}")
        if statuses[rid]["status"] != "active":
            continue
        out.append(light(rid, market, view, records.get(rid), backtests.get(rid)))
    return out
