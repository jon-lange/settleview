"""Kalshi fees and per-contract returns (KTD12).

Verified on the user's real KXBTC15M fills (2026-10-28): the taker fee is exactly
rate x contracts x price x (1 - price) with rate 0.07, ROUNDED UP to 4 decimals per fill (14 of 14 production
fills; e.g. 0.05 contracts at 19c -> $0.0006, not the whole cent), and maker fills paid no fee. Contracts may be
fractional.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal


def kalshi_fee(contracts: float, price: float, rate: float = 0.07) -> float:
    """Taker fee: rate x C x P x (1 - P), rounded up to 4 decimals as Kalshi charges it."""
    p = Decimal(repr(price))
    raw = Decimal(repr(rate)) * Decimal(repr(contracts)) * p * (1 - p)
    return float(raw.quantize(Decimal("0.0001"), rounding=ROUND_CEILING))


def contract_return(won: bool, price: float, rate: float = 0.07) -> float:
    """Return per contract bought at `price` (dollars, 0–1) that pays $1 on a win, after fee."""
    fee = kalshi_fee(1, price, rate)
    return (1.0 - price - fee) if won else (-price - fee)


def fair_price(win_rate: float) -> float:
    """Break-even contract price implied by a win rate (used as the 1m/5m payout assumption)."""
    return min(max(win_rate, 0.01), 0.99)


POLYMARKET_FEE_RATE = 0.0695  # Polymarket US BTC Up/Down `feeCoefficient` (taker; makers pay 0 at our tier)


def polymarket_fee(contracts: float, price: float, rate: float = POLYMARKET_FEE_RATE) -> float:
    """Polymarket US taker fee: rate x C x P x (1 - P), rounded to the NEAREST cent (half up) per order.

    Observed on production fills 2026-10-29: 2 contracts at 22c and 2 at 17c each cost $0.02 (raw $0.0239 and
    $0.0196), which rules out rounding up to the cent. `price` is the price paid for the contract bought or sold
    (P(1 - P) is symmetric, so the YES or NO price gives the same fee). Not yet verified on more fills: the adapter
    records the fee each execution actually reports and uses this only as the estimate before a fill is known."""
    p = Decimal(repr(price))
    raw = Decimal(repr(rate)) * Decimal(repr(contracts)) * p * (1 - p)
    return float(raw.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
