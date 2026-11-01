"""Coach rules for the Kalshi 15-minute window (deterministic; the human decides, nothing is ordered).

Prices are dollars per contract (0–1). UP = buy Up (Kalshi YES), DOWN = buy Down (Kalshi NO). `fair(side)` is the fair-odds
model's probability for that side (fair_value.py); `bid` is what the held contract could be sold for.

ENTER (one open setup at a time):
  our odds are first shrunk halfway toward the market (MARKET_WEIGHT): fair_used = ½·fair_up + ½·market mid.
  edge(side) = fair_used(side) − ask(side) − the Kalshi fee on the buy.
  A side is eligible only if its ask is at least LONGSHOT_FLOOR (25¢); under LONGSHOT_ZONE (35¢) it needs
    MIN_EDGE + LONGSHOT_EXTRA_EDGE (8¢), otherwise MIN_EDGE (5¢).
  Enter the eligible side with the larger edge, when more than 2 minutes are left, Kalshi quotes both asks, and
    the 15m engine does not point the other way (its official call or live lean is not the
    opposite side; an engine that sits out does not block, its reason is shown).
  entry limit = the current ask, plus up to ENTRY_SLACK while the BLENDED edge still clears MIN_EDGE.

ENTRY must be confirmed: the same side has to qualify continuously for CONFIRM_MS (10s) first, so a
one-tick blip in the odds never opens a position (see `Confirmation`).

EXIT is value-based on our raw (unblended) calibrated odds, judged every quote in this order:
  1. stop        EITHER fair(side) < stop OR the held side's bid <= stop, where stop = entry − 0.15
                 (floored at 0.02) -> exit at the bid. The price trigger sells while the bid is still near the
                 stop instead of waiting for our odds to catch up after the bid has collapsed.
  2. min hold    otherwise hold for the first MIN_HOLD_MS (30s): odds jitter is not a reason to sell
  3. take value  bid >= fair(side) − 0.02 AND the sale is a profit after both Kalshi fees -> exit at the bid
  4. time        in the last 60s, unless holding is clearly better (fair(side) >= bid + 0.05) -> exit at the bid
  otherwise HOLD; "hold to settlement" when fair(side) >= bid + 0.05 (selling would give up value).
  A setup still open at the close is settled at the payout (1 or 0) once the window's result is known.

Paper P&L per contract: exit − entry − fees (Kalshi taker fee on the buy and on the sell; no fee at settlement).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from btc_signal_engine.domain.coach.text import cents, pct
from btc_signal_engine.domain.settlement.fees import kalshi_fee

MIN_EDGE = 0.05

# Longshot floor. MERID, a live Kalshi 15-minute crypto bot, won 0 of 16 trades bought under 20¢; its study of
# 390k settled candidates found bought sides under 25¢ net negative and 25–34¢ positive, so it rejects entries
# under 25¢ and wants extra edge under 35¢ (merid/prediction/trade_decision.py, MERID_FLB_LONGSHOT_SLOPE).
# Becker's Kalshi-wide study agrees: contracts under 20¢ win less often than their price implies.
LONGSHOT_FLOOR = 0.25  # an ask below this is never bought
LONGSHOT_ZONE = 0.35  # an ask in [FLOOR, ZONE) needs MIN_EDGE + LONGSHOT_EXTRA_EDGE
LONGSHOT_EXTRA_EDGE = 0.03

# Shrink toward the market before measuring edge. Good bots weight the market heavily (MERID puts 0.25–0.85 on
# it); a walk-forward ML model beat the Kalshi midpoint by only 0.0014 log loss, so a raw gap between our odds and
# the market is mostly our error. Exits keep the raw calibrated odds.
MARKET_WEIGHT = 0.5
MIN_LEFT_MS = 120_000
TIME_RULE_MS = 60_000
STOP_GAP, STOP_FLOOR = 0.15, 0.02
TAKE_VALUE_GAP = 0.02
HOLD_GAP = 0.05
CONFIRM_MS = 10_000
MIN_HOLD_MS = 30_000
MIN_PROFIT = 0.01
EPS = 1e-9


@dataclass(frozen=True, slots=True)
class Setup:
    side: str  # "UP" | "DOWN"
    entry: float
    fair_up: float  # at entry
    stop: float  # exit if fair(side) drops below this
    time_exit: int  # start of the last-60s time rule
    window_end: int
    edge: float = 0.0  # after the buy fee, measured on fair_used
    entry_ts: int = 0
    fair_used: float | None = None  # fair_up blended toward the market mid at entry (fair_up stays raw)
    market_mid: float | None = None  # the market's UP odds used in the blend


@dataclass(frozen=True, slots=True)
class NoTrade:
    code: str  # too_late | no_market | no_edge | longshot | mixed
    sentence: str


@dataclass(frozen=True, slots=True)
class Advice:
    action: str  # "exit" | "hold_to_settlement" | "hold"
    reason: str | None  # exit: "stop" | "take_value" | "time"
    target_bid: float  # bid at which taking value starts: fair(side) − 2¢
    fair_side: float


@dataclass(frozen=True, slots=True)
class Exit:
    outcome: str  # stop | take_value | time | settled
    price: float
    ts: int
    fair_side: float | None = None


def side_fair(fair_up: float, side: str) -> float:
    return fair_up if side == "UP" else 1.0 - fair_up


def _ok(x: float | None) -> bool:
    return x is not None and 0.0 < x < 1.0


def market_mid(quote: dict) -> float | None:
    """The market's UP odds: the yes bid/ask midpoint, else the midpoint of the two asks, else None."""
    yb, ya, na = quote.get("yes_bid"), quote.get("yes_ask"), quote.get("no_ask")
    if _ok(yb) and _ok(ya):
        return (yb + ya) / 2
    if _ok(ya) and _ok(na):
        return (ya + (1.0 - na)) / 2
    return None


def blend_fair(fair_up: float, quote: dict, market_weight: float = MARKET_WEIGHT) -> tuple[float, float | None]:
    """(fair_used_up, market_mid_up): our odds shrunk toward the market's; unchanged when no mid is computable."""
    mid = market_mid(quote)
    if mid is None:
        return fair_up, None
    return (1.0 - market_weight) * fair_up + market_weight * mid, mid


def required_edge(ask: float, min_edge: float = MIN_EDGE) -> float | None:
    """Edge a side bought at `ask` needs; None = below the longshot floor, never bought."""
    if ask < LONGSHOT_FLOOR - EPS:
        return None
    return min_edge + LONGSHOT_EXTRA_EDGE if ask < LONGSHOT_ZONE - EPS else min_edge


def _longshot_sentence(side: str, ask: float, edge: float, min_edge: float) -> str:
    name = "Up" if side == "UP" else "Down"
    if ask < LONGSHOT_FLOOR - EPS:
        return (f"The cheap side is a long shot: {name} costs {cents(ask)}, and contracts under "
                f"{cents(LONGSHOT_FLOOR)} have lost money for live bots even when models said they were cheap.")
    return (f"The cheap side is a long shot: {name} costs {cents(ask)}. Under {cents(LONGSHOT_ZONE)} we want at "
            f"least {cents(min_edge + LONGSHOT_EXTRA_EDGE)} of edge, because cheap contracts lose more often than "
            f"models expect; this one has {cents(edge)}.")


def entry_decision(fair_up: float, quote: dict, at: int, window_end: int, *, engine_against: str | None = None,
                   min_edge: float = MIN_EDGE, min_left_ms: int = MIN_LEFT_MS) -> Setup | NoTrade:
    """`engine_against`: the engine's direction (official call, else live lean) if it has one, else None.

    `fair_up` is our raw calibrated odds. Edges are measured on its blend toward the market mid (MARKET_WEIGHT),
    and a side under LONGSHOT_FLOOR is never bought (see the constants for the evidence)."""
    if window_end - at <= min_left_ms:
        return NoTrade("too_late", f"Too close to the close: {min_left_ms // 60_000} minutes or less left, "
                                   "not enough time to get in and out.")
    yes_ask, no_ask = quote.get("yes_ask"), quote.get("no_ask")
    if not (_ok(yes_ask) and _ok(no_ask)):
        return NoTrade("no_market", "Kalshi is not quoting both sides of this window right now.")
    used, mid = blend_fair(fair_up, quote)
    asks = {"UP": yes_ask, "DOWN": no_ask}
    # Edge is net of the Kalshi fee on the buy: at extreme prices the fee alone can eat a 5c gap.
    edges = {"UP": used - yes_ask - kalshi_fee(1, yes_ask),
             "DOWN": (1.0 - used) - no_ask - kalshi_fee(1, no_ask)}
    need = {s: required_edge(asks[s], min_edge) for s in asks}
    eligible = [s for s in asks if need[s] is not None and edges[s] >= need[s] - EPS]
    if not eligible:
        blocked = [s for s in asks if edges[s] >= min_edge - EPS]  # would qualify but for the floor or zone
        if blocked:
            s = max(blocked, key=lambda k: edges[k])
            return NoTrade("longshot", _longshot_sentence(s, asks[s], edges[s], min_edge))
        mkt = f", the market's {pct(mid)}, blended {pct(used)}" if mid is not None else ""
        return NoTrade("no_edge", (
            f"The market already prices it: UP costs {cents(yes_ask)} and DOWN costs {cents(no_ask)}; "
            f"our odds UP {pct(fair_up)}{mkt}. "
            f"We only enter when a side is at least {cents(min_edge)} cheaper than the blended odds after "
            "Kalshi's fee."))
    side = max(eligible, key=lambda s: edges[s])
    if engine_against in ("UP", "DOWN") and engine_against != side:
        return NoTrade("mixed", f"Conditions are mixed: our odds make {side} {cents(edges[side])} cheap, "
                                f"but the engine points {engine_against}.")
    entry = asks[side]
    stop = round(max(STOP_FLOOR, entry - STOP_GAP), 2)
    return Setup(side, entry, fair_up, stop, window_end - TIME_RULE_MS, window_end, round(edges[side], 4), at,
                 round(used, 4), None if mid is None else round(mid, 4))


ENTRY_SLACK = 0.02  # live tests (2026-10-28) lost 3 of 4 setups to a 1c move between proposal and send


def entry_limit(entry: float, fair_side: float, *, min_edge: float = MIN_EDGE, slack: float = ENTRY_SLACK) -> float:
    """The order's limit: up to `slack` above the ask we saw, but only as far as our edge after the buy fee still
    clears the edge required at that price (required_edge: 8c in the longshot zone, else `min_edge`). Never below
    the seen ask; an IOC buy still pays the best ask available.
    Pass the BLENDED fair for the side (blend_fair), so the slack only applies while the blended edge clears."""
    best = entry
    for k in range(1, int(round(slack * 100)) + 1):
        p = round(entry + k / 100, 2)
        need = required_edge(p, min_edge)
        if p >= 1.0 or need is None or fair_side - p - kalshi_fee(1, p) < need - EPS:
            break
        best = p
    return best


def profitable_exit(entry: float, bid: float) -> bool:
    """Selling at `bid` beats the entry after the buy and sell fees by at least MIN_PROFIT."""
    return bid - entry - kalshi_fee(1, entry) - kalshi_fee(1, bid) >= MIN_PROFIT - EPS


def exit_advice(setup: Setup, bid: float | None, fair_up: float, ts: int, *, min_hold_ms: int = MIN_HOLD_MS) -> Advice:
    fair = side_fair(fair_up, setup.side)
    target = round(fair - TAKE_VALUE_GAP, 4)
    if bid is None:
        return Advice("hold", None, target, fair)  # nothing to sell into right now
    if fair < setup.stop - EPS or bid <= setup.stop + EPS:
        return Advice("exit", "stop", target, fair)
    if ts - setup.entry_ts < min_hold_ms:
        return Advice("hold", None, target, fair)
    if bid >= target - EPS and profitable_exit(setup.entry, bid):
        return Advice("exit", "take_value", target, fair)
    clearly_better = fair >= bid + HOLD_GAP - EPS
    if ts >= setup.time_exit and not clearly_better:
        return Advice("exit", "time", target, fair)
    return Advice("hold_to_settlement" if clearly_better else "hold", None, target, fair)


class Confirmation:
    """Tracks how long one side has qualified for entry without interruption."""

    def __init__(self, confirm_ms: int = CONFIRM_MS) -> None:
        self._confirm = confirm_ms
        self._side: str | None = None
        self._since: int | None = None

    def observe(self, side: str | None, at: int) -> bool:
        """Report the side that qualifies at `at` (None = nothing qualifies). True once confirmed."""
        if side is None:
            self._side, self._since = None, None
            return False
        if side != self._side:
            self._side, self._since = side, at
        return at - self._since >= self._confirm

    def remaining_ms(self, at: int) -> int:
        return 0 if self._since is None else max(0, self._confirm - (at - self._since))


def held_bid(side: str, quote: dict) -> float | None:
    b = quote.get("yes_bid" if side == "UP" else "no_bid")
    return b if b is not None and 0.0 <= b <= 1.0 else None


def replay(setup: Setup, quotes: Iterable[dict], fair_at: Callable[[int], float | None], *,
           min_hold_ms: int = MIN_HOLD_MS) -> Exit | None:
    """First exit on a recorded quote path (quotes after entry, sorted by ts), using the live rules.

    `fair_at(ts)` returns fair_up from recorded index data at that moment (None = no data; skipped).
    Quotes at or after the window end are ignored (the market has closed; settlement takes over).
    """
    for q in quotes:
        if q["ts"] >= setup.window_end:
            break
        f = fair_at(q["ts"])
        if f is None:
            continue
        a = exit_advice(setup, held_bid(setup.side, q), f, q["ts"], min_hold_ms=min_hold_ms)
        if a.action == "exit":
            return Exit(a.reason, held_bid(setup.side, q), q["ts"], round(a.fair_side, 4))
    return None


def settlement_exit(setup: Setup, actual: str) -> Exit:
    return Exit("settled", 1.0 if actual == setup.side else 0.0, setup.window_end)


def pnl(entry: float, exit_price: float, *, settled: bool, fee_rate: float = 0.07) -> tuple[float, float]:
    """(net P&L per contract, fees)."""
    fees = kalshi_fee(1, entry, fee_rate) + (0.0 if settled else kalshi_fee(1, exit_price, fee_rate))
    return round(exit_price - entry - fees, 4), round(fees, 4)
