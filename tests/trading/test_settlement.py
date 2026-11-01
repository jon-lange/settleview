"""Settlement proceeds (code review #4): a bracket held to the close waits for the market's official result
(`settling`), then books $1 a contract for the winning side and $0 for the losing side. A pending result is never a
win or a loss; a failed read leaves it pending and is retried politely. Every exchange is the in-memory simulator."""

from __future__ import annotations

import asyncio

from btc_signal_engine.adapters.kalshi.trading import KalshiTrading
from btc_signal_engine.application.trading import TradingConfig
from tests.trading.helpers import CLOSE, NOW, TICKER, Exchange, Market, gateway, proposal, ready, service


def run(coro):
    return asyncio.run(coro)


async def held_to_close(store, m: Market, **kw):
    ex = Exchange(m)
    svc = await ready(service(store, ex, **kw))
    prep = await svc.prepare(proposal(), NOW)
    assert prep["ok"], prep
    r = await svc.approve(prep["token"], "yes", prep["ticket"], NOW + 1000)
    assert r["ok"] and r["bracket"]["state"] == "open"
    return svc, ex, r["bracket"]


def _row(store) -> dict:
    return store.select("trading_brackets")[0]


def _market_reads(ex: Exchange) -> int:
    return sum(1 for r in ex.seen if r["method"] == "GET" and r["path"].endswith(f"/markets/{TICKER}"))


def test_a_settled_winner_books_1_dollar_a_contract(store):
    async def go():
        m = Market()
        svc, ex, b = await held_to_close(store, m)
        m.settle("yes")  # an UP bracket: won
        await svc.tick(CLOSE)
        row = _row(store)
        assert row["state"] == "closed" and row["exit_reason"] == "settled"
        assert row["proceeds"] == row["held"] == b["held"] and row["exit_fees"] == 0.0  # Kalshi: no settlement fee
        (t,) = svc.trades(0, CLOSE)
        assert t["pnl"] == round(row["held"] - row["entry_cost"], 4) > 0
        assert t["pnl_source"] == "kalshi_market_result"
        (a,) = [x for x in store.select("trading_audit") if x["event"] == "settled"]
        assert a["detail"]["result"] == "yes" and a["detail"]["won"] is True and a["detail"]["payout"] == row["held"]
        assert [x["event"] for x in store.select("trading_audit")].count("settle_pending") == 1
        assert ex.sim.settlements and TICKER not in ex.sim.positions  # the simulated account settled too
        done = await svc.prepare_exit(b["id"], CLOSE)
        assert done["code"] == "already_closed" and "settled (+$" in done["reason"]
    run(go())


def test_a_settled_loser_books_0(store):
    async def go():
        m = Market()
        svc, ex, b = await held_to_close(store, m)
        m.settle("no")  # an UP bracket: lost
        await svc.tick(CLOSE)
        row = _row(store)
        assert row["state"] == "closed" and row["exit_reason"] == "settled" and row["proceeds"] == 0.0
        (t,) = svc.trades(0, CLOSE)
        assert t["pnl"] == round(-row["entry_cost"], 4) and t["pnl_source"] == "kalshi_market_result"
    run(go())


def test_a_pending_result_is_neither_a_win_nor_a_loss(store):
    async def go():
        m = Market()
        svc, ex, b = await held_to_close(store, m)
        await svc.tick(CLOSE)  # no result yet
        row = _row(store)
        assert row["state"] == "settling" and row["exit_reason"] is None and row["proceeds"] == 0.0
        (t,) = svc.trades(0, CLOSE)
        assert t["pnl"] is None and t["state"] == "settling"  # not finished: not on the scoreboard
        st = svc.status(CLOSE)
        assert st["open"] == [] and [x["id"] for x in st["settling"]] == [b["id"]]
        assert svc.live_brackets() == [] and svc.exposure("prod", CLOSE)["open_positions"] == 0  # never blocks
        out = await svc.prepare_exit(b["id"], CLOSE)
        assert not out["ok"] and out["code"] == "settling"
        n = len(ex.writes())
        await svc.tick(CLOSE + 1000)
        assert len(ex.writes()) == n  # nothing is ever sent for it
    run(go())


def test_a_result_read_failure_leaves_it_pending_and_is_retried(store):
    async def go():
        m = Market()
        svc, ex, b = await held_to_close(store, m)
        m.fail_reads = 3  # the gateway retries a transport error 3 times: this read fails
        await svc.tick(CLOSE)
        assert _row(store)["state"] == "settling"
        (err,) = [a for a in store.select("trading_audit") if a["code"] == "settle_read"]
        assert "retrying" in err["reason"]
        m.settle("yes")
        reads = _market_reads(ex)
        await svc.tick(CLOSE + 5_000)  # politely: not before settle_poll_ms (15 s)
        assert _market_reads(ex) == reads and _row(store)["state"] == "settling"
        await svc.tick(CLOSE + 15_000)
        row = _row(store)
        assert row["state"] == "closed" and row["proceeds"] == row["held"]
    run(go())


def test_a_result_that_is_not_final_is_not_booked(store):
    async def go():
        m = Market()
        svc, ex, b = await held_to_close(store, m)
        m.settle("yes", status="disputed")
        await svc.tick(CLOSE)
        assert _row(store)["state"] == "settling"
        m.settle("yes", status="determined")
        await svc.tick(CLOSE + 15_000)
        assert _row(store)["state"] == "closed"
    run(go())


def test_reads_slow_down_after_ten_minutes(store):
    async def go():
        m = Market()
        svc, ex, b = await held_to_close(store, m, config=TradingConfig(enabled=True))
        late = CLOSE + 600_000
        await svc.tick(CLOSE)
        await svc.tick(late)
        reads = _market_reads(ex)
        await svc.tick(late + 30_000)
        assert _market_reads(ex) == reads  # every 60 s now
        await svc.tick(late + 60_000)
        assert _market_reads(ex) == reads + 1
    run(go())


def test_a_restart_keeps_watching_a_settling_bracket(store):
    async def go():
        m = Market()
        svc, ex, b = await held_to_close(store, m)
        await svc.tick(CLOSE)
        svc2 = await ready(service(store, ex), CLOSE + 1000)
        m.settle("no")
        await svc2.tick(CLOSE + 2000)
        assert _row(store)["state"] == "closed" and _row(store)["proceeds"] == 0.0
    run(go())


class _NoResults:
    """A gateway without market_result (wraps the real one, hides the method)."""

    def __init__(self, gw: KalshiTrading):
        self._gw = gw
        self.env = gw.env
        self.max_order_cost = gw.max_order_cost

    def __getattr__(self, name):
        if name == "market_result":
            raise AttributeError(name)
        return getattr(self._gw, name)


def test_a_gateway_that_cannot_read_results_leaves_it_pending_never_a_loss(store):
    async def go():
        m = Market()
        ex = Exchange(m)
        svc, _, b = await held_to_close(store, m, env_gateways={"prod": _NoResults(gateway(ex))})
        m.settle("yes")
        await svc.tick(CLOSE)
        await svc.tick(CLOSE + 15_000)
        assert _row(store)["state"] == "settling"
        assert [a["code"] for a in store.select("trading_audit")].count("settle_unsupported") == 1
    run(go())


def test_a_bracket_that_sold_everything_before_the_close_closes_at_once(store):
    async def go():
        m = Market()
        svc, ex, b = await held_to_close(store, m)
        m.set(0.75, 0.24)  # through the 70c take profit: it fills
        await svc.tick(NOW + 3000)
        assert _row(store)["exit_reason"] == "tp"
        await svc.tick(CLOSE)
        assert _row(store)["exit_reason"] == "tp"  # closed already: settlement never touches it
    run(go())


# -- ride to settlement: no take-profit order at all --------------------------------------------------------------

def test_ride_to_settlement_sends_one_buy_and_no_take_profit_then_settles(store):
    async def go():
        m = Market()
        ex = Exchange(m)
        svc = await ready(service(store, ex))
        p = proposal(ride_to_settlement=True)
        del p["take_profit"]
        prep = await svc.prepare(p, NOW)
        assert prep["ok"], prep
        t = prep["ticket"]
        assert t["no_tp"] is True and "No take-profit order" in t["sentence"]
        r = await svc.approve(prep["token"], "yes", t, NOW + 1000)
        assert r["ok"] and r["bracket"]["take_profit"] is None and r["bracket"]["no_tp"] is True
        await svc.tick(NOW + 3000)
        assert len(ex.posts()) == 1 and ex.posts()[0]["side"] == "bid"  # the buy only
        m.settle("yes")
        await svc.tick(CLOSE)
        row = _row(store)
        assert row["state"] == "closed" and row["proceeds"] == row["held"] > 0 and len(ex.posts()) == 1
    run(go())


def test_ride_to_settlement_values_are_never_guessed(store):
    async def go():
        svc = await ready(service(store, Exchange(Market())))
        both = await svc.prepare(proposal(ride_to_settlement=True), NOW)  # with a take_profit too
        assert not both["ok"] and both["code"] == "invalid"
        p = proposal()
        del p["take_profit"]
        missing = await svc.prepare(p, NOW)  # neither: refused, as before
        assert not missing["ok"] and missing["code"] == "invalid"
        odd = await svc.prepare({**p, "ride_to_settlement": "yes"}, NOW)
        assert not odd["ok"] and odd["code"] == "invalid"
    run(go())
