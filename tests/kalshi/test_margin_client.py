"""The guarded perps (margin) REST client (adapters/kalshi/margin.py). NO network, no credentials: every request goes to
the recording MarginFake (tests/kalshi/margin_fake.py) or a scripted httpx.MockTransport; keys are generated here.

Covers: the signer's allow-list and host check, the order and trigger guards before transport, the single retry
loop and its exception contract (definitive / status), duplicate-order recovery, redirects, the wire format of prices
and counts, a few parsed read shapes and the one timestamp parser.
"""

from __future__ import annotations

import asyncio
import base64
from decimal import Decimal

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from btc_signal_engine.adapters.kalshi.margin import (BASE_URLS, MarginAuth, MarginClient, MarginRejected,
                                                       MarginUnknown, MarginViolation, to_epoch_ms)
from tests.kalshi.margin_fake import DEMO_HOST, P, PROD_HOST, MarginFake

DEMO = "KXBTCPERP1"  # the demo BTC perp (LEARNINGS: ticker differs per env)
SUB = 7  # the configured Settleview subaccount
NOW_MS = 1793361600123
KEY = ed25519.Ed25519PrivateKey.generate()
CID = "5f0c2a8e-0f4e-4c39-9f57-3b1f6f2b8a10"


def run(coro):
    return asyncio.run(coro)


def auth(env: str = "perp_demo") -> MarginAuth:
    return MarginAuth("test-key", KEY, env=env, clock_ms=lambda: NOW_MS)


def client(fake: MarginFake, *, env: str = "perp_demo", max_notional: str = "500", sleeps: list | None = None):
    async def sleep(s: float) -> None:
        if sleeps is not None:
            sleeps.append(s)
    return MarginClient(auth(env), env=env, subaccount=SUB, max_notional=Decimal(max_notional),
                        transport=fake.transport(), sleep=sleep)


def entry(**kw) -> dict:
    o = {"ticker": DEMO, "side": "bid", "count": Decimal("3"), "price": Decimal("7.8034"), "client_order_id": CID,
         "time_in_force": "immediate_or_cancel", "reduce_only": False, "purpose": "entry"}
    o.update(kw)
    return o


def exit_(**kw) -> dict:
    return entry(side="ask", reduce_only=True, purpose="exit", **kw)


async def using(fake: MarginFake, fn, **ckw):
    c = client(fake, **ckw)
    try:
        return await fn(c)
    finally:
        await c.aclose()


# -- signer ---------------------------------------------------------------------------------------------------------
def test_an_allow_listed_margin_request_carries_the_three_signed_headers():
    fake = MarginFake()
    fake.queue("GET", "/margin/positions", 200, {"positions": []})
    run(using(fake, lambda c: c.positions(subaccount=SUB, ticker=DEMO)))
    (req,) = fake.seen
    h = req["headers"]
    assert req["host"] == DEMO_HOST
    assert h["kalshi-access-key"] == "test-key" and h["kalshi-access-timestamp"] == str(NOW_MS)
    # signed over timestamp + METHOD + path with /trade-api/v2 and without the query string
    KEY.public_key().verify(base64.b64decode(h["kalshi-access-signature"]),
                            f"{NOW_MS}GET{P}/margin/positions".encode())
    assert req["query"] == {"subaccount": str(SUB), "ticker": DEMO}


def test_the_margin_websocket_handshake_is_signed_like_a_rest_get():
    h = auth().headers("GET", "/trade-api/ws/v2/margin")
    KEY.public_key().verify(base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]),
                            f"{NOW_MS}GET/trade-api/ws/v2/margin".encode())


@pytest.mark.parametrize("method,path", [
    ("DELETE", "/margin/orders"),  # cancel-all
    ("POST", "/portfolio/margin/subaccounts"),  # subaccount create
    ("POST", "/portfolio/margin/subaccounts/transfer"),  # no transfers: Settleview trades from one account
    ("PUT", f"/margin/isolated/positions/{DEMO}/exit_trigger"),
    ("DELETE", f"/margin/isolated/positions/{DEMO}/exit_trigger"),
    ("GET", f"/margin/isolated/positions/{DEMO}/exit_trigger"),
    ("POST", "/margin/orders/ord-1/amend"),
    ("POST", "/margin/order_groups/create"),
    ("POST", "/margin/fcm/subtraders"),
    ("GET", "/margin/fcm/subtraders/risk_controls"),
    ("POST", "/portfolio/intra_exchange_instance_transfer"),
    ("POST", "/portfolio/events/orders"),  # the event-contract order path
])
def test_a_request_off_the_margin_allow_list_raises_before_any_request_exists(method, path):
    fake = MarginFake()

    async def go():
        async with httpx.AsyncClient(base_url=BASE_URLS["perp_demo"], auth=auth(), transport=fake.transport()) as h:
            await h.request(method, path)
    with pytest.raises(MarginViolation):
        run(go())
    assert fake.seen == []
    with pytest.raises(MarginViolation):
        auth().headers(method, P + path)


def test_a_perp_demo_signer_refuses_the_production_host_before_signing():
    fake = MarginFake()

    async def go():
        async with httpx.AsyncClient(auth=auth("perp_demo"), transport=fake.transport()) as h:
            await h.get(f"https://{PROD_HOST}{P}/margin/positions")
    with pytest.raises(MarginViolation, match="host"):
        run(go())
    assert fake.seen == []


def test_the_signer_and_client_never_show_key_material():
    a = auth()
    assert "test-key" not in repr(a) and "test-key" not in repr(MarginClient(a, env="perp_demo", subaccount=SUB,
                                                                             max_notional=Decimal(500),
                                                                             transport=MarginFake().transport()))


# -- order guards -----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("kw", [
    {"reduce_only": False, "purpose": "exit"},  # an exit without reduce_only
    {"time_in_force": "good_till_canceled", "reduce_only": True, "purpose": "exit"},
    {"time_in_force": "good_till_canceled"},  # no resting orders in v1 (KTD5)
    {"ticker": "KXBTCPERP"},  # the production ticker on a perp_demo client
    {"ticker": "KXETHPERP1"},
    {"price": Decimal("7.80345")},  # off the 0.0001 tick
    {"price": Decimal("0")},
    {"price": 7.8034},  # a float is never coerced
    {"count": Decimal("2.5")},  # BTC trades whole contracts (count step 1.0)
    {"count": Decimal("0")},
    {"count": Decimal("1.001")},
    {"side": "buy"},
    {"client_order_id": ""},
    {"purpose": "hedge"},
    {"count": Decimal("70"), "price": Decimal("7.8034")},  # 546.24 > the 500 notional ceiling
])
def test_a_refused_order_never_reaches_the_transport(kw):
    fake = MarginFake()
    with pytest.raises(MarginViolation) as e:
        run(using(fake, lambda c: c.create_order(**entry(**kw))))
    assert fake.seen == [] and e.value.definitive is True and e.value.status == 0


def test_the_notional_ceiling_does_not_block_an_exit():
    """Exits are reduce-only (they can only shrink the position), so a close larger than one entry is allowed (R4)."""
    fake = MarginFake()
    got = run(using(fake, lambda c: c.create_order(**exit_(count=Decimal("70")))))
    assert got["fill_count"] == Decimal("70.00")


def test_a_perp_prod_client_trades_only_the_production_ticker():
    fake = MarginFake()
    with pytest.raises(MarginViolation):
        run(using(fake, lambda c: c.create_order(**entry()), env="perp_prod"))
    assert fake.seen == []


def test_a_client_is_built_for_one_configured_subaccount_and_one_perp_env():
    c = MarginClient(auth(), env="perp_demo", subaccount=0, max_notional=Decimal(500),
                     transport=MarginFake().transport())  # the primary account (production)
    assert c.subaccount == 0
    run(c.aclose())
    for bad in (64, -1, True, "7", None):
        with pytest.raises(ValueError):
            MarginClient(auth(), env="perp_demo", subaccount=bad, max_notional=Decimal(500),
                         transport=MarginFake().transport())
    with pytest.raises(ValueError):
        MarginClient(auth(), env="demo", subaccount=SUB, max_notional=Decimal(500), transport=MarginFake().transport())
    with pytest.raises(ValueError):  # the signer is bound to another env
        MarginClient(auth("perp_prod"), env="perp_demo", subaccount=SUB, max_notional=Decimal(500),
                     transport=MarginFake().transport())
    with pytest.raises(ValueError):
        MarginClient(auth(), env="perp_demo", subaccount=SUB, max_notional=Decimal(0),
                     transport=MarginFake().transport())


def test_an_order_goes_out_with_its_price_on_the_tick_and_its_count_with_two_decimals():
    fake = MarginFake()
    got = run(using(fake, lambda c: c.create_order(**entry())))
    (req,) = fake.seen
    assert req["method"] == "POST" and req["path"] == f"{P}/margin/orders"
    assert req["body"] == {"ticker": DEMO, "client_order_id": CID, "side": "bid", "count": "3.00", "price": "7.8034",
                           "time_in_force": "immediate_or_cancel", "self_trade_prevention_type": "taker_at_cross",
                           "reduce_only": False, "subaccount": SUB}
    assert got == {"order_id": "ord-1", "client_order_id": CID, "fill_count": Decimal("3.00"),
                   "remaining_count": Decimal("0.00"), "average_fill_price": Decimal("7.8034"),
                   "average_fee_paid": Decimal("0.0094"), "duplicate": False}


def test_string_prices_and_counts_are_accepted_and_normalised():
    fake = MarginFake()
    run(using(fake, lambda c: c.create_order(**exit_(price="7.80", count="2", time_in_force="fill_or_kill"))))
    body = fake.seen[0]["body"]
    assert (body["price"], body["count"], body["reduce_only"], body["time_in_force"]) == \
        ("7.8000", "2.00", True, "fill_or_kill")


# -- retries and the exception contract --------------------------------------------------------------------------------
def test_a_503_then_201_on_create_sends_two_requests_with_the_same_client_order_id():
    fake = MarginFake()
    fake.queue("POST", "/margin/orders", 503, {"code": "unavailable"})
    got = run(using(fake, lambda c: c.create_order(**entry())))
    posts = fake.requests("POST", "/margin/orders")
    assert len(posts) == 2 and posts[0]["body"] == posts[1]["body"]
    assert got["order_id"] == "ord-1" and got["duplicate"] is False


def test_a_429_is_backed_off_and_retried_within_the_same_loop():
    fake = MarginFake()
    fake.queue("POST", "/margin/orders", 429, {"code": "too_many_requests"})
    sleeps: list = []
    got = run(using(fake, lambda c: c.create_order(**entry()), sleeps=sleeps))
    assert got["order_id"] == "ord-1" and len(fake.requests("POST")) == 2 and sleeps and sleeps[0] > 0


def test_a_post_is_sent_only_by_our_loop_never_resent_by_the_transport():
    """Every POST that reaches the wire is one of our loop's attempts (3), each with the same body; after that the
    order lists are searched, and with nothing found the outcome is unknown."""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append({"method": request.method, "content": request.content})
        if request.method == "POST":
            raise httpx.ReadError("connection reset", request=request)
        return httpx.Response(200, json={"orders": [], "cursor": ""})
    c = MarginClient(auth(), env="perp_demo", subaccount=SUB, max_notional=Decimal(500),
                     transport=httpx.MockTransport(handler), sleep=lambda s: asyncio.sleep(0))

    async def go():
        try:
            await c.create_order(**entry())
        finally:
            await c.aclose()
    with pytest.raises(MarginUnknown) as e:
        run(go())
    posts = [r for r in seen if r["method"] == "POST"]
    assert len(posts) == 3 and len({p["content"] for p in posts}) == 1
    assert e.value.definitive is False


def test_the_production_transport_never_retries_follows_no_redirects_and_drops_idle_sockets_early():
    c = MarginClient(auth("perp_prod"), env="perp_prod", subaccount=SUB, max_notional=Decimal(500))
    try:
        pool = c._http._transport._pool
        assert pool._retries == 0  # httpcore resends nothing; our loop is the only retry
        assert pool._keepalive_expiry < 45.0  # below Kalshi's own idle close (45-60 s, backend.py)
        assert c._http.follow_redirects is False
        assert str(c._http.base_url).rstrip("/") == "https://external-api.kalshi.com/trade-api/v2"
    finally:
        run(c.aclose())


def test_a_lost_reply_then_409_resolves_to_the_existing_order_by_list_and_match():
    fake = MarginFake(lose_first_post_reply=True)
    got = run(using(fake, lambda c: c.create_order(**entry())))
    posts = fake.requests("POST", "/margin/orders")
    assert len(posts) == 2 and posts[0]["body"]["client_order_id"] == posts[1]["body"]["client_order_id"] == CID
    lists = fake.requests("GET", "/margin/orders")
    assert lists and lists[0]["query"]["ticker"] == DEMO and lists[0]["query"]["subaccount"] == str(SUB)
    assert got["duplicate"] is True and got["order_id"] == "ord-1" and got["client_order_id"] == CID
    assert got["fill_count"] == Decimal("3.00") and got["remaining_count"] == Decimal("0.00")
    assert len(fake.orders) == 1  # one order, never a second one


def test_a_409_whose_order_is_not_listed_is_unknown_never_a_rejection():
    fake = MarginFake(lose_first_post_reply=True, hide_from_lists=True)
    with pytest.raises(MarginUnknown) as e:
        run(using(fake, lambda c: c.create_order(**entry())))
    assert e.value.definitive is False and e.value.status == 409
    assert not isinstance(e.value, MarginRejected)
    assert len(fake.requests("GET", "/margin/orders")) > 1  # re-read: the lists can lag a placement


def test_a_redirect_is_not_followed_and_is_non_definitive():
    fake = MarginFake(hide_from_lists=True)
    fake.queue("POST", "/margin/orders", 302, {}, headers={"location": "https://elsewhere.example/steal"})
    with pytest.raises(MarginUnknown) as e:
        run(using(fake, lambda c: c.create_order(**entry())))
    assert e.value.status == 302 and e.value.definitive is False
    assert len(fake.requests("POST")) == 1  # not retried
    assert all(r["host"] == DEMO_HOST for r in fake.seen)  # the Location was never requested


def test_a_400_on_create_is_a_definitive_rejection_with_kalshis_body():
    fake = MarginFake()
    fake.queue("POST", "/margin/orders", 400, {"code": "invalid_price", "message": "price out of band"})
    with pytest.raises(MarginRejected) as e:
        run(using(fake, lambda c: c.create_order(**entry())))
    assert e.value.definitive is True and e.value.status == 400 and "invalid_price" in e.value.body
    assert len(fake.requests("POST")) == 1


def test_a_408_on_create_is_unknown_and_looked_up_never_a_rejection():
    """Only 400/401/403/404/422 prove a refusal (domain/order_resolution.DEFINITIVE_4XX); a 408 may have been placed."""
    fake = MarginFake(hide_from_lists=True)
    fake.queue("POST", "/margin/orders", 408, {"code": "timeout"})
    with pytest.raises(MarginUnknown) as e:
        run(using(fake, lambda c: c.create_order(**entry())))
    assert e.value.definitive is False and e.value.status == 408
    assert fake.requests("GET", "/margin/orders")  # looked up by client_order_id, like a lost reply


def test_persistent_5xx_on_a_read_raises_non_definitive_after_our_retries():
    fake = MarginFake()
    for _ in range(3):
        fake.queue("GET", "/margin/risk", 500, {"code": "internal"})
    with pytest.raises(MarginUnknown) as e:
        run(using(fake, lambda c: c.risk()))
    assert e.value.status == 500 and len(fake.seen) == 3


# -- cancel, decrease, exit triggers ----------------------------------------------------------------------------------
def test_an_order_on_the_primary_account_carries_subaccount_0():
    fake = MarginFake()
    c = MarginClient(auth(), env="perp_demo", subaccount=0, max_notional=Decimal(500), transport=fake.transport())

    async def go():
        try:
            await c.create_order(**entry())
            await c.set_exit_trigger(DEMO, stop_loss_price=Decimal("7.5"))
        finally:
            await c.aclose()
    fake.queue("PUT", f"/margin/cross/positions/{DEMO}/exit_trigger", 200, TRIGGER)
    run(go())
    assert fake.seen[0]["body"]["subaccount"] == 0 and fake.seen[1]["query"] == {"subaccount": "0"}


def test_cancel_one_order_names_the_configured_subaccount():
    fake = MarginFake()
    fake.queue("DELETE", "/margin/orders/ord-9", 200, {"order_id": "ord-9", "client_order_id": CID,
                                                       "reduced_by": "2.00"})
    got = run(using(fake, lambda c: c.cancel_order("ord-9")))
    assert fake.seen[0]["query"] == {"subaccount": str(SUB)}
    assert got == {"order_id": "ord-9", "client_order_id": CID, "reduced_by": Decimal("2.00")}


def test_an_order_id_that_would_change_the_path_or_query_is_refused():
    fake = MarginFake()
    for bad in ("ord-9?subaccount=0", "ord/../x", ""):
        with pytest.raises(MarginViolation):
            run(using(fake, lambda c, bad=bad: c.cancel_order(bad)))
    assert fake.seen == []


def test_decrease_sends_reduce_to_as_a_count_string_for_the_configured_subaccount():
    fake = MarginFake()
    fake.queue("POST", "/margin/orders/ord-9/decrease", 200, {"order_id": "ord-9", "remaining_count": "1.00"})
    got = run(using(fake, lambda c: c.decrease_order("ord-9", reduce_to=Decimal("1"))))
    assert fake.seen[0]["body"] == {"reduce_to": "1.00"} and fake.seen[0]["query"] == {"subaccount": str(SUB)}
    assert got["remaining_count"] == Decimal("1.00")


TRIGGER = {"id": "trg-1", "ticker": DEMO, "kind": "bracket", "status": "active", "count": "0.00",
           "filled_count": "0.00", "stop_loss_price": "7.5000", "take_profit_price": "8.1000",
           "created_time": "2026-10-30T12:00:02Z", "updated_time": "2026-10-30T12:00:02.5Z"}


def test_a_whole_position_bracket_goes_out_with_both_legs_on_the_tick():
    fake = MarginFake()
    fake.queue("PUT", f"/margin/cross/positions/{DEMO}/exit_trigger", 200, TRIGGER)
    got = run(using(fake, lambda c: c.set_exit_trigger(DEMO, stop_loss_price=Decimal("7.5"),
                                                       take_profit_price=Decimal("8.1"))))
    (req,) = fake.seen
    assert req["body"] == {"kind": "bracket", "stop_loss_price": "7.5000", "take_profit_price": "8.1000"}
    assert req["query"] == {"subaccount": str(SUB)}
    assert got["stop_loss_price"] == Decimal("7.5000") and got["updated_ts"] == 1793361602500
    assert "updated_time" not in got


@pytest.mark.parametrize("kw", [
    {},  # neither leg
    {"stop_loss_price": Decimal("7.50005")},
    {"stop_loss_price": Decimal("-1")},
])
def test_a_bracket_without_a_leg_or_off_the_tick_is_refused_before_transport(kw):
    fake = MarginFake()
    with pytest.raises(MarginViolation):
        run(using(fake, lambda c: c.set_exit_trigger(DEMO, **kw)))
    with pytest.raises(MarginViolation):
        run(using(fake, lambda c: c.update_exit_trigger(DEMO, "trg-1", **kw)))
    with pytest.raises(MarginViolation):  # a trigger on a ticker this env may not trade
        run(using(fake, lambda c: c.set_exit_trigger("KXBTCPERP", stop_loss_price=Decimal("7.5"))))
    assert fake.seen == []


def test_exit_trigger_reads_and_deletes_name_the_configured_subaccount():
    fake = MarginFake()
    path = f"/margin/cross/positions/{DEMO}/exit_trigger"
    fake.queue("GET", path, 200, {"exit_triggers": [TRIGGER]})
    fake.queue("DELETE", path, 204)
    fake.queue("DELETE", path + "/trg-1", 204)

    async def go(c):
        got = await c.exit_triggers(DEMO)
        await c.delete_exit_triggers(DEMO)
        await c.delete_exit_trigger(DEMO, "trg-1")
        return got
    got = run(using(fake, go))
    assert [r["method"] for r in fake.seen] == ["GET", "DELETE", "DELETE"]
    assert all(r["query"] == {"subaccount": str(SUB)} for r in fake.seen)
    assert got[0]["id"] == "trg-1" and got[0]["created_ts"] == 1793361602000


# -- reads -------------------------------------------------------------------------------------------------------------
def test_positions_parse_counts_and_dollars_to_decimal():
    fake = MarginFake()
    fake.queue("GET", "/margin/positions", 200, {"positions": [
        {"subaccount": SUB, "market_ticker": DEMO, "position": "-3.00",  # production omits subaccount_type
         "entry_price": "7.8034", "unrealized_pnl": "-0.0300", "margin_used": None, "fees": "0.0281",
         "is_portfolio": False}]})
    (p,) = run(using(fake, lambda c: c.positions()))
    assert fake.seen[0]["query"] == {}  # every subaccount (R17 shows positions Settleview did not open)
    assert (p["position"], p["entry_price"], p["margin_used"]) == (Decimal("-3.00"), Decimal("7.8034"), None)


def test_balance_passes_the_available_balance_flag():
    fake = MarginFake()
    fake.queue("GET", "/margin/balance", 200, {"settled_funds": "40.0000", "subaccount_balances": [
        {"subaccount": SUB, "position_value": "0", "account_equity": "25.0000",
         "maintenance_margin": "0", "initial_margin": "0", "resting_orders_margin": "0",
         "available_balance": "25.0000"}]})
    got = run(using(fake, lambda c: c.balance(compute_available_balance=True)))
    assert fake.seen[0]["query"] == {"compute_available_balance": "true"}
    assert got["settled_funds"] == Decimal("40.0000")
    assert got["subaccount_balances"][0]["available_balance"] == Decimal("25.0000")


def test_funding_history_sends_its_required_dates():
    fake = MarginFake()
    fake.queue("GET", "/margin/funding_history", 200, {"funding_history": [
        {"market_ticker": DEMO, "funding_time": "2026-10-30T12:00:00Z", "funding_rate": 0.0001,
         "mark_price": "7.8000", "funding_amount": "-0.0023", "quantity": "3.00", "subaccount_number": SUB,
         "subaccount_type": "cross"}]})
    (row,) = run(using(fake, lambda c: c.funding_history(start_date="2026-10-30", end_date="2026-10-30",
                                                         ticker=DEMO, subaccount=SUB)))
    assert fake.seen[0]["query"] == {"start_date": "2026-10-30", "end_date": "2026-10-30", "ticker": DEMO,
                                     "subaccount": str(SUB)}
    assert row["funding_ts"] == 1793361600000 and row["funding_amount"] == Decimal("-0.0023")
    with pytest.raises(ValueError):
        run(using(MarginFake(), lambda c: c.funding_history(start_date="30/10/2026", end_date="2026-10-30")))


def test_the_orderbook_levels_are_decimal_pairs():
    fake = MarginFake()
    fake.queue("GET", f"/margin/markets/{DEMO}/orderbook", 200, {"orderbook": {
        "bids": [["7.4000", "12.00"]], "asks": [["8.0300", "5.00"]]}})
    got = run(using(fake, lambda c: c.orderbook(DEMO)))
    assert got == {"bids": [(Decimal("7.4000"), Decimal("12.00"))], "asks": [(Decimal("8.0300"), Decimal("5.00"))]}


# -- timestamps --------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("value,unit,ms", [
    ("2026-10-30T12:00:00Z", "rfc3339", 1793361600000),
    ("2026-10-30T12:00:00.123456789Z", "rfc3339", 1793361600123),
    ("2026-10-30T14:00:00.5+02:00", "rfc3339", 1793361600500),
    (1793361600123, "ms", 1793361600123),
    (1793361600, "s", 1793361600000),
])
def test_one_parser_reads_rfc3339_milliseconds_and_seconds_to_epoch_ms(value, unit, ms):
    assert to_epoch_ms(value, unit=unit) == ms


@pytest.mark.parametrize("value,unit", [
    ("2026-10-30T12:00:00", "rfc3339"),  # no offset: ambiguous
    ("2026-10-30", "rfc3339"),
    ("1793361600", "s"),
    (1793361600.5, "s"),
    (True, "ms"),
    (1793361600, "minutes"),
])
def test_the_timestamp_parser_refuses_what_it_cannot_read_exactly(value, unit):
    with pytest.raises(ValueError):
        to_epoch_ms(value, unit=unit)


def test_the_notional_risk_limit_reads_productions_empty_default_as_no_default():
    """Production answered "" for the account default (reads 2026-10-30) with a per-market $500 for each perp."""
    fake = MarginFake()
    fake.queue("GET", "/margin/notional_risk_limit", 200, {
        "current_usage_by_market_ticker": {}, "default_notional_value_risk_limit": "",
        "notional_value_risk_limits_by_market_ticker": {"KXBTCPERP": "500.0000"}, "total_current_usage": "0.0000"})
    d = run(using(fake, lambda c: c.notional_risk_limit(), env="perp_prod"))
    assert d["default_notional_value_risk_limit"] is None
    assert d["notional_value_risk_limits_by_market_ticker"] == {"KXBTCPERP": Decimal("500.0000")}
    assert d["total_current_usage"] == Decimal("0")


def test_enabled_reads_kalshis_margin_switch():
    fake = MarginFake()
    fake.queue("GET", "/margin/enabled", 200, {"enabled": True})
    fake.queue("GET", "/margin/enabled", 200, {"enabled": False})
    assert run(using(fake, lambda c: c.enabled())) is True
    assert run(using(fake, lambda c: c.enabled())) is False
