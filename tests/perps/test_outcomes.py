"""R12 outcomes from recorded fills, never estimates: a fired take profit, a liquidation (full and partial), the
funding ledger (one row per funding time) and the realized P&L against Kalshi's figures."""

from __future__ import annotations

import asyncio
import math

from tests.perps.helpers import DAY0, NOW, FakeMargin, approve, open_long, ready, service


def run(coro):
    return asyncio.run(coro)


def test_a_fired_take_profit_closes_with_the_outcome_from_fills(store):
    async def go():
        fx = FakeMargin()
        svc = await ready(service(store, fx))
        await open_long(svc, fx, stop_loss=7.50, take_profit=8.031)
        tid = fx.triggers[0]["id"]
        ev = fx.system_fill(f"etr:{tid}:0", 3, "8.0310", reason="take_profit_stop_loss")
        assert ev["post_position"] == 0.0 and ev["order_source"] == "system"
        await svc.on_stream_event(ev, NOW + 5_000)
        v = svc.view(NOW + 5_000)
        # Kalshi's realized (8.0310 - 7.7971) x 3 = 0.7017; fees 0.0281 + 8.031 x 3 x 0.12% = 0.02891 -> 0.0290
        assert v["position"] is None and v["outcome"]["kind"] == "take_profit"
        assert v["outcome"]["text"] == ("Flat. Your take profit closed 3 long @ $8.0310 avg. Realized P&L +$0.6446 "
                                        "(fees $0.0571, funding +$0.0000).")
        row = store.select("perp_positions", {"env": fx.env})[0]
        assert (row["close_reason"], row["realized_pnl"], row["fees"]) == ("take_profit", 0.6446, 0.0571)
        assert store.select("perp_triggers", {"position_id": row["id"]})[0]["state"] == "filled"
    run(go())


def test_the_record_lists_closed_positions_newest_first_with_their_realized_pnl(store):
    async def go():
        fx = FakeMargin()
        svc = await ready(service(store, fx))
        assert svc.record(10)["rows"] == []
        await open_long(svc, fx, stop_loss=7.50, take_profit=8.031)
        ev = fx.system_fill(f"etr:{fx.triggers[0]['id']}:0", 3, "8.0310", reason="take_profit_stop_loss")
        await svc.on_stream_event(ev, NOW + 5_000)
        rec = svc.record(10)
        assert (rec["env"], rec["wins"], rec["losses"], rec["pnl"]) == (fx.env, 1, 0, 0.6446)
        assert [(r["direction"], r["close_reason"], r["realized_pnl"]) for r in rec["rows"]] == [("long", "take_profit", 0.6446)]
    run(go())


def test_production_cannot_be_switched_off_while_a_position_is_open_and_can_once_it_is_closed(store):
    async def go():
        fx = FakeMargin()
        svc = await ready(service(store, fx))
        await open_long(svc, fx, stop_loss=7.50, take_profit=8.031)
        out = await svc.set_mode("off", NOW + 1_000)
        assert (out["ok"], out["code"]) == (False, "position_open") and svc.mode == "perp_prod"
        assert out["reason"] == "Close your production position first: perps stay in production while it is open."
        ev = fx.system_fill(f"etr:{fx.triggers[0]['id']}:0", 3, "8.0310", reason="take_profit_stop_loss")
        await svc.on_stream_event(ev, NOW + 5_000)
        assert (await svc.set_mode("off", NOW + 6_000))["ok"] and svc.mode == "off"
    run(go())


def test_a_fired_stop_is_named_as_the_stop(store):
    async def go():
        fx = FakeMargin()
        svc = await ready(service(store, fx))
        await open_long(svc, fx, stop_loss=7.50, take_profit=8.031)
        ev = fx.system_fill(f"etr:{fx.triggers[0]['id']}:0", 3, "7.4990", reason="take_profit_stop_loss")
        await svc.on_stream_event(ev, NOW + 5_000)
        assert svc.view(NOW + 5_000)["outcome"]["text"].startswith("Flat. Your stop closed 3 long @ $7.4990 avg.")
    run(go())


def test_a_liquidation_closes_with_kalshis_realized_figures(store):
    async def go():
        fx = FakeMargin()
        svc = await ready(service(store, fx))
        await open_long(svc, fx)
        ev = fx.system_fill("liq-01", 3, "3.1509", reason="liquidation")
        await svc.on_stream_event(ev, NOW + 5_000)
        v = svc.view(NOW + 5_000)
        # realized (3.1509 - 7.7971) x 3 = -13.9386; fees 0.0281 + 3.1509 x 3 x 0.12% = 0.01134 -> 0.0114
        assert v["outcome"]["kind"] == "liquidated"
        assert v["outcome"]["text"] == ("Flat. Kalshi liquidated 3 long @ $3.1509 avg. Realized P&L −$13.9781 (fees "
                                        "$0.0395, funding +$0.0000).")
        assert store.select("perp_positions", {"env": fx.env})[0]["close_reason"] == "liquidated"
    run(go())


def test_a_partial_liquidation_keeps_it_open_at_the_new_size(store):
    async def go():
        fx = FakeMargin()
        svc = await ready(service(store, fx))
        await open_long(svc, fx)
        ev = fx.system_fill("liq-01", 1, "3.1509", reason="liquidation")
        await svc.on_stream_event(ev, NOW + 5_000)
        v = svc.view(NOW + 5_000)
        assert v["position"]["size"] == 2.0 and v["position"]["state"] == "open"
        assert v["outcome"]["kind"] == "liquidated_partial"
        # its realized (3.1509 - 7.7971) = -4.6462, fee 3.1509 x 0.12% = 0.00378 -> 0.0038
        assert v["outcome"]["text"] == ("Kalshi liquidated 1 of your 3 long @ $3.1509; 2 remain open. Realized on it "
                                        "−$4.6500 incl. its fee.")
        await svc.tick(NOW + 10_000)  # REST agrees: nothing diverged
        assert svc.view(NOW + 10_000)["position"]["state"] == "open"
    run(go())


def test_funding_ledger_one_row_per_funding_time_and_pnl_matches(store):
    async def go():
        fx = FakeMargin()
        svc = await ready(service(store, fx))
        await open_long(svc, fx)
        later = DAY0 + 16 * 3_600_000 + 60_000
        fx.funding = [
            {"market_ticker": fx.ticker, "funding_ts": DAY0 + 12 * 3_600_000, "funding_rate": 0.0001,
             "mark_price": 7.80, "funding_amount": -0.0023, "quantity": 3, "subaccount_number": 0},
            {"market_ticker": fx.ticker, "funding_ts": DAY0 + 16 * 3_600_000, "funding_rate": -0.00005,
             "mark_price": 7.81, "funding_amount": 0.0011, "quantity": 3, "subaccount_number": 0},
            {"market_ticker": fx.ticker, "funding_ts": NOW - 3_600_000, "funding_rate": 0.0002,  # before we opened
             "mark_price": 7.79, "funding_amount": -0.0047, "quantity": 0, "subaccount_number": 0}]
        for k in range(3):
            await svc.tick(later + k)
        rows = store.select("perp_funding", {"env": fx.env})
        assert sorted(r["funding_ms"] for r in rows) == [DAY0 + 12 * 3_600_000, DAY0 + 16 * 3_600_000]
        assert math.isclose(svc.view(later)["position"]["funding_total"], -0.0012, abs_tol=1e-9)
        svc.heartbeat(later)
        await approve(svc, await svc.prepare_exit({"action": "close"}, later), later)
        row = store.select("perp_positions", {"env": fx.env})[0]
        # Kalshi's figures: realized (7.7969 - 7.7971) x 3 = -0.0006, fees 0.0281 x 2, funding -0.0023 + 0.0011
        assert abs(row["realized_pnl"] - (-0.0006 - 0.0562 - 0.0012)) <= 0.01
        assert row["realized_pnl"] == -0.058 and row["funding_total"] == -0.0012
        assert "funding −$0.0012" in svc.view(later)["outcome"]["text"]
    run(go())
