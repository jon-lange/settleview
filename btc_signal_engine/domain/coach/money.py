"""Dollar sizing: how many contracts a stake buys (used by the trading service and the Play plan).

A stake of S dollars buys the largest number of contracts C (fractional, to the hundredth, as Kalshi
allows) whose cost C*P plus the Kalshi taker fee on the buy fits inside S. Selling pays the taker fee
again; a contract held to settlement pays $1 or $0 with no fee (domain/settlement/fees.py).
"""

from __future__ import annotations

import math

from btc_signal_engine.domain.settlement.fees import kalshi_fee

EPS = 1e-9


def position_for_stake(stake: float, price: float, fee_rate: float = 0.07) -> float:
    """Contracts (2 dp) such that contracts*price + buy fee <= stake. 0 when nothing can be bought."""
    if stake is None or price is None or stake <= 0 or not 0.0 < price < 1.0:
        return 0.0
    c = math.floor(stake / (price * (1 + fee_rate * (1 - price))) * 100 + EPS) / 100
    while c > 0 and c * price + kalshi_fee(c, price, fee_rate) > stake + EPS:
        c = round(c - 0.01, 2)
    return round(max(c, 0.0), 2)
