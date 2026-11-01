"""Play: the numbers behind the phone "Play" screens, where the USER makes the call (strategy "you"). Pure.

Display only: nothing here decides or sends a trade. The human taps BUY UP / BUY DOWN / EXIT NOW and every order still
goes through TradingService's ticket, token binding and OrderGate.

- The call: the market's own lean from the Kalshi mid (UP at 55% or more, DOWN at 45% or less, else COIN FLIP).
  Kalshi's 15m prices are well calibrated (docs/research/LEARNINGS.md), so this is the market's read, not an edge.
- Ideal entry for a side = min(ask, bid + 1c): the price a resting bid could hope for. Shown as information only; v1
  buys at the ask with the existing immediate-or-cancel entry (limit = ask + 1c). Resting-bid entry is a later feature.
- Ideal exit = RIDE (live-tested 2026-10-29): ride to settlement, the resting sell at the 99c cap; the BTC index stop
  $10 past the strike on the wrong side is offered as a suggestion only (the user sets a stop, if any).
- In-trade advice: HOLD by default; CONSIDER EXIT when BTC is within $20 of the stop level or the held side's bid
  (the market's probability for it) is under 50%. Always with the numbers behind it.
- Scoreboard: record, streak, P&L by period and "your calls vs the market" (win % next to the average price paid,
  which is the break-even win %). Under ~50 calls it is mostly luck.
- Settlement: UP when the CF Benchmarks BRTI 60-second average at the close is at or above the strike (the open
  average; ties are UP) on both Kalshi KXBTC15M and Polymarket US (adapters/polymarket_us/markets.py: "Up when
  settlementPrice >= priceToBeat", identical to Kalshi since 2026-09-24).
- Entry cutoff: TradingService refuses an entry with the strategy's min_left_ms or less left (strategy "you": the
  configured 2 minutes); entry_open() mirrors that gate for the screen.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from btc_signal_engine.domain.coach.money import position_for_stake
from btc_signal_engine.domain.settlement.fees import kalshi_fee

UP_AT = 0.55  # the Kalshi mid at or above this: the market leans UP
DOWN_AT = 0.45  # at or below this: it leans DOWN; between: COIN FLIP
CROWD_MIN_LEAN = 0.2  # crowd_summary's own threshold for "more buying Up/Down"
NEAR_STOP_USD = 20.0  # CONSIDER EXIT when BTC is this close to the stop level
EXIT_BELOW_PROB = 0.50  # ... or the held side's bid is under this
DEFAULT_STAKE = 5.0
LUCK_N = 50  # under this many decided calls, a win rate is mostly luck
DAY_MS = 86_400_000
EPS = 1e-9


@dataclass(frozen=True)
class RideParams:
    """The phone's entry and exit plan (the exit chosen 2026-10-29 from the pre-registered exit study,
    docs/research/pnw-tuning.md row H-PNW-X, `bstop-10`): an IoC buy at up to ask + `slack`; take profit = ask +
    `take_profit_gap` capped at 99c (1.0 = always the 99c cap: ride to settlement), or `take_profit_fixed`; the stop
    level once BTC is `stop_gap` dollars past the strike on the wrong side (negative = past it)."""
    slack: float = 0.01
    take_profit_gap: float = 1.0
    take_profit_fixed: float | None = None
    stop_gap: float = -10.0


RIDE = RideParams()


def index_stop_level(side: str, strike: float, stop_gap: float) -> float:
    """The BTC index level that stops a `side` position: UP sells once S <= K + stop_gap, DOWN once S >= K - stop_gap
    (RIDE's stop_gap -10: $10 past the strike on the wrong side)."""
    return round(strike + stop_gap, 2) if side == "UP" else round(strike - stop_gap, 2)
ENTRY_NOTE = ("Ideal entry = the lower of the ask and the bid + 1¢ (what a resting bid might get). This version "
              "buys at the ask with an immediate-or-cancel order (limit ask + 1¢); resting-bid entry is a later "
              "feature.")
SETTLE_UP_OP = "≥"  # UP iff the closing 60 s average >= the strike (ties are UP), both venues
WARN_LEFT_MS = 120_000  # the countdown turns amber under this
LUCK_NOTE = f"Under ~{LUCK_N} calls a win rate is mostly luck."


def _price(x: float | None) -> float | None:
    return x if x is not None and 0.0 < x < 1.0 else None


def mid(bid: float | None, ask: float | None) -> float | None:
    bid, ask = _price(bid), _price(ask)
    return None if bid is None or ask is None else round((bid + ask) / 2, 4)


def short_side(yes_bid: float | None, yes_ask: float | None) -> tuple[float | None, float | None]:
    """DOWN (the short side) from a YES-only book: no_bid = 1 - yes_ask, no_ask = 1 - yes_bid."""
    ya, yb = _price(yes_ask), _price(yes_bid)
    return (None if ya is None else round(1 - ya, 4)), (None if yb is None else round(1 - yb, 4))


def verdict(mid_up: float | None, up_at: float = UP_AT, down_at: float = DOWN_AT) -> dict:
    """The one plain answer for the top of the screen: ENTER UP / ENTER DOWN / WAIT, with a one-line reason. It is
    the market's own lean (well calibrated, so not an edge) at the user's thresholds."""
    side = lean(mid_up, up_at, down_at)
    if mid_up is None:
        action, headline, reason = "WAIT", "WAIT", "No live price yet."
    elif side == "UP":
        action, headline = "ENTER UP", "ENTER UP"
        reason = f"The market prices UP at {mid_up * 100:.0f}% (≥ {up_at * 100:.0f}%)."
    elif side == "DOWN":
        action, headline = "ENTER DOWN", "ENTER DOWN"
        reason = f"The market prices DOWN at {(1 - mid_up) * 100:.0f}% (UP ≤ {down_at * 100:.0f}%)."
    else:
        action, headline = "WAIT", "WAIT / coin flip"
        reason = f"UP at {mid_up * 100:.0f}%: between {down_at * 100:.0f}% and {up_at * 100:.0f}%, a coin flip."
    return {"lean": side, "action": action, "headline": headline, "reason": reason, "sentence": reason,
            "mid_up": mid_up, "up_at": up_at, "down_at": down_at}


def agreement(leans: Sequence[str | None], side: str | None) -> dict:
    """How many signals lean the verdict's way ("N of M signals agree"); no side (WAIT) -> n None."""
    return {"n": None if side not in ("UP", "DOWN") else sum(1 for x in leans if x == side), "of": len(leans),
            "side": side if side in ("UP", "DOWN") else None}


def thin_series(points: Sequence[tuple[int, float]], max_points: int = 180) -> list[list]:
    """Every k-th (ts, price) so a chart gets at most `max_points` (the newest always kept)."""
    pts = list(points)
    if len(pts) <= max_points:
        return [[int(t), round(float(p), 2)] for t, p in pts]
    step = math.ceil(len(pts) / max_points)
    out = pts[::step]
    if out[-1] != pts[-1]:
        if len(out) >= max_points:
            out[-1] = pts[-1]
        else:
            out.append(pts[-1])
    return [[int(t), round(float(p), 2)] for t, p in out]


def window_levels(points: Sequence[tuple[int, float]]) -> dict:
    """Reference lines for the live chart from the window's full per-second series (before thinning): the running
    high and low so far and the first price at or after the open. Reference only, not a signal."""
    pts = list(points)
    if not pts:
        return {"hi": None, "lo": None, "open": None}
    prices = [float(p) for _, p in pts]
    return {"hi": round(max(prices), 2), "lo": round(min(prices), 2), "open": round(prices[0], 2)}


def contracts_for(stake: float, price: float | None, fee_fn: Callable[[float, float], float] | None = None, *,
                  whole: bool = False, fee_rate: float = 0.07) -> float:
    """Contracts a ticket would buy for `stake` at `price`, as TradingService.prepare sizes it (2 dp; whole contracts
    on Polymarket, whose cost incl. its fee fits the stake)."""
    if price is None or not 0 < price < 1 or not stake or stake <= 0:
        return 0.0
    fee_fn = fee_fn or (lambda c, p: kalshi_fee(c, p, fee_rate))
    n = position_for_stake(stake, price, fee_rate)
    if whole:
        n = float(math.floor(n + EPS))
        while n > 0 and n * price + fee_fn(n, price) > stake + EPS:
            n -= 1.0
    return n


def lean(p: float | None, up_at: float = UP_AT, down_at: float = DOWN_AT) -> str | None:
    """UP / DOWN / COIN FLIP from a probability of UP; None when unknown."""
    if p is None:
        return None
    if p >= up_at - EPS:
        return "UP"
    if p <= down_at + EPS:
        return "DOWN"
    return "COIN FLIP"


def call_lean(call: str | None) -> str | None:
    """An engine / pressure call ("UP", "DOWN", "SKIP", None) as a lean (SKIP and unknown -> None)."""
    return call if call in ("UP", "DOWN") else None


def crowd_lean(net_lean: float | None, min_abs: float = CROWD_MIN_LEAN) -> str | None:
    """crowd_summary's `lean` (-1..1, dollars taken on UP minus DOWN over the total) as UP / DOWN / BALANCED."""
    if net_lean is None:
        return None
    if net_lean >= min_abs - EPS:
        return "UP"
    if net_lean <= -min_abs + EPS:
        return "DOWN"
    return "BALANCED"


def ideal_entry(bid: float | None, ask: float | None) -> float | None:
    """min(ask, bid + 1c): information only (v1 buys at the ask)."""
    bid, ask = _price(bid), _price(ask)
    if ask is None:
        return None
    return ask if bid is None else round(min(ask, bid + 0.01), 2)


MIN_STOP_ROOM = 25.0  # dollars: the phone's stop is never closer than this to BTC at entry (2026-10-29 lesson)


def ride_plan(side: str, strike: float | None, ask: float | None, params: RideParams = RIDE,
              btc: float | None = None, min_room: float = MIN_STOP_ROOM) -> dict:
    """The ticket fields for buying `side` now with the RIDE exit: IoC limit ask + slack, resting sell at
    min(ask + take_profit_gap, 99c) (= 99c for RIDE) and the index stop. The stop is the strike rule
    (strike -/+ $10) or, when BTC is already near it, `min_room` dollars beyond BTC now, whichever is farther:
    a near-coin-flip entry sits next to the strike, and the strike rule alone stopped one out 10 s after
    entry on a $2 wiggle (2026-10-29 15:16). `buyable` is False when there is no
    ask or strike, or the limit leaves no room under the take profit."""
    ask = _price(ask)
    stop = None if strike is None else index_stop_level(side, strike, params.stop_gap)
    if stop is not None and btc is not None:
        stop = round(min(stop, btc - min_room), 2) if side == "UP" else round(max(stop, btc + min_room), 2)
    max_price = None if ask is None else round(min(ask + params.slack, 0.99), 2)
    tp = None if ask is None else round(params.take_profit_fixed if params.take_profit_fixed is not None
                                        else min(ask + params.take_profit_gap, 0.99), 2)
    buyable = max_price is not None and stop is not None and max_price < tp - EPS
    return {"side": side, "max_price": max_price, "take_profit": tp, "stop_level": stop,
            "stop_index_below": stop if side == "UP" else None, "stop_index_above": stop if side == "DOWN" else None,
            "buyable": buyable}


def btc_gap(btc: float | None, strike: float | None) -> dict:
    """BTC vs Kalshi's strike: the gap, which side it favours now and the move that would flip it."""
    if btc is None or strike is None:
        return {"btc": btc, "strike": strike, "gap": None, "leader": None, "flip_move": None, "sentence": None}
    gap = round(btc - strike, 2)
    leader = "UP" if gap >= 0 else "DOWN"
    if gap >= 0:
        sentence = f"BTC is ${gap:,.0f} above the strike: a ${gap:,.0f} drop flips it to DOWN."
    else:
        sentence = f"BTC is ${-gap:,.0f} below the strike: a ${-gap:,.0f} rise flips it to UP."
    return {"btc": btc, "strike": strike, "gap": gap, "leader": leader, "flip_move": round(-gap, 2),
            "sentence": sentence}


def stake_default(max_stake: float, default: float = DEFAULT_STAKE) -> float:
    """$5 unless the server's per-trade limit is lower."""
    return round(max(0.0, min(default, max_stake)), 2)


def entry_open(left_ms: int, cutoff_ms: int) -> bool:
    """True while an entry is still allowed: more than `cutoff_ms` left (TradingService's too_late gate refuses at
    `cutoff_ms` or less)."""
    return left_ms > cutoff_ms


def clock_phase(left_ms: int, cutoff_ms: int, warn_ms: int = WARN_LEFT_MS) -> str:
    """The countdown's colour: "late" at or under the entry cutoff, "warn" under `warn_ms`, else "normal"."""
    if not entry_open(left_ms, cutoff_ms):
        return "late"
    return "warn" if left_ms < warn_ms else "normal"


def stop_distance(side: str, btc: float | None, stop_level: float | None) -> float | None:
    """Dollars BTC can still move against `side` before the stop level (<= 0: at or past it)."""
    if btc is None or stop_level is None:
        return None
    return round(btc - stop_level if side == "UP" else stop_level - btc, 2)


def trade_advice(side: str, *, bid: float | None, btc: float | None, stop_level: float | None, held: float,
                 cost: float | None, fee_rate: float = 0.07, fee_fn: Callable[[float, float], float] | None = None,
                 near_usd: float = NEAR_STOP_USD,
                 min_prob: float = EXIT_BELOW_PROB) -> dict:
    """HOLD unless BTC is within `near_usd` of the stop or the held side's bid is under `min_prob`
    (-> CONSIDER EXIT). `cost` = what the held contracts cost incl. the entry fee. Display only."""
    bid = _price(bid)
    dist = stop_distance(side, btc, stop_level)
    reasons = []
    if dist is not None and dist <= near_usd + EPS:
        reasons.append(f"BTC is past your stop level (${stop_level:,.2f}); the stop sells at the bid." if dist <= 0
                       else f"BTC is ${dist:,.0f} from your stop (${stop_level:,.2f}).")
    if bid is not None and bid < min_prob - EPS:
        reasons.append(f"The market gives your side {bid * 100:.0f}% (bid {bid * 100:.0f}¢), under "
                       f"{min_prob * 100:.0f}%.")
    fee = fee_fn or (lambda c, p: kalshi_fee(c, p, fee_rate))
    sell_now = None if bid is None or held <= 0 else round(held * bid - fee(held, bid), 2)
    if_wins = round(held, 2) if held > 0 else None
    out = {"action": "CONSIDER EXIT" if reasons else "HOLD", "reasons": reasons, "bid": bid,
           "bid_prob": bid, "stop_level": stop_level, "stop_distance": dist, "held": held, "cost": cost,
           "sell_now": sell_now, "if_wins": if_wins, "if_loses": 0.0 if held > 0 else None,
           "pnl_sell_now": None if sell_now is None or cost is None else round(sell_now - cost, 2),
           "pnl_if_wins": None if if_wins is None or cost is None else round(if_wins - cost, 2),
           "pnl_if_loses": None if cost is None or held <= 0 else round(-cost, 2)}
    if held <= 0:
        out.update(action="WAITING", reasons=["Waiting for the entry to fill."])
    out["sentence"] = (" ".join(out["reasons"]) if out["reasons"] else
                       "Hold: BTC is clear of your stop and the market still favours your side.")
    return out


DISAGREE_AT = 0.10  # the market and our model differ by this much (10 points) or more on the held side: say so


def outlook(side: str, *, bid: float | None, ask: float | None, fresh: bool, fair_up: float | None, held: float,
            sell_now: float | None, disagree_at: float = DISAGREE_AT) -> dict:
    """Where an open position is headed, for the held side (`bid` / `ask` = that side's book: YES for UP, NO for DOWN,
    the NO side derived from the YES book on Polymarket). Display only; every unknown number is None.

    chance_now = the market's probability for the held side (its mid), only from a fresh quote; chance_bid = its bid.
    model_chance = our fair odds for the held side (fair_up, or 1 - fair_up for DOWN). disagree = the two differ by
    `disagree_at` or more (None when either is unknown). hold_value = chance_now x held: the market's value of holding
    to settlement, which pays $1 per contract with no settlement fee on either venue. give_up = hold_value - sell_now
    (sell_now = bid x held - exit fee): what exiting now costs in spread and fee versus the market's own odds."""
    chance_now = mid(bid, ask) if fresh else None
    chance_bid = _price(bid) if fresh else None
    model = None if fair_up is None else round(fair_up if side == "UP" else 1.0 - fair_up, 4)
    disagree = None if chance_now is None or model is None else abs(chance_now - model) >= disagree_at - EPS
    hold_value = None if chance_now is None or held <= 0 else round(chance_now * held, 2)
    give_up = None if hold_value is None or sell_now is None else round(hold_value - sell_now, 2)
    return {"chance_now": chance_now, "chance_bid": chance_bid, "model_chance": model, "disagree": disagree,
            "disagree_at": disagree_at, "hold_value": hold_value, "sell_now": sell_now, "give_up": give_up}


def cone(*, now: int, end: int, btc: float | None, strike: float | None, model: dict | None) -> dict | None:
    """The "where it's headed" band from now to the close, from the fair-odds model's own computation (`model` =
    fair_value.fair_from_index's result or the Coach's evaluation model: sigma / sigma_per_s, at, mean_close, sd_close).

    sigma_per_s: per-second dollar volatility; the spot price's 1-sigma band t seconds ahead is sigma_per_s * sqrt(t).
    sd_close / mean_close: the SETTLEMENT price's sd and mean, the 60-second average of the closing minute
    (fair_value.close_avg_dist, the same variance V as prob_up), so P(UP) = Phi((mean_close - strike) / sd_close) and
    the band agrees with the model odds. Averaging makes sd_close a little narrower than sigma * sqrt(seconds to the
    close), which would over-state the settlement's spread. bands = mean_close -/+ 1 and 2 sd_close. None when the
    model (sigma) or BTC is unknown."""
    if btc is None or not model:
        return None
    sigma = model.get("sigma_per_s", model.get("sigma"))
    sd, mean = model.get("sd_close"), model.get("mean_close")
    if sigma is None or sd is None or mean is None:
        return None
    return {"now_ms": now, "end_ms": end, "model_at": model.get("at"), "btc": btc, "strike": strike,
            "sigma_per_s": round(float(sigma), 4), "sd_close": round(float(sd), 2), "mean_close": round(float(mean), 2),
            "bands": {"1": [round(mean - sd, 2), round(mean + sd, 2)], "2": [round(mean - 2 * sd, 2),
                                                                            round(mean + 2 * sd, 2)]}}


def plan_payoff(contracts: float | None, price: float | None, fee_fn: Callable[[float, float], float]) -> dict:
    """A pre-entry plan's settlement payoff for `contracts` bought at `price` (the ticket's max price, as max_loss):
    total cost = contracts x price + entry fee; if_wins = contracts x $1 - cost (no settlement fee), if_loses = -cost.
    None when there are no contracts or no price."""
    if not contracts or price is None:
        return {"if_wins": None, "if_loses": None}
    cost = contracts * price + fee_fn(contracts, price)
    return {"if_wins": round(contracts - cost, 2), "if_loses": round(-cost, 2)}


# -- scoreboard ------------------------------------------------------------------------------------------------
def _ts(t: dict) -> int:
    return int(t.get("exit_ts") or t.get("window_end") or t.get("created_ts") or 0)


def _result(pnl: float) -> str:
    return "W" if pnl > EPS else "L" if pnl < -EPS else "P"


def record(trades: Iterable[dict]) -> dict:
    """W-L-push over trades with a P&L (closed and filled; settled, stopped and manual exits alike, by P&L sign)."""
    done = sorted((t for t in trades if t.get("pnl") is not None), key=_ts)
    wins = sum(1 for t in done if _result(t["pnl"]) == "W")
    losses = sum(1 for t in done if _result(t["pnl"]) == "L")
    return {"wins": wins, "losses": losses, "pushes": len(done) - wins - losses, "trades": len(done),
            "pnl": round(sum(float(t["pnl"]) for t in done), 2)}


def streak(trades: Sequence[dict]) -> dict:
    """The current run of wins or losses, newest first (pushes end it)."""
    done = sorted((t for t in trades if t.get("pnl") is not None), key=_ts, reverse=True)
    if not done:
        return {"kind": None, "n": 0, "text": "—"}
    kind = _result(done[0]["pnl"])
    if kind == "P":
        return {"kind": None, "n": 0, "text": "—"}
    n = 0
    for t in done:
        if _result(t["pnl"]) != kind:
            break
        n += 1
    return {"kind": kind, "n": n, "text": f"{kind}{n}"}


def followed(t: dict) -> bool | None:
    """Whether the trade took the side the call favoured at entry, from the bracket's stored `fair_up` (the odds the
    ticket was prepared with): True / False, None when unknown or a coin flip."""
    call = lean(t.get("fair_up"))
    if call not in ("UP", "DOWN") or t.get("side") not in ("UP", "DOWN"):
        return None
    return t["side"] == call


def calibration(trades: Sequence[dict]) -> dict:
    """Finished trades split by whether they followed the call at entry: each side's record (W-L, P&L); trades with
    no stored call or a coin-flip call are counted as `unknown`."""
    done = [t for t in trades if t.get("pnl") is not None]
    f = [followed(t) for t in done]
    return {"followed": record(t for t, x in zip(done, f, strict=True) if x is True),
            "against": record(t for t, x in zip(done, f, strict=True) if x is False),
            "unknown": sum(1 for x in f if x is None)}


def attempts(trades: Sequence[dict], last: int | None = None) -> dict:
    """The last `last` finished attempts (None = all), oldest first, with the cumulative P&L for the chart and each
    attempt's details; plus their record, P&L total and streak."""
    done = sorted((t for t in trades if t.get("pnl") is not None), key=_ts)
    if last is not None and last > 0:
        done = done[-last:]
    cum, points = 0.0, []
    for i, t in enumerate(done):
        cum += float(t["pnl"])
        points.append({"i": i + 1, "id": t.get("id"), "ts": _ts(t), "side": t.get("side"),
                       "entry_avg": t.get("entry_avg"), "exit_avg": t.get("exit_avg"), "exit": t.get("exit"),
                       "pnl": round(float(t["pnl"]), 2), "cum": round(cum, 2), "result": _result(t["pnl"]),
                       "venue": t.get("venue"), "ticker": t.get("ticker")})
    return {"n": len(done), "last": last, "record": record(done), "streak": streak(done), "points": points,
            "calibration": calibration(done)}


def scoreboard(trades: Sequence[dict], now: int, *, luck_n: int = LUCK_N, last: int | None = None) -> dict:
    """The gamified scoreboard from TradingService.trades() rows (any order)."""
    done = sorted((t for t in trades if t.get("pnl") is not None), key=_ts)
    today = now - now % DAY_MS
    week = now - 7 * DAY_MS

    def pnl_since(since: int) -> float:
        return round(sum(float(t["pnl"]) for t in done if _ts(t) >= since), 2)

    rec = record(done)
    decided = [t for t in done if _result(t["pnl"]) != "P"]
    prices = [float(t["entry_avg"]) for t in decided if t.get("entry_avg") is not None]
    n = len(decided)
    win_rate = rec["wins"] / n if n else None
    avg_price = sum(prices) / len(prices) if prices else None
    edge = None if win_rate is None or avg_price is None else round(win_rate - avg_price, 4)
    if n == 0:
        verdict = "No finished calls yet."
    else:
        verdict = (f"You won {rec['wins']} of {n} ({win_rate * 100:.0f}%) paying {avg_price * 100:.0f}¢ on average "
                   f"(break-even {avg_price * 100:.0f}%)." if avg_price is not None
                   else f"You won {rec['wins']} of {n} ({win_rate * 100:.0f}%).")
        if n < luck_n:
            verdict += " " + LUCK_NOTE
    out = {"record": rec, "streak": streak(done),
           "pnl": {"today": pnl_since(today), "week": pnl_since(week), "all": rec["pnl"]},
           "open": sum(1 for t in trades if t.get("state") in ("sending", "unknown", "open", "stopping")),
           "settling": sum(1 for t in trades if t.get("state") == "settling"),  # window closed, result pending
           "calls": {"n": n, "win_rate": None if win_rate is None else round(win_rate, 4),
                     "avg_price": None if avg_price is None else round(avg_price, 4), "edge": edge,
                     "mostly_luck": n < luck_n, "luck_n": luck_n, "verdict": verdict, "note": LUCK_NOTE},
           "recent": [{"id": t.get("id"), "side": t.get("side"), "ts": _ts(t), "pnl": round(float(t["pnl"]), 2),
                       "result": _result(t["pnl"]), "exit": t.get("exit"), "entry_avg": t.get("entry_avg")}
                      for t in reversed(done[-20:])],
           "attempts": attempts(done, last)}
    return out
