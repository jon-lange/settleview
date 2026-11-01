"""Restart and reconciliation (KTD8, R18): a 'sending' row with a fill on Kalshi resolves to Open; an open row with a
flat exchange closes with the reason from fills; a stream resync reconciles over REST; REST positions lagging our own
fill never mark the position diverged or closed."""

from __future__ import annotations

import asyncio
from decimal import Decimal

from tests.perps.helpers import NOW, FakeMargin, open_long, ready, service


def run(coro):
    return asyncio.run(coro)


def _rows(store, fx, *, state, size, order_id=None, outcome="sending", filled=0.0):
    store.insert("perp_positions", [{
        "id": "p1", "env": fx.env, "ticker": fx.ticker, "subaccount": 0, "direction": "long", "size": size,
        "avg_entry": 7.7971 if size else None, "committed_collateral": 10.0, "leverage_chosen": 3.0, "state": state,
        "close_reason": None, "realized_pnl": 0.0, "fees": 0.0281 if size else 0.0, "funding_total": 0.0,
        "opened_ms": NOW - 61_000, "closed_ms": None, "updated_ms": NOW - 61_000}], ignore_conflicts=False)
    store.insert("perp_orders", [{
        "id": "o1", "env": fx.env, "position_id": "p1", "client_order_id": "svperp-o1", "order_id": order_id,
        "role": "entry", "side": "bid", "price": 7.836, "count": 3.0, "time_in_force": "immediate_or_cancel",
        "reduce_only": False, "filled_count": filled, "avg_fill_price": 7.7971 if filled else None,
        "fees": 0.0281 if filled else None, "outcome": outcome, "reason": None, "created_ms": NOW - 61_000,
        "updated_ms": NOW - 61_000}], ignore_conflicts=False)


async def _place(fx, coid="svperp-o1"):
    """The entry reached Kalshi before the crash: 3 filled at the ask."""
    res = await fx.create_order(ticker=fx.ticker, side="bid", count=Decimal(3), price=Decimal("7.8360"),
                                client_order_id=coid, time_in_force="immediate_or_cancel", reduce_only=False,
                                purpose="entry")
    fx.calls.clear()
    return res


def test_restart_with_a_sending_row_and_a_fill_on_kalshi_resolves_to_open(store):
    async def go():
        fx = FakeMargin()
        _rows(store, fx, state="sending", size=0.0)
        await _place(fx)
        svc = await ready(service(store, fx))
        v = svc.view(NOW)
        assert v["unknown_order"] is False
        assert (v["position"]["size"], v["position"]["avg_entry"], v["position"]["state"]) == (3.0, 7.7971, "open")
        o = store.select("perp_orders", {"client_order_id": "svperp-o1"})[0]
        assert (o["outcome"], o["filled_count"], o["fees"]) == ("filled", 3.0, 0.0281)
        assert fx.writes() == []
    run(go())


def test_restart_with_an_unlisted_sending_row_closes_as_never_reached_after_a_minute(store):
    async def go():
        fx = FakeMargin()
        _rows(store, fx, state="sending", size=0.0)
        svc = await ready(service(store, fx))  # 61 s old: given up
        assert svc.view(NOW)["position"] is None
        assert svc.view(NOW)["outcome"]["text"] == "Your order never reached Kalshi; nothing is open."
    run(go())


def test_restart_with_an_open_row_and_a_flat_exchange_closes_with_the_reason_from_fills(store):
    async def go():
        fx = FakeMargin()
        res = await _place(fx)
        _rows(store, fx, state="protected", size=3.0, order_id=res["order_id"], outcome="filled", filled=3.0)
        store.insert("perp_triggers", [{"id": "t1", "env": fx.env, "position_id": "p1",
                                        "exchange_trigger_id": "trg-x", "stop_loss_price": 7.5,
                                        "take_profit_price": 8.031, "state": "active",
                                        "exchange_updated_time": "1", "fired_leg": None, "fired_order_id": None,
                                        "created_ms": NOW - 61_000, "updated_ms": NOW - 61_000}])
        fx.system_fill("etr:trg-x:0", 3, "8.0310", reason="take_profit_stop_loss")  # fired while we were down
        fx.calls.clear()
        svc = await ready(service(store, fx))
        v = svc.view(NOW)
        assert v["position"] is None and v["outcome"]["kind"] == "take_profit"
        assert v["outcome"]["text"].startswith("Flat. Your take profit closed 3 long @ $8.0310 avg.")
        row = store.select("perp_positions", {"id": "p1"})[0]
        assert row["close_reason"] == "take_profit" and row["state"] == "closed"
        assert fx.writes() == []
    run(go())


def test_restart_flat_after_a_close_in_kalshis_app(store):
    async def go():
        fx = FakeMargin()
        res = await _place(fx)
        _rows(store, fx, state="open", size=3.0, order_id=res["order_id"], outcome="filled", filled=3.0)
        await fx.create_order(ticker=fx.ticker, side="ask", count=Decimal(3), price=Decimal("7.7000"),
                              client_order_id="kalshi-app-1", time_in_force="immediate_or_cancel", reduce_only=True,
                              purpose="exit")
        svc = await ready(service(store, fx))
        assert svc.view(NOW)["outcome"]["kind"] == "closed_outside"
        assert store.select("perp_positions", {"id": "p1"})[0]["close_reason"] == "closed_outside"
    run(go())


def test_resync_reconciles_over_rest_before_trusting_more(store):
    async def go():
        fx = FakeMargin()
        svc = await ready(service(store, fx))
        await open_long(svc, fx)
        fx.calls.clear()
        fx.set_position(0, "1", "7.7971")  # 2 sold elsewhere while the stream was down
        await svc.on_stream_event({"type": "resync", "env": fx.env, "reason": "connected"}, NOW + 10_000)
        names = [n for n, _ in fx.calls]
        assert {"positions", "orders", "exit_triggers", "risk"} <= set(names)
        v = svc.view(NOW + 10_000)
        assert v["ready"] is True and v["position"]["state"] == "diverged"
    run(go())


def test_rest_positions_lagging_our_fill_do_not_close_or_diverge(store):
    async def go():
        fx = FakeMargin()
        svc = await ready(service(store, fx))
        fx.stale_positions = []  # REST still shows the step before our entry: flat
        await open_long(svc, fx)
        await svc.tick(NOW + 1_000)
        v = svc.view(NOW + 1_000)
        assert v["position"]["state"] == "open" and v["position"]["size"] == 3.0
        fx.stale_positions = None
        await svc.tick(NOW + 10_000)
        assert svc.view(NOW + 10_000)["position"]["state"] == "open"
    run(go())
