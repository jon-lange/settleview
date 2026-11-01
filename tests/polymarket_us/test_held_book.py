"""The local position book (production penny test 2026-10-29: the positions list lagged our own fills, so a sale cap
taken from it skipped a take profit and would have skipped a stop). Our own fills are authoritative; the list is the
backup: a stale list can neither skip / shrink a sale nor allow a second sale of what was already sold. Fills from the
create answer, reads and the stream are counted once. Disagreements are noted (audited by the app) and re-read.
Restart seeding takes the larger of the app's brackets and the list."""

from __future__ import annotations

import asyncio

from btc_signal_engine.adapters.polymarket_us.trading import OrderOutcomeUnknown, PolymarketTrading
from tests.polymarket_us.fakes import SLUG, APIConnectionError, Clock, amount, client
from tests.polymarket_us.test_adapter import buy, live_quote, sell


def run(coro):
    return asyncio.run(coro)


async def _no_sleep(_):
    return None


def gw(c, clock=None, **kw) -> PolymarketTrading:
    return PolymarketTrading(c, quote_fn=live_quote(c), clock_ms=clock or Clock(), sleep=_no_sleep, **kw)


def test_stale_list_after_a_down_buy_never_skips_the_take_profit_or_the_stop():
    c = client(bid=0.33, ask=0.34)
    clock = Clock()
    g = gw(c, clock)
    c.acct.frozen = {}  # the list still shows nothing after our Down buy (penny test B1/B2)
    r = run(buy(g, "no", 1, 0.67))
    assert r["filled"] == 1 and g.local_position(SLUG) == -1.0
    tp = run(sell(g, "no", 1, 0.99, coid="tp", tif="good_till_canceled", post_only=True))
    assert "not_sent" not in tp and c.acct.creates()[-1]["quantity"] == 1  # sent for the full holding
    run(g.cancel(tp["order_id"], SLUG))
    stop = run(sell(g, "no", 1, 0.60, coid="stop"))
    assert stop["filled"] == 1 and c.acct.creates()[-1]["intent"] == "ORDER_INTENT_SELL_SHORT"
    assert c.acct.pos[SLUG] == 0 and g.local_position(SLUG) == 0.0
    assert [n["code"] for n in g.notes][-1] == "position_mismatch"  # noted (the app audits it)


def test_stale_list_after_a_sale_never_allows_a_second_sale():
    c = client(bid=0.33, ask=0.34)
    g = gw(c)
    run(buy(g, "yes", 2, 0.35))
    c.acct.frozen = {SLUG: 2}  # the list still shows +2 after we sold (penny test A4)
    r = run(sell(g, "yes", 2, 0.30))
    assert r["filled"] == 2 and c.acct.pos[SLUG] == 0
    again = run(sell(g, "yes", 2, 0.30, coid="s2"))
    assert again["not_sent"] is True and len(c.acct.creates()) == 2  # nothing more to sell: no short opened
    assert run(g.positions()) == []  # the service's own position checks see the truth too


def test_stale_list_never_blocks_or_allows_the_wrong_buy():
    c = client(bid=0.33, ask=0.34)
    g = gw(c)
    run(buy(g, "no", 1, 0.67))
    run(sell(g, "no", 1, 0.60, coid="s1"))
    c.acct.frozen = {SLUG: -1}  # list still shows the NO holding we already sold
    r = run(buy(g, "yes", 1, 0.35, coid="b2"))  # local book: flat, so an Up buy is allowed
    assert r["filled"] == 1


def test_after_the_grace_the_list_wins_both_ways_and_the_book_resyncs():
    c = client(bid=0.33, ask=0.34)
    clock = Clock()
    g = gw(c, clock)
    run(buy(g, "yes", 1, 0.35))
    c.acct.pos[SLUG] = 3  # a purchase we never made (in the Polymarket app), visible in the list
    clock.t += 11_000
    assert run(g.positions()) == [{"ticker": SLUG, "position": 3.0}]
    assert g.local_position(SLUG) == 3.0 and "position_mismatch" in {n["code"] for n in g.notes}
    c.acct.pos[SLUG] = 0  # the user sold everything by hand
    assert run(g.positions()) == []  # not resurrected by our own (older) fills
    r = run(sell(g, "yes", 1, 0.30, coid="stop"))  # a stop now sells nothing
    assert r["not_sent"] is True and c.acct.pos[SLUG] == 0


def test_create_answer_stream_and_reads_count_each_fill_once():
    c = client(bid=0.33, ask=0.34)
    g = gw(c)
    r = run(buy(g, "yes", 2, 0.35))
    o = dict(c.acct.orders[r["order_id"]])
    g.observe_order(o)  # the stream's copy
    run(g.get_order(r["order_id"]))  # a read-back
    run(g.find_order(SLUG, "c1", "yes"))
    assert g.local_position(SLUG) == 2.0


def test_lost_create_resolved_then_its_stream_copy_is_not_counted_twice():
    c = client(bid=0.33, ask=0.34)
    clock = Clock()
    g = gw(c, clock)
    c.acct.fail_next = (APIConnectionError(message="timeout"), True)
    try:
        run(buy(g, "yes", 2, 0.35))
    except OrderOutcomeUnknown:
        pass
    clock.t += 15_000
    found = run(g.find_order(SLUG, "c1", "yes"))
    assert found["filled"] == 2 and g.local_position(SLUG) == 2.0
    real = dict(next(iter(c.acct.orders.values())))
    g.observe_order(real)
    assert g.local_position(SLUG) == 2.0 and g.order_id_for("c1") == real["id"]


def test_stream_copy_resolves_a_lost_create_before_the_list_does():
    c = client(bid=0.33, ask=0.34)
    g = gw(c)
    c.acct.fail_next = (APIConnectionError(message="timeout"), True)
    try:
        run(buy(g, "yes", 2, 0.35))
    except OrderOutcomeUnknown:
        pass
    real = dict(next(iter(c.acct.orders.values())))
    g.observe_order(real)
    assert g.order_id_for("c1") == real["id"] and g.local_position(SLUG) == 2.0
    assert run(g.find_order(SLUG, "c1", "yes"))["filled"] == 2


def test_restart_seeds_from_brackets_and_the_list_taking_the_larger():
    c = client(bid=0.33, ask=0.34)
    c.acct.pos[SLUG] = -1  # we really hold 1 NO ...
    c.acct.frozen = {}  # ... but the list lags
    notes = []
    g = gw(c, seed_fn=lambda: {SLUG: -1.0}, note_fn=notes.append)
    assert run(g.positions()) == [{"ticker": SLUG, "position": -1.0}]
    assert notes and notes[0]["code"] == "seed_mismatch" and "key" not in str(notes).lower()
    r = run(sell(g, "no", 1, 0.60))  # the stop after a restart is still sent
    assert r["filled"] == 1 and c.acct.pos[SLUG] == 0


def test_restart_seed_list_larger_wins():
    c = client()
    c.acct.pos[SLUG] = 2
    g = gw(c, seed_fn=lambda: {SLUG: 1.0})
    assert run(g.positions()) == [{"ticker": SLUG, "position": 2.0}]


def test_unknown_stream_order_fills_update_the_book():
    c = client()
    g = gw(c)
    g.observe_order({"id": "x1", "marketSlug": SLUG, "intent": "ORDER_INTENT_BUY_SHORT", "price": amount(0.3),
                     "quantity": 1, "cumQuantity": 1, "state": "ORDER_STATE_FILLED"})
    assert g.local_position(SLUG) == -1.0
