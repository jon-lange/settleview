"""The stop monitor's I/O and the threads around it (review fix plan 2026-10-31 P1, B1#6 / B1#7):

- `tick` reads the REST quotes its brackets need BEFORE it takes the monitor lock: an approve (or exit, or another
  bracket's stop) is never held behind a slow quote, and a stop still fires on the quote read for it;
- the sync routes' threads read the watched brackets from a snapshot (never the live dict);
- reconciliation and the external-fill check look up only the order ids they judge, never every order ever placed.

Exchange = httpx.MockTransport around the in-memory simulated book (tests/trading/helpers.py); no network."""

from __future__ import annotations

import asyncio
import threading

from tests.trading.helpers import NOW, Exchange, Market
from tests.trading.test_bracket import opened, run
from tests.trading.test_strategy_scope import _resting, svc_for, up


class SlowQuote:
    """Wraps the gateway's quote: each read waits until `release` is set (a slow REST order book)."""

    def __init__(self, gw) -> None:
        self.gw, self.real = gw, gw.quote
        self.started, self.release, self.calls = asyncio.Event(), asyncio.Event(), 0
        gw.quote = self

    async def __call__(self, ticker):
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return await self.real(ticker)


def test_the_monitor_reads_a_slow_rest_quote_without_holding_the_lock_and_still_stops(store):
    async def go():
        m = Market()
        svc, ex, prep, r = await opened(store, m)
        assert r["ok"] and r["bracket"]["state"] == "open"
        assert not svc.stream_ok("prod")  # no stream: every tick reads the REST quote
        slow = SlowQuote(svc.gateways["prod"])
        m.set(0.43, 0.50)  # the bid at the 43c stop
        tick = asyncio.ensure_future(svc.tick(NOW + 3000))
        await asyncio.wait_for(slow.started.wait(), 5)
        assert not svc._lock.locked()  # the quote is in flight and the lock is free: an approve would go through
        slow.release.set()
        await asyncio.wait_for(tick, 5)
        assert slow.calls == 1  # one read for the market, used under the lock (no second read)
        b = store.select("trading_brackets")[0]
        assert b["state"] == "closed" and b["exit_reason"] == "stop" and b["stop_trigger_bid"] == 0.43
    run(go())


def test_a_failed_prefetched_quote_means_no_bid_this_tick_as_before(store):
    async def go():
        m = Market()
        svc, ex, prep, r = await opened(store, m)
        gw = svc.gateways["prod"]

        async def down(ticker):
            raise RuntimeError("order book read failed")
        real, gw.quote = gw.quote, down
        m.set(0.43, 0.50)
        await svc.tick(NOW + 3000)
        assert store.select("trading_brackets")[0]["state"] == "open"  # no quote, no stop, nothing sent
        gw.quote = real
        await svc.tick(NOW + 4000)
        assert store.select("trading_brackets")[0]["exit_reason"] == "stop"
    run(go())


def test_sync_route_reads_iterate_a_snapshot_while_the_loop_changes_the_brackets(store):
    async def go():
        svc, ex, prep, r = await opened(store, Market())
        b = dict(next(iter(svc._brackets.values())))
        stop, errors = threading.Event(), []

        def reader():  # what /api/trading/status and /api/play/now do from a worker thread
            while not stop.is_set():
                try:
                    svc.live_brackets(), svc.settling_brackets(), svc.exposure("prod", NOW), svc.trades(0, NOW)
                except RuntimeError as e:  # "dictionary changed size during iteration"
                    errors.append(e)
        t = threading.Thread(target=reader)
        t.start()
        try:
            for i in range(20_000):  # the loop adding and dropping watched brackets
                svc._brackets[f"x{i}"] = {**b, "id": f"x{i}"}
                if i % 2:
                    svc._brackets.pop(f"x{i - 1}", None)
                if i % 500 == 0:
                    await asyncio.sleep(0)
        finally:
            stop.set()
            t.join()
        assert errors == []
        assert isinstance(svc._watched(), list) and svc._watched() is not svc._watched()
    run(go())


class Selects:
    """The store's selects of one table, with their filters."""

    def __init__(self, store, table: str) -> None:
        self.seen: list[list] = []
        real = store.select

        def select(t, where=None, **kw):
            if t == table:
                self.seen.append(list(kw.get("filters") or []))
            return real(t, where, **kw)
        store.select = select


def test_reconciliation_looks_up_only_the_resting_orders_never_every_order(store):
    async def go():
        m = Market()
        svc, ex, prep, r = await opened(store, m)  # its take profit rests
        history = [{"bracket_id": f"old{i}", "role": "entry", "ticker": "T", "side": "yes", "action": "buy",
                    "count": 1.0, "price": 0.5, "tif": "ioc", "client_order_id": f"c-old{i}", "order_id": f"x-old{i}",
                    "status": "executed", "created_ts": 1, "updated_ts": 1, "strategy": "you"} for i in range(500)]
        store.insert("trading_orders", history)
        orders = Selects(store, "trading_orders")
        brackets = Selects(store, "trading_brackets")
        out = await svc.reconcile("prod", NOW + 2000)
        assert out["ok"] and out["external"] == {"positions": [], "orders": []}  # our TP and position are ours
        assert all(op != "is_null" for f in orders.seen for _, op, _ in f)  # no scan of every order id
        looked = [v for f in orders.seen for col, op, v in f if col == "order_id" and op == "in"]
        assert looked and all(len(v) == 1 for v in looked)  # the one resting take profit
        assert any(("ticker", "in", [r["bracket"]["ticker"]]) in f for f in brackets.seen)
    run(go())


def test_another_strategys_resting_order_is_asked_about_by_its_ids_only(store):
    async def go():
        m = Market()
        ex = Exchange(m)
        asked = []
        oid = await _resting(ex, "other-1")

        def known(ids):
            asked.append(set(ids))
            return {"other-1"} & ids
        svc = await up(svc_for(store, ex, "you", known_order_ids_fn=known))
        assert svc.status(NOW)["external"]["orders"] == []
        assert asked and asked[-1] == {"other-1", oid}
    run(go())
