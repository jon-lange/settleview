"""The perps gate (U6 Approach 2): every refusal has its own code, comes in the documented order and sends nothing;
R2's sign rule; the kill switch; Kalshi's pause; limits (R9, KD7); balance (R10); subaccount 64 and foreign positions
(R17); stop checks."""

from __future__ import annotations

import asyncio

import pytest

from btc_signal_engine.application.perp_trading import PerpConfig
from btc_signal_engine.application.play_limits import CommittedToday
from btc_signal_engine.application.trading import PROD_CONFIRM, TradingConfig, TradingService
from tests.perps.helpers import (NOW, FakeMargin, FakeQuotes, Limits, approve, entry, open_long, ready, service)
from tests.trading import helpers as th


def run(coro):
    return asyncio.run(coro)


async def _code(svc, req, now=NOW):
    return (await svc.prepare(req, now)).get("code")


def test_entry_gate_refuses_in_order_and_sends_nothing(store):
    """Every condition fails at first; fixing them one at a time walks the documented order."""
    async def go():
        fx = FakeMargin(equity="5.0000")
        fx.available = fx.equity - 1  # $4 available: under the $5 collateral
        fx.set_position(0, "2", "7.70")  # a position Settleview did not open
        fx.trading_active = False
        fx.risk_limit = fx.risk_limit / 50  # $10
        q = FakeQuotes(fx)
        q.stale(3_400)
        lim = Limits(per_trade=4.0)
        lim.broken = True
        svc = service(store, fx, quotes=q, limits=lim)
        req = entry(collateral=5.0, leverage=6.0, stop_loss=6.90, band=0.2)  # 3 contracts (30 / 7.7971)
        assert await _code(svc, req) == "not_reconciled"
        assert (await svc.reconcile(NOW))["ok"]
        await svc.kill(NOW)
        assert await _code(svc, req) == "killed"
        svc.resume(NOW)
        assert await _code(svc, req) == "not_present"
        svc.heartbeat(NOW)
        assert await _code(svc, req) == "exchange_paused"
        fx.trading_active = True
        await svc.refresh_status(NOW)
        assert await _code(svc, req) == "not_reconciled"  # a pause ended: not before a reconcile
        await svc.reconcile(NOW)
        r = await svc.prepare(req, NOW)
        assert r["code"] == "quote_stale" and "3.4 s old" in r["reason"]
        q.why = None
        assert await _code(svc, req) == "foreign_position"
        fx.pos.clear()
        await svc.reconcile(NOW)
        assert await _code(svc, req) == "limits_unreadable"
        lim.broken = False
        r = await svc.prepare(req, NOW)
        assert r["code"] == "over_trade_limit" and "$4.00 per-trade limit" in r["reason"]
        lim.value["per_trade"] = None
        r = await svc.prepare(req, NOW)
        assert r["code"] == "balance_too_small" and "$4.00 available" in r["reason"]
        fx.available = fx.equity
        r = await svc.prepare(req, NOW)
        assert r["code"] == "over_risk_limit" and "$10.00 risk limit" in r["reason"]
        fx.risk_limit = fx.risk_limit * 50
        await svc.reconcile(NOW)
        r = await svc.prepare(req, NOW)
        assert r["code"] == "price_out_of_band"
        req["band"] = None
        r = await svc.prepare(req, NOW)
        # equity $5, 3 long at the 7.8360 cap: (3 x 7.836 - (5 - fee)) / (3 x (1 - 1/(6.8473 x 1.3))) = 6.96
        assert r["code"] == "stop_past_liquidation"
        assert "$6.9000" in r["reason"] and "$6.96" in r["reason"]
        req["stop_loss"] = 7.90  # above the 7.7970 mark: a long's stop would fire at once
        assert await _code(svc, req) == "stop_wrong_side"
        req["stop_loss"] = 7.50
        assert (await svc.prepare(req, NOW))["ok"]
        assert fx.writes() == []
    run(go())


def test_balance_unknown_when_unread_or_stale(store):
    async def go():
        fx = FakeMargin()
        svc = await ready(service(store, fx))
        fx.fail["balance"] = [RuntimeError("down")]
        r = await svc.prepare(entry(), NOW)  # the balance read on prepare fails; the reconcile's is fresh
        assert r["ok"]
        later = NOW + 301_000
        svc.heartbeat(later)
        fx.fail["balance"] = [RuntimeError("down")]
        r = await svc.prepare(entry(), later)
        assert r["code"] == "balance_unknown" and "301 s ago" in r["reason"]
        assert fx.writes() == []
    run(go())


@pytest.mark.parametrize("held", ["long", "short"])
def test_opposite_direction_entry_refused(store, held):
    async def go():
        fx = FakeMargin()
        svc = await ready(service(store, fx))
        await open_long(svc, fx, side=held)
        other = "short" if held == "long" else "long"
        r = await svc.prepare(entry(side=other), NOW)
        assert r["code"] == "close_first" and r["reason"] == f"Close your {held} first."
        r = await svc.prepare(entry(action="add", side=other), NOW)
        assert r["code"] == "close_first"
        assert len(fx.creates()) == 1
    run(go())


ACTIONS = [("entry", "long"), ("entry", "short"), ("add", "long"), ("add", "short"), ("reduce", None),
           ("close", None)]


@pytest.mark.parametrize("start", [3, 0, -3])
@pytest.mark.parametrize("action,side", ACTIONS)
def test_no_request_flips_the_sign_except_an_entry_from_flat(store, start, action, side):
    async def go():
        fx = FakeMargin()
        svc = await ready(service(store, fx))
        if start:
            await open_long(svc, fx, side="long" if start > 0 else "short")
        assert fx.held() == start
        before = len(fx.creates())
        if action in ("entry", "add"):
            prep = await svc.prepare(entry(action=action, side=side), NOW)
        else:
            prep = await svc.prepare_exit({"action": action, "count": 1 if action == "reduce" else None}, NOW)
        if prep["ok"]:
            await approve(svc, prep)
        sent = fx.creates()[before:]
        for o in sent:
            if start == 0:
                assert action == "entry" and not o["reduce_only"]
            elif (o["side"] == "bid") != (start > 0):  # an order against the position
                assert o["reduce_only"] and o["count"] <= abs(start)
        after = fx.held()
        assert after == 0 or start == 0 or (after > 0) == (start > 0)
        expected_sent = {0: action == "entry",
                         3: (action, side) in (("add", "long"), ("reduce", None), ("close", None)),
                         -3: (action, side) in (("add", "short"), ("reduce", None), ("close", None))}[start]
        assert bool(sent) == expected_sent
    run(go())


def test_kill_blocks_entries_and_adds_only_and_sends_nothing_itself(store):
    async def go():
        fx = FakeMargin()
        svc = await ready(service(store, fx))
        await open_long(svc, fx, stop_loss=7.50, take_profit=8.03)
        n = len(fx.writes())
        await svc.kill(NOW)
        assert len(fx.writes()) == n  # turning it on sends nothing
        assert (await svc.prepare(entry(), NOW))["code"] == "killed"
        assert (await svc.prepare(entry(action="add"), NOW))["code"] == "killed"
        assert (await approve(svc, await svc.prepare_exit({"action": "reduce", "count": 1}, NOW)))["ok"]
        assert (await approve(svc, await svc.prepare_trigger({"stop_loss": 7.45}, NOW)))["ok"]
        assert (await approve(svc, await svc.prepare_exit({"action": "close"}, NOW)))["ok"]
        names = [c for c, _ in fx.writes()[n:]]
        assert names == ["create_order", "update_exit_trigger", "create_order"]
        assert fx.held() == 0
    run(go())


def test_exchange_paused_refuses_every_write_and_stays_refused_until_reconciled(store):
    async def go():
        fx = FakeMargin()
        svc = await ready(service(store, fx))
        await open_long(svc, fx, stop_loss=7.50)
        waiting = await svc.prepare_exit({"action": "close"}, NOW)  # prepared before the pause
        n = len(fx.writes())
        fx.trading_active = False
        await svc.tick(NOW)
        for prep in (svc.prepare(entry(action="add"), NOW), svc.prepare_exit({"action": "reduce", "count": 1}, NOW),
                     svc.prepare_exit({"action": "close"}, NOW), svc.prepare_trigger({"stop_loss": 7.4}, NOW)):
            assert (await prep)["code"] == "exchange_paused"
        assert (await approve(svc, waiting))["code"] == "exchange_paused"
        assert svc.view(NOW)["banners"][0]["code"] == "paused"
        fx.trading_active = True
        await svc.refresh_status(NOW)  # resumed, not yet re-checked
        r = await svc.prepare_exit({"action": "close"}, NOW)
        assert r["code"] == "not_reconciled" and "resumed" in r["reason"]
        assert len(fx.writes()) == n
        await svc.tick(NOW)  # the monitor reconciles once trading is active
        assert (await approve(svc, await svc.prepare_exit({"action": "close"}, NOW)))["ok"]
    run(go())


def test_per_trade_limit_names_the_limit(store):
    async def go():
        fx = FakeMargin()
        svc = await ready(service(store, fx, limits=Limits(per_trade=10.0)))
        r = await svc.prepare(entry(collateral=15.0, leverage=2.0), NOW)
        assert r["code"] == "over_trade_limit"
        assert r["reason"] == "$15.00 of collateral is over the $10.00 per-trade limit."
        assert (await svc.prepare(entry(collateral=10.0, leverage=2.0), NOW))["ok"]
        assert fx.writes() == []
    run(go())


def test_limits_unreadable_refuses(store):
    async def go():
        fx = FakeMargin()
        lim = Limits()
        lim.broken = True
        svc = await ready(service(store, fx, limits=lim))
        r = await svc.prepare(entry(), NOW)
        assert r["code"] == "limits_unreadable" and "unreadable" in r["reason"]
        assert svc.view(NOW)["can"]["entry"] is False
    run(go())


def _event_service(store, ex, combined, limits):
    store.insert("trading_settings", [{"key": "you.mode", "value": {"mode": "prod"}, "updated_ts": 0}],
                 ignore_conflicts=False)
    return TradingService(store, {"prod": th.gateway(ex, "prod")}, TradingConfig(enabled=True), strategy="you",
                          id_fn=th.Ids(), token_fn=th.Ids(), limits_fn=limits, committed_today_fn=combined.total)


def test_daily_limit_is_one_total_with_event_stakes(store):
    """$20 a day; $15 staked on an event contract today; committing $10 to perps is refused naming the total."""
    async def go():
        combined = CommittedToday()
        lim = Limits(per_day=20.0)
        event = _event_service(store, th.Exchange(th.Market()), combined, lim)
        fx = FakeMargin()
        perps = service(store, fx, limits=lim, committed_today_fn=combined.total, now=th.NOW)
        combined.add_source(event.committed_by_env)
        combined.add_source(perps.committed_by_env)
        await th.ready(event)
        prep = await event.prepare(th.proposal(stake=15.0, max_price=0.58), th.NOW)
        out = await event.approve(prep["token"], "yes", prep["ticket"], th.NOW)
        assert out["ok"], out
        staked = event.committed_by_env(th.NOW)["prod"]
        assert 14.0 < staked <= 15.0
        await ready(perps, th.NOW)
        r = await perps.prepare(entry(collateral=10.0, leverage=2.0), th.NOW)
        assert r["code"] == "over_day_limit"
        assert f"Today already has ${staked:,.2f} in across event contracts and perps" in r["reason"]
        assert "$20.00 daily limit" in r["reason"]
        assert fx.writes() == []
        assert (await perps.prepare(entry(collateral=5.0, leverage=2.0), th.NOW))["ok"]
    run(go())


def test_event_day_check_counts_perps_collateral(store):
    async def go():
        combined = CommittedToday()
        lim = Limits(per_day=20.0)
        ex = th.Exchange(th.Market())
        event = _event_service(store, ex, combined, lim)
        fx = FakeMargin()
        perps = service(store, fx, limits=lim, committed_today_fn=combined.total, now=th.NOW)
        combined.add_source(event.committed_by_env)
        combined.add_source(perps.committed_by_env)
        await ready(perps, th.NOW)
        await open_long(perps, fx, th.NOW, collateral=10.0, leverage=2.0)  # 2 contracts: $10 committed
        assert perps.committed_by_env(th.NOW) == {"perp_demo": 0.0, "perp_prod": 10.0}
        await th.ready(event)
        prep = await event.prepare(th.proposal(stake=15.0, max_price=0.58), th.NOW)
        if prep["ok"]:
            prep = await event.approve(prep["token"], "yes", prep["ticket"], th.NOW)
        assert prep["code"] == "over_day_limit"
        assert "Today already has $10.00 in across event contracts and perps" in prep["reason"]
        assert ex.writes() == []
    run(go())


def test_practice_envs_do_not_count_toward_real_money_day(store):
    combined = CommittedToday()
    combined.add_source(lambda now: {"perp_demo": 7.0, "perp_prod": 3.0})
    combined.add_source(lambda now: {"prod": 4.0, "demo": 9.0, "shadow": 1.0})
    assert combined.total("perp_prod", NOW) == 7.0
    assert combined.total("prod", NOW) == 7.0
    assert combined.total("perp_demo", NOW) == 7.0
    assert combined.total("shadow", NOW) == 1.0


def test_isolated_position_is_shown_and_counted_but_never_blocks(store):
    async def go():
        fx = FakeMargin()
        fx.set_position(64, "60", "7.80", unrealized="-0.12")  # Kalshi's app, isolated: 60 x ~7.797 = $468 notional
        svc = await ready(service(store, fx))
        v = svc.view(NOW)
        assert v["foreign"] == [{"subaccount": 64, "kind": "app_isolated", "size": 60.0, "avg_entry": 7.8,
                                 "unrealized_pnl": -0.12}]
        assert v["can"]["entry"] is True and not any(b["code"] == "diverged" for b in v["banners"])
        assert (await svc.prepare(entry(), NOW))["ok"]  # 3 contracts: $468 + $23.5 fits the $500 limit
        r = await svc.prepare(entry(collateral=10.0, leverage=5.0), NOW)  # 6 contracts at 7.836 = $47
        assert r["code"] == "over_risk_limit" and "$500.00 risk limit" in r["reason"]
    run(go())


def test_foreign_size_diverges_blocks_entries_until_adopted_and_never_blocks_exits(store):
    async def go():
        fx = FakeMargin()
        svc = await ready(service(store, fx))
        await open_long(svc, fx)
        fx.set_position(0, "5", "7.78")  # 2 more bought in Kalshi's app on the same account
        await svc.tick(NOW + 10_000)  # past the fill-lag window
        now = NOW + 10_000
        svc.heartbeat(now)
        v = svc.view(now)
        assert v["position"]["state"] == "diverged"
        assert v["foreign"][0] == {"subaccount": 0, "kind": "foreign", "size": 5.0, "avg_entry": 7.78,
                                   "unrealized_pnl": 0.0}
        assert [b["code"] for b in v["banners"]] == ["diverged"]
        assert v["can"]["entry"] is False and v["can"]["exit"] is True
        assert (await svc.prepare(entry(action="add"), now))["code"] == "diverged"
        r = await svc.adopt(4, now)
        assert r["code"] == "position_changed"
        r = await svc.adopt(5, now)
        assert r["ok"] and r["view"]["position"]["size"] == 5.0 and r["view"]["position"]["state"] == "open"
        assert (await svc.prepare(entry(action="add"), now))["ok"]
        out = await approve(svc, await svc.prepare_exit({"action": "close"}, now), now)
        assert out["ok"] and fx.creates()[-1]["count"] == 5
    run(go())


def test_foreign_position_with_nothing_of_ours_is_adopted(store):
    async def go():
        fx = FakeMargin()
        fx.set_position(0, "-2", "7.81")
        svc = await ready(service(store, fx))
        v = svc.view(NOW)
        assert v["foreign"][0]["kind"] == "foreign" and v["foreign"][0]["size"] == -2.0
        assert (await svc.prepare(entry(), NOW))["code"] == "foreign_position"
        r = await svc.adopt(-2, NOW)
        assert r["ok"] and r["view"]["position"]["direction"] == "short"
        assert r["view"]["position"]["collateral"] is None  # not sized by the slider
        assert (await svc.prepare(entry(side="long"), NOW))["code"] == "close_first"
    run(go())


def test_foreign_resting_order_blocks_entries(store):
    async def go():
        fx = FakeMargin()
        fx.resting_foreign = [{"order_id": "x1", "client_order_id": "app-1", "ticker": fx.ticker, "side": "bid",
                               "remaining_count": 2}]
        svc = await ready(service(store, fx))
        r = await svc.prepare(entry(), NOW)
        assert r["code"] == "foreign_orders" and "cancel them on Kalshi" in r["reason"]
    run(go())


def test_production_needs_typed_confirmation(store):
    async def go():
        fx = FakeMargin()
        svc = service(store, fx, mode="off")
        assert svc.mode == "off"
        assert (await svc.prepare(entry(), NOW))["code"] == "mode_off"
        assert (await svc.set_mode("perp_prod", NOW, "yes"))["code"] == "confirm"
        r = await svc.set_mode("perp_prod", NOW, PROD_CONFIRM)
        assert r["ok"] and r["ready"]
        assert store.select("trading_settings", {"key": "perps.mode"})[0]["value"] == {"mode": "perp_prod"}
    run(go())


def test_production_switched_off_in_settings(store):
    async def go():
        fx = FakeMargin()
        svc = service(store, fx, config=PerpConfig(prod_enabled=False))
        assert svc.mode == "off"  # stored perp_prod without the master switch is never used
        assert (await svc.set_mode("perp_prod", NOW, PROD_CONFIRM))["code"] == "not_configured"
    run(go())
