"""Agent entry rules: "rules open, code exits, the agent supervises" (2026-10-30, research-backed user decision).

Opening by a deterministic rule is the reliable half; hard exits belong in code (the bracket's take profit and its BTC
index stop, watched by TradingService's monitor); the LLM supervises: it journals, monitors and may propose a soft
thesis exit, and never decides these opens.

Each registered rule runs on one or more markets (agent market keys) and is a pure function over one market's
`PlayService.now_view` dict and that market's key -> a match (the side, the price cap, the invalidation BTC stop or
none, and every input it used) or None. No I/O, no clock: the view carries `now`. The strike is the view's
`btc.strike` (Kalshi floor_strike on Kalshi; on Polymarket the window's price to beat).

Rules (docs/research/LEARNINGS.md rows H4, EXP-CUSHION100, H-PANIC, LATE_RESIDUAL); all hold to settlement otherwise:
- H4 (Kalshi 15m, Polymarket 15m): 3:00-5:00 elapsed (10:00-12:00 left, both ends included); |BTC - strike| >= 0.4%
  of the strike; buy BTC's side if its ask is at most 95c. Invalidation stop: strike + half the gap on BTC's side
  (UP: stop_index_below = strike + gap/2; DOWN: stop_index_above = strike - |gap|/2).
- CUSHION100 (Kalshi 15m, Polymarket 15m): 5:00-10:00 left (both ends included); BTC at least $100 past the strike;
  BTC's side's ask between 70c and 90c (both included). Invalidation stop: strike +/- $40 on BTC's side.
  Neither has a research basis for 5-minute windows, so neither runs on Polymarket 5m (no invented thresholds).
- HPANIC (killed in backtest, row H-PANIC, variant 35c / <= 4 min; a live forward check; Kalshi 15m, Polymarket 15m
  and 5m): at most 4:00 left and more than the market's entry cutoff left (2:00 on the 15-minute markets, 1:00 on
  Polymarket 5m); BTC within 0.1% of the strike (|BTC - strike| <= 0.001 x strike); a side whose ask is <= 35c and
  whose residual (model fair - ask - fee) is more than 3c: buy it (the cheaper side when both qualify; a tie is UP).
  No stop: it rides to settlement (h_panic is hold-only).
- LATERESID (killed in backtest, row LATE_RESIDUAL, best-holdout variant edge 5c / cap 65c; Kalshi 15m, Polymarket
  15m): 4:00-3:00 left (both ends included); edge per side = model fair - ask - fee; the side with the larger edge (a
  tie, or no DOWN ask, is UP) if its edge is >= 5c and its ask <= 65c. No stop: it rides to settlement.
Model fair = the view's `call.fair_up` (DOWN: 1 - fair_up); none = no trade. Fee = the venue's own taker fee function
for one contract at the ask (Kalshi 0.07 x C x P x (1 - P) rounded up to $0.0001; Polymarket US `polymarket_fee`, the
nearest cent per order), at the view's `stake.fee_rate` when it carries one.
All need an available view with a fresh quote, a strike, a BTC price and entry open. The executor (the runner) adds
the session guards: armed, running, one rule entry per market window, one per settlement instant across venues when
the session's one_bet_per_window is on (the default), none while the session holds that market, none with under the
rule's `min_left_ms` left (MIN_LEFT_MS; HPANIC: None, the market's own entry cutoff, which the rule checks).

The price cap (one rule for every way a rule's entry is sent; review fix plan 2026-10-31, B2 "Auto vs Assist price
caps differ"): `entry_cap(rule, ask)` = the ask the rule matched + APPROVE_SLACK (2c), never above the rule's own
`ask_cap`. Auto's rule entry (agent_runner), an Assist proposal (agent_proposals: the ask shown when proposed) and a
setup light's match (setups) all take it; before 2026-10-31 Auto sent at the rule's cap (up to 95c on a 58c match).
A match without a usable ask has no cap: None, and the caller sends nothing (Auto used to fall back to the rule's cap).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from btc_signal_engine.domain.brackets import stop_fields, stop_of
from btc_signal_engine.domain.markets import market as market_spec
from btc_signal_engine.domain.settlement.fees import kalshi_fee, polymarket_fee

MIN_LEFT_MS = 180_000  # no H4/CUSHION100/LATERESID entry with under 3 minutes left (the user's own losses)
DEFAULT_CUTOFF_MS = 120_000  # TradingConfig.min_left_ms: the cutoff of a market with none of its own
TAKE_PROFIT = 0.99  # hold to settlement
KILLED = "killed in backtest — live forward check"
FEES: dict[str, Callable[..., float]] = {"kalshi": kalshi_fee, "polymarket": polymarket_fee}  # by venue
EPS = 1e-9
APPROVE_SLACK = 0.02  # how far above the matched (or shown) ask a rule's entry may fill: entry_cap


@dataclass(frozen=True)
class Rule:
    id: str
    markets: tuple[str, ...]  # the agent market keys it trades
    description: str  # one line, for the launch form, status and the agent
    ask_cap: float  # the most paid per contract (the order's max_price)
    match: Callable[[dict, str], dict | None]
    label: str = ""  # KILLED for the backtest-killed forward checks
    min_left_ms: int | None = MIN_LEFT_MS  # the runner's floor; None: the market's entry cutoff (the rule checks it)


def entry_cap(rule: Rule, ask: object) -> float | None:
    """The most a rule's entry may pay per contract: the matched ask + APPROVE_SLACK, never above the rule's cap
    (module docstring). None when `ask` is not a price strictly between 0 and 1 (nothing may be sent)."""
    try:
        a = float(ask)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not 0.0 < a < 1.0:
        return None
    return min(rule.ask_cap, round(a + APPROVE_SLACK, 2))


def _base(v: dict, market: str) -> dict | None:
    """The common inputs of a rule (no side yet), or None when the view cannot be decided on."""
    if not v or not v.get("available"):
        return None
    w, m, b = v.get("window") or {}, v.get("market") or {}, v.get("btc") or {}
    if not m.get("fresh") or not w.get("entry_open"):
        return None
    strike, btc = b.get("strike"), b.get("btc")
    if strike is None or btc is None or not strike > 0:
        return None
    now = int(v["now"])
    gap = round(btc - strike, 2)
    return {"market": market, "now": now, "window_start": int(w["start"]), "window_end": int(w["end"]),
            "ticker": m.get("ticker"), "elapsed_s": (now - int(w["start"])) / 1000,
            "left_s": (int(w["end"]) - now) / 1000, "btc": btc, "strike": strike, "gap": gap,
            "gap_pct": round(abs(gap) / strike * 100, 4), "yes_ask": m.get("yes_ask"), "no_ask": m.get("no_ask")}


def _inputs(v: dict, market: str) -> dict | None:
    """The common inputs with BTC's side and that side's ask; None when BTC is at the strike."""
    x = _base(v, market)
    if x is None or x["gap"] == 0:
        return None
    side = "UP" if x["gap"] > 0 else "DOWN"
    return {**x, "side": side, "ask": x["yes_ask"] if side == "UP" else x["no_ask"]}


def _fee(v: dict, market: str, ask: float) -> float:
    """The venue's taker fee for one contract at `ask`."""
    fn = FEES[market_spec(market).venue]
    rate = (v.get("stake") or {}).get("fee_rate")
    return fn(1, ask) if rate is None else fn(1, ask, rate)


def order_fee(v: dict, market: str, contracts: float, ask: float) -> float:
    """The venue's taker fee for one order of `contracts` at `ask` (an assist proposal's "could make / lose")."""
    fn = FEES[market_spec(market).venue]
    rate = (v.get("stake") or {}).get("fee_rate")
    return fn(contracts, ask) if rate is None else fn(contracts, ask, rate)


def _edges(v: dict, x: dict, market: str) -> dict | None:
    """{fair_up, UP, DOWN}: per side (ask, fee, fair - ask - fee), or None without an ask; None without a model
    fair value."""
    fair = (v.get("call") or {}).get("fair_up")
    if fair is None or not 0 <= fair <= 1:
        return None
    out: dict = {"fair_up": fair}
    for side, ask, p in (("UP", x["yes_ask"], fair), ("DOWN", x["no_ask"], 1 - fair)):
        fee = None if ask is None else _fee(v, market, ask)
        out[side] = None if ask is None else (ask, fee, round(p - ask - fee, 4))
    return out


def _cutoff_ms(market: str) -> int:
    c = market_spec(market).entry_cutoff_ms
    return DEFAULT_CUTOFF_MS if c is None else int(c)


def _stop(side: str, level: float) -> dict:
    return stop_fields(side, round(level, 2))


NO_STOP = {"stop_index_below": None, "stop_index_above": None}


def h4(v: dict, market: str) -> dict | None:
    x = _inputs(v, market)
    if x is None or not 600 <= x["left_s"] <= 720:
        return None
    if abs(x["gap"]) < 0.004 * x["strike"] - EPS:
        return None
    if x["ask"] is None or x["ask"] > 0.95 + EPS:
        return None
    level = x["strike"] + x["gap"] / 2  # half the gap back toward the strike, on BTC's side
    return {**x, "rule": "H4", "ask_cap": 0.95, **_stop(x["side"], level)}


def cushion100(v: dict, market: str) -> dict | None:
    x = _inputs(v, market)
    if x is None or not 300 <= x["left_s"] <= 600:
        return None
    if abs(x["gap"]) < 100 - EPS:
        return None
    if x["ask"] is None or not 0.70 - EPS <= x["ask"] <= 0.90 + EPS:
        return None
    level = x["strike"] + (40 if x["side"] == "UP" else -40)
    return {**x, "rule": "CUSHION100", "ask_cap": 0.90, **_stop(x["side"], level)}


def hpanic(v: dict, market: str) -> dict | None:
    x = _base(v, market)
    if x is None or not _cutoff_ms(market) / 1000 < x["left_s"] <= 240:
        return None
    if abs(x["btc"] - x["strike"]) > 0.001 * x["strike"] + EPS:
        return None
    e = _edges(v, x, market)
    if e is None:
        return None
    ok = {s: e[s] for s in ("UP", "DOWN") if e[s] is not None and e[s][0] <= 0.35 + EPS and e[s][2] > 0.03 + EPS}
    if not ok:
        return None
    side = "UP" if "UP" in ok and ("DOWN" not in ok or ok["UP"][0] <= ok["DOWN"][0]) else "DOWN"
    ask, fee, resid = ok[side]
    return {**x, "rule": "HPANIC", "side": side, "ask": ask, "ask_cap": 0.35, "fair_up": e["fair_up"], "fee": fee,
            "residual": resid, **NO_STOP}


def lateresid(v: dict, market: str) -> dict | None:
    x = _base(v, market)
    if x is None or not 180 <= x["left_s"] <= 240:
        return None
    e = _edges(v, x, market)
    if e is None or e["UP"] is None and e["DOWN"] is None:
        return None
    side = "UP" if e["DOWN"] is None or (e["UP"] is not None and e["UP"][2] >= e["DOWN"][2]) else "DOWN"
    ask, fee, edge = e[side]
    if edge < 0.05 - EPS or ask > 0.65 + EPS:
        return None
    return {**x, "rule": "LATERESID", "side": side, "ask": ask, "ask_cap": 0.65, "fair_up": e["fair_up"], "fee": fee,
            "edge": edge, **NO_STOP}


K15, P15, P5 = "kalshi_15m", "polymarket_15m", "polymarket_5m"
RULES: dict[str, Rule] = {r.id: r for r in (
    Rule("H4", (K15, P15), "15m windows, 3:00-5:00 elapsed, BTC >= 0.4% from the strike (Polymarket: the price to "
                           "beat): buy BTC's side at <= 95c; BTC stop at half the gap; else hold to settlement.",
         0.95, h4),
    Rule("CUSHION100", (K15, P15), "15m windows, 5:00-10:00 left, BTC >= $100 past the strike: buy BTC's side at "
                                   "70-90c; BTC stop at strike +/- $40; else hold to settlement.", 0.90, cushion100),
    Rule("HPANIC", (K15, P15, P5), f"{KILLED}: <= 4:00 left (more than the market's entry cutoff), BTC within 0.1% "
                                   "of the strike, the cheaper side at <= 35c with model fair - ask - fee > 3c: buy "
                                   "it; no stop, hold to settlement.", 0.35, hpanic, label=KILLED, min_left_ms=None),
    Rule("LATERESID", (K15, P15), f"{KILLED}: 4:00-3:00 left, the side with the larger model fair - ask - fee if "
                                  ">= 5c and its ask <= 65c; no stop, hold to settlement.", 0.65, lateresid,
         label=KILLED),
)}


def catalog() -> list[dict]:
    """Every registered rule: {id, markets, market_labels, label, description} (the launch form, status, the agent)."""
    return [{"id": r.id, "markets": list(r.markets), "market_labels": [market_spec(k).label for k in r.markets],
             "label": r.label, "description": r.description} for r in RULES.values()]


def evaluate(rule_id: str, market: str, view: dict) -> dict | None:
    """The rule's match on this market's view, or None. An unknown rule id, or a market the rule does not trade, is
    an error."""
    if rule_id not in RULES:
        raise ValueError(f"unknown rule {rule_id!r}; rules are {', '.join(RULES)}")
    rule = RULES[rule_id]
    if market not in rule.markets:
        raise ValueError(f"rule {rule_id} does not trade {market!r}; it trades {', '.join(rule.markets)}")
    return rule.match(view, market)


def note(m: dict) -> str:
    """The audit note of a rule entry: the tag and every input used."""
    stop = stop_of(m)
    extra = "".join(f"; {k} {m[k]:.4f}" for k in ("fair_up", "fee", "residual", "edge") if m.get(k) is not None)
    stop_txt = "no stop" if stop is None else f"BTC stop {stop:.2f}"
    return (f"[rule:{m['rule']}] {m['side']} at ask {m['ask']:.2f} (cap {m['ask_cap']:.2f}) on {m['market']}; BTC "
            f"{m['btc']:.2f} vs strike {m['strike']:.2f}: gap {m['gap']:+.2f} ({m['gap_pct']:.3f}%); "
            f"{m['elapsed_s']:.0f}s elapsed, {m['left_s']:.0f}s left{extra}; {stop_txt}; hold to settlement "
            "otherwise.")
