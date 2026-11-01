"""The whole-position bracket (KTD6, KTD9, R11, R14): set after the entry fills with both legs, failure leaves the
position unprotected with one warning and no retry, edits re-send the other leg, a stale updated time refuses, Kalshi's
400 stop_loss_past_liquidation maps to the gate's refusal, removal."""

from __future__ import annotations

import asyncio
from decimal import Decimal

from tests.perps.helpers import NOW, FakeMargin, FakeRejected, FakeUnknown, approve, open_long, ready, service


def run(coro):
    return asyncio.run(coro)


def test_entry_sets_the_bracket_with_both_legs_after_the_fill(store):
    async def go():
        fx = FakeMargin()
        svc = await ready(service(store, fx))
        out = await open_long(svc, fx, stop_loss=7.50, take_profit=8.03)
        names = [n for n, _ in fx.writes()]
        assert names == ["create_order", "set_exit_trigger"]
        assert out["outcome"]["text"] == ("Long 3 @ $7.7971 (fee $0.0281); stop $7.5000 and take profit $8.0300 on "
                                          "Kalshi.")
        v = out["view"]
        assert v["position"]["state"] == "protected"
        assert v["trigger"] == {"state": "active", "stop_loss": 7.5, "take_profit": 8.03,
                                "updated_time": str(fx.triggers[0]["updated_ts"]), "reason": None}
    run(go())


def test_no_trigger_by_default(store):
    async def go():
        fx = FakeMargin()
        svc = await ready(service(store, fx))
        out = await open_long(svc, fx)
        assert [n for n, _ in fx.writes()] == ["create_order"]
        assert out["view"]["trigger"]["state"] == "none" and out["view"]["position"]["state"] == "open"
    run(go())


def test_trigger_write_failure_leaves_it_unprotected_with_one_warning_and_no_retry(store):
    async def go():
        fx = FakeMargin()
        fx.fail["set_exit_trigger"] = [FakeUnknown()]
        svc = await ready(service(store, fx))
        out = await open_long(svc, fx, stop_loss=7.50)
        assert out["outcome"]["text"].endswith("; no stop on Kalshi.")
        v = out["view"]
        assert v["position"]["state"] == "unprotected" and v["trigger"]["state"] == "failed"
        assert v["banners"][0]["code"] == "no_stop" and v["banners"][0]["severity"] == "red"
        for k in range(3):
            await svc.tick(NOW + 1_000 * (k + 1))
            assert svc.view(NOW + 1_000 * (k + 1))["banners"][0]["code"] == "no_stop"  # persistent
        assert [n for n, _ in fx.writes()] == ["create_order", "set_exit_trigger"]  # never retried
        warned = store.select("trading_audit", {"event": "no_stop", "strategy": "perps"})
        assert len(warned) == 1
        assert svc.view(NOW)["can"]["exit"] is True  # the close slide sits beside the warning
    run(go())


def test_changing_only_the_stop_resends_the_take_profit(store):
    async def go():
        fx = FakeMargin()
        svc = await ready(service(store, fx))
        await open_long(svc, fx, stop_loss=7.50, take_profit=8.03)
        tid = fx.triggers[0]["id"]
        prep = await svc.prepare_trigger({"stop_loss": 7.45}, NOW)
        assert prep["ticket"]["take_profit"] == 8.03
        out = await approve(svc, prep)
        name, kw = fx.writes()[-1]
        assert name == "update_exit_trigger" and kw["trigger_id"] == tid
        assert (kw["stop_loss_price"], kw["take_profit_price"]) == ("7.4500", "8.0300")
        assert out["view"]["trigger"]["stop_loss"] == 7.45 and out["view"]["trigger"]["take_profit"] == 8.03
        assert fx.triggers[0]["id"] == tid
    run(go())


def test_an_edit_made_elsewhere_refuses_with_trigger_changed(store):
    async def go():
        fx = FakeMargin()
        svc = await ready(service(store, fx))
        await open_long(svc, fx, stop_loss=7.50, take_profit=8.03)
        prep = await svc.prepare_trigger({"stop_loss": 7.45}, NOW)
        fx.edit_trigger_elsewhere(stop_loss_price="7.40")  # Kalshi's app: same id, new updated_time
        n = len(fx.writes())
        out = await approve(svc, prep)
        assert out["code"] == "trigger_changed"
        assert len(fx.writes()) == n and fx.triggers[0]["stop_loss_price"] == Decimal("7.40")
    run(go())


def test_kalshi_stop_past_liquidation_maps_to_the_same_refusal(store):
    async def go():
        fx = FakeMargin()
        svc = await ready(service(store, fx))
        await open_long(svc, fx, stop_loss=7.50, take_profit=8.03)
        fx.reject_stop_past = Decimal("7.20")  # Kalshi's own liquidation check (our estimate has none at $50)
        out = await approve(svc, await svc.prepare_trigger({"stop_loss": 7.10}, NOW))
        assert out["code"] == "stop_past_liquidation" and "$7.1000" in out["reason"]
        assert svc.view(NOW)["trigger"]["stop_loss"] == 7.5  # unchanged
        assert svc.view(NOW)["position"]["state"] == "protected"
    run(go())


def test_stop_past_kalshis_liquidation_price_refused_before_sending(store):
    async def go():
        fx = FakeMargin()
        svc = await ready(service(store, fx))
        await open_long(svc, fx)
        fx.kalshi_liq = Decimal("7.2000")
        await svc.tick(NOW)
        assert svc.view(NOW)["position"]["liquidation_source"] == "kalshi"
        r = await svc.prepare_trigger({"stop_loss": 7.15}, NOW)
        assert r["code"] == "stop_past_liquidation"
        assert r["reason"] == ("A stop at $7.1500 is at or past the Kalshi liquidation price $7.2000: it could never "
                               "fire before liquidation.")
        assert [n for n, _ in fx.writes()] == ["create_order"]
    run(go())


def test_remove_the_trigger(store):
    async def go():
        fx = FakeMargin()
        svc = await ready(service(store, fx))
        await open_long(svc, fx, stop_loss=7.50, take_profit=8.03)
        out = await approve(svc, await svc.prepare_trigger({"stop_loss": None, "take_profit": None}, NOW))
        assert fx.writes()[-1][0] == "delete_exit_triggers"
        assert out["view"]["trigger"]["state"] == "none" and out["view"]["position"]["state"] == "open"
    run(go())


def test_a_definitive_rejection_of_an_edit_keeps_the_trigger(store):
    async def go():
        fx = FakeMargin()
        svc = await ready(service(store, fx))
        await open_long(svc, fx, stop_loss=7.50, take_profit=8.03)
        fx.fail["update_exit_trigger"] = [FakeRejected(400, "invalid_parameters")]
        out = await approve(svc, await svc.prepare_trigger({"take_profit": 8.10}, NOW))
        assert out["code"] == "trigger_rejected"
        assert svc.view(NOW)["trigger"]["take_profit"] == 8.03 and svc.view(NOW)["trigger"]["state"] == "active"
    run(go())
