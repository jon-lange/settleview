"""The perps path's cost on the shared event loop (behaviour unchanged): view() reuses its kill switch and limits reads
for VIEW_TTL_MS until the service writes (the gate always reads fresh); a monitor tick that changes nothing writes
nothing; the foreign-orders check reads only the resting orders' own rows; the monitor's REST reads run at once, with
each read's failure handled as before; /api/health's readiness needs no database read."""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal

from btc_signal_engine.application.perp_trading import VIEW_TTL_MS
from btc_signal_engine.application.play_limits import CommittedToday
from tests.perps.helpers import NOW, FakeMargin, FakeQuotes, Limits, entry, open_long, ready, service


def run(coro):
    return asyncio.run(coro)


class Counting:
    """The store, counting (and recording) every select and insert."""

    def __init__(self, store) -> None:
        self.store = store
        self.selects: list[tuple] = []
        self.inserts: list[str] = []

    def select(self, table, where=None, **kw):
        self.selects.append((table, where, kw.get("filters", ())))
        return self.store.select(table, where, **kw)

    def insert(self, table, rows, **kw):
        self.inserts.append(table)
        return self.store.insert(table, rows, **kw)

    def __getattr__(self, name):
        return getattr(self.store, name)


# -- E1: view() reuses the kill switch and limits; the gate never does ---------------------------------------------
def test_views_inside_the_ttl_read_the_database_far_less_than_fresh_ones(store):
    async def go():
        fx = FakeMargin()
        total = CommittedToday()
        svc = service(store, fx, limits=Limits(per_trade=100.0, per_day=500.0), committed_today_fn=total.total)
        total.add_source(svc.committed_by_env)
        await ready(svc)
        await open_long(svc, fx)
        counted = svc.repo = Counting(store)
        for i in range(10):  # 20 views a second: all inside one TTL
            svc.view(NOW + 10_000 + i * 50)
        inside = len(counted.selects)
        counted.selects.clear()
        for i in range(10):  # one view per TTL: every one reads again
            svc.view(NOW + 20_000 + i * VIEW_TTL_MS)
        fresh = len(counted.selects)
        assert fresh == 20  # each fresh view: the kill switch and today's orders
        assert inside == 11  # the kill switch once, today's orders every time
    run(go())


def test_a_kill_shows_in_the_very_next_view_and_a_resume_too(store):
    async def go():
        fx = FakeMargin()
        svc = await ready(service(store, fx))
        assert svc.view(NOW + 100)["killed"] is False
        await svc.kill(NOW + 200)
        v = svc.view(NOW + 300)
        assert v["killed"] is True and v["can"]["entry"] is False
        assert v["can"]["entry_reason"] == "The kill switch is on: no new perps entries or adds."
        svc.resume(NOW + 400)
        assert svc.view(NOW + 500)["killed"] is False
    run(go())


def test_the_gate_uses_limits_changed_inside_the_ttl(store):
    async def go():
        fx = FakeMargin()
        lim = Limits(per_trade=100.0)
        svc = await ready(service(store, fx, limits=lim))
        assert svc.view(NOW)["limits"]["per_trade"] == 100.0
        lim.value["per_trade"] = 4.0  # saved on the Play screen: no write by the perps service
        r = await svc.prepare(entry(collateral=10.0), NOW + 100)
        assert r["code"] == "over_trade_limit" and "$4.00 per-trade limit" in r["reason"]
        assert fx.writes() == []
    run(go())


def test_the_gate_reads_a_kill_written_by_another_service_inside_the_ttl(store):
    async def go():
        fx = FakeMargin()
        svc = await ready(service(store, fx))
        assert svc.view(NOW)["killed"] is False
        store.insert("trading_settings", [{"key": "killed", "value": {"on": True}, "updated_ts": NOW}],
                     ignore_conflicts=False)  # an event service's kill: the shared switch
        assert (await svc.prepare(entry(), NOW + 100))["code"] == "killed"
        assert fx.writes() == []
    run(go())


def test_readiness_is_the_views_mode_ready_and_reason(store):
    async def go():
        fx = FakeMargin()
        svc = service(store, fx)
        pick = lambda v: {k: v[k] for k in ("mode", "ready", "ready_reason")}  # noqa: E731
        assert svc.readiness() == pick(svc.view(NOW)) == {
            "mode": "perp_prod", "ready": False,
            "ready_reason": "Not yet reconciled with Kalshi after start-up; nothing can be sent until it is."}
        await ready(svc)
        assert svc.readiness() == pick(svc.view(NOW)) == {"mode": "perp_prod", "ready": True, "ready_reason": None}
        await svc.set_mode("off", NOW)
        assert svc.readiness() == pick(svc.view(NOW)) == {"mode": "off", "ready": False,
                                                          "ready_reason": "Perps trading is off."}
    run(go())


# -- E2: a tick that changes nothing writes nothing ---------------------------------------------------------------
def test_identical_monitor_ticks_write_the_trigger_and_position_at_most_once(store):
    async def go():
        fx = FakeMargin()
        svc = await ready(service(store, fx))
        await open_long(svc, fx, stop_loss=7.50, take_profit=8.03)
        counted = svc.repo = Counting(store)
        await svc.tick(NOW + 2_000)
        first = list(counted.inserts)
        counted.inserts.clear()
        await svc.tick(NOW + 4_000)
        assert first.count("perp_triggers") <= 1 and first.count("perp_positions") <= 1
        assert counted.inserts == []  # the second identical tick writes nothing
        row = store.select("perp_triggers")[0]
        assert row["updated_ms"] < NOW + 4_000
        fx.edit_trigger_elsewhere(stop_loss_price="7.40")  # a real change is still written, once
        await svc.tick(NOW + 6_000)
        assert counted.inserts.count("perp_triggers") == 1
        row = store.select("perp_triggers")[0]
        assert (row["stop_loss_price"], row["updated_ms"]) == (7.40, NOW + 6_000)
        assert svc.view(NOW + 6_000)["trigger"]["stop_loss"] == 7.40
    run(go())


# -- E3: the foreign-orders check reads only the resting orders' rows ---------------------------------------------
def test_foreign_orders_check_reads_only_the_resting_client_order_ids(store):
    async def go():
        fx = FakeMargin()
        svc = await ready(service(store, fx))
        await open_long(svc, fx)
        counted = svc.repo = Counting(store)
        await svc.tick(NOW + 2_000)  # nothing resting: no order rows read at all
        assert [s for s in counted.selects if s[0] == "perp_orders"] == []
        ours = store.select("perp_orders")[0]["client_order_id"]
        fx.resting_foreign = [{"order_id": "x1", "client_order_id": "kalshi-app-7", "remaining_count": Decimal(2)},
                              {"order_id": "x2", "client_order_id": ours, "remaining_count": Decimal(1)},
                              {"order_id": "x3", "client_order_id": None, "remaining_count": Decimal(1)}]
        counted.selects.clear()
        await svc.tick(NOW + 4_000)
        reads = [s for s in counted.selects if s[0] == "perp_orders"]
        assert reads == [("perp_orders", {"env": fx.env}, [("client_order_id", "in", ["kalshi-app-7", ours])])]
        v = svc.view(NOW + 4_000)
        assert [b["text"] for b in v["banners"] if b["code"] == "diverged"] == [
            "2 resting order(s) in your perps account were not placed by Settleview. Cancel them on Kalshi; new "
            "entries wait until then."]
    run(go())


# -- E4: the monitor's reads at once, each failure handled as before ----------------------------------------------
READS = ("exchange_status", "market", "positions", "risk", "exit_triggers", "orders", "funding_estimate",
         "notional_risk_limit", "balance")


class Slow:
    """FakeMargin whose reads take 10 ms each, recording how many are in flight at once."""

    def __init__(self, fx: FakeMargin) -> None:
        self.fx = fx
        self.active = self.most = 0

    def __getattr__(self, name):
        f = getattr(self.fx, name)
        if name not in READS:
            return f

        async def call(*a, **kw):
            self.active += 1
            self.most = max(self.most, self.active)
            try:
                await asyncio.sleep(0.01)
                return await f(*a, **kw)
            finally:
                self.active -= 1
        return call


def test_a_tick_runs_its_independent_reads_at_once(store):
    async def go():
        fx = FakeMargin()
        slow = Slow(fx)
        svc = await ready(service(store, slow, quotes=FakeQuotes(fx)))
        await open_long(svc, fx, stop_loss=7.50)
        slow.most = 0
        await svc.tick(NOW + 2_000)
        # status + market together, then positions, risk, triggers, resting orders, funding, risk limit, balance
        assert slow.most == 7
        assert svc.view(NOW + 2_000)["position"]["state"] == "protected"
    run(go())


def test_a_failed_positions_read_applies_nothing_from_that_tick(store, caplog):
    async def go():
        fx = FakeMargin()
        svc = await ready(service(store, fx))
        fx.fail["positions"] = [RuntimeError("down")]
        fx.risk_limit = Decimal("1.0000")  # read in the same tick: must not be applied
        with caplog.at_level(logging.WARNING):
            await svc.tick(NOW + 2_000)
        assert "perps monitor read failed: down" in caplog.text
        assert (await svc.prepare(entry(), NOW + 2_000))["ok"]  # the $500 limit from before still stands
    run(go())


def test_a_failed_funding_estimate_still_applies_the_rest_of_the_tick(store, caplog):
    async def go():
        fx = FakeMargin()
        svc = await ready(service(store, fx))
        fx.fail["funding_estimate"] = [RuntimeError("funding down")]
        fx.risk_limit = Decimal("1.0000")
        with caplog.at_level(logging.WARNING):
            await svc.tick(NOW + 2_000)
        assert "perps funding estimate failed: funding down" in caplog.text
        assert "perps monitor read failed" not in caplog.text
        r = await svc.prepare(entry(), NOW + 2_000)
        assert r["code"] == "over_risk_limit" and "$1.00 risk limit" in r["reason"]
    run(go())


# -- E4 (wiring): queued stream events go before the tick; /api/health reads no view --------------------------------
def test_the_perps_loop_applies_queued_stream_events_before_a_due_tick(tmp_path, monkeypatch):
    from btc_signal_engine.bootstrap import app as app_mod
    from tests.perps.test_perp_wiring import key_file, settings

    for k in ("BTCFLOW_PERPS_ENABLED", "BTCFLOW_TRADING_DEMO_KEY_ID", "BTCFLOW_TRADING_DEMO_KEY_PATH",
              "BTCFLOW_TRADING_PROD_KEY_ID", "BTCFLOW_TRADING_PROD_KEY_PATH", "BTCFLOW_TRADING_STREAM"):
        monkeypatch.delenv(k, raising=False)
    _, demo = key_file(tmp_path, "demo")
    rt = app_mod.Runtime(settings(tmp_path, perps_enabled=True, trading_demo_key_id="demo-key-id",
                                  trading_demo_key_path=demo))
    try:
        assert rt.perps.env is None  # off: no reconcile, no stream, nothing on the network
        seen: list[str] = []

        async def on_event(ev, now):
            seen.append(ev["trade_id"])

        async def tick(now):
            seen.append("tick")
            raise asyncio.CancelledError  # ends the loop after its first tick

        rt.perps.on_stream_event = on_event
        rt.perps.tick = tick
        monkeypatch.setattr(app_mod, "PERPS_TICK_S", 0.0)  # the first tick is due at once
        for tid in ("t1", "t2", "t3"):
            rt.perp_events.put_nowait({"type": "fill", "env": None, "trade_id": tid})

        async def go():
            try:
                await rt.perps_loop()
            except asyncio.CancelledError:
                pass
        run(go())
        assert seen == ["t1", "t2", "t3", "tick"]

        def no_view(now):
            raise AssertionError("/api/health must not build the whole view")
        rt.perps.view = no_view
        assert rt.perps_health() == {"enabled": True, "mode": "off", "ready": False,
                                     "ready_reason": "Perps trading is off.", "stream": None}
    finally:
        rt.writer.close()
