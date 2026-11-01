"""Kalshi DEMO exchange checklist for the SDK backend (feat/kalshi-sdk). DEMO ONLY: fake money.

Builds the demo trading client exactly as bootstrap/app.py does (`trading_from_settings("demo", ...)`), on the current
KXBTC15M demo market with at least 4 minutes left, 1 contract per order, at most $2 of demo money at risk:

  1. reads: quote, positions, orders, market_fills, balance (no 401s)
  2. order group + far GTC post-only buy with group and ~3 min expiry -> resting, visible in orders()
  3. amend (still far, new coid) -> new coid; decrease skipped (1 contract); cancel; second cancel -> not_found
  4. duplicate client_order_id -> second answer duplicate=True, same order_id; cancel
  5. batch cancel of two far orders, one already cancelled -> per-item not_found for that one
  6. IOC buy 1 at the ask, reduce-only IOC sell at the bid -> fills/fees vs market_fills; flat; group reset
  7. nothing of ours left resting, position flat; transport retries, aiohttp warnings and hosts reported

Safety: refuses to run for anything but the demo host (checked before the first request, and every aiohttp request
is checked against the demo host before it is sent). Credentials come from the app's .env through Settings and are
never printed. Run: .venv/Scripts/python.exe scripts/quick/kalshi_sdk_demo_check.py
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import sys
import time
import uuid
import warnings
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import aiohttp  # noqa: E402

from btc_signal_engine.adapters.kalshi.auth import KalshiAuth, load_private_key  # noqa: E402
from btc_signal_engine.adapters.kalshi.backend import SdkBackend  # noqa: E402
from btc_signal_engine.adapters.kalshi.client import iso_to_ms  # noqa: E402
from btc_signal_engine.adapters.kalshi.portfolio import KalshiPortfolio  # noqa: E402
from btc_signal_engine.adapters.kalshi.trading import BASE_URLS, trading_from_settings  # noqa: E402
from btc_signal_engine.bootstrap.config import Settings  # noqa: E402

ENV_FILE = Path(os.environ.get("BTCFLOW_ENV_FILE") or ROOT / ".env")  # the .env holding the keys (never printed)
DEMO = BASE_URLS["demo"]
DEMO_HOST = urlsplit(DEMO).hostname
MAX_COST = 2.0  # per-order notional cap handed to the gateway (its own guard)
FAR = 0.10  # resting orders sit at least this far below the bid: they cannot fill

hosts: set[str] = set()
log_lines: list[str] = []
loop_errors: list[str] = []
results: list[tuple[str, bool, str]] = []


def coid(tag: str) -> str:
    return f"sdkdemo-{tag}-{uuid.uuid4().hex[:10]}"


def step(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)


def fee_formula(count: float, price: float) -> float:
    return math.ceil(round(0.07 * count * price * (1 - price) * 10000, 6)) / 10000


def demo_only_requests() -> None:
    """Every aiohttp request is checked against the demo host BEFORE it is sent; the host is recorded."""
    original = aiohttp.ClientSession.request

    def guarded(self, method, url, **kw):
        host = urlsplit(str(url)).hostname
        if host != DEMO_HOST:
            raise SystemExit(f"refusing a request to {host!r}: this script talks to the demo exchange only")
        hosts.add(host)
        return original(self, method, url, **kw)

    aiohttp.ClientSession.request = guarded  # type: ignore[method-assign]


class Capture(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        if "transport error" in msg or "Unclosed" in msg or record.levelno >= logging.WARNING:
            log_lines.append(f"{record.levelname} {record.name}: {msg}")


async def current_market(public: SdkBackend, series: str) -> dict:
    data = (await public.get_markets(series_ticker=series, status="open", limit=50)).json()
    now = time.time() * 1000
    live = [m for m in data.get("markets", [])
            if m.get("open_time") and m.get("close_time") and iso_to_ms(m["open_time"]) <= now
            and iso_to_ms(m["close_time"]) - now >= 240_000]
    if not live:
        raise SystemExit("no KXBTC15M demo market with at least 4 minutes left; try again in a few minutes")
    return min(live, key=lambda m: iso_to_ms(m["close_time"]))


async def listed_after(gw, ticker: str, pred, limit_s: float = 10.0) -> tuple[dict | None, float | None]:
    """Poll the account-wide AND the market's order list until `pred(order)` matches (demo lists lag placements).
    Returns (order, seconds until it appeared) or (None, None). The wait only makes this demo check meaningful; the
    lag it measures is demo-only data, not a prod timing."""
    t0 = time.monotonic()
    while True:
        for kw in ({}, {"ticker": ticker}):
            hit = [x for x in await gw.orders(**kw) if pred(x)]
            if hit:
                return hit[0], round(time.monotonic() - t0, 2)
        if time.monotonic() - t0 >= limit_s:
            return None, None
        await asyncio.sleep(0.5)


def far_order(q: dict) -> tuple[str, float]:
    """(side, price) for a buy at least FAR below that side's bid (never fills), price >= 1c."""
    for side in ("yes", "no"):
        bid = q.get(f"{side}_bid")
        if bid is not None and bid - FAR >= 0.01:
            return side, round(max(0.01, math.floor((bid - FAR) * 100) / 100), 2)
    raise SystemExit(f"book too thin for a far resting order: {q}")


async def main() -> None:
    settings = Settings(_env_file=ENV_FILE)
    key_id, key_path = settings.trading_demo_key_id, settings.trading_demo_key_path
    if not key_id or not key_path:
        raise SystemExit("demo trading credentials are not configured (BTCFLOW_TRADING_DEMO_KEY_ID / _PATH): stopping")
    series = settings.kalshi_series

    gw = trading_from_settings("demo", key_id, key_path, max_order_cost=MAX_COST)  # exactly as bootstrap/app.py
    if gw is None:
        raise SystemExit("the demo trading key could not be loaded: stopping")
    assert gw.env == "demo" and gw.base_url == DEMO, "not the demo exchange"
    assert gw._backend.api_client.configuration.host == DEMO, "SDK host is not the demo exchange"
    pf = KalshiPortfolio(KalshiAuth(key_id, load_private_key(key_path)), base_url=DEMO)
    assert pf._backend.api_client.configuration.host == DEMO
    public = SdkBackend(DEMO, signer=None, timeout=10.0)
    del key_id, key_path

    mine: set[str] = set()  # order ids this script placed
    ticker = None
    side_ioc = None
    try:
        m = await current_market(public, series)
        ticker = m["ticker"]
        left = (iso_to_ms(m["close_time"]) - time.time() * 1000) / 1000
        print(f"market {ticker}, {left:.0f} s left", flush=True)

        # 1. reads
        bal0 = await pf.balance()
        q = await gw.quote(ticker)
        pos = await gw.positions()
        ords = await gw.orders(ticker=ticker)
        since = int(time.time() * 1000) - 60_000
        fills0 = await gw.market_fills(ticker, since)
        step("1 reads", True, f"quote yes {q['yes_bid']}/{q['yes_ask']} no {q['no_bid']}/{q['no_ask']} "
                              f"status={q.get('status')}; positions={len(pos)}; orders on ticker={len(ords)}; "
                              f"fills={len(fills0)}; balance=${float(bal0.get('balance', 0)) / 100:.2f}")
        start_ms = int(time.time() * 1000)

        # 2. order group + far resting order with expiry
        group = await gw.create_order_group(contracts_limit=1)
        side, price = far_order(q)
        c1 = coid("g")
        o = await gw.place_limit(ticker=ticker, side=side, action="buy", count=1, price=price, client_order_id=c1,
                                 tif="good_till_canceled", post_only=True, order_group_id=group,
                                 expiration_ts=int(time.time() * 1000) + 180_000)
        mine.add(o["order_id"])
        hit, lag = await listed_after(gw, ticker, lambda x: x["order_id"] == o["order_id"])
        step("2 group + far GTC post-only", o["status"] == "resting" and hit is not None,
             f"group={group} order={o['order_id']} {side}@{price} status={o['status']} "
             f"listed={hit is not None} after {lag} s (demo list lag)")

        # 3. amend, decrease (skipped), cancel twice
        c2 = coid("a")
        price2 = round(max(0.01, price - 0.01), 2)
        a = await gw.amend(o["order_id"], ticker=ticker, side=side, action="buy", price=price2, count=1,
                           client_order_id=c1, updated_client_order_id=c2)
        hit, lag = await listed_after(gw, ticker, lambda x: x["order_id"] == o["order_id"]
                                      and x["client_order_id"] == c2)
        step("3a amend", a.get("client_order_id") == c2 and hit is not None,
             f"answer coid={a.get('client_order_id')} status={a.get('status')} remaining={a.get('remaining')}; "
             f"listed under new coid={hit is not None} after {lag} s (demo list lag) "
             f"price={hit['price'] if hit else None}")
        step("3b decrease", True, "skipped: the order is 1 contract (reduce_to below 1 would be a cancel)")
        cx = await gw.cancel(o["order_id"], ticker)
        cy = await gw.cancel(o["order_id"], ticker)
        step("3c cancel, then cancel again", cx.get("reduced_by") == 1.0 and cy.get("not_found") is True,
             f"first={cx} second={cy}")

        # 4. duplicate client_order_id
        q = await gw.quote(ticker)
        side, price = far_order(q)
        cd = coid("d")
        d1 = await gw.place_limit(ticker=ticker, side=side, action="buy", count=1, price=price, client_order_id=cd,
                                  tif="good_till_canceled", post_only=True)
        mine.add(d1["order_id"])
        _, lag = await listed_after(gw, ticker, lambda x: x["order_id"] == d1["order_id"])
        print(f"   (first duplicate-test order listed after {lag} s)", flush=True)
        try:
            d2 = await gw.place_limit(ticker=ticker, side=side, action="buy", count=1, price=price,
                                      client_order_id=cd, tif="good_till_canceled", post_only=True)
            mine.add(d2["order_id"])
            step("4 duplicate client_order_id", d2.get("duplicate") is True and d2["order_id"] == d1["order_id"],
                 f"first={d1['order_id']} second={d2['order_id']} duplicate={d2.get('duplicate')}")
        except Exception as exc:  # OrderUnknown (not definitive) if the lists still lag: reported, not hidden
            step("4 duplicate client_order_id", False,
                 f"{type(exc).__name__} definitive={getattr(exc, 'definitive', None)}: {str(exc)[:160]}")
        await gw.cancel(d1["order_id"], ticker)

        # 5. batch cancel, one already gone
        b1 = await gw.place_limit(ticker=ticker, side=side, action="buy", count=1, price=price,
                                  client_order_id=coid("b1"), tif="good_till_canceled", post_only=True)
        b2 = await gw.place_limit(ticker=ticker, side=side, action="buy", count=1, price=price,
                                  client_order_id=coid("b2"), tif="good_till_canceled", post_only=True)
        mine.update({b1["order_id"], b2["order_id"]})
        await gw.cancel(b1["order_id"], ticker)
        rows = {r["order_id"]: r for r in await gw.batch_cancel([b1["order_id"], b2["order_id"]], ticker=ticker)}
        r1, r2 = rows.get(b1["order_id"], {}), rows.get(b2["order_id"], {})
        step("5 batch cancel", r1.get("not_found") is True and r2.get("reduced_by") == 1.0 and not r2.get("not_found"),
             f"gone={r1} live={r2}")

        # 6. IOC round trip
        q = await gw.quote(ticker)
        side_ioc = "yes" if (q["yes_ask"] or 1) <= (q["no_ask"] or 1) else "no"
        ask = q[f"{side_ioc}_ask"]
        if ask is None or ask > 0.95:
            raise SystemExit(f"no usable ask for an IOC buy: {q}")
        buy = await gw.place_limit(ticker=ticker, side=side_ioc, action="buy", count=1, price=ask,
                                   client_order_id=coid("ib"))
        mine.add(buy["order_id"])
        sells = []
        if buy["filled"] > 0:
            for extra in (0.0, 0.02, 0.05):  # the bid can move between the two orders
                q = await gw.quote(ticker)
                bid = q[f"{side_ioc}_bid"]
                if bid is None:
                    continue
                s = await gw.place_limit(ticker=ticker, side=side_ioc, action="sell", count=buy["filled"],
                                         price=round(max(0.01, bid - extra), 2), client_order_id=coid("is"),
                                         reduce_only=True)
                mine.add(s["order_id"])
                sells.append(s)
                if s["filled"] >= buy["filled"]:
                    break
        fills: list[dict] = []
        for _ in range(6):  # the fills list can lag the order answer by a moment
            fills = [f for f in await gw.market_fills(ticker, start_ms) if f["order_id"] in mine]
            if len(fills) >= 1 + bool(sells):
                break
            await asyncio.sleep(1.0)
        checks = []
        for f in fills:
            px = f["yes_price"] if side_ioc == "yes" else round(1 - f["yes_price"], 4)
            checks.append(f"fill {f['order_id']} n={f['count']} {side_ioc}@{px} fee={f['fee']} "
                          f"formula={fee_formula(f['count'], px)}")
        fees_ok = all(f["fee"] is not None and abs(f["fee"] - fee_formula(
            f["count"], f["yes_price"] if side_ioc == "yes" else round(1 - f["yes_price"], 4))) < 1e-9 for f in fills)
        flat = not [p for p in await gw.positions() if p["ticker"] == ticker]
        sell_line = "; ".join(f"sell {s['status']} filled={s['filled']} avg={s['avg_price']} fees={s['fees']}"
                              for s in sells)
        step("6 IOC buy at ask, reduce-only IOC sell at bid",
             buy["filled"] == 1 and bool(sells) and sells[-1]["filled"] == 1 and flat and fees_ok,
             f"buy {buy['status']} filled={buy['filled']} avg={buy['avg_price']} fees={buy['fees']}; {sell_line}; "
             f"{' | '.join(checks)}; flat={flat}")
        await gw.reset_order_group(group)
        step("6b reset order group", True, group)
    finally:
        if ticker is not None:
            left_over = [x for x in await gw.orders(ticker=ticker, status="resting") if x["order_id"] in mine]
            for x in left_over:
                await gw.cancel(x["order_id"], ticker)
            still = [p for p in await gw.positions() if p["ticker"] == ticker]
            if still and side_ioc is not None:  # flatten a leftover position (demo money), one reduce-only IOC
                q = await gw.quote(ticker)
                bid = q[f"{side_ioc}_bid"]
                if bid is not None:
                    await gw.place_limit(ticker=ticker, side=side_ioc, action="sell", count=abs(still[0]["position"]),
                                         price=round(max(0.01, bid - 0.05), 2), client_order_id=coid("fl"),
                                         reduce_only=True)
                still = [p for p in await gw.positions() if p["ticker"] == ticker]
            resting = [x for x in await gw.orders(ticker=ticker, status="resting") if x["order_id"] in mine]
            bal1 = await pf.balance()
            step("7 clean up", not resting and not still,
                 f"resting of ours={len(resting)} (cancelled {len(left_over)} in cleanup); position={still}; "
                 f"balance delta=${(float(bal1.get('balance', 0)) - float(bal0.get('balance', 0))) / 100:+.2f}")
        for c in (gw, pf, public):
            await c.aclose()


def run() -> int:
    if len(sys.argv) > 1:
        raise SystemExit("this script takes no arguments: it only ever talks to the demo exchange")
    demo_only_requests()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger().addHandler(Capture())
    logging.captureWarnings(True)
    warnings.simplefilter("always", ResourceWarning)
    loop = asyncio.new_event_loop()
    loop.set_exception_handler(lambda _l, ctx: loop_errors.append(str(ctx.get("message"))))
    try:
        loop.run_until_complete(main())
    finally:
        loop.run_until_complete(asyncio.sleep(0.25))  # let aiohttp finish closing its connectors
        loop.close()
    print("\nhosts contacted:", sorted(hosts))
    print("transport retries / warnings:", [x for x in log_lines if "kalshi_python_async" not in x] or "none")
    print("loop errors (e.g. Unclosed client session):", loop_errors or "none")
    print("summary:", f"{sum(ok for _, ok, _ in results)}/{len(results)} passed")
    return 0 if all(ok for _, ok, _ in results) else 1


if __name__ == "__main__":
    sys.exit(run())
