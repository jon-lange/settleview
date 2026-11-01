"""Kalshi perps (margin) WebSocket stream and quote cache (perps plan U4).

Public messages are REAL: tests/fixtures/kalshi_margin_ws/public_capture.json, captured read-only on demo
2026-10-30 (subscribed, orderbook_snapshot, orderbook_delta, ticker, trade for KXBTCPERP1). The demo account is not
margin-enabled, so no fill or user_order could be captured: those come from spec_examples_private.json, built from
Kalshi's perps asyncapi schema (marked as such in the file). Everything runs on fake sockets; nothing touches a network.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path

import pytest

from btc_signal_engine.adapters.kalshi.margin_stream import MARGIN_WS_PATH, MARGIN_WS_URLS, MarginStream
from btc_signal_engine.domain.markets import perp

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "kalshi_margin_ws"
CAPTURE = [e["msg"] for e in json.loads((FIX / "public_capture.json").read_text(encoding="utf-8"))]
SPEC = json.loads((FIX / "spec_examples_private.json").read_text(encoding="utf-8"))
SPEC_PERP = perp("kalshi_btc_perp")
TICKER = "KXBTCPERP1"
NOW = 1_793_347_175_000
CLOSED = object()


def acks(*channels, start_sid=1):
    return [{"type": "subscribed", "id": 1, "msg": {"channel": c, "sid": start_sid + i}}
            for i, c in enumerate(channels)]


ALL_ACKS = acks("orderbook_delta", "ticker", "trade", "fill", "user_orders")


@pytest.fixture(autouse=True)
def top_of_the_jitter(jitter_top):
    """Reconnect waits are full-jitter draws (tenacity, adapters/backoff.py); at the top of each draw they are the
    backoff ceilings themselves (1, 2, 4 ... 30 s), which these tests check exactly."""


class FakeWS:
    def __init__(self, *msgs):
        self.q: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []
        self.feed(*msgs)

    def feed(self, *msgs):
        for m in msgs:
            self.q.put_nowait(m if m is CLOSED else json.dumps(m))

    async def send(self, s):
        self.sent.append(json.loads(s))

    async def recv(self):
        m = await self.q.get()
        if m is CLOSED:
            raise ConnectionError("closed by peer")
        return m

    async def close(self):
        pass


class Signer:
    def __init__(self):
        self.calls = []

    def headers(self, method, path):
        self.calls.append((method, path))
        return {"KALSHI-ACCESS-KEY": "k", "KALSHI-ACCESS-TIMESTAMP": "1", "KALSHI-ACCESS-SIGNATURE": "s"}


class Rig:
    def __init__(self, *sockets, env="perp_demo", fail_connects=0):
        self.sockets, self.events, self.sleeps, self.urls = list(sockets), [], [], []
        self.clock = [NOW]
        self.signer = Signer()
        self.fail_connects = fail_connects

        async def connect(url, headers):
            self.urls.append((url, set(headers)))
            if self.fail_connects:
                self.fail_connects -= 1
                raise OSError("refused")
            if not self.sockets:
                raise ConnectionError("no more sockets")
            return self.sockets.pop(0)

        async def sleep(s):
            self.sleeps.append(s)
            await asyncio.sleep(0)

        self.stream = MarginStream(env, SPEC_PERP, self.signer, self.events.append, connect=connect,
                                   clock_ms=lambda: self.clock[0], sleep=sleep)

    def of(self, kind):
        return [e for e in self.events if e["type"] == kind]

    async def start(self):
        return asyncio.create_task(self.stream.run())

    async def stop(self, task):
        self.stream.close()
        try:
            await asyncio.wait_for(task, 2)
        except (asyncio.CancelledError, Exception):
            pass


async def until(cond, timeout=3.0):
    for _ in range(int(timeout / 0.005)):
        if cond():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition not reached")


CAPTURE_TRADES = sum(1 for m in CAPTURE if m["type"] == "trade")


async def read_whole_capture(rig):
    """Wait until the stream has handled the whole CAPTURE, whose LAST message is a trade. Never wait on an earlier
    sign (a quote, a ticker's last price, the last delta's seq): the stream reads one message per loop turn, so on a
    slow runner such a sign comes true while the trailing prints are still unread, and a test that then moves the
    clock judges a book with no print after it (docs/solutions/2026-11-01-wait-for-the-message-under-test.md)."""
    assert CAPTURE[-1]["type"] == "trade"
    await until(lambda: len(rig.of("trade")) == CAPTURE_TRADES)


def replay_top(msgs):
    """Expected best bid / ask computed independently: levels as Decimal in plain dicts, deltas in seq order."""
    bids: dict[Decimal, Decimal] = {}
    asks: dict[Decimal, Decimal] = {}
    for m in msgs:
        if m["type"] == "orderbook_snapshot":
            bids = {Decimal(p): Decimal(q) for p, q in m["msg"]["bid"]}
            asks = {Decimal(p): Decimal(q) for p, q in m["msg"]["ask"]}
        elif m["type"] == "orderbook_delta":
            side = bids if m["msg"]["side"] == "bid" else asks
            p = Decimal(m["msg"]["price"])
            side[p] = side.get(p, Decimal(0)) + Decimal(m["msg"]["delta"])
            if side[p] == 0:
                del side[p]
    return float(max(bids)), float(min(asks)), float(bids[max(bids)]), float(asks[min(asks)])


def run(coro):
    return asyncio.run(coro)


def test_endpoints_and_the_signed_handshake():
    assert MARGIN_WS_PATH == "/trade-api/ws/v2/margin"
    assert MARGIN_WS_URLS == {"perp_demo": "wss://external-api-margin-ws.demo.kalshi.co/trade-api/ws/v2/margin",
                              "perp_prod": "wss://external-api-margin-ws.kalshi.com/trade-api/ws/v2/margin"}
    with pytest.raises(ValueError):
        MarginStream("demo", SPEC_PERP, Signer(), lambda e: None)


def test_captured_snapshot_and_deltas_give_the_expected_best_bid_and_ask():
    async def go():
        ws = FakeWS(*CAPTURE)
        rig = Rig(ws)
        task = await rig.start()
        last_seq = max(m["seq"] for m in CAPTURE if m["type"] == "orderbook_delta")
        await read_whole_capture(rig)
        assert rig.stream.health()["last_seq"].get(1) == last_seq
        assert rig.signer.calls == [("GET", MARGIN_WS_PATH)]
        assert rig.urls[0] == (MARGIN_WS_URLS["perp_demo"], {"KALSHI-ACCESS-KEY", "KALSHI-ACCESS-TIMESTAMP",
                                                              "KALSHI-ACCESS-SIGNATURE"})
        cmds = [(c["cmd"], c["params"]) for c in ws.sent]
        assert cmds == [("subscribe", {"channels": ["orderbook_delta", "ticker", "trade"], "market_tickers": [TICKER]}),
                        ("subscribe", {"channels": ["fill", "user_orders"], "market_tickers": [TICKER]})]
        bid, ask, bid_size, ask_size = replay_top(CAPTURE)
        q = rig.stream.quotes.quote()
        assert (q.bid, q.ask, q.bid_size, q.ask_size) == (bid, ask, bid_size, ask_size)
        assert q.ticker == TICKER and q.age_ms == 0
        tk = [m for m in CAPTURE if m["type"] == "ticker"][-1]["msg"]
        assert (q.last_price, q.settlement_mark, q.liquidation_mark, q.reference_price) == (
            float(tk["price"]), float(tk["settlement_mark_price"]["price"]),
            float(tk["liquidation_mark_price"]["price"]), float(tk["reference_price"]["price"]))
        assert (q.funding_rate, q.next_funding_ms) == (tk["funding_rate"]["rate"],
                                                       tk["funding_rate"]["next_funding_time_ms"])
        # the capture ends mid-session: every channel was acknowledged, so the stream is up and the book is live
        assert rig.stream.health()["connected"] and rig.stream.health()["book_live"]
        first_up = next(i for i, e in enumerate(rig.events) if e["type"] == "health" and e["connected"])
        assert [e["type"] for e in rig.events].index("resync") < first_up
        trades = rig.of("trade")
        assert len(trades) == 4 and trades[0]["price"] == 7.9899 and trades[0]["taker_side"] == "ask"
        tops = rig.of("book_top")
        assert tops and (tops[-1]["bid"], tops[-1]["ask"]) == (bid, ask)
        # every ticker message (mark, funding) is announced too, so the screen gets marks without waiting for a book move
        assert len(rig.of("ticker")) == sum(1 for m in CAPTURE if m["type"] == "ticker")
        await rig.stop(task)
    run(go())


def test_a_sequence_gap_requests_a_snapshot_and_serves_no_quote_until_it_arrives():
    async def go():
        # the capture's acks, snapshot and deltas up to seq 9 (no ticker or trade messages)
        head = [m for m in CAPTURE if not (m["type"] == "orderbook_delta" and m["seq"] > 9)
                and m["type"] not in ("ticker", "trade")]
        ws = FakeWS(*head)
        rig = Rig(ws)
        task = await rig.start()
        await until(lambda: rig.stream.health()["last_seq"].get(1) == 9)
        assert rig.stream.quotes.quote() is not None
        # seq 10 is lost: 11 arrives next
        d11 = next(m for m in CAPTURE if m["type"] == "orderbook_delta" and m["seq"] == 11)
        ws.feed(d11)
        await until(lambda: rig.stream.health()["last_seq"].get(1) == 11)
        snap_req = ws.sent[-1]
        assert snap_req["cmd"] == "update_subscription" and snap_req["params"] == {
            "sids": [1], "market_tickers": [TICKER], "action": "get_snapshot"}
        assert rig.stream.quotes.quote() is None
        assert not rig.stream.health()["book_live"]
        assert rig.stream.quotes.why_none() == "order book resyncing after a sequence gap"
        # more deltas before the snapshot do not bring a quote back
        d12 = next(m for m in CAPTURE if m["type"] == "orderbook_delta" and m["seq"] == 12)
        ws.feed(d12)
        await until(lambda: rig.stream.health()["last_seq"].get(1) == 12)
        assert rig.stream.quotes.quote() is None
        # the fresh snapshot (the captured one, renumbered) replaces the book
        snap = next(m for m in CAPTURE if m["type"] == "orderbook_snapshot")
        ws.feed({**snap, "seq": 13, "id": snap_req["id"]})
        await until(lambda: rig.stream.quotes.quote() is not None)
        bid, ask, _, _ = replay_top([snap])
        q = rig.stream.quotes.quote()
        assert (q.bid, q.ask) == (bid, ask) and rig.stream.health()["book_live"]
        await rig.stop(task)
    run(go())


def test_a_book_older_than_two_seconds_with_a_print_after_it_is_not_served():
    """The capture ends on a trade after the last delta: within 2 s the book is fresh; past the 2 s grace with no book
    update for that print, the book feed counts as broken (domain/book_freshness.py)."""
    async def go():
        rig = Rig(FakeWS(*CAPTURE))
        task = await rig.start()
        await read_whole_capture(rig)
        assert rig.stream.quotes.quote() is not None and rig.stream.quotes.quote().last_price
        rig.clock[0] = NOW + 2000
        assert rig.stream.quotes.quote() is not None and rig.stream.quotes.age_ms() == 2000
        rig.clock[0] = NOW + 3400
        assert rig.stream.quotes.quote() is None
        assert rig.stream.quotes.age_ms() == 3400
        assert rig.stream.quotes.why_none() == "book feed broken: trades for 3.4 s without a book update (grace 2 s)"
        await rig.stop(task)
    run(go())


HEAD_NO_LAST_TRADE = CAPTURE[:-1]  # ends on orderbook_delta seq 41: no print after the book's last update
TICKER_MSG = next(m for m in CAPTURE if m["type"] == "ticker")
LAST_TRADE = CAPTURE[-1]


async def at(rig, ws, ms, *msgs):
    """Feed `msgs` at NOW + ms and wait until the stream has read them."""
    rig.clock[0] = NOW + ms
    before = rig.stream.quotes.last_msg_ms
    ws.feed(*msgs)
    if msgs:
        await until(lambda: rig.stream.quotes.last_msg_ms != before or before == rig.clock[0])
        await asyncio.sleep(0.01)


def test_ticker_messages_never_make_a_frozen_book_fresh_and_a_print_without_a_book_update_refuses_it():
    """B3#8 (the 2026-10-31 13:59 Polymarket failure, on perps): the ticker channel keeps sending every second while
    orderbook_delta has stopped. A ticker message keeps the connection alive but never refreshes the book; the book is
    served quiet only while no print arrived after it."""
    async def go():
        ws = FakeWS(*HEAD_NO_LAST_TRADE)
        rig = Rig(ws)
        task = await rig.start()
        await until(lambda: rig.stream.health()["last_seq"].get(1) == 41)
        q = rig.stream.quotes
        await at(rig, ws, 1000, TICKER_MSG)
        await at(rig, ws, 2500, TICKER_MSG)
        assert q.quote() is not None and q.age_ms() == 2500  # quiet but current: nothing traded since the book
        # a print arrives and no book update follows it: refused at once, broken after the 2 s grace
        await at(rig, ws, 3000, {**LAST_TRADE, "seq": 4})
        assert q.quote() is None and q.why_none() == ("a trade arrived after the last book update; waiting for its "
                                                      "book update")
        await at(rig, ws, 4000, TICKER_MSG)
        await at(rig, ws, 5500, TICKER_MSG)  # tickers keep coming: still no book update
        assert q.quote() is None and q.why_none().startswith("book feed broken: trades for 2.5 s")
        # the book update for it arrives: fresh again
        delta = next(m for m in CAPTURE if m["type"] == "orderbook_delta" and m["seq"] == 41)
        await at(rig, ws, 5600, {**delta, "seq": 42, "msg": {**delta["msg"], "delta": "1.00"}})
        assert q.quote() is not None and q.age_ms() == 0
        await rig.stop(task)
    run(go())


def test_a_quiet_book_is_served_for_at_most_30_s_and_never_on_a_silent_connection():
    async def go():
        ws = FakeWS(*HEAD_NO_LAST_TRADE)
        rig = Rig(ws)
        task = await rig.start()
        await until(lambda: rig.stream.health()["last_seq"].get(1) == 41)
        q = rig.stream.quotes
        for ms in range(1000, 30_001, 1000):
            await at(rig, ws, ms, TICKER_MSG)
        assert q.quote() is not None and q.quote().age_ms == 30_000
        await at(rig, ws, 31_000, TICKER_MSG)
        assert q.quote() is None and q.why_none() == "the order book is 31.0 s old (quiet limit 30 s)"
        await rig.stop(task)
    run(go())

    async def silent():
        ws = FakeWS(*HEAD_NO_LAST_TRADE)
        rig = Rig(ws)
        task = await rig.start()
        await until(lambda: rig.stream.health()["last_seq"].get(1) == 41)
        rig.clock[0] = NOW + 16_000  # nothing at all for 16 s: the socket may be dead
        assert rig.stream.quotes.quote() is None
        assert rig.stream.quotes.why_none().startswith("the perps connection silent for 16.0 s")
        await rig.stop(task)
    run(silent())


def test_a_trade_sequence_gap_stops_vouching_for_the_quiet_book_until_the_next_book_update():
    async def go():
        ws = FakeWS(*HEAD_NO_LAST_TRADE)
        rig = Rig(ws)
        task = await rig.start()
        await until(lambda: rig.stream.health()["last_seq"].get(1) == 41)
        q = rig.stream.quotes
        # trade seq 4 is lost (5 arrives next): a print may have been missed, so the quiet book is not vouched for
        await at(rig, ws, 2500, {**LAST_TRADE, "seq": 5, "msg": {**LAST_TRADE["msg"], "market_ticker": "OTHER"}})
        assert q.quote() is None and "has not covered the time since this order book arrived" in q.why_none()
        delta = next(m for m in CAPTURE if m["type"] == "orderbook_delta" and m["seq"] == 41)
        await at(rig, ws, 2600, {**delta, "seq": 42, "msg": {**delta["msg"], "delta": "1.00"}})
        assert q.quote() is not None
        await rig.stop(task)
    run(go())


def test_a_reconnect_emits_resync_before_any_fill_from_the_new_connection():
    async def go():
        snap = next(m for m in CAPTURE if m["type"] == "orderbook_snapshot")
        first = FakeWS(*ALL_ACKS, snap, CLOSED)
        # on the second connection a fill arrives after its own channel's ack but before user_orders is acked
        second = FakeWS(*ALL_ACKS[:4], SPEC["fill_user"], ALL_ACKS[4], snap)
        rig = Rig(first, second)
        task = await rig.start()
        await until(lambda: len(rig.of("fill")) == 1)
        kinds = [(e["type"], e.get("connected")) for e in rig.events if e["type"] in ("resync", "fill", "health")]
        resyncs = [i for i, e in enumerate(rig.events) if e["type"] == "resync"]
        fill_at = next(i for i, e in enumerate(rig.events) if e["type"] == "fill")
        downs = [i for i, e in enumerate(rig.events) if e["type"] == "health" and not e["connected"] and i > resyncs[0]]
        assert len(resyncs) == 2 and downs and resyncs[0] < downs[0] < resyncs[1] < fill_at, kinds
        assert rig.events[downs[0]]["reason"] == "ConnectionError"
        assert rig.sleeps == [1.0]
        assert all(e["env"] == "perp_demo" for e in rig.events)
        await rig.stop(task)
    run(go())


def test_spec_built_fill_and_user_order_messages_parse_including_system_orders():
    async def go():
        ws = FakeWS(*ALL_ACKS, SPEC["fill_user"], SPEC["fill_system"], SPEC["user_order_user"],
                    SPEC["user_order_system"])
        rig = Rig(ws)
        task = await rig.start()
        await until(lambda: len(rig.of("order")) == 2)
        f_user, f_sys = rig.of("fill")
        assert f_user == {"type": "fill", "env": "perp_demo", "trade_id": "11111111-2222-4333-8444-555555555555",
                          "order_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "client_order_id": "sv-perp-0001",
                          "ticker": TICKER, "is_taker": True, "side": "bid", "price": 7.9997, "count": 3.0,
                          "fee": 0.0288, "post_position": 3.0, "subaccount": 7, "order_source": "user",
                          "ts_ms": 1793347200100}
        assert (f_sys["order_source"], f_sys["side"], f_sys["post_position"], f_sys["client_order_id"]) == (
            "system", "ask", 0.0, None)
        o_user, o_sys = rig.of("order")
        assert o_user == {"type": "order", "env": "perp_demo", "order_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
                          "client_order_id": "sv-perp-0001", "ticker": TICKER, "side": "bid", "price": 8.0397,
                          "fill_count": 3.0, "remaining_count": 0.0, "created_ts_ms": 1793347200090,
                          "last_updated_ts_ms": 1793347200100, "last_update_reason": "Trade",
                          "expiration_ts_ms": None, "order_group_id": None, "subaccount": 7,
                          "order_source": "user"}
        assert (o_sys["order_source"], o_sys["created_ts_ms"], o_sys["side"]) == ("system", None, "ask")
        await rig.stop(task)
    run(go())


def test_backoff_doubles_to_thirty_seconds_and_resets_after_a_minute_connected():
    async def go():
        long_lived = FakeWS(*ALL_ACKS)
        rig = Rig(long_lived, fail_connects=6)
        task = await rig.start()
        await until(lambda: rig.stream.health()["connected"])
        assert rig.sleeps == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0]
        rig.clock[0] += 61_000
        long_lived.feed(CLOSED)
        await until(lambda: len(rig.sleeps) >= 8)
        assert rig.sleeps[6:8] == [1.0, 2.0]
        await rig.stop(task)
    run(go())


def test_real_production_fills_and_orders_parse():
    """The private capture from the 2026-10-30 real-money check (LEARNINGS): our fills, then the take-profit leg's
    system order `etr:<trigger id>:0` closing the position."""
    import json
    from pathlib import Path

    from btc_signal_engine.adapters.kalshi.margin_stream import fill_event, order_event

    msgs = json.loads((Path(__file__).parents[1] / "fixtures" / "kalshi_margin_ws" / "prod_private_capture.json")
                      .read_text(encoding="utf-8"))
    fills = [fill_event(x["msg"]["msg"]) for x in msgs if x["msg"]["type"] == "fill"]
    orders = [order_event(x["msg"]["msg"]) for x in msgs if x["msg"]["type"] == "user_order"]
    assert [(f["side"], f["price"], f["fee"], f["post_position"], f["order_source"]) for f in fills] == [
        ("bid", 7.797, 0.0094, 2.0, "user"), ("ask", 7.7969, 0.0094, 1.0, "user"),
        ("ask", 7.7971, 0.0094, 0.0, "system")]
    fired = orders[-1]
    assert fired["client_order_id"].startswith("etr:") and fired["order_source"] == "system"
    assert fired["price"] == 7.6803 and fired["last_update_reason"] == "ReduceOnlyCancel" and fired["subaccount"] == 0
