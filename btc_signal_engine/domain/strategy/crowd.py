"""Crowd flow (pure, descriptive only): what Kalshi traders are actually doing in the current window.

From the public trade prints (each with its YES price, contracts and taker side: `bid` = the taker bought YES/Up,
`ask` = the taker bought NO/Down) over the last `window_ms`: dollars taken on each side, the net, large orders, the
contract's price move and, when known, BTC's move over the same span. A "panic" label marks a price move of
`panic_cents` or more with BTC nearly flat. Research (docs/research/order-flow.md, O1-O3: KILLED) found none of these
predicts the next minutes or settlement, so the summary says what happened and never what to do.
"""

from __future__ import annotations

from collections.abc import Sequence

WHALE_USD = 800.0  # about the top 1% of taker orders over 30 days of KXBTC15M tapes (order-flow study)


def crowd_summary(prints: Sequence[dict], now: int, *, window_ms: int = 60_000, btc_move: float | None = None,
                  whale_usd: float = WHALE_USD, panic_cents: float = 8.0, flat_btc: float = 15.0) -> dict:
    recent = sorted((p for p in prints if p.get("ts_ms", 0) > now - window_ms), key=lambda p: p["ts_ms"])
    up = down = 0.0
    whales = []
    for p in recent:
        y, n = float(p["yes_price"]), float(p["count"])
        bought_up = p.get("taker_book_side") == "bid"
        usd = n * (y if bought_up else 1.0 - y)
        if bought_up:
            up += usd
        else:
            down += usd
        if usd >= whale_usd:
            whales.append({"ts_ms": p["ts_ms"], "side": "UP" if bought_up else "DOWN", "usd": round(usd, 2),
                           "price": round(y if bought_up else 1.0 - y, 2)})
    move = round((float(recent[-1]["yes_price"]) - float(recent[0]["yes_price"])) * 100, 1) if len(recent) >= 2 else None
    total = up + down
    lean = None if total < 1 else round((up - down) / total, 3)
    label = "quiet"
    if total >= 1:
        side = "Up" if up >= down else "Down"
        label = f"more buying {side}" if abs(lean) >= 0.2 else "balanced"
    if move is not None and abs(move) >= panic_cents and (btc_move is None or abs(btc_move) < flat_btc):
        label = (f"price {'up' if move > 0 else 'down'} {abs(move):.0f}¢ with BTC "
                 + ("nearly flat" if btc_move is not None else "move unknown") + " (crowd-driven)")
    return {"window_s": window_ms // 1000, "trades": len(recent), "up_usd": round(up, 2), "down_usd": round(down, 2),
            "net_usd": round(up - down, 2), "lean": lean, "whales": whales[-5:], "price_move_c": move,
            "btc_move": None if btc_move is None else round(btc_move, 2), "label": label,
            "note": "Descriptive only: in 30 days of Kalshi tapes, crowd flow, panics and whale orders did not predict "
                    "the next minutes or the result."}
