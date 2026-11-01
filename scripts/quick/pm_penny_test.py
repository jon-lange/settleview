"""Polymarket US production penny test (user-approved 2026-10-29): buy 1-2 YES contracts on the current BTC 15-minute
Up/Down window at the ask (limit, IOC), then sell them back at the bid (limit, IOC), and report balances, order
results and positions before/after. Hard cap $1.50 per order. Keys from the live app's .env, never printed.

    python scripts/dev.py script scripts/quick/pm_penny_test.py
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from btc_signal_engine.bootstrap.config import Settings  # noqa: E402
from polymarket_us import PolymarketUS  # noqa: E402

# The .env holding the Polymarket US keys (never printed), named explicitly: never guessed.
if not os.environ.get("BTCFLOW_ENV_FILE"):
    raise SystemExit("set BTCFLOW_ENV_FILE to the .env file that holds the Polymarket US keys")
ENV = Path(os.environ["BTCFLOW_ENV_FILE"])
MAX_COST = 1.50


def px(x):
    return None if x is None else float(x.get("value") if isinstance(x, dict) else x)


def live_bbo(slug: str) -> tuple[float | None, float | None, float]:
    """Latest bid/ask from the WebSocket recorder (the REST BBO is a cached snapshot) and its age in seconds."""
    import sqlite3
    db = sqlite3.connect(f"file:{ROOT / 'data' / 'research' / 'polymarket_us.db'}?mode=ro", uri=True)
    r = db.execute("select bid, ask, recv_ms from pm_ws_quotes where slug=? order by recv_ms desc limit 1", (slug,)).fetchone()
    if not r:
        return None, None, 1e9
    return r[0], r[1], time.time() - r[2] / 1000


def show(label, obj):
    print(label, json.dumps(obj, default=str)[:900])


def main() -> None:
    s = Settings(_env_file=ENV)
    c = PolymarketUS(key_id=s.polymarket_key_id, secret_key=s.polymarket_secret_key.get_secret_value())
    now = int(time.time())
    start = now // 900 * 900
    if start + 900 - now < 180:
        raise SystemExit("less than 3 minutes left in this window; run again in the next one")
    slug = "cpc-btc-updown-15m-" + dt.datetime.fromtimestamp(start, dt.UTC).strftime("%Y-%m-%d-%H%Mz")
    bal0 = c.account.balances()["balances"][0]
    print("market", slug, "| balance before", bal0.get("currentBalance"), "buying power", bal0.get("buyingPower"))
    bid, ask, age = live_bbo(slug)
    print("live bbo bid", bid, "ask", ask, f"age {age:.1f}s")
    if age > 2.0:
        raise SystemExit("live quote older than 2 s; is the WebSocket recorder running?")
    if ask is None or bid is None or not (0.05 <= ask <= 0.95):
        raise SystemExit("no usable two-sided quote; try again")
    qty = 2 if ask * 2 <= MAX_COST else 1
    buy = c.orders.create({"marketSlug": slug, "intent": "ORDER_INTENT_BUY_LONG", "type": "ORDER_TYPE_LIMIT",
                           "price": {"value": f"{min(ask + 0.01, 0.99):.2f}", "currency": "USD"}, "quantity": qty,
                           "tif": "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL", "synchronousExecution": True})
    show("BUY result:", buy)
    print("BUY executions:", [(e.get("type"), (e.get("order") or {}).get("state"), (e.get("order") or {}).get("cumQuantity"), e.get("lastPx"), e.get("lastShares")) for e in buy.get("executions", [])])
    time.sleep(2)
    pos = c.portfolio.positions()
    show("positions after buy:", pos)
    held = 0.0
    # positions come back as a dict keyed by market slug (penny test 2026-10-29 found this the hard way)
    held = float(((pos.get("positions") or {}).get(slug) or {}).get("netPosition") or 0)
    if held <= 0:
        print("nothing held (IOC may not have filled); no sell needed")
    else:
        bid, _, age = live_bbo(slug)
        print("live bid for the sale", bid, f"age {age:.1f}s")
        sell = c.orders.create({"marketSlug": slug, "intent": "ORDER_INTENT_SELL_LONG", "type": "ORDER_TYPE_LIMIT",
                                "price": {"value": f"{max(bid - 0.01, 0.01):.2f}", "currency": "USD"}, "quantity": held,
                                "tif": "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL", "synchronousExecution": True})
        show("SELL result:", sell)
        print("SELL executions:", [(e.get("type"), (e.get("order") or {}).get("state"), (e.get("order") or {}).get("cumQuantity"), e.get("lastPx"), e.get("lastShares")) for e in sell.get("executions", [])])
        time.sleep(2)
        show("positions after sell:", c.portfolio.positions())
    show("open orders:", c.orders.list() if hasattr(c.orders, "list") else "n/a")
    bal1 = c.account.balances()["balances"][0]
    print("balance after", bal1.get("currentBalance"), "buying power", bal1.get("buyingPower"),
          "| change", round(float(bal1.get("currentBalance")) - float(bal0.get("currentBalance")), 4))


if __name__ == "__main__":
    main()
