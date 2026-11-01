"""Production penny checklist for the PolymarketTrading gateway (user-approved $1 tests, 2026-10-29).

One contract per step on the current BTC 15-minute window, flat after every step:
  A. Up:   IoC buy at ask+1c -> 99c post-only GTC take profit rests -> cancel -> IoC sale at bid-5c (stop path)
  B. Down: IoC buy at no-ask+1c -> 99c (YES 1c) post-only take profit rests -> cancel -> IoC sale at no-bid-5c
  C. A post-only buy that would cross must be rejected.
Prices come from the WebSocket recorder (never the cached REST BBO). Keys from the live app's .env, never printed.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import sqlite3
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from btc_signal_engine.adapters.polymarket_us.client import sdk_client  # noqa: E402
from btc_signal_engine.adapters.polymarket_us.trading import PolymarketTrading  # noqa: E402
from btc_signal_engine.bootstrap.config import Settings  # noqa: E402

# The .env holding the Polymarket US keys (never printed), named explicitly: never guessed.
if not os.environ.get("BTCFLOW_ENV_FILE"):
    raise SystemExit("set BTCFLOW_ENV_FILE to the .env file that holds the Polymarket US keys")
ENV = Path(os.environ["BTCFLOW_ENV_FILE"])
DB = ROOT / "data" / "research" / "polymarket_us.db"


def ws_quote(slug: str) -> dict | None:
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    r = db.execute("select bid, ask, recv_ms from pm_ws_quotes where slug=? order by recv_ms desc limit 1", (slug,)).fetchone()
    if not r or r[0] is None or r[1] is None or time.time() * 1000 - r[2] > 2000:
        return None
    bid, ask = r[0], r[1]
    return {"ticker": slug, "yes_bid": bid, "yes_ask": ask, "no_bid": round(1 - ask, 2), "no_ask": round(1 - bid, 2)}


def coid() -> str:
    return "pen-" + uuid.uuid4().hex[:20]


async def main() -> None:
    s = Settings(_env_file=ENV)
    c = sdk_client(s.polymarket_key_id, s.polymarket_secret_key)
    now = int(time.time())
    start = now // 900 * 900
    if start + 900 - now < 300:
        raise SystemExit("under 5 minutes left in this window; run in the next one")
    slug = "cpc-btc-updown-15m-" + dt.datetime.fromtimestamp(start, dt.UTC).strftime("%Y-%m-%d-%H%Mz")
    gw = PolymarketTrading(c, quote_fn=ws_quote, max_order_cost=1.5)
    log = []

    async def pos():
        p = await gw.positions()
        return [x for x in p if x.get("ticker") == slug or x.get("slug") == slug] or p

    async def step(name, coro):
        try:
            r = await coro
            log.append((name, "OK", {k: r.get(k) for k in ("status", "filled", "avg_price", "fees", "order_id", "not_sent")} if isinstance(r, dict) else r))
        except Exception as e:  # report and continue to flatten
            log.append((name, "ERR", f"{type(e).__name__}: {e}"[:200]))
        print(log[-1])
        return log[-1]

    q = ws_quote(slug)
    print("market", slug, "quote", q)
    if not q:
        raise SystemExit("no fresh WebSocket quote")
    # A. Up
    await step("A1 Up IoC buy", gw.place_limit(ticker=slug, side="yes", action="buy", count=1, price=min(q["yes_ask"] + 0.01, 0.99), client_order_id=coid()))
    print("  positions:", await pos())
    tp = await step("A2 Up TP 99c post-only GTC", gw.place_limit(ticker=slug, side="yes", action="sell", count=1, price=0.99, client_order_id=coid(), tif="good_till_canceled", post_only=True))
    if tp[1] == "OK" and tp[2].get("order_id"):
        print("  open orders:", [(o.get("order_id"), o.get("status"), o.get("price")) for o in await gw.orders(ticker=slug)])
        await step("A3 cancel TP", gw.cancel(tp[2]["order_id"], slug))
        await step("A3b get_order after cancel", gw.get_order(tp[2]["order_id"]))
    q = ws_quote(slug) or q
    await step("A4 Up IoC sale (stop path)", gw.place_limit(ticker=slug, side="yes", action="sell", count=1, price=max(q["yes_bid"] - 0.05, 0.01), client_order_id=coid()))
    print("  positions:", await pos())
    # B. Down
    q = ws_quote(slug) or q
    await step("B1 Down IoC buy", gw.place_limit(ticker=slug, side="no", action="buy", count=1, price=min(q["no_ask"] + 0.01, 0.99), client_order_id=coid()))
    print("  positions:", await pos())
    tp = await step("B2 Down TP 99c post-only GTC", gw.place_limit(ticker=slug, side="no", action="sell", count=1, price=0.99, client_order_id=coid(), tif="good_till_canceled", post_only=True))
    if tp[1] == "OK" and tp[2].get("order_id"):
        await step("B3 cancel TP", gw.cancel(tp[2]["order_id"], slug))
    q = ws_quote(slug) or q
    await step("B4 Down IoC sale", gw.place_limit(ticker=slug, side="no", action="sell", count=1, price=max(q["no_bid"] - 0.05, 0.01), client_order_id=coid()))
    print("  positions:", await pos())
    # C. post-only that would cross
    q = ws_quote(slug) or q
    r = await step("C post-only crossing buy (expect reject)", gw.place_limit(ticker=slug, side="yes", action="buy", count=1, price=min(q["yes_ask"] + 0.02, 0.99), client_order_id=coid(), tif="good_till_canceled", post_only=True))
    if r[1] == "OK" and r[2].get("order_id") and (r[2].get("filled") or 0) == 0:
        await step("C cleanup cancel", gw.cancel(r[2]["order_id"], slug))
    left = await pos()
    print("FINAL positions:", left)
    print("open orders:", await gw.orders(ticker=slug))
    b = await c.account.balances()
    print("balance:", b["balances"][0].get("currentBalance"))


if __name__ == "__main__":
    asyncio.run(main())
