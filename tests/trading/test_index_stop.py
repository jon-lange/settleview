"""Index-based bracket stops (the phone's optional BTC stop): the take profit rests where the ticket says; the stop
fires on the live BTC index coming back to within $20 of the strike (not on the contract price), exactly once, cancels the take profit first and
sells reduce-only IoC at the bid minus the slippage cap. A missing or stale (> 2 s) index never triggers it; a
contract-price stop, if set, still works. A held bracket settles at the close. Persisted across restarts and bound
into the approval token like every other exit price."""

from __future__ import annotations

import asyncio

from tests.trading.helpers import CLOSE, NOW, TICKER, Exchange, Market
from tests.trading.test_strategy_scope import enter, svc_for, up

K = 80_000.0


def run(coro):
    return asyncio.run(coro)


class Index:
    """The live index tap: (exchange ts, price), or None while unavailable."""

    def __init__(self, price: float = K + 60, ts: int = NOW) -> None:
        self.price, self.ts, self.fail = price, ts, False

    def set(self, price: float, ts: int) -> None:
        self.price, self.ts = price, ts

    def __call__(self):
        if self.fail:
            raise RuntimeError("tap down")
        return None if self.price is None else (self.ts, self.price)


def pnw(**kw) -> dict:
    p = {"ticker": TICKER, "side": "UP", "stake": 5.0, "max_price": 0.71, "take_profit": 0.90, "stop": None,
         "stop_index_below": K + 20, "window_end": CLOSE}
    p.update(kw)
    return p


def fav_market() -> Market:
    return Market(yes_bid=0.69, no_bid=0.30)  # YES 69c / 70c


async def opened(store, idx: Index, m: Market | None = None, p: dict | None = None, **kw):
    m = m or fav_market()
    ex = Exchange(m)
    svc = await up(svc_for(store, ex, "you", index_price_fn=idx, **kw))
    r = await enter(svc, p or pnw())
    assert r["ok"] and r["bracket"]["state"] == "open", r
    return svc, ex, m, r["bracket"]


def _row(store) -> dict:
    return store.select("trading_brackets")[0]


def test_take_profit_rests_at_entry_plus_20c_and_no_contract_stop(store):
    async def go():
        idx = Index()
        svc, ex, m, b = await opened(store, idx)
        entry, tp = ex.posts()
        assert entry["time_in_force"] == "immediate_or_cancel" and entry["price"] == "0.7100"
        assert tp["side"] == "ask" and tp["price"] == "0.9000" and tp["post_only"] is True
        assert tp["time_in_force"] == "good_till_canceled"
        row = _row(store)
        assert row["strategy"] == "you" and row["stop_index_below"] == K + 20
        assert row["stop_index_above"] is None and row["stop"] == 0.0
        assert b["stop"] is None and b["stop_index_below"] == K + 20
        m.set(0.05, 0.94)  # the contract collapses but BTC is still $60 over the strike: no contract stop
        await svc.tick(NOW + 3000)
        assert len(ex.writes()) == 2 and _row(store)["state"] == "open"
    run(go())


def test_index_stop_fires_once_cancels_tp_first_and_sells_reduce_only(store):
    async def go():
        idx = Index()
        svc, ex, m, b = await opened(store, idx)
        held = b["held"]
        m.set(0.60, 0.30, depth=([(0.60, 1.0)], [(0.30, 100.0)]))  # only 1 contract bid at 60c
        idx.set(K + 20, NOW + 2500)  # BTC back to $20 over the strike
        await svc.tick(NOW + 3000)
        w = ex.writes()
        assert [x["method"] for x in w] == ["POST", "POST", "DELETE", "POST"]  # TP cancelled BEFORE the sale
        sale = w[3]["body"]
        assert sale["side"] == "ask" and sale["price"] == "0.5500" and sale["reduce_only"] is True
        assert sale["time_in_force"] == "immediate_or_cancel" and float(sale["count"]) == held
        row = _row(store)
        assert row["state"] == "stopping" and row["held"] == round(held - 1.0, 2)
        assert row["stop_trigger_bid"] == 0.60
        idx.set(K + 200, NOW + 3500)  # BTC recovers: the stop already fired and keeps selling the rest
        m.set(0.62, 0.30)
        await svc.tick(NOW + 4000)
        w = ex.writes()
        assert [x["method"] for x in w] == ["POST", "POST", "DELETE", "POST", "POST"]  # no second cancel
        assert w[4]["body"]["reduce_only"] is True
        row = _row(store)
        assert row["state"] == "closed" and row["exit_reason"] == "stop" and row["held"] == 0
        trig = [a for a in svc.audit() if a["event"] == "stop_triggered"]
        assert len(trig) == 1 and trig[0]["detail"]["index"] == K + 20
        assert trig[0]["detail"]["stop_index_below"] == K + 20
        assert ex.sim.positions[TICKER] == 0  # never sold more than held
        await svc.tick(NOW + 6000)
        assert len(ex.writes()) == 5
    run(go())


def test_no_stop_on_a_stale_missing_or_failing_index(store):
    async def go():
        idx = Index()
        svc, ex, m, b = await opened(store, idx)
        idx.set(K, NOW + 3000 - 2001)  # at the strike, but 2.001 s old
        await svc.tick(NOW + 3000)
        idx.set(None, NOW + 4000)
        await svc.tick(NOW + 4000)
        idx.set(K, NOW + 5000)
        idx.fail = True
        await svc.tick(NOW + 5000)
        assert len(ex.writes()) == 2 and _row(store)["state"] == "open"
        idx.fail = False
        idx.set(K, NOW + 6000 - 2000)  # exactly 2 s old: still usable
        await svc.tick(NOW + 6000)
        assert _row(store)["exit_reason"] == "stop"
    run(go())


def test_no_index_tap_at_all_never_triggers(store):
    async def go():
        ex = Exchange(fav_market())
        svc = await up(svc_for(store, ex, "you"))
        assert (await enter(svc, pnw()))["ok"]
        await svc.tick(NOW + 3000)
        assert len(ex.writes()) == 2
    run(go())


def test_safe_index_does_not_trigger(store):
    async def go():
        idx = Index()
        svc, ex, m, b = await opened(store, idx)
        idx.set(K + 20.01, NOW + 3000)
        await svc.tick(NOW + 3000)
        assert len(ex.writes()) == 2
    run(go())


def test_contract_stop_is_the_fallback_when_the_index_is_stale(store):
    async def go():
        idx = Index()
        svc, ex, m, b = await opened(store, idx, p=pnw(stop=0.50))
        idx.set(K + 60, NOW - 60_000)  # stale
        m.set(0.50, 0.45)  # bid at the 50c contract stop
        await svc.tick(NOW + 3000)
        row = _row(store)
        assert row["exit_reason"] == "stop" and row["stop_trigger_bid"] == 0.50
    run(go())


def test_down_side_stop_fires_when_btc_comes_back_up_to_the_strike_minus_20(store):
    async def go():
        idx = Index(price=K - 60)
        m = Market(yes_bid=0.29, no_bid=0.70)  # NO 70c / 71c
        p = pnw(side="DOWN", max_price=0.72, take_profit=0.91, stop_index_below=None, stop_index_above=K - 20)
        svc, ex, m, b = await opened(store, idx, m=m, p=p)
        idx.set(K - 25, NOW + 3000)
        await svc.tick(NOW + 3000)
        assert len(ex.writes()) == 2
        idx.set(K - 19, NOW + 4000)
        await svc.tick(NOW + 4000)
        sale = ex.writes()[-1]["body"]
        assert sale["reduce_only"] is True and sale["side"] == "bid"  # sell NO == buy YES
        assert _row(store)["exit_reason"] == "stop"
    run(go())


def test_held_bracket_settles_at_the_close(store):
    async def go():
        idx = Index()
        svc, ex, m, b = await opened(store, idx)
        await svc.tick(CLOSE - 1000)
        m.settle("no")  # the market's result is already in at the first look
        await svc.tick(CLOSE + 1000)
        row = _row(store)
        assert row["state"] == "closed" and row["exit_reason"] == "settled" and row["held"] == b["held"]
        assert [x["method"] for x in ex.writes()] == ["POST", "POST"]
    run(go())


def test_index_stop_survives_a_restart(store):
    async def go():
        idx = Index()
        svc, ex, m, b = await opened(store, idx)
        again = await up(svc_for(store, ex, "you", set_mode=False, index_price_fn=idx), NOW + 2000)
        idx.set(K + 10, NOW + 3000)
        await again.tick(NOW + 3000)
        assert _row(store)["exit_reason"] == "stop"
    run(go())


def test_approval_is_bound_to_the_index_stop(store):
    async def go():
        ex = Exchange(fav_market())
        svc = await up(svc_for(store, ex, "you", index_price_fn=Index()))
        prep = await svc.prepare(pnw(), NOW)
        t = prep["ticket"]
        assert t["stop"] is None and t["stop_index_below"] == K + 20
        assert "BTC" in t["sentence"] and "$80,020" in t["sentence"]
        r = await svc.approve(prep["token"], "yes", {**t, "stop_index_below": K - 500}, NOW + 500)
        assert r["code"] == "altered" and ex.writes() == []
    run(go())


def test_invalid_index_stops_are_refused(store):
    async def go():
        ex = Exchange(fav_market())
        svc = await up(svc_for(store, ex, "you"))
        # an Up bracket's index stop is a level below, never above
        assert (await svc.prepare(pnw(stop_index_below=None, stop_index_above=K - 20), NOW))["code"] == "invalid"
        assert ex.writes() == []
    run(go())

