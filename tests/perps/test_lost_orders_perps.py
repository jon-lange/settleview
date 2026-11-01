"""Perps lost answers (review fix plan 2026-10-31: B3#1, B3#3, B3#4, B3#12; domain/order_resolution.py).

An order the lists do not show stays unknown until the fills and the position prove its outcome; an entry found
filled late gets the bracket approved with it, or is marked unprotected; stream fills count only with our subaccount;
the approve's venue calls are bounded by a deadline. FakeMargin (tests/perps/helpers.py), no network."""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal

from btc_signal_engine.application.perp_trading import PerpConfig
from tests.perps.helpers import NOW, FakeMargin, FakeRejected, approve, entry, open_long, ready, service, stream_fill


def run(coro):
    return asyncio.run(coro)


def _sending_entry(store, fx, *, stop_loss=7.70, take_profit=8.00):
    """A 'sending' entry 61 s old (the app crashed, or its answer was lost) that approved a stop and take profit."""
    store.insert("perp_positions", [{
        "id": "p1", "env": fx.env, "ticker": fx.ticker, "subaccount": 0, "direction": "long", "size": 0.0,
        "avg_entry": None, "committed_collateral": 0.0, "leverage_chosen": 3.0, "state": "sending",
        "close_reason": None, "realized_pnl": 0.0, "fees": 0.0, "funding_total": 0.0, "opened_ms": NOW - 61_000,
        "closed_ms": None, "updated_ms": NOW - 61_000}], ignore_conflicts=False)
    store.insert("perp_orders", [{
        "id": "o1", "env": fx.env, "position_id": "p1", "client_order_id": "svperp-o1", "order_id": None,
        "role": "entry", "side": "bid", "price": 7.836, "count": 3.0, "collateral": 10.0,
        "time_in_force": "immediate_or_cancel", "reduce_only": False, "filled_count": 0.0, "avg_fill_price": None,
        "fees": None, "outcome": "sending", "reason": None, "stop_loss": stop_loss, "take_profit": take_profit,
        "created_ms": NOW - 61_000, "updated_ms": NOW - 61_000}], ignore_conflicts=False)


async def _placed_and_hidden(fx, *, hide_fills=False):
    """The entry reached Kalshi and filled 3, but the order lists (and with `hide_fills` the fills) do not show it."""
    real_orders, real_fills = fx.orders, fx.fills
    await fx.create_order(ticker=fx.ticker, side="bid", count=Decimal(3), price=Decimal("7.8360"),
                          client_order_id="svperp-o1", time_in_force="immediate_or_cancel", reduce_only=False,
                          purpose="entry")
    fx.calls.clear()

    async def orders(**kw):
        got = await real_orders(**kw)
        return {**got, "orders": [o for o in got["orders"] if o.get("client_order_id") != "svperp-o1"]}

    async def fills(**kw):
        got = await real_fills(**kw)
        return {**got, "fills": []} if fx.hide_fills else got
    fx.hide_fills = hide_fills
    fx.orders, fx.fills = orders, fills


# -- B3#3 + B3#1 ---------------------------------------------------------------------------------------------------
def test_unlisted_entry_found_from_the_fills_gets_its_saved_stop_and_take_profit(store):
    async def go():
        fx = FakeMargin()
        _sending_entry(store, fx)
        await _placed_and_hidden(fx)
        svc = await ready(service(store, fx))  # 61 s old and not listed: main closed it "never reached Kalshi"
        v = svc.view(NOW)
        assert v["position"] is not None and v["position"]["size"] == 3.0 and v["unknown_order"] is False
        assert v["position"]["state"] == "protected"  # main: no bracket, shown as plain "open"
        sets = [kw for n, kw in fx.calls if n == "set_exit_trigger"]
        assert len(sets) == 1 and Decimal(sets[0]["stop_loss_price"]) == Decimal("7.7")
        assert Decimal(sets[0]["take_profit_price"]) == Decimal("8")
        assert [n for n, _ in fx.writes()] == ["set_exit_trigger"]  # nothing re-sent
    run(go())


def test_late_fill_whose_bracket_fails_is_unprotected_and_warned(store):
    async def go():
        fx = FakeMargin()
        _sending_entry(store, fx)
        await _placed_and_hidden(fx)
        fx.fail["set_exit_trigger"] = [FakeRejected(400, "invalid", "nope")]
        svc = await ready(service(store, fx))
        v = svc.view(NOW)
        assert v["position"]["size"] == 3.0 and v["position"]["state"] == "unprotected"
        assert [a for a in store.select("trading_audit") if a["event"] == "no_stop"]
        assert any(b["code"] == "no_stop" for b in v["banners"])
    run(go())


def test_unlisted_entry_with_an_unexplained_position_stays_unknown(store):
    async def go():
        fx = FakeMargin()
        _sending_entry(store, fx)
        await _placed_and_hidden(fx, hide_fills=True)  # neither the lists nor the fills show it; the position does
        svc = await ready(service(store, fx))
        for t in (0, 15_000, 30_000):
            await svc.tick(NOW + t)
        v = svc.view(NOW + 30_000)
        assert v["unknown_order"] is True and v["position"]["state"] in ("sending", "unknown")  # main: closed
        assert len([a for a in store.select("trading_audit") if a["event"] == "order_unresolved"]) == 1
        assert store.select("perp_orders", {"id": "o1"})[0]["outcome"] == "sending"
        fx.hide_fills = False  # the fills catch up: found, bracket placed
        await svc.tick(NOW + 45_000)
        v = svc.view(NOW + 45_000)
        assert v["unknown_order"] is False and v["position"]["size"] == 3.0 and v["position"]["state"] == "protected"
    run(go())


# -- B3#4 -------------------------------------------------------------------------------------------------------------
def test_a_stream_fill_without_a_subaccount_is_never_applied_and_reconciles(store):
    async def go():
        fx = FakeMargin()
        svc = await ready(service(store, fx))
        await open_long(svc, fx)
        fx.calls.clear()
        row = {"fill_id": "f-x", "order_id": "ord-x", "ticker": fx.ticker, "side": "ask", "price": Decimal("7.79"),
               "count": Decimal(3), "fees": Decimal("0.03"), "created_ts": NOW + 5_000}
        ev = {**stream_fill(row, None, Decimal(0), "user", fx.env), "subaccount": None}
        await svc.on_stream_event(ev, NOW + 5_000)  # says "flat" for an unknown subaccount
        v = svc.view(NOW + 5_000)
        assert v["position"] is not None and v["position"]["size"] == 3.0  # main: closed on its post_position 0
        assert "positions" in [n for n, _ in fx.calls]  # re-read over REST instead
        assert svc.exchange_size(NOW + 5_000) == 3.0
    run(go())


# -- B3#12 ------------------------------------------------------------------------------------------------------------
def test_approve_never_holds_the_lock_past_its_deadline(store):
    async def go():
        fx = FakeMargin()
        svc = await ready(service(store, fx, config=PerpConfig(prod_enabled=True, send_deadline_s=0.2)))

        async def slow(_coid):
            await asyncio.sleep(5)  # Kalshi (or the client's own retries) hanging
        fx.before_create = slow
        prep = await svc.prepare(entry(), NOW)
        t0 = time.monotonic()
        out = await approve(svc, prep)
        assert time.monotonic() - t0 < 2.0  # main: waited the full 5 s holding the lock
        assert out["code"] == "unknown" and not svc._lock.locked()
        assert svc.view(NOW)["unknown_order"] is True  # left unknown: the monitor resolves it
    run(go())
