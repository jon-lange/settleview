"""The phone's optional stop (strategies "you" / "you_poly"): an entry with no stop at all is accepted and nothing
sells it but the resting auto-sell; the user can set, move or remove a BTC stop later (no order is sent for it); a
level BTC has already reached is refused. Simulated exchange only."""

from __future__ import annotations

import asyncio

from tests.trading.helpers import NOW, Exchange
from tests.trading.test_index_stop import K, Index, fav_market, pnw
from tests.trading.test_strategy_scope import enter, svc_for, up


def run(coro):
    return asyncio.run(coro)


async def opened_without_stop(store, idx: Index):
    ex = Exchange(fav_market())
    svc = await up(svc_for(store, ex, "you", index_price_fn=idx))
    r = await enter(svc, pnw(take_profit=0.99, stop_index_below=None))
    assert r["ok"] and r["bracket"]["state"] == "open", r
    return svc, ex, r["bracket"]


def test_no_stop_entry_is_never_stopped_by_btc_moves(store):
    async def go():
        idx = Index(K + 60)
        svc, ex, b = await opened_without_stop(store, idx)
        writes = len(ex.writes())
        idx.set(K - 500, NOW + 3000)  # BTC far through the strike: nothing sells without a stop
        await svc.tick(NOW + 3000)
        row = store.select("trading_brackets")[0]
        assert row["state"] == "open" and len(ex.writes()) == writes
    run(go())


def test_set_move_and_remove_a_stop(store):
    async def go():
        idx = Index(K + 60, NOW + 2000)
        svc, ex, b = await opened_without_stop(store, idx)
        writes = len(ex.writes())
        r = await svc.set_index_stop(b["id"], K + 30, NOW + 2000)
        assert r["ok"], r
        row = store.select("trading_brackets")[0]
        assert row["stop_index_below"] == K + 30 and row["stop_index_above"] is None
        assert len(ex.writes()) == writes  # setting a stop sends no order
        refused = await svc.set_index_stop(b["id"], K + 70, NOW + 2100)  # BTC (K+60) is already at or below it
        assert not refused["ok"] and refused["code"] == "already_hit"
        assert (await svc.set_index_stop(b["id"], None, NOW + 2200))["ok"]
        assert store.select("trading_brackets")[0]["stop_index_below"] is None
        assert (await svc.set_index_stop(b["id"], K + 30, NOW + 2300))["ok"]
        idx.set(K + 25, NOW + 4000)  # through the stop: the monitor sells
        await svc.tick(NOW + 4000)
        assert store.select("trading_brackets")[0]["state"] in ("stopping", "closed")
        events = [a["event"] for a in svc.audit()]
        assert events.count("stop_changed") == 3 and "stop_triggered" in events
    run(go())

