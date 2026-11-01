"""Settlement fixes from the 2026-10-30 code review of commit 0aba66e: the side mapping (DOWN pays on NO), a stop
still selling at the close, take-profit fills before (and unseen at) the close, a restart after the close, result
reads outside the monitor lock, and the "settling too long" warning. Every exchange is the in-memory simulator."""

from __future__ import annotations

import asyncio

import pytest

from btc_signal_engine.adapters.kalshi.trading import KalshiTrading
from btc_signal_engine.application.trading import TradingConfig
from tests.trading.helpers import CLOSE, NOW, TICKER, Exchange, Market, gateway, proposal, ready, service


def run(coro):
    return asyncio.run(coro)


def _row(store) -> dict:
    return store.select("trading_brackets")[0]


def _audits(store, event: str) -> list[dict]:
    return [a for a in store.select("trading_audit") if a["event"] == event]


async def opened(store, m: Market, p: dict | None = None, **kw):
    ex = Exchange(m)
    svc = await ready(service(store, ex, **kw))
    prep = await svc.prepare(p or proposal(), NOW)
    assert prep["ok"], prep
    r = await svc.approve(prep["token"], "yes", prep["ticket"], NOW + 1000)
    assert r["ok"] and r["bracket"]["state"] == "open", r
    await svc.tick(NOW + 2000)  # the take profit rests
    return svc, ex, r["bracket"]


def _down() -> tuple[Market, dict]:
    """A DOWN (NO) bracket: NO 70c bid / 71c ask."""
    return Market(yes_bid=0.29, no_bid=0.70), proposal(side="DOWN", max_price=0.72, take_profit=0.91, stop=0.50,
                                                       fair_up=0.25)


# -- #4: the side mapping ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("side,result,wins", [("UP", "yes", True), ("UP", "no", False),
                                              ("DOWN", "no", True), ("DOWN", "yes", False)])
def test_settlement_pays_only_the_winning_side(store, side, result, wins):
    async def go():
        m, p = _down() if side == "DOWN" else (Market(), proposal())
        svc, ex, b = await opened(store, m, p)
        assert _row(store)["side"] == side and _row(store)["held"] > 0
        m.settle(result)
        await svc.tick(CLOSE)
        row = _row(store)
        assert row["state"] == "closed" and row["exit_reason"] == "settled"
        assert row["proceeds"] == (row["held"] if wins else 0.0)
        (a,) = _audits(store, "settled")
        assert a["detail"]["won"] is wins and a["detail"]["result"] == result
        (s,) = ex.sim.settlements  # the simulated account is paid the same way
        assert s["result"] == result
        assert s["payout"] == pytest.approx(row["held"] if wins else 0.0)
    run(go())


# -- #4: a stop still selling at the close -----------------------------------------------------------------------

def test_a_bracket_stopping_at_the_close_settles_what_it_still_holds(store):
    async def go():
        m = Market()
        svc, ex, b = await opened(store, m)
        filled = _row(store)["held"]
        m.set(0.40, 0.59, size=2.0)  # under the 43c stop, but only 2 contracts bid
        await svc.tick(NOW + 3000)
        row = _row(store)
        assert row["state"] == "stopping" and row["held"] == pytest.approx(filled - 2.0)
        sold = row["proceeds"]
        assert sold > 0
        m.set(0.40, 0.59, depth=([], [(0.59, 100.0)]))  # no YES bid any more: the resell cannot fill
        await svc.tick(NOW + 4000)
        m.settle("yes")
        await svc.tick(CLOSE)
        row = _row(store)
        assert row["state"] == "closed" and row["exit_reason"] == "settled"
        assert row["held"] == pytest.approx(filled - 2.0)
        assert row["proceeds"] == pytest.approx(sold + (filled - 2.0))  # the sale plus $1 a contract still held
    run(go())


# -- #1 / #4: take-profit fills before the close -----------------------------------------------------------------

@pytest.mark.parametrize("result", ["yes", "no"])
def test_a_partial_take_profit_before_the_close_is_counted_once(store, result):
    async def go():
        m = Market()
        svc, ex, b = await opened(store, m)
        filled = _row(store)["held"]
        m.set(0.75, 0.24, size=3.0)  # through the 70c take profit for 3 contracts
        await svc.tick(NOW + 5000)  # its next poll learns the fill
        m.set(0.55, 0.42)
        row = _row(store)
        assert row["tp_filled"] == 3.0 and row["held"] == pytest.approx(filled - 3.0)
        m.settle(result)
        await svc.tick(CLOSE)
        row = _row(store)
        assert row["state"] == "closed" and row["exit_reason"] == "settled"
        rest = filled - 3.0
        assert row["proceeds"] == pytest.approx(3 * 0.70 + (rest if result == "yes" else 0.0))
        (t,) = svc.trades(0, CLOSE)
        assert t["pnl"] == pytest.approx(row["proceeds"] - row["entry_cost"] - (row["tp_fees"] or 0.0)
                                         - (row["exit_fees"] or 0.0), abs=1e-4)
    run(go())


class _FlakyOrders:
    """The real gateway with its order reads failing while `fail` > 0 (a lagging or down order endpoint)."""

    def __init__(self, gw: KalshiTrading):
        self._gw, self.fail = gw, 0
        self.env, self.max_order_cost = gw.env, gw.max_order_cost

    async def find_order(self, *a, **kw):
        if self.fail > 0:
            self.fail -= 1
            raise RuntimeError("order read failed")
        return await self._gw.find_order(*a, **kw)

    def __getattr__(self, name):
        return getattr(self._gw, name)


def test_take_profit_fills_unseen_at_the_close_are_never_paid_again_at_settlement(store):
    async def go():
        m = Market()
        ex = Exchange(m)
        gw = _FlakyOrders(gateway(ex))
        svc, _, b = await opened(store, m, env_gateways={"prod": gw})
        filled = _row(store)["held"]
        m.set(0.75, 0.24, size=4.0)
        await ex.sim.advance(TICKER)  # 4 fill on the venue in the last moments, unseen by the app
        m.set(0.55, 0.42)
        gw.fail = 2  # ... and the final read at the close fails, and so does the first one after the result
        await svc.tick(CLOSE)
        row = _row(store)
        assert row["state"] == "settling" and row["tp_order_id"]  # the take profit's id is kept
        assert _audits(store, "error")[-1]["code"] == "tp_final_read" and row["held"] == filled  # not learnt yet
        m.settle("yes")
        await svc.tick(CLOSE + 15_000)  # result final, but its take profit cannot be read: not booked
        row = _row(store)
        assert row["state"] == "settling" and [a["code"] for a in _audits(store, "error")].count("settle_tp_read") == 1
        await svc.tick(CLOSE + 30_000)
        row = _row(store)
        assert row["state"] == "closed" and row["exit_reason"] == "settled"
        assert row["tp_filled"] == 4.0 and row["held"] == pytest.approx(filled - 4.0)
        assert row["proceeds"] == pytest.approx(4 * 0.70 + (filled - 4.0))  # never 4 x $1 on top of the sale
    run(go())


# -- #3: a restart after the close --------------------------------------------------------------------------------

def test_a_restart_after_the_close_settles_an_open_bracket_never_a_reconciled_loss(store):
    async def go():
        m = Market()
        svc, ex, b = await opened(store, m)
        held = _row(store)["held"]
        m.settle("yes")
        ex.sim.settle(TICKER, "yes")  # the venue settled it while the app was down: the position reads 0
        later = CLOSE + 120_000
        svc2 = await ready(service(store, ex), later)
        assert _row(store)["state"] == "open"  # reconcile leaves a closed window to the monitor
        await svc2.tick(later + 1000)
        row = _row(store)
        assert row["state"] == "closed" and row["exit_reason"] == "settled" and row["proceeds"] == held
        assert not _audits(store, "error")
    run(go())


# -- #2: result reads outside the monitor lock --------------------------------------------------------------------

class _SlowResults:
    """The real gateway with a market_result that never answers."""

    def __init__(self, gw: KalshiTrading):
        self._gw, self.started = gw, asyncio.Event()
        self.env, self.max_order_cost = gw.env, gw.max_order_cost

    async def market_result(self, ticker):
        self.started.set()
        await asyncio.sleep(3600)

    def __getattr__(self, name):
        return getattr(self._gw, name)


def test_a_hanging_result_read_does_not_hold_the_lock_and_times_out(store):
    async def go():
        m = Market()
        ex = Exchange(m)
        gw = _SlowResults(gateway(ex))
        svc, _, b = await opened(store, m, env_gateways={"prod": gw},
                                 config=TradingConfig(enabled=True, settle_read_timeout_ms=200))
        task = asyncio.create_task(svc.tick(CLOSE))
        await asyncio.wait_for(gw.started.wait(), 5)
        assert not svc._lock.locked()  # stops, stream events and approvals are not waiting on it
        await asyncio.wait_for(task, 5)
        assert _row(store)["state"] == "settling"
        (err,) = [a for a in _audits(store, "error") if a["code"] == "settle_read"]
        assert "timed out" in err["reason"]
    run(go())


class _HeldResults(_SlowResults):
    """market_result answers a final YES only once `go` is set."""

    def __init__(self, gw: KalshiTrading):
        super().__init__(gw)
        self.go = asyncio.Event()

    async def market_result(self, ticker):
        self.started.set()
        await self.go.wait()
        return {"ticker": ticker, "result": "yes", "final": True, "status": "finalized", "source": "test"}


def test_a_bracket_closed_while_its_result_was_read_is_not_booked_again(store):
    async def go():
        m = Market()
        ex = Exchange(m)
        gw = _HeldResults(gateway(ex))
        svc, _, b = await opened(store, m, env_gateways={"prod": gw})
        task = asyncio.create_task(svc.tick(CLOSE))
        await asyncio.wait_for(gw.started.wait(), 5)
        async with svc._lock:  # e.g. a stream fill of its take profit closed it meanwhile
            bk = svc._brackets[b["id"]]
            bk.update(state="closed", exit_reason="tp", closed_ts=CLOSE, proceeds=1.23)
            svc._save(bk, CLOSE)
        gw.go.set()
        await asyncio.wait_for(task, 5)
        row = _row(store)
        assert row["exit_reason"] == "tp" and row["proceeds"] == 1.23 and not _audits(store, "settled")
    run(go())


# -- #7: settling too long: a visible warning, audited once, never closed automatically -------------------------

def test_a_bracket_settling_over_an_hour_is_flagged_once_and_left_open(store):
    async def go():
        m = Market()
        svc, ex, b = await opened(store, m)
        m.settle("yes", status="disputed")  # never final
        await svc.tick(CLOSE)
        assert svc.settle_warnings(CLOSE + 59 * 60_000) == []
        late = CLOSE + 61 * 60_000
        await svc.tick(late)
        await svc.tick(late + 60_000)
        (w,) = svc.status(late)["settle_warnings"]
        assert w["id"] == b["id"] and w["minutes"] == 61
        assert "settling for 61 min — check the venue" in w["message"] and "'disputed'" in w["message"]
        assert len(_audits(store, "settle_overdue")) == 1
        svc2 = await ready(service(store, ex), late + 120_000)
        await svc2.tick(late + 121_000)
        assert len(_audits(store, "settle_overdue")) == 1  # once, also across a restart
        assert _row(store)["state"] == "settling"  # nothing closes or voids it automatically
    run(go())
