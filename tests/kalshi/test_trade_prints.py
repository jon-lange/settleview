"""TradePrints (Play's crowd signal) and the public `trade` channel on the market-data stream.

Stream tests replay REAL `trade` messages captured 2026-10-28 from a read-only production subscription (no orders)
in tests/fixtures/kalshi_ws/prod_trade.jsonl (channel `trade`, market KXBTC15M-26OCT282030-30). Message shape per
https://docs.kalshi.com/websockets/public-trades.md."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric import ed25519

from btc_signal_engine.adapters.kalshi.prints import TradePrints
from btc_signal_engine.adapters.kalshi.stream import KalshiStream, WsOnlyAuth, trade_event

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "kalshi_ws"
TRADE_TICKER = "KXBTC15M-26OCT282030-30"
NOW = 1793233134000


def run(coro):
    return asyncio.run(coro)


def msgs() -> list[dict]:
    return [json.loads(x) for x in (FIX / "prod_trade.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


def trade(price=0.31, count=2.0, side="ask", tid="t1", block=False, ticker=TRADE_TICKER):
    return {"type": "trade", "ticker": ticker, "trade_id": tid, "yes_price": price, "count": count,
            "taker_book_side": side, "is_block_trade": block, "ts_ms": 1}


def test_prints_keep_the_last_two_minutes_drop_block_trades_and_are_copies():
    clock = Clock()
    c = TradePrints(clock_ms=clock, keep_ms=1_000)
    c.on_event({"type": "quote", "ticker": TRADE_TICKER})  # not a print: ignored
    c.on_event(trade(tid="t1"))
    c.on_event(trade(tid="blk", block=True))  # matched off the book
    got = c.recent_trades(TRADE_TICKER)
    assert [g["trade_id"] for g in got] == ["t1"]
    assert got[0]["ts_ms"] == NOW and got[0]["exchange_ts_ms"] == 1  # local arrival time; Kalshi's kept
    got[0]["count"] = 999
    assert c.recent_trades(TRADE_TICKER)[0]["count"] == 2.0
    clock.t += 600
    c.on_event(trade(tid="t2"))
    clock.t += 600
    c.on_event(trade(tid="t3", ticker="KXBTC15M-OTHER"))
    assert [g["trade_id"] for g in c.recent_trades(TRADE_TICKER)] == ["t2"]  # t1 is older than keep_ms
    clock.t += 2_000
    c.on_event(trade(tid="t4", ticker="KXBTC15M-OTHER"))
    assert c.recent_trades(TRADE_TICKER) == [] and TRADE_TICKER not in c._trades  # nothing left: market dropped


# -- the stream's trade channel ------------------------------------------------------------------------------
class FakeWS:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, s):
        self.sent.append(json.loads(s))


def stream(events, trades=True, clock=None):
    auth = WsOnlyAuth("ro", ed25519.Ed25519PrivateKey.generate())
    st = KalshiStream("prod", auth, events.append, private=False, trades=trades,
                      clock_ms=clock or (lambda: NOW), requote_ms=10**9)
    st._ws = FakeWS()
    st._connected = True
    return st


def test_trade_channel_subscribes_and_emits_real_prints_into_the_prints():
    async def go():
        events: list = []
        clock = Clock()
        st = stream(events, clock=clock)
        st.subscribe_tickers({TRADE_TICKER})
        await st._sync_trades()
        assert st._ws.sent == [{"id": 1, "cmd": "subscribe",
                                "params": {"channels": ["trade"], "market_tickers": [TRADE_TICKER]}}]
        c = TradePrints(clock_ms=clock)
        st._emit_cb = lambda e: (events.append(e), c.on_event(e))
        await st.handle(msgs()[0])  # subscribed: prints are covered from now
        clock.t += 1
        for m in msgs()[1:]:
            await st.handle(m)
        feed = [e for e in events if e["type"] == "trade_feed"]
        assert feed and feed[0]["tickers"] == [TRADE_TICKER]
        trades = [e for e in events if e["type"] == "trade"]
        assert len(trades) == 40 and not [e for e in events if e["type"] in ("trade_gap", "resync")]
        first = msgs()[1]["msg"]
        assert trades[0] == {**trade_event(first), "env": "prod"}
        assert trades[0]["yes_price"] == 0.32 and trades[0]["count"] == 0.59
        assert trades[0]["taker_book_side"] == "bid" and trades[0]["ts_ms"] == 1793233134818
        assert {e["taker_book_side"] for e in trades} == {"bid", "ask"}
        assert len(c.recent_trades(TRADE_TICKER)) == 40
        # a seq gap on the trade subscription says prints may be missing (the book subscription is untouched)
        await st.handle({"type": "trade", "sid": 1, "seq": 99, "msg": {**first, "trade_id": "late"}})
        assert events[-2]["type"] == "trade_gap" and not st.health()["stale"]
    run(go())


def test_trade_markets_follow_the_wanted_set_and_are_covered_once_acknowledged():
    async def go():
        events: list = []
        st = stream(events)
        other = "KXBTC15M-26OCT282045-45"
        st.subscribe_tickers({TRADE_TICKER})
        await st._sync_trades()
        await st.handle({"type": "subscribed", "id": 1, "msg": {"channel": "trade", "sid": 7}})
        st.subscribe_tickers({TRADE_TICKER, other})
        await st._sync_trades()
        assert st._ws.sent[-1] == {"id": 2, "cmd": "update_subscription",
                                   "params": {"sids": [7], "market_tickers": [other], "action": "add_markets"}}
        assert [e["tickers"] for e in events if e["type"] == "trade_feed"] == [[TRADE_TICKER]]
        await st.handle({"type": "ok", "id": 2, "sid": 7, "seq": 1, "msg": {}})
        assert events[-1]["type"] == "trade_feed" and events[-1]["tickers"] == sorted([TRADE_TICKER, other])
        st.subscribe_tickers({other})
        await st._sync_trades()
        assert st._ws.sent[-1]["params"]["action"] == "delete_markets"
        assert events[-1]["tickers"] == [other]
        await st.handle({"type": "trade", "sid": 7, "seq": 2, "msg": {**msgs()[1]["msg"]}})  # a dropped market
        assert events[-1]["type"] == "trade_feed"
        st.subscribe_tickers(())
        await st._sync_trades()
        assert st._ws.sent[-1] == {"id": 4, "cmd": "unsubscribe", "params": {"sids": [7]}} and \
            events[-1]["tickers"] == []
    run(go())


def test_without_trades_the_market_stream_never_subscribes_the_trade_channel():
    async def go():
        events: list = []
        st = stream(events, trades=False)
        st.subscribe_tickers({TRADE_TICKER})
        await st._sync_trades()
        assert st._ws.sent == []
        await st.handle(msgs()[1])
        assert events == [] or all(e["type"] != "trade" for e in events)
    run(go())

