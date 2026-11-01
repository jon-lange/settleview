"""Perps: the numbers behind the Perps tab for Kalshi's BTC perpetual (KXBTCPERP). Pure.

Display and ticket maths only: nothing here sends an order. Inputs are Kalshi's own figures (market data, risk
parameters, fills); every function raises PerpRefused with a plain-English reason on an input it cannot use, never a
default. Sides are "long" / "short". Prices are dollars per contract (one contract = 0.0001 BTC, tick $0.0001, about
$8 at $80k BTC); counts are contracts (whole contracts for BTC: `fractional_trading_enabled` false, LEARNINGS).

Facts from Kalshi's spec (perps_openapi.yaml, 2026-10-30):
- `leverage_estimates` / `long_leverage_estimates` / `short_leverage_estimates` are "1 / margin_rate" keyed by
  notional dollars ("1000", "10000", ...); leverage falls at larger notionals.
- The initial margin requirement is the maintenance margin x `initial_margin_multiplier` (1.3 for BTC).
- A position is liquidated when its margin ratio (maintenance margin / equity) reaches
  `liquidation_margin_ratio_threshold` (theta: production 1.0, demo 0.97).
- Fees: "multiply notional by this value" (taker 0.0012 on demo BTC). Kalshi does not document fee rounding, so
  fee figures here are estimates.
- `funding_cap` is expressed in `funding_limit_unit`; for "percentage" the numbers are fractions (0.02 = 2% per
  interval, LEARNINGS).

Calibrated on production (real-money check 2026-10-30, LEARNINGS: 1 contract long at 7.7970 on a small test equity;
Kalshi's /margin/risk said maintenance 11.22% of notional, position leverage 8.909):
- Kalshi's leverage estimate is 1 / INITIAL margin rate (6.8473 x 1.3 = 8.90). So the largest leverage an entry can
  open at is the side's estimate at the smallest notional key at or above the order's notional (above the largest
  key: refused), and the maintenance rate is mmr = 1 / (estimate x initial_margin_multiplier).
- Liquidation estimate (KTD7): equity = C + q(P - E) long, C + q(E - P) short, with C the collateral (the perps
  equity backing the position) less fees already paid; maintenance = mmr x q x P at the entry notional q x E.
  Solving maintenance / equity = theta for P (within 0.01% of Kalshi's liquidation price on the real position):
      long  P = (qE - C) / (q (1 - mmr/theta))
      short P = (C + qE) / (q (1 + mmr/theta))
  None when no positive price solves it. Replaced by Kalshi's `/margin/risk` figure once the position exists.
- Funding: amount = rate x |q| x mark per contract; a long pays a positive rate, a short receives it.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

SIDES = ("long", "short")
PRICE_DP = Decimal("0.0001")  # Kalshi's FixedPointDollars strings carry 4 decimals


class PerpRefused(ValueError):
    """A perp calculation that cannot be done from these inputs; the message is the plain-English reason."""


def _side(side: str) -> str:
    if side not in SIDES:
        raise PerpRefused(f"unknown side {side!r} (long or short)")
    return side


def _positive(**values: float) -> None:
    for name, v in values.items():
        if v is None or not math.isfinite(v) or v <= 0:
            raise PerpRefused(f"{name} must be positive (got {v!r})")


def _dec(x: float | Decimal) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


def max_leverage(side_estimates: Mapping[str, float] | None, notional: float) -> float:
    """The largest leverage an entry of `notional` dollars can open at: Kalshi's side estimate (module docstring)."""
    if not side_estimates:
        raise PerpRefused("Kalshi gave no leverage estimates for this market")
    _positive(notional=notional)
    keys = sorted((float(k), v) for k, v in side_estimates.items())
    for key, est in keys:
        if notional <= key:
            _positive(leverage_estimate=est)
            return est
    raise PerpRefused(f"a ${notional:,.2f} position is larger than Kalshi's largest leverage estimate "
                      f"(${keys[-1][0]:,.0f})")


@dataclass(frozen=True)
class Sizing:
    contracts: float
    notional: float  # contracts x price
    max_leverage: float


def size_position(collateral: float, leverage: float, price: float, count_step: float,
                  side_estimates: Mapping[str, float] | None) -> Sizing:
    """Contracts = floor(collateral x leverage / price / count_step) x count_step, refused above Kalshi's maximum
    leverage at the resulting notional or at zero contracts."""
    _positive(collateral=collateral, leverage=leverage, price=price, count_step=count_step)
    step = _dec(count_step)
    steps = (_dec(collateral) * _dec(leverage) / _dec(price) / step).to_integral_value(ROUND_FLOOR)
    contracts = float(steps * step)
    if contracts <= 0:
        raise PerpRefused(f"${collateral:,.2f} at {leverage:.2f}x buys zero contracts at ${price:,.4f} each")
    notional = float(steps * step * _dec(price))
    cap = max_leverage(side_estimates, notional)
    if leverage > cap:
        raise PerpRefused(f"{leverage:.2f}x is above Kalshi's maximum of {cap:.2f}x for a ${notional:,.2f} position")
    return Sizing(contracts, notional, cap)


def protective_price(side: str, touch: float, band: float, tick: float) -> Decimal:
    """KTD4: a long's cap = best ask x (1 + band) rounded DOWN onto the tick; a short's floor = best bid x (1 - band)
    rounded UP. Exact decimal arithmetic; the result is an exact multiple of `tick`."""
    _side(side)
    _positive(touch=touch, tick=tick)
    if band is None or not math.isfinite(band) or not 0 <= band < 1:
        raise PerpRefused(f"protective band must be a fraction from 0 to under 1 (got {band!r})")
    t = _dec(tick)
    if side == "long":
        raw, rounding = _dec(touch) * (1 + _dec(band)), ROUND_FLOOR
    else:
        raw, rounding = _dec(touch) * (1 - _dec(band)), ROUND_CEILING
    price = (raw / t).to_integral_value(rounding) * t
    if price <= 0:
        raise PerpRefused(f"protective price {price} is not positive")
    return price


def price_str(price: Decimal | float, tick: float) -> str:
    """Kalshi's 4-decimal price string; refused if the price is not on the tick."""
    p, t = _dec(price), _dec(tick)
    if p % t != 0:
        raise PerpRefused(f"price {p} is not on the ${t} tick")
    return str(p.quantize(PRICE_DP))


def fee_estimate(notional: float, fee_rate: float) -> float:
    """Kalshi's fee = notional x rate (rounding undocumented: an estimate)."""
    return notional * fee_rate


@dataclass(frozen=True)
class WorstCase:
    notional: float  # count x protective price: the position size, shown separately
    entry_fee: float
    exit_fee: float
    total: float  # the most the trade can lose (R8): the collateral at risk + entry and exit fees


def worst_case(committed: float, count: float, protective: Decimal | float, taker_rate: float) -> WorstCase:
    """R8: the collateral at risk plus entry and exit fees, both at the protective price x size x taker rate.
    Trading from the primary perps account (KD4 as changed 2026-10-30), the collateral at risk is the whole perps
    balance: cross margin lets the position draw on all of it, whatever collateral the slider sized it from."""
    _positive(committed=committed, count=count, protective=float(protective))
    notional = float(_dec(count) * _dec(protective))
    fee = fee_estimate(notional, taker_rate)
    return WorstCase(notional, fee, fee, committed + 2 * fee)


def liquidation_estimate(side: str, collateral: float, count: float, entry: float,
                         side_estimates: Mapping[str, float] | None, initial_margin_multiplier: float, theta: float,
                         *, fees_paid: float) -> float | None:
    """Estimated liquidation price (KTD7, formula and status in the module docstring); None when none exists."""
    _side(side)
    _positive(collateral=collateral, count=count, entry=entry, theta=theta,
              initial_margin_multiplier=initial_margin_multiplier)
    if fees_paid is None or not math.isfinite(fees_paid) or fees_paid < 0:
        raise PerpRefused(f"fees paid must be zero or more (got {fees_paid!r})")
    mmr = 1.0 / (max_leverage(side_estimates, count * entry) * initial_margin_multiplier)
    c, k = collateral - fees_paid, mmr / theta
    if side == "long":
        if k >= 1:
            return None
        p = (count * entry - c) / (count * (1 - k))
    else:
        p = (c + count * entry) / (count * (1 + k))
    return p if p > 0 else None


@dataclass(frozen=True)
class Add:
    contracts: float
    average_entry: float
    liquidation: float | None


def add_to_position(side: str, held: float, held_entry: float, added: float, added_entry: float,
                    total_collateral: float, side_estimates: Mapping[str, float] | None,
                    initial_margin_multiplier: float, theta: float, *, fees_paid: float) -> Add:
    """R7: new size, new average entry (q1 E1 + q2 E2) / (q1 + q2) and the new liquidation estimate on the total
    committed collateral."""
    _positive(held=held, held_entry=held_entry, added=added, added_entry=added_entry)
    q = held + added
    avg = (held * held_entry + added * added_entry) / q
    return Add(q, avg, liquidation_estimate(side, total_collateral, q, avg, side_estimates, initial_margin_multiplier,
                                            theta, fees_paid=fees_paid))


def funding_estimate(side: str, rate: float, count: float, mark: float) -> float:
    """One funding payment, signed to the user (negative = you pay): rate x |q| x mark; a long pays a positive rate,
    a short receives it."""
    _side(side)
    amount = rate * abs(count) * mark
    return -amount if side == "long" else amount


def funding_cap_worst_case(cap: float, unit: str, count: float, mark: float) -> float:
    """The most one funding interval can cost (signed to the user, so <= 0) from Kalshi's `funding_cap`. Only the
    "percentage" unit (fractions: 0.02 = 2%) is interpreted; "dollar" and anything else are refused."""
    if unit != "percentage":
        raise PerpRefused(f"funding_limit_unit {unit!r} is not one this app can interpret (only 'percentage')")
    if cap is None or not math.isfinite(cap) or cap < 0:
        raise PerpRefused(f"funding cap must be zero or more (got {cap!r})")
    return -(cap * abs(count) * mark)


def pnl_total(realized: float, unrealized: float, funding: float, fees: float) -> float:
    """Kalshi's figures: realized + unrealized + funding (signed to the user) - fees (paid, positive)."""
    return realized + unrealized + funding - fees


def liquidation_distance(side: str, mark: float, liquidation: float) -> float:
    """How far the mark can move against the position before the liquidation price, as a fraction of the mark
    (negative: already past it)."""
    _side(side)
    _positive(mark=mark, liquidation=liquidation)
    return (mark - liquidation) / mark if side == "long" else (liquidation - mark) / mark


def liquidation_band(distance: float, *, amber: float, red: float) -> str:
    """"red" under `red`, "amber" under `amber`, else "ok" (thresholds are the caller's settings)."""
    if distance < red:
        return "red"
    return "amber" if distance < amber else "ok"
