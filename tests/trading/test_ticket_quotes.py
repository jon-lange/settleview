"""Ticket quotes from the live WebSocket book, step timings, and sells of a position that is already closed.

2026-10-29 (live, $1 Kalshi trade): the exit slide took ~5 s (its REST order-book read failed once on the way) and,
meanwhile, the 85c auto-sell filled, so the sell answered with something other than "your auto-sell sold". Here:
prepare / prepare_exit / approve read the fresh WebSocket book (REST only when it is stale), log the source and their
step timings, and a sell of a bracket its take profit or stop already closed answers `already_closed` with the
recorded fills. Simulated exchange only."""

from __future__ import annotations

import asyncio
import contextlib
import logging

import pytest

from tests.trading.helpers import CLOSE, NOW, TICKER, W0, Exchange, Market
from tests.trading.test_index_stop import pnw
from tests.trading.test_strategy_scope import enter, svc_for, up


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def caplog(monkeypatch):
    """The trading service's log lines, captured on its own logger (see tests/kalshi/test_sdk_backend.py)."""
    lg = logging.getLogger("btc_signal_engine.application.trading")
    h = _Lines()
    monkeypatch.setattr(lg, "disabled", False)
    old = lg.level
    lg.setLevel(logging.DEBUG)  # setLevel (not the attribute): it also clears the logger's isEnabledFor cache
    lg.addHandler(h)
    yield h
    lg.removeHandler(h)
    lg.setLevel(old)


class _Lines(logging.Handler):
    """Records one logger's messages directly: immune to other tests' logging setup (caplog missed them in the full
    suite). `at_level` mirrors caplog's context manager so the tests read the same."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    def at_level(self, *_a, **_k):
        return contextlib.nullcontext()


def gets(ex: Exchange, suffix: str) -> int:
    return sum(1 for r in ex.seen if r["method"] == "GET" and r["path"].endswith(suffix))


class Live:
    """A market_quote_fn: the production market stream's quote for TICKER (None = stale or down)."""

    def __init__(self, yes_bid=0.69, yes_ask=0.70) -> None:
        self.q: dict | None = None
        self.set(yes_bid, yes_ask)

    def set(self, yes_bid, yes_ask) -> None:
        self.q = {"ticker": TICKER, "yes_bid": yes_bid, "yes_ask": yes_ask, "no_bid": round(1 - yes_ask, 2),
                  "no_ask": round(1 - yes_bid, 2), "status": "active", "open_ts": W0, "close_ts": CLOSE}

    def __call__(self, env: str, ticker: str, now: int) -> dict | None:
        return dict(self.q) if self.q and env == "prod" and ticker == TICKER else None


def you(store, m: Market | None = None, live: Live | None = None):
    ex = Exchange(m or Market(yes_bid=0.69, no_bid=0.30))  # YES 69c / 70c
    return svc_for(store, ex, "you", market_quote_fn=live), ex


# -- quotes ------------------------------------------------------------------------------------------------------
def test_prepare_and_approve_use_the_fresh_websocket_book_and_never_read_the_rest_book(store, caplog):
    async def go():
        live = Live()
        svc, ex = you(store, live=live)
        await up(svc)
        with caplog.at_level(logging.INFO, logger="btc_signal_engine.application.trading"):
            r = await enter(svc, pnw(take_profit=0.85))
        assert r["ok"] and r["bracket"]["state"] == "open", r
        assert gets(ex, "/orderbook") == 0 and gets(ex, f"/markets/{TICKER}") == 0
        assert "you prepare quote KXBTC15M-26OCT281315-15: ws" in caplog.text
        assert "you approve quote KXBTC15M-26OCT281315-15: ws" in caplog.text
        assert "you prepare timing: quote" in caplog.text and "(ws), gate" in caplog.text
        assert "you approve timing: lock" in caplog.text and "send" in caplog.text
    run(go())


def test_a_stale_websocket_book_falls_back_to_rest_and_says_so(store, caplog):
    async def go():
        live = Live()
        live.q = None  # stream down or older than stream_quote_max_age_ms
        svc, ex = you(store, live=live)
        await up(svc)
        with caplog.at_level(logging.INFO, logger="btc_signal_engine.application.trading"):
            r = await svc.prepare(pnw(take_profit=0.85), NOW)
        assert r["ok"], r
        assert gets(ex, "/orderbook") == 1
        assert "you prepare quote KXBTC15M-26OCT281315-15: rest" in caplog.text
    run(go())


def test_the_approve_recheck_reads_the_same_live_book(store):
    """The REST book still shows 70c, the live book has moved to 75c: a 71c limit is refused at approval."""
    async def go():
        live = Live()
        svc, ex = you(store, live=live)
        await up(svc)
        prep = await svc.prepare(pnw(take_profit=0.85), NOW)
        assert prep["ok"], prep
        live.set(0.74, 0.75)
        r = await svc.approve(prep["token"], "yes", prep["ticket"], NOW + 500)
        assert not r["ok"] and r["code"] == "price_moved" and "75" in r["reason"], r
        assert not ex.posts()
    run(go())


class FakeStream:
    def __init__(self) -> None:
        self.tickers: set[str] = set()

    def subscribe_tickers(self, tickers) -> None:
        self.tickers = set(tickers)

    def health(self) -> dict:
        return {"connected": True, "stale": False}


def test_the_trading_streams_quote_is_used_with_cached_market_metadata(store, caplog):
    """No market_quote_fn (e.g. demo): the trading stream's prices plus GET /markets/{t} metadata (cached), never the
    REST order book while the stream quote is fresh."""
    async def go():
        svc, ex = you(store)
        await up(svc)
        svc.attach_stream("prod", FakeStream(), NOW)
        await svc.on_stream_event({"env": "prod", "type": "quote", "ticker": TICKER, "yes_bid": 0.69,
                                   "yes_ask": 0.70, "no_bid": 0.30, "no_ask": 0.31}, NOW)
        with caplog.at_level(logging.INFO, logger="btc_signal_engine.application.trading"):
            prep = await svc.prepare(pnw(take_profit=0.85), NOW + 100)
            assert prep["ok"], prep
            await svc.on_stream_event({"env": "prod", "type": "quote", "ticker": TICKER, "yes_bid": 0.69,
                                       "yes_ask": 0.70, "no_bid": 0.30, "no_ask": 0.31}, NOW + 400)
            r = await svc.approve(prep["token"], "yes", prep["ticket"], NOW + 500)
        assert r["ok"], r
        assert gets(ex, "/orderbook") == 0 and gets(ex, f"/markets/{TICKER}") == 1  # metadata once, then cached
        assert "you prepare quote KXBTC15M-26OCT281315-15: ws" in caplog.text
        # a stale stream quote (older than stream_quote_max_age_ms) is not used: REST again
        prep2 = await svc.prepare(pnw(take_profit=0.85), NOW + 5_000)
        assert gets(ex, "/orderbook") == 1, prep2
    run(go())


# -- the exit path ---------------------------------------------------------------------------------------------------
async def opened(store, live: Live | None = None, tp: float = 0.85):
    m = Market(yes_bid=0.69, no_bid=0.30)
    svc, ex = you(store, m, live)
    await up(svc)
    r = await enter(svc, pnw(take_profit=tp))
    assert r["ok"] and r["bracket"]["state"] == "open", r
    return svc, ex, m, r["bracket"]


def test_an_exit_whose_auto_sell_filled_before_the_approval_says_so_and_sends_nothing(store):
    async def go():
        svc, ex, m, b = await opened(store)
        prep = await svc.prepare_exit(b["id"], NOW + 1000)
        assert prep["ok"], prep
        m.set(0.86, 0.13)  # bids through the 85c auto-sell
        await svc.tick(NOW + 4000)
        row = store.select("trading_brackets")[0]
        assert row["state"] == "closed" and row["exit_reason"] == "tp", row
        posts = len(ex.posts())
        r = await svc.approve(prep["token"], "yes", prep["ticket"], NOW + 4500)
        assert not r["ok"] and r["code"] == "already_closed", r
        pnl = round(row["proceeds"] - row["tp_fees"] - row["exit_fees"] - row["entry_cost"], 2)
        assert r["reason"] == f"Your auto-sell already sold {row['tp_filled']:.2f} @ 85¢ (+${pnl:.2f}).", r
        assert len(ex.posts()) == posts  # nothing sent
        again = await svc.prepare_exit(b["id"], NOW + 5000)  # a new slide on the closed position: same answer
        assert again == {**r, "bracket": again["bracket"]} and again["bracket"]["state"] == "closed"
        assert [a["code"] for a in svc.audit() if a["event"] == "exit_not_needed"] == ["already_closed"] * 2
    run(go())


def test_an_auto_sell_that_fills_while_the_exit_cancels_it_is_reported_not_sold_again(store):
    async def go():
        live = Live()
        svc, ex, m, b = await opened(store, live)
        prep = await svc.prepare_exit(b["id"], NOW + 1000)
        assert prep["ok"], prep
        m.set(0.86, 0.13)  # the exchange fills the 85c auto-sell; our monitor has not seen it yet
        live.set(0.86, 0.87)
        r = await svc.approve(prep["token"], "yes", prep["ticket"], NOW + 1500)
        assert not r["ok"] and r["code"] == "already_closed", r
        assert r["reason"].startswith("Your auto-sell already sold ") and "@ 85¢ (+$" in r["reason"], r
        assert not [p for p in ex.posts() if p.get("reduce_only")]  # no sale of our own was sent
    run(go())


def test_a_stop_that_already_fired_is_reported(store):
    async def go():
        svc, ex, m, b = await opened(store)
        bk = svc._brackets[b["id"]]
        bk["state"] = "stopping"  # the stop fired and is still selling
        r = await svc.prepare_exit(b["id"], NOW + 1000)
        assert not r["ok"] and r["code"] == "already_closing" and "stop already fired" in r["reason"], r
        bk.update(state="closed", exit_reason="stop", exit_filled=bk["held"], exit_avg=0.60,
                  proceeds=round(0.60 * bk["held"], 4), held=0.0)
        svc._save(bk, NOW + 1500)
        r = await svc.prepare_exit(b["id"], NOW + 2000)
        assert not r["ok"] and r["code"] == "already_closed", r
        assert r["reason"].startswith(f"Your stop already sold {bk['exit_filled']:.2f} @ 60¢ (−$"), r
    run(go())


def test_an_exit_with_an_unfilled_auto_sell_skips_the_order_list_read(store, caplog):
    """The cancel took back the whole auto-sell, so nothing filled: no order-list read between cancel and sale."""
    async def go():
        live = Live()
        svc, ex, m, b = await opened(store, live)
        n0 = len(ex.seen)
        with caplog.at_level(logging.INFO, logger="btc_signal_engine.application.trading"):
            prep = await svc.prepare_exit(b["id"], NOW + 1000)
            n = len(ex.seen)
            r = await svc.approve(prep["token"], "yes", prep["ticket"], NOW + 1500)
        assert n == n0  # prepare_exit read the live book: no request at all
        assert r["ok"] and r["bracket"]["state"] == "closed" and r["bracket"]["exit_reason"] == "exit", r
        after = [(x["method"], x["path"].rsplit("/v2", 1)[-1]) for x in ex.seen[n:]]
        assert after[0][0] == "DELETE" and after[1][0] == "POST", after  # cancel, then the sale
        assert not any(p == "/portfolio/orders" for _, p in after[:2])
        cancelled = [a for a in svc.audit() if a["event"] == "cancelled"][0]
        assert cancelled["detail"]["fills_from"] == "cancel"
        assert "you exit quote KXBTC15M-26OCT281315-15: ws" in caplog.text
        assert "you prepare_exit timing: quote" in caplog.text and "you approve timing: lock" in caplog.text
    run(go())


def test_the_positions_backup_poll_slows_to_the_backup_cadence_while_the_feed_is_healthy(store):
    """Manual-exit detection: every external_check_ms (5 s) without a live feed; every reconcile_backup_ms (10 s)
    with one (a fill on the account pulls the check forward to within 1 s)."""
    async def go():
        svc, ex, m, b = await opened(store)
        def reads():
            return gets(ex, "/portfolio/positions")
        n = reads()
        for t in range(1, 21):
            await svc.tick(NOW + 1000 * t)
        no_feed = reads() - n
        svc.attach_stream("prod", FakeStream(), NOW + 21_000)
        n = reads()
        for t in range(22, 42):
            await svc.on_stream_event({"env": "prod", "type": "quote", "ticker": TICKER, "yes_bid": 0.69,
                                       "yes_ask": 0.70, "no_bid": 0.30, "no_ask": 0.31}, NOW + 1000 * t)
            await svc.tick(NOW + 1000 * t)
        with_feed = reads() - n
        assert no_feed >= 4 and with_feed <= 2 and with_feed < no_feed, (no_feed, with_feed)
    run(go())
