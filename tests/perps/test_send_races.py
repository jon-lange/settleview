"""Sends racing the stream and the monitor (code review 2026-10-30, findings #1, #2, #3, #6): an order found through a
409 / lost reply (no averages in the reply) still gets its stop and its fill price from Kalshi's fills; a stream fill or
a monitor tick that lands while our own order is in flight is applied exactly once, by the send."""

from __future__ import annotations

import asyncio

from tests.perps.helpers import NOW, FakeMargin, approve, entry, open_long, ready, service, stream_fill


def run(coro):
    return asyncio.run(coro)


def _audits(store, event):
    return [r for r in store.select("trading_audit", {"event": event}) if r["strategy"] == "perps"]


def _stream_mid_send(svc, fx, now):
    """Deliver the stream's fill for our own in-flight order before create_order answers."""
    async def hook(coid):
        fx.after_create = None
        await svc.on_stream_event(stream_fill(fx.fills_list[-1], coid, fx.held(), "user", fx.env), now)
    return hook


# -- #1: a create answered in MarginClient._resolve's shape (found in the order list: no averages) ------------------
def test_entry_found_by_409_or_lost_reply_still_sets_the_stop_and_reads_its_price(store):
    async def go():
        fx = FakeMargin()
        fx.resolve_create_reply = True
        svc = await ready(service(store, fx))
        out = await open_long(svc, fx, stop_loss=7.50, take_profit=8.03)
        assert [c[0] for c in fx.writes()] == ["create_order", "set_exit_trigger"]
        pos = out["view"]["position"]
        # avg and fee from Kalshi's fills of that order: 3 @ 7.7971, fee 0.0281
        assert (pos["size"], pos["avg_entry"], pos["fees"], pos["state"]) == (3.0, 7.7971, 0.0281, "protected")
        assert out["outcome"]["text"] == ("Long 3 @ $7.7971 (fee $0.0281); stop $7.5000 and take profit $8.0300 on "
                                          "Kalshi.")
        o = store.select("perp_orders", {"client_order_id": fx.creates()[0]["client_order_id"]})[0]
        assert (o["outcome"], o["avg_fill_price"], o["fees"]) == ("filled", 7.7971, 0.0281)
    run(go())


def test_entry_found_in_the_lists_with_fills_unreadable_is_protected_and_says_the_price_is_unknown(store):
    async def go():
        fx = FakeMargin()
        fx.resolve_create_reply = True
        svc = await ready(service(store, fx))
        fx.fail["fills"] = [RuntimeError("fills read failed")]
        out = await open_long(svc, fx, stop_loss=7.50)
        assert [c[0] for c in fx.writes()] == ["create_order", "set_exit_trigger"]
        assert out["view"]["position"]["state"] == "protected" and out["view"]["position"]["size"] == 3.0
        assert out["outcome"]["text"] == "Long 3 (fill price not yet read from Kalshi); stop $7.5000 on Kalshi."
    run(go())


def test_exit_found_by_409_or_lost_reply_reads_its_price(store):
    async def go():
        fx = FakeMargin()
        svc = await ready(service(store, fx))
        await open_long(svc, fx)
        fx.resolve_create_reply = True
        out = await approve(svc, await svc.prepare_exit({"action": "reduce", "count": 1}, NOW))
        assert out["outcome"]["text"] == "Sold 1 @ $7.7969; 2 still open."
        assert out["view"]["position"]["size"] == 2.0
    run(go())


# -- #2 / #6: the stream's fill for our own order arrives before create_order answers ------------------------------
def test_reduce_with_the_stream_fill_first_counts_it_once(store):
    async def go():
        fx = FakeMargin()
        svc = await ready(service(store, fx))
        await open_long(svc, fx)
        fx.after_create = _stream_mid_send(svc, fx, NOW + 1)
        out = await approve(svc, await svc.prepare_exit({"action": "reduce", "count": 2}, NOW + 1), NOW + 1)
        assert fx.held() == 1
        assert out["outcome"]["text"] == "Sold 2 @ $7.7969; 1 still open."
        assert (out["view"]["position"]["size"], out["view"]["position"]["state"]) == (1.0, "open")
        await svc.tick(NOW + 2_000)
        v = svc.view(NOW + 2_000)
        assert (v["position"]["size"], v["position"]["state"]) == (1.0, "open") and v["banners"] == []
    run(go())


def test_reduce_with_the_stream_fill_and_a_tick_mid_send_never_marks_diverged(store):
    async def go():
        fx = FakeMargin()
        svc = await ready(service(store, fx))
        await open_long(svc, fx)
        stream = _stream_mid_send(svc, fx, NOW + 1)

        async def hook(coid):
            await stream(coid)
            await svc.tick(NOW + 1)
        fx.after_create = hook
        out = await approve(svc, await svc.prepare_exit({"action": "reduce", "count": 2}, NOW + 1), NOW + 1)
        assert (out["view"]["position"]["size"], out["view"]["position"]["state"]) == (1.0, "open")
        assert _audits(store, "diverged") == []
    run(go())


def test_close_with_the_streams_flat_fill_first_finishes_once_without_a_crash(store):
    async def go():
        fx = FakeMargin()
        svc = await ready(service(store, fx))
        await open_long(svc, fx)
        fx.after_create = _stream_mid_send(svc, fx, NOW + 1)
        out = await approve(svc, await svc.prepare_exit({"action": "close"}, NOW + 1), NOW + 1)
        assert out["ok"] and out["view"]["position"] is None
        assert out["outcome"]["kind"] == "closed" and out["outcome"]["text"].startswith("Flat. You closed 3 long")
        assert len(_audits(store, "closed")) == 1
        row = store.select("perp_positions", {"env": fx.env})[0]
        assert (row["state"], row["close_reason"]) == ("closed", "user_close")
    run(go())


def test_close_confirmed_by_the_streams_flat_fill_while_rest_lags(store):
    async def go():
        fx = FakeMargin()
        svc = await ready(service(store, fx))
        await open_long(svc, fx)
        fx.stale_positions = fx._positions()  # REST still shows 3 after the close
        fx.after_create = _stream_mid_send(svc, fx, NOW + 1)
        out = await approve(svc, await svc.prepare_exit({"action": "close"}, NOW + 1), NOW + 1)
        assert out["ok"] and out["view"]["position"] is None and out["outcome"]["kind"] == "closed"
        assert len(_audits(store, "closed")) == 1
    run(go())


# -- #3: the monitor runs while an entry's send is in flight -----------------------------------------------------
def test_tick_during_an_in_flight_entry_applies_the_fill_once(store):
    async def go():
        fx = FakeMargin()
        svc = await ready(service(store, fx))

        async def hook(coid):
            fx.after_create = None
            await svc.tick(NOW + 1)
        fx.after_create = hook
        out = await open_long(svc, fx)
        assert fx.held() == 3
        assert (out["view"]["position"]["size"], out["view"]["position"]["state"]) == (3.0, "open")
        assert out["outcome"]["text"] == "Long 3 @ $7.7971 (fee $0.0281)."
        await svc.tick(NOW + 2_000)
        assert svc.view(NOW + 2_000)["position"]["state"] == "open"
    run(go())


def test_a_slow_in_flight_entry_is_not_given_up_by_the_monitor(store):
    async def go():
        fx = FakeMargin()
        svc = await ready(service(store, fx))

        async def hook(coid):  # the create is still retrying a minute later; Kalshi does not list it yet
            fx.before_create = None
            await svc.tick(NOW + 61_000)
        fx.before_create = hook
        out = await open_long(svc, fx)
        assert out["outcome"]["kind"] == "opened" and out["view"]["position"]["size"] == 3.0
        assert _audits(store, "cancelled") == []
    run(go())


# -- a lost reply and the stream: the fill is applied exactly once ------------------------------------------------
def test_lost_reply_then_the_stream_fill_is_applied_once(store):
    async def go():
        fx = FakeMargin()
        fx.lose_create_reply = True
        svc = await ready(service(store, fx))
        out = await approve(svc, await svc.prepare(entry(), NOW))
        assert out["code"] == "unknown"
        coid = fx.creates()[0]["client_order_id"]
        await svc.on_stream_event(stream_fill(fx.fills_list[-1], coid, fx.held(), "user", fx.env), NOW + 10)
        await svc.tick(NOW + 2_000)
        v = svc.view(NOW + 2_000)
        assert (v["position"]["size"], v["position"]["state"]) == (3.0, "open") and fx.held() == 3
        await svc.tick(NOW + 4_000)
        assert svc.view(NOW + 4_000)["position"]["size"] == 3.0
    run(go())


def test_stream_fill_mid_send_then_a_lost_reply_is_applied_once(store):
    async def go():
        fx = FakeMargin()
        fx.lose_create_reply = True
        svc = await ready(service(store, fx))
        fx.after_create = _stream_mid_send(svc, fx, NOW)
        out = await approve(svc, await svc.prepare(entry(), NOW))
        assert out["code"] == "unknown"
        await svc.tick(NOW + 2_000)
        v = svc.view(NOW + 2_000)
        assert (v["position"]["size"], v["position"]["state"]) == (3.0, "open") and fx.held() == 3
    run(go())
