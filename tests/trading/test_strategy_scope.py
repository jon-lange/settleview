"""TradingService per phone strategy: rows and settings scoped by strategy with one shared kill switch, the
per-strategy entry cutoff, other strategies' orders known to the app, and no strategy outside the market table."""

from __future__ import annotations

import asyncio

import pytest

from btc_signal_engine.application.trading import TradingConfig, TradingService
from tests.trading.helpers import CLOSE, NOW, TICKER, Exchange, Market, gateway, proposal

ENVS = {"sandbox": "demo", "shadow": "shadow", "prod": "prod"}


def run(coro):
    return asyncio.run(coro)


class Seq:
    def __init__(self, prefix: str) -> None:
        self.prefix, self.n = prefix, 0

    def __call__(self) -> str:
        self.n += 1
        return f"{self.prefix}{self.n:04d}"


def svc_for(store, ex: Exchange, strategy: str = "you", *, mode: str = "prod", set_mode: bool = True,
            **kw) -> TradingService:
    if set_mode:
        store.insert("trading_settings", [{"key": f"{strategy}.mode", "value": {"mode": mode}, "updated_ts": 0}],
                     ignore_conflicts=False)
    env = ENVS[mode]
    return TradingService(store, {env: gateway(ex, env)}, kw.pop("config", TradingConfig(enabled=True)),
                          id_fn=Seq(f"{strategy}-"), token_fn=Seq(f"t{strategy}-"), strategy=strategy, **kw)


async def up(svc: TradingService, now: int = NOW, present: bool = True) -> TradingService:
    await svc.reconcile(ENVS[svc.mode], now)
    if present:
        svc.heartbeat(now)
    return svc


async def enter(svc: TradingService, p: dict, now: int = NOW) -> dict:
    prep = await svc.prepare(p, now)
    if not prep["ok"]:
        return prep
    return await svc.approve(prep["token"], "yes", prep["ticket"], now + 500)


# -- isolation ---------------------------------------------------------------------------------------------------
def test_two_strategies_on_one_store_are_isolated_but_share_the_kill_switch(store):
    async def go():
        you_ex, other_ex = Exchange(Market()), Exchange(Market())
        you = await up(svc_for(store, you_ex, "you"))
        other = await up(svc_for(store, other_ex, "you_poly"))  # a second strategy (Kalshi-shaped rig)
        ry = await enter(you, proposal())
        ro = await enter(other, proposal())
        assert ry["ok"] and ro["ok"], (ry, ro)
        # brackets: stamped, and each service sees only its own (the open-positions limit of 1 is per strategy)
        rows = store.select("trading_brackets")
        assert sorted(r["strategy"] for r in rows) == ["you", "you_poly"]
        assert [b["id"] for b in you.status(NOW)["open"]] == [ry["bracket"]["id"]]
        assert [b["id"] for b in other.status(NOW)["open"]] == [ro["bracket"]["id"]]
        assert you.exposure("prod", NOW)["open_positions"] == 1 == other.exposure("prod", NOW)["open_positions"]
        # orders and audit: stamped and scoped
        assert {o["strategy"] for o in store.select("trading_orders", {"bracket_id": ro["bracket"]["id"]})} == {
            "you_poly"}
        assert {a["strategy"] for a in you.audit(500)} == {"you"}
        assert {a["strategy"] for a in other.audit(500)} == {"you_poly"}
        assert all(a["bracket_id"] != ry["bracket"]["id"] for a in other.audit(500))
        # settings: prefixed by the strategy
        await other.set_mode("shadow", NOW)
        assert store.select("trading_settings", {"key": "you_poly.mode"})[0]["value"] == {"mode": "shadow"}
        assert store.select("trading_settings", {"key": "you.mode"})[0]["value"] == {"mode": "prod"}
        assert you.mode == "prod"
        # a restart of one strategy reloads only its own live brackets
        again = svc_for(store, you_ex, "you", set_mode=False)
        assert set(again._brackets) == {ry["bracket"]["id"]}
        # the kill switch is shared: a kill from either halts both; a resume from either resumes both
        await other.kill(NOW + 1000)
        assert you.killed() and other.killed() and again.killed()
        assert (await you.prepare(proposal(), NOW + 2000))["code"] == "killed"
        you.resume(NOW + 3000)
        assert not other.killed()
        assert store.select("trading_settings", {"key": "killed"})[0]["value"] == {"on": False}
    run(go())


# -- per-strategy entry cutoff -------------------------------------------------------------------------------------
def test_the_15_minute_tab_is_refused_under_two_minutes(store):
    async def go():
        ex = Exchange(Market())
        you = await up(svc_for(store, ex, "you"), CLOSE - 60_000)
        assert you.min_left_ms() == 120_000
        r = await you.prepare(proposal(), CLOSE - 60_000)
        assert r["code"] == "too_late" and "2 minutes" in r["reason"]
        assert ex.writes() == []
    run(go())


def test_the_five_minute_tab_enters_until_one_minute_left(store):
    async def go():
        ex = Exchange(Market())
        five = await up(svc_for(store, ex, "you_poly5"), CLOSE - 90_000)
        assert five.min_left_ms() == 60_000
        assert (await enter(five, proposal(), CLOSE - 90_000))["ok"]
        five2 = await up(svc_for(store, Exchange(Market()), "you_poly5", set_mode=False), CLOSE - 60_000)
        r = await five2.prepare(proposal(), CLOSE - 60_000)
        assert r["code"] == "too_late" and "1 minutes" in r["reason"]
    run(go())


def test_the_five_second_floor_holds_whatever_the_config(store):
    svc = svc_for(store, Exchange(Market()), "you", config=TradingConfig(enabled=True, min_left_ms=0))
    assert svc.min_left_ms() == 5_000


# -- other strategies' orders are known to the app ----------------------------------------------------------------
async def _resting(ex: Exchange, coid: str) -> str:
    resp = await ex.sim._create({"ticker": TICKER, "client_order_id": coid, "side": "bid", "count": "1",
                                 "price": "0.0100", "time_in_force": "good_till_canceled", "post_only": True})
    return resp.json()["order_id"]


def test_known_order_ids_fn_keeps_other_strategies_orders_from_blocking_trading(store):
    async def go():
        ex = Exchange(Market())
        oid = await _resting(ex, "other-1")
        await _resting(ex, "other-2")
        # by client_order_id and by order id
        svc = await up(svc_for(store, ex, "you", known_order_ids_fn=lambda ids: {"other-1", "nothing"} & ids))
        assert svc.status(NOW)["external"]["orders"] and (await svc.prepare(proposal(), NOW))["code"] == \
            "unknown_orders"  # other-2 is not known: it still blocks
        n = len(ex.writes())
        svc2 = await up(svc_for(store, ex, "you", set_mode=False,
                                known_order_ids_fn=lambda ids: {"other-2", oid} & ids))  # a coid and an order id
        assert svc2.status(NOW)["external"]["orders"] == []
        r = await enter(svc2, proposal())
        assert r["ok"] and r["bracket"]["state"] == "open", r
        assert len(ex.writes()) > n
    run(go())


def test_a_failing_known_order_ids_fn_leaves_the_service_unreconciled(store):
    async def go():
        ex = Exchange(Market())
        await _resting(ex, "other-1")  # a resting order to judge: the lookup is asked (only about it)
        n = len(ex.writes())

        def boom(ids):
            raise RuntimeError("order lookup down")
        svc = await up(svc_for(store, ex, "you", known_order_ids_fn=boom))
        assert (await svc.prepare(proposal(), NOW))["code"] == "not_reconciled"
        assert len(ex.writes()) == n
    run(go())


def test_nobody_present_means_no_entry_in_any_mode(store):
    async def go():
        for mode in ("shadow", "sandbox", "prod"):
            ex = Exchange(Market())
            svc = await up(svc_for(store, ex, "you", mode=mode), present=False)
            assert (await svc.prepare(proposal(), NOW))["code"] == "not_present", mode
            assert ex.writes() == []
    run(go())


# -- no silent fallbacks -------------------------------------------------------------------------------------------
def test_a_strategy_outside_the_market_table_is_refused_at_construction(store):
    for name in ("nope", "taker", "pnw_cushion", "final_minute"):
        with pytest.raises(ValueError, match="not a phone strategy"):
            svc_for(store, Exchange(Market()), name, mode="shadow")
    with pytest.raises(TypeError):  # the strategy is required: there is no default service
        TradingService(store, {}, TradingConfig())


def test_an_unknown_side_raises_instead_of_meaning_down():
    from btc_signal_engine.application.trading import _ask, _bid, _contract_side

    q = {"yes_bid": 0.6, "yes_ask": 0.62, "no_bid": 0.38, "no_ask": 0.4}
    assert _contract_side("UP") == "yes" and _contract_side("DOWN") == "no"
    assert _bid("UP", q) == 0.6 and _ask("DOWN", q) == 0.4
    for bad in ("down", "SIDEWAYS", "", None):
        with pytest.raises(ValueError, match="unknown side"):
            _contract_side(bad)
        with pytest.raises(ValueError, match="unknown side"):
            _bid(bad, q)


def test_an_unknown_stored_mode_falls_back_to_a_safe_mode_and_says_so(store):
    store.insert("trading_settings", [{"key": "you.mode", "value": {"mode": "live!"}, "updated_ts": 0}],
                 ignore_conflicts=False)
    svc = TradingService(store, {"demo": gateway(Exchange(Market()), "demo"), "prod": None},
                         TradingConfig(enabled=True), now=NOW, strategy="you")
    assert svc.mode == "sandbox"
    [row] = [a for a in svc.audit(50) if a["event"] == "mode_invalid"]
    assert row["code"] == "mode_invalid" and row["detail"] == {"stored": "live!", "using": "sandbox"}
    ok = svc_for(store, Exchange(Market()), "you_poly", mode="shadow")  # a known mode writes no such row
    assert ok.mode == "shadow" and not [a for a in ok.audit(50) if a["event"] == "mode_invalid"]
