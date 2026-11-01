"""Entries, adds and exits (U6 Approach 3-5, KTD4, KTD5, R1-R4, R18): the ticket, persist-before-send, what is sent,
fills, partial fills, nothing filled, lost replies, racing approvals, closes confirmed flat, and the timing log with the
real margin client over a mock transport."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from decimal import Decimal
from urllib.parse import parse_qsl

import httpx
from cryptography.hazmat.primitives.asymmetric import ed25519

from btc_signal_engine.adapters.kalshi.margin import MarginAuth, MarginClient
from btc_signal_engine.application.perp_trading import PerpConfig, PerpTradingService
from tests.perps.helpers import (NOW, FakeMargin, FakeQuotes, Ids, approve, entry, open_long, ready, service,
                                 stream_fill)


def run(coro):
    return asyncio.run(coro)


def test_market_entry_ticket_send_and_fill(store):
    async def go():
        fx = FakeMargin()
        svc = await ready(service(store, fx))
        prep = await svc.prepare(entry(), NOW)
        t = prep["ticket"]
        # $10 x 3 / 7.7971 = 3.85 -> 3 whole contracts; cap = 7.7971 x 1.005 = 7.83609 rounded down to the tick
        assert (t["count"], t["btc"], t["protective_price"]) == (3.0, 0.0003, 7.836)
        assert t["protective_text"] == "at most $7.8360" and t["time_in_force"] == "immediate_or_cancel"
        assert t["notional"] == round(3 * 7.7971, 4) and t["liquidation_label"] == "estimate"
        assert t["at_risk_text"] == ("Your whole perps balance ($50.00) backs this position: a big enough move can "
                                     "lose all of it.")
        assert t["bound"] == {"position_sign": 0, "position_size": 0.0, "trigger_updated_time": None}
        assert t["funding"]["next_ms"] is not None and t["chips"] == []
        assert prep["expires_ms"] == NOW + 10_000
        out = await approve(svc, prep)
        sent = fx.creates()[0]
        assert sent["side"] == "bid" and sent["count"] == Decimal(3) and sent["price"] == Decimal("7.8360")
        assert sent["time_in_force"] == "immediate_or_cancel" and not sent["reduce_only"]
        assert sent["purpose"] == "entry" and sent["client_order_id"].startswith("svperp-")
        # filled at the 7.7971 touch; fee 3 x 7.7971 x 0.12% = 0.02807 rounded up = 0.0281
        assert out["outcome"]["kind"] == "opened"
        assert out["outcome"]["text"] == "Long 3 @ $7.7971 (fee $0.0281)."
        pos = out["view"]["position"]
        assert (pos["size"], pos["avg_entry"], pos["fees"], pos["state"]) == (3.0, 7.7971, 0.0281, "open")
        assert pos["collateral"] == 10.0 and pos["leverage"] == 3.0
        o = store.select("perp_orders", {"client_order_id": sent["client_order_id"]})[0]
        assert (o["outcome"], o["filled_count"], o["avg_fill_price"], o["fees"]) == ("filled", 3.0, 7.7971, 0.0281)
    run(go())


def test_order_row_saved_as_sending_before_the_send(store):
    async def go():
        fx = FakeMargin()
        svc = await ready(service(store, fx))
        seen = {}
        real = fx.create_order

        async def spy(**kw):
            seen["rows"] = store.select("perp_orders", {"client_order_id": kw["client_order_id"]})
            seen["pos"] = store.select("perp_positions", {"env": fx.env})
            return await real(**kw)
        fx.create_order = spy
        await open_long(svc, fx)
        assert seen["rows"][0]["outcome"] == "sending" and seen["pos"][0]["state"] == "sending"
    run(go())


def test_nothing_filled_is_a_normal_outcome(store):
    async def go():
        fx = FakeMargin()
        svc = await ready(service(store, fx))
        prep = await svc.prepare(entry(), NOW)
        fx.ask = Decimal("7.9000")  # the book moved past "at most $7.8360"
        out = await approve(svc, prep)
        assert out["ok"] and out["outcome"]["kind"] == "nothing_filled"
        assert out["outcome"]["text"] == "Nothing filled at most $7.8360; nothing is open."
        assert out["view"]["position"] is None
        fx.ask = Decimal("7.7971")
        assert (await svc.prepare(entry(), NOW))["ok"]  # the gate reopens
        assert svc.committed_by_env(NOW)["perp_prod"] == 0.0
    run(go())


def test_partial_fill_records_what_filled_and_the_trigger_has_no_count(store):
    async def go():
        fx = FakeMargin()
        fx.fill_limit = Decimal(1)
        svc = await ready(service(store, fx))
        out = await open_long(svc, fx, stop_loss=7.50, take_profit=8.03)
        assert out["view"]["position"]["size"] == 1.0
        assert "Filled 1 of 3; the rest was cancelled" in out["outcome"]["text"]
        assert out["outcome"]["kind"] == "partial"
        (name, kw), = [c for c in fx.writes() if c[0] == "set_exit_trigger"]
        assert set(kw) == {"ticker", "stop_loss_price", "take_profit_price"}
        assert (kw["stop_loss_price"], kw["take_profit_price"]) == ("7.5000", "8.0300")
        assert out["view"]["position"]["collateral"] == round(10 / 3, 4)  # a third of the collateral was used
        assert svc.committed_by_env(NOW)["perp_prod"] == round(10 / 3, 4)
    run(go())


def test_all_or_nothing_is_fill_or_kill_and_limit_uses_the_users_price(store):
    async def go():
        fx = FakeMargin()
        svc = await ready(service(store, fx))
        prep = await svc.prepare(entry(fill="all_or_nothing"), NOW)
        assert prep["ticket"]["time_in_force"] == "fill_or_kill" and prep["ticket"]["chips"] == ["All or nothing"]
        r = await svc.approve(prep["token"], {**prep["ticket"], "time_in_force": "immediate_or_cancel"}, NOW)
        assert r["code"] == "altered"  # bound into the ticket
        prep = await svc.prepare(entry(fill="all_or_nothing"), NOW)
        await approve(svc, prep)
        assert fx.creates()[-1]["time_in_force"] == "fill_or_kill"
        await approve(svc, await svc.prepare_exit({"action": "close"}, NOW))
        prep = await svc.prepare(entry(fill="limit", limit_price=7.80), NOW)
        assert prep["ticket"]["protective_price"] == 7.8 and prep["ticket"]["chips"] == ["Limit $7.8000"]
        r = await svc.approve(prep["token"], {**prep["ticket"], "limit_price": 7.85}, NOW)
        assert r["code"] == "altered"
        prep = await svc.prepare(entry(fill="limit", limit_price=7.80), NOW)
        await approve(svc, prep)
        assert fx.creates()[-1]["price"] == Decimal("7.8000")
        assert fx.creates()[-1]["time_in_force"] == "immediate_or_cancel"
    run(go())


def test_expired_and_reused_tokens_send_nothing(store):
    async def go():
        fx = FakeMargin()
        svc = await ready(service(store, fx))
        prep = await svc.prepare(entry(), NOW)
        assert (await approve(svc, prep, NOW + 10_001))["code"] == "expired"
        assert (await approve(svc, prep))["code"] == "bad_token"
        assert fx.writes() == []
    run(go())


def test_lost_reply_is_unknown_blocks_entries_and_resolves_to_open(store):
    async def go():
        fx = FakeMargin()
        fx.lose_create_reply = True  # placed and filled on Kalshi; the answer never arrives
        svc = await ready(service(store, fx))
        out = await approve(svc, await svc.prepare(entry(), NOW))
        assert out["code"] == "unknown"
        v = out["view"]
        assert v["unknown_order"] is True and v["banners"][0]["code"] == "unknown_order"
        assert v["can"]["entry"] is False
        assert (await svc.prepare(entry(), NOW))["code"] == "unknown_order"
        assert svc.committed_by_env(NOW)["perp_prod"] == 10.0  # counted in full while unknown
        await svc.tick(NOW + 2_000)
        v = svc.view(NOW + 2_000)
        assert v["unknown_order"] is False
        assert (v["position"]["size"], v["position"]["avg_entry"], v["position"]["state"]) == (3.0, 7.7971, "open")
        assert v["position"]["fees"] == 0.0281  # from Kalshi's fills of that order
        assert v["outcome"]["text"] == "Your order went through: long 3 @ $7.7971."
        assert len(fx.creates()) == 1
    run(go())


def test_two_approvals_racing_on_an_add(store):
    async def go():
        fx = FakeMargin()
        svc = await ready(service(store, fx))
        await open_long(svc, fx)
        a = await svc.prepare(entry(action="add"), NOW)
        b = await svc.prepare(entry(action="add"), NOW)
        assert a["ticket"]["bound"]["position_size"] == 3.0 and a["ticket"]["new_size"] == 6.0
        assert a["ticket"]["new_avg_entry"] == round((3 * 7.7971 + 3 * 7.836) / 6, 4)
        r1, r2 = await asyncio.gather(approve(svc, a), approve(svc, b))
        assert r1["ok"] and r1["outcome"]["text"].startswith("Added 3 @ $7.7971; now 6 @ $7.7971")
        assert r2["code"] == "position_changed"
        assert fx.held() == 6 and len(fx.creates()) == 2
    run(go())


def test_reduce_is_reduce_only_and_keeps_the_rest_open(store):
    async def go():
        fx = FakeMargin()
        svc = await ready(service(store, fx))
        await open_long(svc, fx)
        prep = await svc.prepare_exit({"action": "reduce", "count": 1}, NOW)
        # floor = bid 7.7969 x 0.995 = 7.75791 rounded UP onto the tick
        assert prep["ticket"]["protective_text"] == "at least $7.7580" and prep["ticket"]["remaining"] == 2.0
        out = await approve(svc, prep)
        sent = fx.creates()[-1]
        assert (sent["side"], sent["count"], sent["price"]) == ("ask", Decimal(1), Decimal("7.7580"))
        assert sent["reduce_only"] and sent["purpose"] == "exit"
        assert out["outcome"]["text"] == "Sold 1 @ $7.7969; 2 still open."
        assert out["view"]["position"]["size"] == 2.0
        r = await svc.prepare_exit({"action": "reduce", "count": 2}, NOW)
        assert r["code"] == "invalid" and "use Close" in r["reason"]
    run(go())


def test_close_says_flat_only_after_the_exchange_confirms(store):
    async def go():
        fx = FakeMargin()
        svc = await ready(service(store, fx))
        await open_long(svc, fx)
        fx.stale_positions = fx._positions()  # REST positions lag one step: still 3 after the close
        out = await approve(svc, await svc.prepare_exit({"action": "close"}, NOW))
        assert out["outcome"]["kind"] == "closing" and "Flat" not in out["outcome"]["text"]
        assert out["view"]["position"]["state"] == "closing"
        await svc.tick(NOW + 20)  # our own order's fill is not Kalshi's confirmation; REST still lags
        assert svc.view(NOW + 20)["position"]["state"] == "closing"
        coid = fx.creates()[-1]["client_order_id"]
        await svc.on_stream_event(stream_fill(fx.fills_list[-1], coid, Decimal(0), "user", fx.env), NOW + 50)
        v = svc.view(NOW + 50)
        assert v["position"] is None and v["outcome"]["kind"] == "closed"
        # realized (7.7969 - 7.7971) x 3 = -0.0006; fees 0.0281 + 0.0281
        assert v["outcome"]["text"] == ("Flat. You closed 3 long @ $7.7969 avg. Realized P&L -$0.0568 (fees "
                                        "$0.0562, funding +$0.0000).")
        row = store.select("perp_positions", {"env": fx.env})[0]
        assert (row["state"], row["close_reason"], row["realized_pnl"]) == ("closed", "user_close", -0.0568)
    run(go())


def test_close_confirmed_by_rest_when_it_shows_zero(store):
    async def go():
        fx = FakeMargin()
        svc = await ready(service(store, fx))
        await open_long(svc, fx)
        out = await approve(svc, await svc.prepare_exit({"action": "close"}, NOW))
        assert out["outcome"]["kind"] == "closed" and out["outcome"]["text"].startswith("Flat. You closed 3 long")
    run(go())


# -- the real margin client over a mock transport: the protocol matches and prepare-to-send stays under 300 ms ------
def _kalshi(request: httpx.Request) -> httpx.Response:
    path = request.url.path.removeprefix("/trade-api/v2")
    q = dict(parse_qsl(request.url.query.decode()))
    t = "KXBTCPERP"
    answers = {
        "/margin/exchange/status": {"exchange_active": True, "trading_active": True},
        f"/margin/markets/{t}": {"market": {"ticker": t, "status": "active", "tick_size": "0.0001",
                                            "contract_size": "0.0001", "long_leverage_estimates": {"1000": 6.8473},
                                            "short_leverage_estimates": {"1000": 6.38}}},
        "/margin/risk_parameters": {"liquidation_margin_ratio_threshold": 1.0,
                                    "initial_margin_multiplier": {t: 1.3}},
        "/margin/fee_tiers": {"maker_fee_rates": {t: 0.0002}, "taker_fee_rates": {t: 0.0012}},
        "/margin/notional_risk_limit": {"default_notional_value_risk_limit": "500.0000",
                                        "notional_value_risk_limits_by_market_ticker": {t: "500.0000"},
                                        "total_current_usage": "0.0000", "current_usage_by_market_ticker": {}},
        "/margin/balance": {"settled_funds": "50.0000", "subaccount_balances": [
            {"subaccount": 0, "account_equity": "50.0000", "available_balance": "50.0000", "position_value": "0",
             "maintenance_margin": "0", "initial_margin": "0", "resting_orders_margin": "0"}]},
        "/margin/positions": {"positions": []},
        "/margin/risk": {"total_position_notional": "0", "total_maintenance_margin": "0", "positions": []},
        "/margin/orders": {"orders": [], "cursor": ""},
        "/margin/funding_rates/estimate": {"market_ticker": t, "funding_rate": 0.0001, "mark_price": "7.7970",
                                           "next_funding_time": "2026-10-30T12:00:00Z"},
    }
    if request.method == "POST" and path == "/margin/orders":
        body = json.loads(request.content)
        return httpx.Response(201, json={"order_id": "o-1", "client_order_id": body["client_order_id"],
                                         "fill_count": body["count"], "remaining_count": "0.00",
                                         "average_fill_price": "7.7971", "average_fee_paid": "0.009367"})
    if request.method == "GET" and path in answers:
        return httpx.Response(200, json=answers[path])
    return httpx.Response(404, json={"code": "not_found", "q": q})


def test_real_margin_client_prepare_and_send_timed_under_300ms(store, caplog):
    async def go():
        posted = []

        async def handler(request):
            if request.method == "POST":
                posted.append(json.loads(request.content))
            return _kalshi(request)
        client = MarginClient(MarginAuth("test-key", ed25519.Ed25519PrivateKey.generate(), env="perp_prod"),
                              env="perp_prod", subaccount=0, max_notional="1000",
                              transport=httpx.MockTransport(handler))
        fx = FakeMargin()  # only its quote figures
        store.insert("trading_settings", [{"key": "perps.mode", "value": {"mode": "perp_prod"}, "updated_ts": 0}],
                     ignore_conflicts=False)
        svc = PerpTradingService(store, {"perp_prod": client}, PerpConfig(prod_enabled=True),
                                 quotes={"perp_prod": FakeQuotes(fx)}, id_fn=Ids(), token_fn=Ids("tok"), now=NOW)
        assert (await svc.reconcile(NOW))["ok"]
        svc.heartbeat(NOW)
        with caplog.at_level(logging.INFO):  # _Steps logs through trading.py's logger
            prep = await svc.prepare(entry(), NOW)
            out = await svc.approve(prep["token"], prep["ticket"], NOW)
        await client.aclose()
        assert out["ok"] and out["outcome"]["kind"] == "opened"
        assert posted[0]["count"] == "3.00" and posted[0]["price"] == "7.8360" and posted[0]["side"] == "bid"
        assert posted[0]["time_in_force"] == "immediate_or_cancel" and posted[0]["reduce_only"] is False
        assert posted[0]["subaccount"] == 0
        totals = {m.group(1): float(m.group(2)) for m in (re.search(r"perps (prepare|approve) timing: .*total (\d+) ms",
                                                                    r.getMessage()) for r in caplog.records) if m}
        assert set(totals) == {"prepare", "approve"}
        assert totals["prepare"] < 300 and totals["approve"] < 300
    run(go())
