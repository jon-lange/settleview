"""Live feed: Kalshi WebSocket stream (primary) with REST polling as the backup.

Adapter tests replay REAL messages captured 2026-10-28 from read-only subscriptions (no orders) under
tests/fixtures/kalshi_ws/:
  prod_orderbook_yes_price.jsonl  production, KXBTC15M, orderbook_delta with use_yes_price=true (+ ticker for a check)
  demo_subscribe_yes_price.jsonl  demo, the same subscription on a quiet book
  demo_control_replies.jsonl      demo, replies to subscribe / update_subscription / unsubscribe / a bad subscribe
No order was placed, so no `fill` or `user_order` message could be captured; those two tests use Kalshi's own
published examples (https://docs.kalshi.com/asyncapi.yaml, messages `fill` and `userOrder`), inlined below.
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from btc_signal_engine.adapters import retry
from btc_signal_engine.adapters.kalshi.shadow import ShadowExchange, ShadowStream, book_quote
from btc_signal_engine.adapters.kalshi.stream import WS_PATH, WS_URLS, KalshiStream, fill_event
from btc_signal_engine.adapters.kalshi.trading import TradingAuth, TradingViolation, normalize_order
from btc_signal_engine.application.trading import TradingConfig
from tests.trading.helpers import NOW, TICKER, Exchange, Market, proposal, ready, service

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "kalshi_ws"
PROD_TICKER = "KXBTC15M-26OCT281715-15"
DOC_FILL = {"type": "fill", "sending_ts_ms": 1671899397123, "sid": 13, "msg": {
    "trade_id": "d91bc706-ee49-470d-82d8-11418bda6fed", "order_id": "ee587a1c-8b87-4dcf-b721-9f6f790619fa",
    "client_order_id": "my-order-1", "market_ticker": "HIGHNY-22DEC23-B53.5", "exchange_index": 2, "is_taker": True,
    "side": "yes", "yes_price_dollars": "0.7500", "count_fp": "278.00", "fee_cost": "0.010000", "action": "buy",
    "ts": 1671899397, "ts_ms": 1671899397000, "post_position_fp": "500.00", "purchased_side": "yes",
    "outcome_side": "yes", "book_side": "bid", "subaccount": 3}}
DOC_USER_ORDER = {"type": "user_order", "sending_ts_ms": 1669149841234, "sid": 22, "msg": {
    "order_id": "ee587a1c-8b87-4dcf-b721-9f6f790619fa", "user_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "ticker": "FED-23DEC-T3.00", "exchange_index": 2, "status": "resting", "side": "yes", "is_yes": True,
    "outcome_side": "yes", "book_side": "bid", "yes_price_dollars": "0.3500", "fill_count_fp": "0.00",
    "remaining_count_fp": "10.00", "initial_count_fp": "10.00", "taker_fill_cost_dollars": "0.000000",
    "maker_fill_cost_dollars": "0.000000", "taker_fees_dollars": "0.000000", "maker_fees_dollars": "0.000000",
    "client_order_id": "my-order-1", "order_group_id": "og_123", "self_trade_prevention_type": "taker_at_cross",
    "created_time": "2024-12-01T10:00:00Z", "created_ts_ms": 1733047200000}}
QUOTE_KEYS = {"type", "env", "ticker", "yes_bid", "yes_ask", "no_bid", "no_ask", "ts_ms"}


def run(coro):
    return asyncio.run(coro)


def lines(name: str) -> list[str]:
    return [ln for ln in (FIX / name).read_text(encoding="utf-8").splitlines() if ln.strip()]


CLOSED = object()


class FakeWS:
    def __init__(self, msgs=()):
        self.q: asyncio.Queue = asyncio.Queue()
        for m in msgs:
            self.q.put_nowait(m)
        self.sent: list[dict] = []
        self.closed = False

    def feed(self, *msgs):
        for m in msgs:
            self.q.put_nowait(m if isinstance(m, str) or m is CLOSED else json.dumps(m))

    async def send(self, s):
        self.sent.append(json.loads(s))

    async def recv(self):
        m = await self.q.get()
        if m is CLOSED:
            raise ConnectionError("closed by peer")
        return m

    async def close(self):
        self.closed = True


class Rig:
    """A KalshiStream on fake sockets; collects events."""

    def __init__(self, *sockets: FakeWS, env="prod", tickers=(PROD_TICKER,)):
        self.sockets, self.events, self.sleeps, self.headers = list(sockets), [], [], []
        self.key = ed25519.Ed25519PrivateKey.generate()
        self.clock = [NOW]

        async def connect(url, headers):
            self.headers.append((url, headers))
            if not self.sockets:
                raise ConnectionError("no more sockets")
            return self.sockets.pop(0)

        async def sleep(s):
            self.sleeps.append(s)
            await asyncio.sleep(0)

        self.stream = KalshiStream(env, TradingAuth("kid", self.key), self.events.append, connect=connect,
                                   clock_ms=lambda: self.clock[0], requote_ms=10**9, sleep=sleep)
        self.stream.subscribe_tickers(tickers)

    def of(self, kind):
        return [e for e in self.events if e["type"] == kind]


async def until(cond, timeout=3.0):
    for _ in range(int(timeout / 0.005)):
        if cond():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition not reached")


async def started(rig: Rig):
    task = asyncio.create_task(rig.stream.run())
    return task


async def stop(rig: Rig, task):
    rig.stream.close()
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


# -- adapter: auth, endpoints ----------------------------------------------------------------------
def test_ws_endpoints_and_signed_handshake_only_for_the_ws_path():
    assert WS_URLS == {"prod": "wss://external-api-ws.kalshi.com/trade-api/ws/v2",
                       "demo": "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2"}
    key = ed25519.Ed25519PrivateKey.generate()
    auth = TradingAuth("kid", key, clock_ms=lambda: 1700000000000)
    h = auth.headers("GET", WS_PATH)
    assert h["KALSHI-ACCESS-KEY"] == "kid" and h["KALSHI-ACCESS-TIMESTAMP"] == "1700000000000"
    key.public_key().verify(base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]), b"1700000000000GET/trade-api/ws/v2")
    for method, path in [("POST", WS_PATH), ("GET", WS_PATH + "/x"), ("GET", "/trade-api/ws/v1")]:
        with pytest.raises(TradingViolation):
            auth.headers(method, path)
    with pytest.raises(ValueError):
        KalshiStream("shadow", auth, lambda e: None)


# -- adapter: parsing real captures ------------------------------------------------------------------
def test_prod_orderbook_capture_rebuilds_the_same_top_of_book_as_kalshis_ticker():
    async def go():
        msgs = lines("prod_orderbook_yes_price.jsonl")
        ticker_msg = json.loads(msgs[-1])
        assert ticker_msg["type"] == "ticker"
        rig = Rig(FakeWS(msgs))
        task = await started(rig)
        await until(lambda: rig.stream._seq.get(3) == 286)
        sent = rig.headers
        assert sent[0][0] == WS_URLS["prod"] and set(sent[0][1]) == {
            "KALSHI-ACCESS-KEY", "KALSHI-ACCESS-TIMESTAMP", "KALSHI-ACCESS-SIGNATURE"}
        q = rig.of("quote")[-1]
        tm = ticker_msg["msg"]
        assert (q["yes_bid"], q["yes_ask"]) == (float(tm["yes_bid_dollars"]), float(tm["yes_ask_dollars"]))
        assert (q["no_bid"], q["no_ask"]) == (round(1 - q["yes_ask"], 4), round(1 - q["yes_bid"], 4))
        assert set(q) == QUOTE_KEYS and q["env"] == "prod" and q["ticker"] == PROD_TICKER
        # a quote is emitted only when the top of book changes, not on every one of the 289 deltas
        assert 1 <= len(rig.of("quote")) < 100
        assert rig.stream.health()["connected"] and not rig.stream.health()["stale"]
        await stop(rig, task)
    run(go())


def test_subscribe_commands_and_a_quiet_demo_book():
    async def go():
        ws = FakeWS(lines("demo_subscribe_yes_price.jsonl"))
        rig = Rig(ws, env="demo")
        task = await started(rig)
        await until(lambda: rig.stream._seq.get(3) == 3)
        cmds = [(c["cmd"], c["params"]) for c in ws.sent]
        assert cmds[:3] == [("subscribe", {"channels": ["fill"]}), ("subscribe", {"channels": ["user_orders"]}),
                            ("subscribe", {"channels": ["orderbook_delta"], "market_tickers": [PROD_TICKER],
                                           "use_yes_price": True})]
        quotes = rig.of("quote")
        # snapshot: best YES bid 1c; "no" levels are YES asks in YES prices (91c, 90c, 89c) -> ask 89c.
        # The two deltas take 1 contract each off 89c (3 -> 1): the top is unchanged, so no new quote.
        assert len(quotes) == 1 and (quotes[0]["yes_bid"], quotes[0]["yes_ask"]) == (0.01, 0.89)
        assert rig.stream._books[PROD_TICKER].asks[0.89] == 1.0
        assert [e["type"] for e in rig.events][:2] == ["resync", "health"]  # connected: catch up by polling
        await stop(rig, task)
    run(go())


def test_control_replies_take_seq_numbers_and_errors_do_not_break_the_stream():
    async def go():
        ctl = [json.loads(x) for x in lines("demo_control_replies.jsonl")]
        assert [m["type"] for m in ctl] == ["subscribed"] * 4 + ["unsubscribed", "ok", "ok", "error"]
        snap = json.loads(lines("demo_subscribe_yes_price.jsonl")[3])
        ws = FakeWS()
        ws.feed(*ctl[:3], snap, *[m for m in ctl[5:]])  # replies carry seq 6, 7, 8 on sid 3 after seq 1: a gap
        rig = Rig(ws, env="demo")
        task = await started(rig)
        await until(lambda: rig.stream._seq.get(3) == 8)
        assert rig.stream.health()["stale"]  # 1 -> 6 is a gap: the book is no longer trusted
        assert ws.sent[-1]["cmd"] == "update_subscription" and ws.sent[-1]["params"]["action"] == "get_snapshot"
        await stop(rig, task)
    run(go())


def test_user_order_and_fill_messages_normalize_like_rest():
    async def go():
        rig = Rig(FakeWS())
        await rig.stream.handle(DOC_USER_ORDER)
        await rig.stream.handle(DOC_FILL)
        o, f = rig.of("order")[0], rig.of("fill")[0]
        assert o["order"] == normalize_order(DOC_USER_ORDER["msg"])
        assert o["order"]["status"] == "resting" and o["order"]["remaining"] == 10.0 and o["env"] == "prod"
        assert f == {**fill_event(DOC_FILL["msg"]), "env": "prod"}
        assert (f["order_id"], f["count"], f["yes_price"], f["fee"], f["action"]) == (
            "ee587a1c-8b87-4dcf-b721-9f6f790619fa", 278.0, 0.75, 0.01, "buy")
    run(go())


# -- adapter: gaps, reconnects, subscriptions ------------------------------------------------------
def test_seq_gap_marks_stale_requests_a_snapshot_and_recovers():
    async def go():
        msgs = [json.loads(x) for x in lines("prod_orderbook_yes_price.jsonl")]
        head = [m for m in msgs if m["type"] in ("subscribed", "orderbook_snapshot")]
        deltas = [m for m in msgs if m["type"] == "orderbook_delta"]
        ws = FakeWS()
        ws.feed(*head, deltas[0])  # seq 1 (snapshot), 2
        rig = Rig(ws)
        task = await started(rig)
        await until(lambda: rig.stream._seq.get(3) == 2)
        n_quotes = len(rig.of("quote"))
        ws.feed({**deltas[5], "seq": 9})  # seq 3..8 lost
        await until(lambda: rig.stream.health()["stale"])
        assert ws.sent[-1] == {"id": ws.sent[-1]["id"], "cmd": "update_subscription",
                               "params": {"sids": [3], "market_tickers": [PROD_TICKER], "action": "get_snapshot"}}
        assert rig.of("health")[-1]["stale"] is True
        ws.feed({**deltas[6], "seq": 10})  # deltas while stale are ignored (no book), no quotes
        await until(lambda: rig.stream._seq.get(3) == 10)
        assert len(rig.of("quote")) == n_quotes
        snap = next(m for m in msgs if m["type"] == "orderbook_snapshot")
        ws.feed({**snap, "id": ws.sent[-1]["id"], "seq": 11})
        await until(lambda: not rig.stream.health()["stale"])
        assert len(rig.of("quote")) == n_quotes + 1 and rig.of("health")[-1]["stale"] is False
        await stop(rig, task)
    run(go())


def test_reconnects_with_backoff_and_resubscribes(monkeypatch):
    # reconnect waits are full-jitter draws (adapters/retry.py); at the top of each draw they are the ceilings
    monkeypatch.setattr(retry, "_uniform", lambda: 1.0)
    async def go():
        head = [json.loads(x) for x in lines("prod_orderbook_yes_price.jsonl")[:5]]
        a, b = FakeWS(), FakeWS()
        a.feed(*head, CLOSED)
        b.feed(*head)
        rig = Rig(a, b)
        task = await started(rig)
        await until(lambda: rig.stream.connects == 2 and rig.stream.health()["connected"])
        assert a.closed and rig.sleeps == [1.0]
        assert [c["cmd"] for c in b.sent][:3] == ["subscribe"] * 3
        health = [(h["connected"], h["stale"]) for h in rig.of("health")]
        assert health == [(True, False), (False, True), (True, False)]
        assert [e["reason"] for e in rig.of("resync")] == ["connected", "connected"]
        await stop(rig, task)
        # a refused connection keeps backing off (doubling, capped)
        rig2 = Rig()
        t2 = await started(rig2)
        await until(lambda: len(rig2.sleeps) >= 7)
        assert rig2.sleeps[:7] == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]
        await stop(rig2, t2)
    run(go())


def test_markets_are_added_and_removed_on_the_live_subscription():
    async def go():
        head = [json.loads(x) for x in lines("prod_orderbook_yes_price.jsonl")[:5]]
        ws = FakeWS(json.dumps(m) for m in head)
        rig = Rig(ws)
        task = await started(rig)
        await until(lambda: rig.stream._seq.get(3) == 1)
        rig.stream.subscribe_tickers({PROD_TICKER, "KXBTC15M-26OCT281730-30"})
        await until(lambda: ws.sent[-1]["cmd"] == "update_subscription")
        assert ws.sent[-1]["params"] == {"sids": [3], "market_tickers": ["KXBTC15M-26OCT281730-30"],
                                         "action": "add_markets"}
        rig.stream.subscribe_tickers({"KXBTC15M-26OCT281730-30"})
        await until(lambda: ws.sent[-1]["params"].get("action") == "delete_markets")
        assert ws.sent[-1]["params"]["market_tickers"] == [PROD_TICKER] and PROD_TICKER not in rig.stream._books
        rig.stream.subscribe_tickers(())
        await until(lambda: ws.sent[-1]["cmd"] == "unsubscribe")
        assert ws.sent[-1]["params"] == {"sids": [3]}
        await stop(rig, task)
    run(go())


# -- application: idempotent transitions from the stream --------------------------------------------
class FakeStream:
    def __init__(self, env="prod"):
        self.env = env
        self.tickers: set = set()
        self.h = {"connected": True, "last_msg_ms": NOW, "stale": False}

    def subscribe_tickers(self, tickers):
        self.tickers = set(tickers)

    def health(self):
        return dict(self.h)


async def opened(store, m: Market, *, mode="prod", stream=True, **prop):
    ex = Exchange(m)
    svc = await ready(service(store, ex, mode=mode))
    env = {"prod": "prod", "shadow": "shadow", "sandbox": "demo"}[mode]
    st = FakeStream(env)
    if stream:
        svc.attach_stream(env, st, NOW)
    prep = await svc.prepare(proposal(**prop), NOW)
    r = await svc.approve(prep["token"], "yes", prep["ticket"], NOW + 1000)
    assert r["ok"] and r["bracket"]["state"] == "open", r
    return svc, ex, st, r["bracket"]


def capture(ex: Exchange, env="prod") -> list:
    """The simulator's order/fill events, relabelled for `env` (a real exchange's stream would carry them)."""
    got: list = []
    ex.sim.listeners.append(lambda e: got.append({**e, "env": env}))
    return got


def test_stream_subscribes_to_open_bracket_and_pending_ticket_markets(store):
    async def go():
        ex = Exchange(Market())
        svc = await ready(service(store, ex))
        st = FakeStream()
        svc.attach_stream("prod", st, NOW)
        assert st.tickers == set()
        await svc.prepare(proposal(), NOW)
        assert st.tickers == {TICKER}  # the pending ticket's market
        assert [a["event"] for a in svc.audit()].count("stream_up") == 1
    run(go())


def test_tp_fill_via_stream_closes_once_and_the_later_poll_is_a_no_op(store):
    async def go():
        m = Market()
        svc, ex, st, b = await opened(store, m)
        held = b["held"]
        ev = capture(ex)
        m.set(0.72, 0.25, size=2.0)  # the real bid trades through 70c, but only 2 contracts deep
        await ex.sim._advance(TICKER)
        m.set(0.55, 0.42)  # and comes back
        assert [e["type"] for e in ev] == ["fill", "order"]
        order_ev = ev[1]
        await svc.on_stream_event(order_ev, NOW + 2000)
        row = store.select("trading_brackets")[0]
        assert row["tp_filled"] == 2.0 and row["held"] == round(held - 2.0, 2) and row["state"] == "open"
        await svc.on_stream_event(order_ev, NOW + 2100)  # the same update twice
        await svc.on_stream_event(ev[0], NOW + 2200)  # the fill: re-reads the order's cumulative state
        svc._force_poll("prod")
        await svc.tick(NOW + 3000)  # the poller (backup) reads the same order
        row = store.select("trading_brackets")[0]
        assert row["tp_filled"] == 2.0 and row["held"] == round(held - 2.0, 2)  # not applied twice
        m.set(0.72, 0.25, size=100.0)
        ev.clear()
        await ex.sim._advance(TICKER)
        await svc.on_stream_event(ev[-1], NOW + 4000)
        row = store.select("trading_brackets")[0]
        assert row["state"] == "closed" and row["exit_reason"] == "tp" and row["held"] == 0
        assert row["tp_filled"] == pytest.approx(held)
        await svc.on_stream_event(ev[-1], NOW + 4100)
        await svc.tick(NOW + 5000)
        events = [a["event"] for a in svc.audit()]
        assert events.count("tp_filled") == 1 and ex.sim.positions[TICKER] == 0
        assert len(ex.writes()) == 2  # entry + take profit: nothing else was ever sent
    run(go())


def test_a_stale_stream_update_after_the_poll_changes_nothing(store):
    async def go():
        m = Market()
        svc, ex, st, b = await opened(store, m)
        ev = capture(ex)
        m.set(0.72, 0.25, size=2.0)
        await ex.sim._advance(TICKER)
        m.set(0.72, 0.25, size=1.0)
        await ex.sim._advance(TICKER)  # 3 filled in all
        m.set(0.55, 0.42)
        svc._force_poll("prod")
        await svc.tick(NOW + 2000)  # the poller sees filled = 3 first
        row = store.select("trading_brackets")[0]
        assert row["tp_filled"] == 3.0
        for e in ev:  # then the stream's older updates (filled 2, then 3) arrive
            await svc.on_stream_event(e, NOW + 2500)
        row = store.select("trading_brackets")[0]
        assert row["tp_filled"] == 3.0 and row["held"] == round(b["held"] - 3.0, 2)
    run(go())


def test_stop_from_a_stream_quote_sells_once(store):
    async def go():
        m = Market()
        svc, ex, st, b = await opened(store, m)
        m.set(0.43, 0.50)
        q = {"type": "quote", "env": "prod", "ticker": TICKER, "yes_bid": 0.43, "yes_ask": 0.50, "no_bid": 0.50,
             "no_ask": 0.57, "ts_ms": NOW + 2000}
        await svc.on_stream_event(q, NOW + 2000)
        w = ex.writes()
        assert [x["method"] for x in w] == ["POST", "POST", "DELETE", "POST"]
        assert w[3]["body"]["reduce_only"] is True and w[3]["body"]["price"] == "0.3800"
        for t in (2100, 2200):
            await svc.on_stream_event({**q, "yes_bid": 0.40}, NOW + t)
        await svc.tick(NOW + 3000)
        assert len(ex.writes()) == 4
        row = store.select("trading_brackets")[0]
        assert row["state"] == "closed" and row["exit_reason"] == "stop" and ex.sim.positions[TICKER] == 0
        assert [a["event"] for a in svc.audit()].count("stop_triggered") == 1
    run(go())


def test_stream_quote_replaces_rest_while_fresh_and_healthy(store):
    async def go():
        m = Market()
        svc, ex, st, b = await opened(store, m)
        markets = lambda: sum(1 for r in ex.seen if r["method"] == "GET" and r["path"].endswith("/orderbook"))  # noqa: E731
        q = {"type": "quote", "env": "prod", "ticker": TICKER, "yes_bid": 0.55, "yes_ask": 0.58, "no_bid": 0.42,
             "no_ask": 0.45, "ts_ms": NOW}
        await svc.on_stream_event(q, NOW + 2000)
        before = markets()
        await svc.tick(NOW + 3000)
        assert markets() == before  # fresh stream quote: no REST quote
        await svc.tick(NOW + 4500)  # 2.5 s old: REST backup
        assert markets() == before + 1
        await svc.on_stream_event(q, NOW + 5000)
        st.h["stale"] = True  # e.g. a sequence gap
        await svc.tick(NOW + 5500)
        assert markets() == before + 2
        s = svc.status(NOW + 5500)["stream"]["prod"]
        assert s["source"] == "polling" and s["stale"] is True
    run(go())


def test_stream_down_restores_the_polling_cadence_and_is_audited(store):
    async def go():
        m = Market()
        svc, ex, st, b = await opened(store, m)
        polls = lambda: sum(1 for r in ex.seen if r["method"] == "GET" and r["path"].endswith(  # noqa: E731
            "/portfolio/orders") and "status=" not in r["query"])  # take-profit reads use the list endpoint
        await svc.tick(NOW + 2000)
        p0 = polls()
        for t in range(3000, 11000, 1000):  # healthy: the take profit is re-polled every 10 s, not every 2 s
            await svc.tick(NOW + t)
        assert polls() - p0 <= 1
        assert svc.status(NOW + 11000)["stream"]["prod"]["source"] == "websocket"
        st.h.update(connected=False, stale=True)
        await svc.tick(NOW + 12000)
        p1 = polls()
        for t in range(13000, 21000, 1000):
            await svc.tick(NOW + t)
        assert polls() - p1 >= 3  # every 2 s again
        assert svc.status(NOW + 21000)["stream"]["prod"]["source"] == "polling"
        st.h.update(connected=True, stale=False)
        p2 = polls()
        await svc.on_stream_event({"type": "health", "env": "prod", **st.h}, NOW + 21500)
        await svc.tick(NOW + 22000)
        assert polls() == p2 + 1  # back up: one catch-up poll at once
        ev = [a["event"] for a in reversed(svc.audit())]
        assert [e for e in ev if e.startswith("stream_")] == ["stream_up", "stream_down", "stream_up"]
    run(go())


def test_gap_in_the_real_stream_falls_back_to_rest_quotes(store):
    async def go():
        msgs = [json.loads(x) for x in lines("prod_orderbook_yes_price.jsonl")]
        m = Market()
        svc, ex, _, b = await opened(store, m, stream=False, stop=0.30)  # the captured book bids ~42c
        events: list = []
        ws = FakeWS()
        rig_stream = KalshiStream("prod", TradingAuth("kid", ed25519.Ed25519PrivateKey.generate()), events.append,
                                  connect=lambda url, h: _ret(ws), clock_ms=lambda: NOW, requote_ms=10**9)
        svc.attach_stream("prod", rig_stream, NOW)
        relabel = [{**x, "msg": {**x["msg"], "market_ticker": TICKER}} if x["type"].startswith("orderbook")
                   else x for x in msgs if x["type"] != "ticker"]
        ws.feed(*relabel[:20])
        task = asyncio.create_task(rig_stream.run())
        await until(lambda: rig_stream._seq.get(3) == 16)
        for e in events:
            await svc.on_stream_event(e, NOW + 2000)
        assert svc.stream_ok("prod") and svc.stream_quote("prod", TICKER, NOW + 2000) is not None
        ws.feed({**relabel[25], "seq": 40})
        await until(lambda: rig_stream.health()["stale"])
        assert svc.stream_quote("prod", TICKER, NOW + 2100) is None  # stale: the REST quote is used
        markets = sum(1 for r in ex.seen if r["path"].endswith("/orderbook"))
        await svc.tick(NOW + 2200)
        assert sum(1 for r in ex.seen if r["path"].endswith("/orderbook")) == markets + 1
        rig_stream.close()
        task.cancel()
    run(go())


class LossyPosts(Exchange):
    """Order POSTs reach the exchange but their responses are lost while `lossy` (the outcome is unknown)."""

    lossy = True

    async def handler(self, request):
        resp = await super().handler(request)
        if self.lossy and request.method == "POST":
            raise httpx.ReadTimeout("lost", request=request)
        return resp


async def _ret(x):
    return x


def test_unknown_entry_is_resolved_from_a_stream_order_update(store):
    async def go():
        m = Market()
        ex = LossyPosts(m)
        svc = await ready(service(store, ex))
        svc.attach_stream("prod", FakeStream(), NOW)
        ev = capture(ex)
        prep = await svc.prepare(proposal(), NOW)
        r = await svc.approve(prep["token"], "yes", prep["ticket"], NOW + 1000)
        assert r["code"] == "unknown"
        entry_ev = [e for e in ev if e["type"] == "order"][0]
        ex.lossy = False
        await svc.on_stream_event(entry_ev, NOW + 1500)
        row = store.select("trading_brackets")[0]
        assert row["state"] == "open" and row["tp_order_id"]
        await svc.on_stream_event(entry_ev, NOW + 1600)  # again: no second take profit
        await svc.tick(NOW + 2000)
        coids = {p["client_order_id"] for p in ex.posts()}  # lost responses were retried with the same id
        assert len(coids) == 2 and ex.posts()[-1]["time_in_force"] == "good_till_canceled"
        assert sorted(o["book_side"] for o in ex.sim.orders.values()) == ["ask", "bid"]  # one entry, one TP
    run(go())


# -- shadow: same events, same code path -------------------------------------------------------------
def test_shadow_emits_the_same_event_shapes_as_the_kalshi_stream():
    async def go():
        m = Market()
        ex = ShadowExchange(book_fn=m.book_fn, market_fn=m.market_fn, clock_ms=lambda: NOW)
        got: list = []
        sst = ShadowStream(ex, got.append, clock_ms=lambda: NOW)
        sst.subscribe_tickers({TICKER})
        ex.listeners.append(got.append)
        await sst.poll_once()
        await ex._create({"ticker": TICKER, "client_order_id": "c1", "side": "bid", "count": "2.00",
                          "price": "0.5800", "time_in_force": "immediate_or_cancel"})
        q = next(e for e in got if e["type"] == "quote")
        assert set(q) == QUOTE_KEYS and (q["yes_bid"], q["yes_ask"]) == (0.55, 0.58) and q["env"] == "shadow"
        f = next(e for e in got if e["type"] == "fill")
        assert set(f) == set(fill_event(DOC_FILL["msg"])) | {"env"}
        o = next(e for e in got if e["type"] == "order")
        assert set(o["order"]) == set(normalize_order(DOC_USER_ORDER["msg"]))
        assert o["order"]["status"] == "executed" and o["order"]["filled"] == 2.0
        assert book_quote(TICKER, await m.book_fn(TICKER), NOW)["no_bid"] == 0.42
        assert sst.health() == {"connected": False, "last_msg_ms": NOW, "stale": True}  # not running
    run(go())


def test_shadow_stream_drives_the_bracket_through_the_identical_application_path(store):
    async def go():
        m = Market()
        ex = Exchange(m)
        svc = await ready(service(store, ex, mode="shadow"))
        events: list = []
        sst = ShadowStream(ex.sim, events.append, clock_ms=lambda: NOW)
        ex.sim.listeners.append(events.append)
        sst._running = True
        svc.attach_stream("shadow", sst, NOW)
        prep = await svc.prepare(proposal(), NOW)
        r = await svc.approve(prep["token"], "yes", prep["ticket"], NOW + 1000)
        assert r["ok"] and r["bracket"]["state"] == "open"
        m.set(0.72, 0.25)
        events.clear()
        await sst.poll_once()  # real-book poll: quote + the resting take profit fills
        assert {e["type"] for e in events} >= {"quote", "fill", "order", "health"}
        for e in events:
            await svc.on_stream_event(e, NOW + 2000)
        row = store.select("trading_brackets")[0]
        assert row["state"] == "closed" and row["exit_reason"] == "tp"
        assert svc.stream_ok("shadow") and svc.status(NOW + 2000)["stream"]["shadow"]["source"] == "websocket"
    run(go())


def test_config_defaults_keep_polling_as_the_backup():
    c = TradingConfig()
    assert (c.reconcile_every_ms, c.reconcile_backup_ms, c.stream_quote_max_age_ms) == (2000, 10000, 2000)


# -- the Coach's price feed: market data only, least-privilege signer ----------------------------------------
def test_market_data_stream_subscribes_only_the_book_and_signs_only_the_handshake():
    from btc_signal_engine.adapters.kalshi.stream import WsOnlyAuth

    async def go():
        key = ed25519.Ed25519PrivateKey.generate()
        auth = WsOnlyAuth("ro", key, clock_ms=lambda: 1700000000000)
        h = auth.headers("GET", WS_PATH)
        key.public_key().verify(base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]), b"1700000000000GET/trade-api/ws/v2")
        for method, path in [("GET", "/trade-api/v2/portfolio/orders"), ("POST", "/trade-api/v2/portfolio/events/orders"),
                             ("POST", WS_PATH)]:
            with pytest.raises(TradingViolation):
                auth.headers(method, path)
        msgs = [x for x in lines("prod_orderbook_yes_price.jsonl")
                if json.loads(x)["type"] not in ("subscribed",) or json.loads(x)["msg"]["channel"] == "orderbook_delta"]
        ws = FakeWS(msgs)
        events: list = []
        st = KalshiStream("prod", auth, events.append, private=False, connect=lambda u, hd: _ret(ws),
                          clock_ms=lambda: NOW, requote_ms=10**9)
        st.subscribe_tickers({PROD_TICKER})
        task = asyncio.create_task(st.run())
        await until(lambda: st._seq.get(3) == 286)
        assert [c["params"]["channels"] for c in ws.sent] == [["orderbook_delta"]]
        assert st.health()["connected"] and not st.health()["stale"]
        assert [e for e in events if e["type"] == "quote"][-1]["yes_ask"] == 0.43
        st.close()
        task.cancel()
    run(go())
