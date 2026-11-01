"""KTD2: every refusal path sends NOTHING that writes (no POST, no DELETE reaches the transport).

Reads of the market quote are allowed (the gate needs the live price); order endpoints are never touched."""

from __future__ import annotations

import asyncio

import pytest

from btc_signal_engine.application.trading import TradingConfig
from tests.trading.helpers import CLOSE, NOW, Exchange, Market, proposal, ready, service


def run(coro):
    return asyncio.run(coro)


def _no_orders(ex: Exchange):
    assert ex.writes() == []
    assert not any("/portfolio/" in r["path"] and r["method"] != "GET" for r in ex.seen)


async def _approve(svc, prep, now=NOW, **change):
    shown = {**prep["ticket"], **change}
    return await svc.approve(prep["token"], "yes", shown, now)


def test_kill_switch_refuses_and_persists(store):
    async def go():
        ex = Exchange(Market())
        svc = await ready(service(store, ex))
        prep = await svc.prepare(proposal(), NOW)
        assert prep["ok"]
        await svc.kill(NOW)
        r = await _approve(svc, prep)
        assert r["code"] == "killed"
        _no_orders(ex)
        svc2 = service(store, ex)  # restart: still killed
        assert svc2.killed()
    run(go())


def test_cockpit_pause_flag_is_a_kill_switch(store):
    async def go():
        ex = Exchange(Market())
        svc = await ready(service(store, ex, paused=lambda: True))
        assert (await svc.prepare(proposal(), NOW))["code"] == "killed"
        _no_orders(ex)
    run(go())


def test_absent_human_refused(store):
    async def go():
        ex = Exchange(Market())
        svc = await ready(service(store, ex))
        later = NOW + 181_000
        assert (await svc.prepare(proposal(), later))["code"] == "not_present"
        svc.heartbeat(later, visible=False)  # a hidden tab does not count
        assert (await svc.prepare(proposal(), later))["code"] == "not_present"
        _no_orders(ex)
    run(go())


def test_expired_token_refused(store):
    async def go():
        ex = Exchange(Market())
        svc = await ready(service(store, ex))
        prep = await svc.prepare(proposal(), NOW)
        r = await _approve(svc, prep, now=NOW + 10_001)
        assert r["code"] == "expired"
        _no_orders(ex)
    run(go())


def test_reused_token_refused(store):
    async def go():
        ex = Exchange(Market())
        svc = await ready(service(store, ex))
        prep = await svc.prepare(proposal(), NOW)
        assert (await svc.approve(prep["token"], "no", prep["ticket"], NOW))["declined"]
        r = await _approve(svc, prep)
        assert r["code"] == "bad_token"
        assert (await svc.approve("made-up", "yes", prep["ticket"], NOW))["code"] == "bad_token"
        _no_orders(ex)
    run(go())


@pytest.mark.parametrize("field,value", [("limit_price", 0.60), ("contracts", 99.0), ("stake", 4.0),
                                         ("take_profit", 0.65), ("stop", 0.30), ("side", "DOWN"),
                                         ("ticker", "KXBTC15M-26OCT281330-30"), ("env", "demo")])
def test_altered_ticket_refused(store, field, value):
    async def go():
        ex = Exchange(Market())
        svc = await ready(service(store, ex))
        prep = await svc.prepare(proposal(), NOW)
        r = await _approve(svc, prep, **{field: value})
        assert r["code"] == "altered"
        _no_orders(ex)
    run(go())


def test_missing_ticket_fields_refused(store):
    async def go():
        ex = Exchange(Market())
        svc = await ready(service(store, ex))
        prep = await svc.prepare(proposal(), NOW)
        assert (await svc.approve(prep["token"], "yes", {}, NOW))["code"] == "altered"
        _no_orders(ex)
    run(go())


def test_over_per_trade_limit(store):
    async def go():
        ex = Exchange(Market())
        svc = await ready(service(store, ex))
        assert (await svc.prepare(proposal(stake=5.01), NOW))["code"] == "over_trade_limit"
        _no_orders(ex)
    run(go())


def test_over_window_and_day_limits_and_open_positions(store):
    async def go():
        ex = Exchange(Market())
        cfg = TradingConfig(enabled=True, max_stake_per_trade=5, max_dollars_per_window=6, max_dollars_per_day=8,
                            max_open_positions=1)
        svc = await ready(service(store, ex, config=cfg))
        # logged exposure: one earlier bracket today in this window, closed, $4.50 spent
        base = {"env": "prod", "strategy": "you", "session_id": None, "kind": "entry", "side": "UP", "window_end": CLOSE,
                "stake": 5.0,
                "contracts": 7.0, "limit_price": 0.6, "take_profit": 0.7, "stop": 0.45, "max_cost": 4.6,
                "ticket_hash": "x", "state": "closed", "filled": 7.0, "entry_cost": 4.5, "held": 0.0, "tp_filled": 7.0,
                "created_ts": NOW - 60_000, "updated_ts": NOW}
        store.insert("trading_brackets", [{**base, "id": "old1", "ticker": proposal()["ticker"]}])
        assert (await svc.prepare(proposal(stake=2.0), NOW))["code"] == "over_window_limit"
        store.insert("trading_brackets", [{**base, "id": "old2", "ticker": "KXBTC15M-26OCT281300-00",
                                           "created_ts": NOW - 900_000}])
        r = await svc.prepare(proposal(stake=1.0), NOW)
        assert r["code"] == "over_day_limit", r  # 4.5 + 4.5 + ~1 > 8
        store.insert("trading_brackets", [{**base, "id": "live", "ticker": "KXBTC15M-26OCT281300-00",
                                           "state": "open", "entry_cost": 0.0, "filled": 0.0,
                                           "created_ts": NOW - 86_400_000}])  # yesterday's still-open position
        svc2 = await ready(service(store, ex, config=cfg))
        assert (await svc2.prepare(proposal(stake=0.5), NOW))["code"] == "max_open"
        _no_orders(ex)
    run(go())


def test_limit_boundaries_exactly_at_limit_pass(store):
    async def go():
        ex = Exchange(Market())
        cfg = TradingConfig(enabled=True, max_stake_per_trade=5, max_dollars_per_window=5, max_dollars_per_day=5)
        svc = await ready(service(store, ex, config=cfg))
        prep = await svc.prepare(proposal(stake=5.0), NOW)  # cost <= stake <= every limit
        assert prep["ok"] and prep["ticket"]["max_cost"] <= 5.0
    run(go())


def test_wrong_series_refused_everywhere(store):
    async def go():
        ex = Exchange(Market(ticker="KXETH15M-26OCT281315-15"))
        svc = await ready(service(store, ex))
        r = await svc.prepare(proposal(ticker="KXETH15M-26OCT281315-15"), NOW)
        assert r["code"] == "wrong_series"
        assert ex.seen[-1]["path"].endswith("/portfolio/orders")  # only reconciliation reads happened
        _no_orders(ex)
    run(go())


def test_too_late_refused(store):
    async def go():
        ex = Exchange(Market())
        svc = await ready(service(store, ex), now=CLOSE - 120_000)
        assert (await svc.prepare(proposal(), CLOSE - 120_000))["code"] == "too_late"
        _no_orders(ex)
    run(go())


def test_price_moved_between_prepare_and_send(store):
    async def go():
        m = Market()
        ex = Exchange(m)
        svc = await ready(service(store, ex))
        prep = await svc.prepare(proposal(), NOW)
        m.set(0.57, 0.40)  # YES ask now 60c > 58c limit
        assert (await _approve(svc, prep))["code"] == "price_moved"
        _no_orders(ex)
    run(go())


def test_not_reconciled_refuses(store):
    async def go():
        ex = Exchange(Market())
        svc = service(store, ex)
        svc.heartbeat(NOW)
        assert (await svc.prepare(proposal(), NOW))["code"] == "not_reconciled"
        _no_orders(ex)
    run(go())


def test_prod_disabled_or_unconfigured_falls_back_to_sandbox(store):
    ex = Exchange(Market())
    svc = service(store, ex, config=TradingConfig(enabled=False))
    assert svc.mode == "sandbox"
    svc2 = service(store, ex, mode="prod", env_gateways={"prod": None})
    assert svc2.mode == "sandbox"
    assert store.select("trading_settings", {"key": "you.mode"})[0]["value"] == {"mode": "sandbox"}
    _no_orders(ex)


def test_unknown_resting_orders_on_exchange_block_entries(store):
    async def go():
        ex = Exchange(Market())
        await ex.sim._create({"ticker": proposal()["ticker"], "client_order_id": "manual", "side": "bid",
                              "count": "1", "price": "0.0100", "time_in_force": "good_till_canceled"})
        svc = await ready(service(store, ex))
        n = len(ex.writes())
        assert (await svc.prepare(proposal(), NOW))["code"] == "unknown_orders"
        assert len(ex.writes()) == n
    run(go())


def test_thin_book_refuses_entry_a_stop_could_not_protect(store):
    # Synthetic replay of the sandbox case that stopped out instantly: ask ~58c but the best Up bid is 2c.
    async def go():
        m = Market()
        ex = Exchange(m)
        svc = await ready(service(store, ex))
        prep = await svc.prepare(proposal(), NOW)
        m.set(0.02, 0.42)  # Up bid 2c, Up ask 58c: 56c wide, bid below the 43c stop
        r = await _approve(svc, prep)
        assert r["code"] == "thin_market" and "too thin" in r["reason"]
        _no_orders(ex)
    run(go())


def test_normal_one_cent_spread_still_trades(store):
    async def go():
        m = Market()  # Up bid 55c, ask 58c: 3c wide, bid above the 43c stop
        ex = Exchange(m)
        svc = await ready(service(store, ex))
        prep = await svc.prepare(proposal(), NOW)
        assert (await _approve(svc, prep))["ok"] is True
    run(go())
