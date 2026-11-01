"""Kalshi REST through the official SDK (adapters/kalshi/backend.py). NO network, no credentials: the SDK's REST
layer (`ApiClient.rest_client`) is replaced by a scripted fake, so the SDK's own request building, path/query
serialisation, model validation and auth hook all run for real; only the socket is fake.

Covers: identical requests to the httpx path for every operation, response shapes, 400/404/409/5xx/timeout handling,
duplicate-order recovery, the signature's timestamp coming from our clock, the SDK's retries being
off, guards running before the SDK is reached, and the shadow environment staying on httpx.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from urllib.parse import parse_qsl, urlsplit

import aiohttp
import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from kalshi_python_async.exceptions import ApiException

from btc_signal_engine.adapters.kalshi.auth import KalshiAuth, ReadOnlyViolation
from btc_signal_engine.adapters.kalshi.backend import (HttpxBackend, KalshiHTTPError, RequestNotSent, SdkBackend,
                                                       TransportFailure)
from btc_signal_engine.adapters.kalshi.client import KalshiClient
from btc_signal_engine.adapters.kalshi.portfolio import KalshiPortfolio
from btc_signal_engine.adapters.kalshi.shadow import shadow_trading
from btc_signal_engine.adapters.kalshi.trading import (BASE_URLS, KalshiTrading, OrderRejected, OrderUnknown,
                                                        TradingAuth, TradingViolation)
from tests.trading.helpers import TICKER

P = "/trade-api/v2"
OID = "ord-1"
NOW_MS = 1793207700123


def run(coro):
    return asyncio.run(coro)


async def no_sleep(_s: float) -> None:
    return None


def r(status: int, body: dict | None = None) -> tuple[int, dict]:
    return status, (body if body is not None else {})


ORDER = {"order_id": OID, "client_order_id": "c1", "ticker": TICKER, "status": "resting", "book_side": "bid",
         "yes_price_dollars": "0.4000", "fill_count_fp": "0.00", "remaining_count_fp": "2.00",
         "initial_count_fp": "2.00", "taker_fees_dollars": "0", "maker_fees_dollars": "0",
         "taker_fill_cost_dollars": "0", "maker_fill_cost_dollars": "0"}
CREATED = {"order_id": OID, "client_order_id": "c1", "fill_count": "2.00", "remaining_count": "0.00",
           "average_fill_price": "0.4000", "average_fee_paid": "0.0168", "ts_ms": 1}
MARKET = {"ticker": TICKER, "status": "active", "open_time": "2026-10-28T17:00:00Z",
          "close_time": "2026-10-28T17:15:00Z"}
BOOK = {"orderbook_fp": {"yes_dollars": [["0.5500", "10.00"]], "no_dollars": [["0.4200", "5.00"]]}}

# A canned answer for every operation, used by the parity test (both backends get the same answers).
CANNED = {
    ("POST", "/portfolio/events/orders"): r(201, CREATED),
    ("DELETE", f"/portfolio/events/orders/{OID}"): r(200, {"order_id": OID, "client_order_id": "c1",
                                                           "reduced_by": "2.00"}),
    ("GET", f"/portfolio/orders/{OID}"): r(200, {"order": ORDER}),
    ("GET", "/portfolio/orders"): r(200, {"orders": [ORDER]}),
    ("GET", "/portfolio/positions"): r(200, {"market_positions": [{"ticker": TICKER, "position_fp": "-3.00"}]}),
    ("GET", "/portfolio/fills"): r(200, {"fills": [{"fill_id": "f1", "order_id": OID, "market_ticker": TICKER,
                                                    "count_fp": "2.00", "yes_price_dollars": "0.4000",
                                                    "book_side": "bid", "created_time": "2026-10-28T17:05:00Z",
                                                    "fee_cost": "0.0336"}]}),
    ("GET", "/portfolio/balance"): r(200, {"balance": 1234, "balance_dollars": "12.3400"}),
    ("GET", f"/markets/{TICKER}"): r(200, {"market": MARKET}),
    ("GET", f"/markets/{TICKER}/orderbook"): r(200, BOOK),
    ("GET", "/markets"): r(200, {"markets": [MARKET], "cursor": "next"}),
}


class Script:
    """A scripted Kalshi shared by both transports: `routes[(method, path)]` is a list of answers served in order
    (the last repeats); an answer is (status, json) or an exception to raise. Records every request it sees."""

    def __init__(self, routes: dict | None = None) -> None:
        self.routes = {k: list(v) if isinstance(v, list) else [v] for k, v in (routes or {}).items()}
        self.seen: list[dict] = []

    def answer(self, method: str, url: str, headers: dict, body: bytes | None) -> tuple[int, bytes]:
        parts = urlsplit(url)
        path = parts.path[len(P):] if parts.path.startswith(P) else parts.path
        self.seen.append({"method": method, "path": path, "query": dict(parse_qsl(parts.query)),
                          "body": json.loads(body) if body else None, "headers": dict(headers)})
        queue = self.routes.get((method, path))
        if not queue:
            return 500, b'{"error": "unscripted"}'
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, BaseException):
            raise item
        return item[0], json.dumps(item[1]).encode()

    def count(self, method: str, path: str) -> int:
        return sum(1 for s in self.seen if s["method"] == method and s["path"] == path)

    def bodies(self, method: str, path: str) -> list[dict]:
        return [s["body"] for s in self.seen if s["method"] == method and s["path"] == path]


class FakeAioResponse:
    def __init__(self, status: int, data: bytes) -> None:
        self.status, self._data, self.released = status, data, False

    async def read(self) -> bytes:
        return self._data

    def release(self) -> None:
        self.released = True


class FakeRest:
    """Stands in for the SDK's `rest.RESTClientObject`: same `request(...)` signature, answers from a Script."""

    def __init__(self, script: Script) -> None:
        self.script = script
        self.timeouts: list = []

    async def request(self, method, url, headers=None, body=None, post_params=None, _request_timeout=None):
        self.timeouts.append(_request_timeout)
        raw = json.dumps(body).encode() if body is not None else None  # what rest.py sends (json.dumps of the body)
        status, data = self.script.answer(method, url, headers or {}, raw)

        class _Wrapped:  # rest.RESTResponse: the *_without_preload_content methods return its `.response`
            response = FakeAioResponse(status, data)
        return _Wrapped()

    async def close(self) -> None:
        return None


def sdk_backend(script: Script, host: str = BASE_URLS["demo"], signer=None) -> SdkBackend:
    b = SdkBackend(host, signer=signer, timeout=10.0)
    b.api_client.rest_client = FakeRest(script)
    return b


def httpx_backend(script: Script, host: str = BASE_URLS["demo"], auth=None) -> HttpxBackend:
    def handler(req: httpx.Request) -> httpx.Response:
        status, data = script.answer(req.method, str(req.url), dict(req.headers), req.content or None)
        return httpx.Response(status, content=data, headers={"content-type": "application/json"})
    return HttpxBackend(httpx.AsyncClient(base_url=host, auth=auth, transport=httpx.MockTransport(handler)))


def auth(clock=lambda: NOW_MS) -> TradingAuth:
    return TradingAuth("kid", ed25519.Ed25519PrivateKey.generate(), clock_ms=clock)


def sdk_gw(script: Script, *, max_order_cost: float = 25.0, a: TradingAuth | None = None) -> KalshiTrading:
    a = a or auth()
    return KalshiTrading(a, env="demo", backend=sdk_backend(script, signer=a), max_order_cost=max_order_cost,
                         sleep=no_sleep)


async def every_operation(gw: KalshiTrading) -> list:
    """Each KalshiTrading call once (the shapes the application reads)."""
    return [
        await gw.place_limit(ticker=TICKER, side="yes", action="buy", count=2, price=0.40, client_order_id="c1",
                             tif="good_till_canceled", post_only=True),
        await gw.place_limit(ticker=TICKER, side="no", action="sell", count=1.5, price=0.6, client_order_id="c9",
                             reduce_only=True),
        await gw.cancel(OID, TICKER),
        await gw.get_order(OID, "no"),
        await gw.orders(ticker=TICKER, status="resting"),
        await gw.find_order(TICKER, "c1"),
        await gw.positions(),
        await gw.market_fills(TICKER, 1793206800000),
        await gw.quote(TICKER),
    ]


# -- the SDK sends what httpx sent, and our answers read the same -------------------------------------------------

def test_every_operation_sends_the_same_request_and_returns_the_same_shape_as_the_httpx_path():
    via_httpx, via_sdk = Script(CANNED), Script(CANNED)
    a = auth()
    old = KalshiTrading(a, env="demo", backend=httpx_backend(via_httpx, auth=a), sleep=no_sleep)
    new = KalshiTrading(a, env="demo", backend=sdk_backend(via_sdk, signer=a), sleep=no_sleep)

    out_old, out_new = run(every_operation(old)), run(every_operation(new))

    assert out_new == out_old
    strip = lambda seen: [{k: v for k, v in s.items() if k != "headers"} for s in seen]  # noqa: E731
    assert strip(via_sdk.seen) == strip(via_httpx.seen)
    # The order body keeps our exact field values: fixed-point count, 4-dp dollar price.
    body = via_sdk.bodies("POST", "/portfolio/events/orders")[0]
    assert body == {"ticker": TICKER, "client_order_id": "c1", "side": "bid", "count": "2.00", "price": "0.4000",
                    "time_in_force": "good_till_canceled", "self_trade_prevention_type": "taker_at_cross",
                    "post_only": True, "reduce_only": False, "cancel_order_on_pause": False}
    assert via_sdk.bodies("POST", "/portfolio/events/orders")[1]["side"] == "bid"  # sell NO @0.60 = bid YES @0.40
    assert via_sdk.bodies("POST", "/portfolio/events/orders")[1]["price"] == "0.4000"
    assert via_sdk.seen[2]["query"] == {"market_ticker": TICKER}
    assert out_new[0]["status"] == "executed" and out_new[0]["fees"] == 0.0336
    assert out_new[6] == [{"ticker": TICKER, "position": -3.0}]


def test_sdk_requests_go_to_the_environment_host_with_a_ten_second_timeout():
    s = Script(CANNED)
    gw = sdk_gw(s)
    run(gw.positions())
    rest = gw._backend.api_client.rest_client
    assert rest.timeouts == [10.0]
    assert gw._backend.api_client.configuration.host == "https://external-api.demo.kalshi.co/trade-api/v2"
    prod = KalshiTrading(auth(), env="prod")
    assert isinstance(prod._backend, SdkBackend)
    assert prod._backend.api_client.configuration.host == "https://external-api.kalshi.com/trade-api/v2"


# -- signing: our clock, our allow-list ------------------------------------------------------------------------------

def test_the_signature_timestamp_comes_from_our_signers_clock_never_the_sdks_own_auth(monkeypatch):
    """The header carries our TradingAuth's clock (injected here; time.time() by default in the app), so the SDK's
    KalshiAuth, which stamps time.time() itself, is never the one signing."""
    import time as _time
    monkeypatch.setattr(_time, "time", lambda: 1.0)  # what the SDK's own KalshiAuth would stamp
    key = ed25519.Ed25519PrivateKey.generate()
    a = TradingAuth("kid", key, clock_ms=lambda: NOW_MS)
    s = Script(CANNED)
    gw = sdk_gw(s, a=a)
    run(gw.place_limit(ticker=TICKER, side="yes", action="buy", count=1, price=0.4, client_order_id="c1"))

    h = s.seen[0]["headers"]
    assert h["KALSHI-ACCESS-TIMESTAMP"] == str(NOW_MS) and h["KALSHI-ACCESS-KEY"] == "kid"
    key.public_key().verify(base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]),
                            f"{NOW_MS}POST{P}/portfolio/events/orders".encode())  # path with prefix, no query


def test_the_signed_path_excludes_the_query_string():
    key = ed25519.Ed25519PrivateKey.generate()
    s = Script(CANNED)
    run(sdk_gw(s, a=TradingAuth("kid", key, clock_ms=lambda: NOW_MS)).cancel(OID, TICKER))
    h = s.seen[0]["headers"]
    key.public_key().verify(base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]),
                            f"{NOW_MS}DELETE{P}/portfolio/events/orders/{OID}".encode())


def test_a_request_off_the_trading_allow_list_is_refused_before_the_sdk_sends_it():
    s = Script(CANNED)
    b = sdk_backend(s, signer=auth())
    with pytest.raises(TradingViolation):
        run(b.get_markets(series_ticker="KXBTC15M"))  # GET /markets is not on the trading allow-list
    assert s.seen == []


def test_the_read_only_account_link_signs_gets_and_refuses_writes_through_the_sdk():
    key = ed25519.Ed25519PrivateKey.generate()
    s = Script(CANNED)
    b = sdk_backend(s, host="https://api.elections.kalshi.com/trade-api/v2",
                    signer=KalshiAuth("kid", key, clock_ms=lambda: NOW_MS))
    assert run(b.get_balance()).json()["balance"] == 1234
    assert s.seen[0]["headers"]["KALSHI-ACCESS-TIMESTAMP"] == str(NOW_MS)
    with pytest.raises(ReadOnlyViolation):
        run(b.create_order({"ticker": TICKER, "side": "bid", "count": "1.00", "price": "0.4000",
                            "time_in_force": "immediate_or_cancel", "self_trade_prevention_type": "taker_at_cross"}))
    assert len(s.seen) == 1


# -- the SDK's own retries are off -----------------------------------------------------------------------------------

def test_sdk_retries_are_off_and_each_call_is_one_http_request(monkeypatch):
    """Through the SDK's real rest layer (aiohttp session patched, no socket): a 503 on a GET, a PUT or a POST is one
    request, never re-sent by aiohttp_retry. Our own loop is the only retry."""
    calls: list[tuple[str, object]] = []

    class Resp:
        status, reason = 503, "Service Unavailable"

        async def read(self):
            return b'{"error": "busy"}'

        def release(self):
            return None

    sessions: list = []

    async def fake_request(self, method, url, **kw):
        calls.append((method, kw.get("timeout")))
        assert kw.get("allow_redirects") is False
        sessions.append(self)
        return Resp()

    def no_retry_client(*a, **k):
        raise AssertionError("aiohttp_retry.RetryClient must never be built")

    monkeypatch.setattr(aiohttp.ClientSession, "request", fake_request)
    monkeypatch.setattr("aiohttp_retry.RetryClient", no_retry_client)
    b = SdkBackend(BASE_URLS["demo"], signer=auth(), timeout=10.0)
    assert b.api_client.configuration.retries is None and b.api_client.rest_client.retries is None

    async def go():
        try:
            assert (await b.get_order(OID)).status_code == 503
            assert (await b.cancel_order(OID, TICKER)).status_code == 503
            assert (await b.create_order({"ticker": TICKER, "client_order_id": "c1", "side": "bid", "count": "1.00",
                                          "price": "0.4000", "time_in_force": "immediate_or_cancel",
                                          "self_trade_prevention_type": "taker_at_cross"})).status_code == 503
        finally:
            await b.aclose()
    run(go())
    assert [m for m, _ in calls] == ["GET", "DELETE", "POST"]
    assert all(t == 10.0 for _, t in calls)
    assert sessions and all(x._retry_connection is False for x in sessions)  # aiohttp's stale-connection resend off


def test_aiohttp_still_honours_the_private_resend_switch_we_turn_off():
    """backend._new_session sets ClientSession._retry_connection = False. If an aiohttp upgrade renames or drops it,
    this fails (and _new_session raises) rather than aiohttp silently resending a GET/PUT/DELETE."""
    import inspect
    assert "_retry_connection" in aiohttp.ClientSession.ATTRS
    assert "self._retry_connection and method in IDEMPOTENT_METHODS" in inspect.getsource(aiohttp.ClientSession._request)
    assert "POST" not in aiohttp.client.IDEMPOTENT_METHODS


@pytest.fixture
def warnings_log(monkeypatch):
    """WARNING lines of the Kalshi adapters, captured on their own loggers (immune to other tests' logging setup)."""
    lines: list[str] = []

    class H(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())
    h = H(logging.DEBUG)
    names = ("btc_signal_engine.adapters.kalshi.backend", "btc_signal_engine.adapters.kalshi.trading")
    old = {}
    for n in names:
        lg = logging.getLogger(n)
        monkeypatch.setattr(lg, "disabled", False)
        old[n] = lg.level
        lg.setLevel(logging.DEBUG)  # setLevel (not the attribute): it also clears the isEnabledFor cache
        lg.addHandler(h)
    yield lines
    for n in names:
        logging.getLogger(n).removeHandler(h)
        logging.getLogger(n).setLevel(old[n])


class _OkResp:
    status, reason = 200, "OK"

    async def read(self):
        return json.dumps(BOOK).encode()

    def release(self):
        return None


def _stale_once(monkeypatch, exc: BaseException):
    """aiohttp's session.request patched (no socket): the POOLED session's first request raises `exc` (its kept-alive
    socket was closed by the server); every other request answers 200. Returns the list of (method, session,
    session-never-reuses-sockets) calls."""
    calls: list[tuple[str, aiohttp.ClientSession, bool]] = []

    async def fake_request(self, method, url, **kw):
        calls.append((method, self, self.connector.force_close))
        assert kw.get("allow_redirects") is False
        if len(calls) == 1:
            raise exc
        return _OkResp()

    monkeypatch.setattr(aiohttp.ClientSession, "request", fake_request)
    return calls


def test_a_get_on_a_stale_pooled_connection_is_retried_once_at_once_on_a_new_connection(monkeypatch, warnings_log):
    """Live 2026-10-29: a read failing on a closed keep-alive socket must not cost the user a retry delay. The GET is
    sent once more, immediately, on a session that never reuses a socket; our own retry loop never sees an error (so
    it never sleeps)."""
    calls = _stale_once(monkeypatch, aiohttp.ServerDisconnectedError("Server disconnected"))
    slept: list[float] = []

    async def sleep(s: float) -> None:
        slept.append(s)

    gw = KalshiTrading(auth(), env="demo", sleep=sleep)

    async def go():
        try:
            return await gw._send("get_orderbook", TICKER)
        finally:
            await gw.aclose()
    resp = run(go())
    assert resp.status_code == 200 and resp.json() == BOOK
    assert [c[0] for c in calls] == ["GET", "GET"]
    assert calls[0][1] is not calls[1][1] and calls[0][2] is False and calls[1][2] is True  # pooled, then brand new
    assert slept == []  # no delay: the retry is immediate and below our loop
    assert any("pooled connection was closed (ServerDisconnectedError" in x for x in warnings_log)


@pytest.mark.parametrize("op,args,method", [
    ("create_order", ({"ticker": TICKER, "client_order_id": "c1", "side": "bid", "count": "1.00", "price": "0.4000",
                       "time_in_force": "immediate_or_cancel", "self_trade_prevention_type": "taker_at_cross"},), "POST"),
    ("cancel_order", (OID, TICKER), "DELETE"),
])
def test_a_write_on_a_stale_pooled_connection_is_never_resent_below_our_loop(monkeypatch, op, args, method):
    """POST / DELETE: one request, and the stale socket surfaces as a non-definitive TransportFailure for our own
    loop (which re-sends with the SAME client_order_id), never a silent resend underneath it."""
    calls = _stale_once(monkeypatch, aiohttp.ServerDisconnectedError("Server disconnected"))
    b = SdkBackend(BASE_URLS["demo"], signer=auth(), timeout=10.0)

    async def go():
        try:
            await getattr(b, op)(*args)
        finally:
            await b.aclose()
    with pytest.raises(TransportFailure) as e:
        run(go())
    assert [c[0] for c in calls] == [method]
    assert isinstance(e.value.__cause__, aiohttp.ServerDisconnectedError)


def test_a_get_whose_new_connection_cannot_be_opened_is_not_retried_below(monkeypatch):
    """A ClientConnectorError (DNS, refused, TLS) is not a stale socket: one request, then our own loop decides."""
    from aiohttp.client_reqrep import ConnectionKey
    key = ConnectionKey("external-api.demo.kalshi.co", 443, True, True, None, None, None)
    calls = _stale_once(monkeypatch, aiohttp.ClientConnectorDNSError(key, OSError("Timeout while contacting DNS servers")))
    b = SdkBackend(BASE_URLS["demo"], signer=auth(), timeout=10.0)

    async def go():
        try:
            await b.get_orderbook(TICKER)
        finally:
            await b.aclose()
    with pytest.raises(TransportFailure) as e:
        run(go())
    assert len(calls) == 1 and "ClientConnectorDNSError" in str(e.value)


def test_the_transport_warning_names_the_underlying_aiohttp_error(warnings_log):
    s = Script({("GET", f"/markets/{TICKER}/orderbook"): [aiohttp.ClientOSError(10054, "connection reset"), r(200, BOOK)]})
    run(sdk_gw(s)._send("get_orderbook", TICKER))
    assert any("get_orderbook transport error (TransportFailure: ClientOSError:" in x for x in warnings_log)


def test_sdk_sessions_drop_idle_sockets_before_kalshi_does_and_use_the_os_resolver():
    from btc_signal_engine.adapters.kalshi import backend

    async def go():
        pooled = backend._new_session(4, None)
        fresh = backend._new_session(4, None, fresh=True)
        try:
            return (pooled.connector._keepalive_timeout, type(pooled.connector._resolver).__name__,
                    fresh.connector.force_close, type(fresh.connector._resolver).__name__,
                    pooled._retry_connection, fresh._retry_connection)
        finally:
            await pooled.close()
            await fresh.close()
    keep, res, force, res2, r1, r2 = run(go())
    assert keep == backend.KEEPALIVE_S < 45  # Kalshi kept a 45 s idle socket open and closed a 60 s one
    assert res == res2 == "ThreadedResolver" and force is True and r1 is False and r2 is False


def test_sdk_requests_are_counted_per_operation():
    from btc_signal_engine.adapters.kalshi.backend import take_request_counts
    take_request_counts()
    run(sdk_gw(Script(CANNED)).positions())
    run(sdk_gw(Script(CANNED)).quote(TICKER))
    counts = take_request_counts()
    assert counts == {"get_positions": 1, "get_market": 1, "get_market_orderbook": 1}
    assert take_request_counts() == {}


def test_a_redirect_is_not_followed_and_is_a_non_definitive_error(monkeypatch):
    """Through the SDK's real rest layer: a 3xx comes back to us (allow_redirects=False, so our KALSHI-ACCESS-*
    headers never reach the Location host) and is raised as a non-definitive error, not retried."""
    calls: list[dict] = []

    class Resp:
        status, reason = 302, "Found"

        async def read(self):
            return b""

        def release(self):
            return None

    async def fake_request(self, method, url, **kw):
        calls.append({"method": method, "url": url, **kw})
        return Resp()

    monkeypatch.setattr(aiohttp.ClientSession, "request", fake_request)
    a = auth()
    gw = KalshiTrading(a, env="demo", sleep=no_sleep)

    async def go():
        try:
            with pytest.raises(KalshiHTTPError) as e:
                await gw.place_limit(ticker=TICKER, side="yes", action="buy", count=1, price=0.4,
                                     client_order_id="c1")
            assert e.value.status == 302 and getattr(e.value, "definitive", False) is False
            with pytest.raises(KalshiHTTPError):
                await gw.positions()
        finally:
            await gw.aclose()
    run(go())
    assert [c["method"] for c in calls] == ["POST", "GET"]  # one request each: not followed, not retried
    assert all(c["allow_redirects"] is False for c in calls)


def test_a_closed_session_is_a_transport_failure_but_our_guard_errors_are_not_masked():
    s = Script({("GET", "/portfolio/positions"): [RuntimeError("Session is closed")]})
    with pytest.raises(TransportFailure):
        run(sdk_gw(s).positions())
    s2 = Script({("GET", "/portfolio/positions"): [RuntimeError("something else")]})
    with pytest.raises(RuntimeError) as e:
        run(sdk_backend(s2).get_positions(limit=200))
    assert type(e.value) is RuntimeError and str(e.value) == "something else"


def test_query_timestamps_are_sent_as_ints_even_from_a_stored_real():
    s = Script(CANNED)
    pf = KalshiPortfolio(KalshiAuth("kid", ed25519.Ed25519PrivateKey.generate()), min_interval=0, sleep=no_sleep)
    pf._backend.api_client.rest_client = FakeRest(s)
    run(pf.fills(min_ts=1793206800.0))
    run(sdk_backend(s).get_fills(min_ts=1793206800.0, max_ts=1793207700.0, limit=200))
    run(sdk_backend(s).get_orders(min_ts=1793206800.0, limit=200))
    assert s.seen[0]["query"]["min_ts"] == "1793206800"
    assert s.seen[1]["query"]["min_ts"] == "1793206800" and s.seen[1]["query"]["max_ts"] == "1793207700"
    assert s.seen[2]["query"]["min_ts"] == "1793206800"


# -- guards run before the SDK ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("kw", [
    dict(ticker="KXETH15M-26OCT281315-15"),  # not a tradable series of the market table
    dict(price=1.0),
    dict(price=0.0),
    dict(count=0),
    dict(count=100, price=0.5),  # $50 > the $25 cap
    dict(side="maybe"),
    dict(tif="good_till_date"),
])
def test_a_refused_order_never_reaches_the_sdk(kw):
    s = Script(CANNED)
    gw = sdk_gw(s)
    args = dict(ticker=TICKER, side="yes", action="buy", count=1, price=0.4, client_order_id="c1") | kw
    with pytest.raises(TradingViolation) as e:
        run(gw.place_limit(**args))
    assert e.value.definitive and s.seen == []


def test_a_refused_cancel_never_reaches_the_sdk():
    s = Script(CANNED)
    gw = sdk_gw(s)
    with pytest.raises(TradingViolation):
        run(gw.cancel(OID, "KXETH15M-26OCT281315-15"))
    assert s.seen == []


# -- error paths -----------------------------------------------------------------------------------------------------

def place(gw: KalshiTrading, coid: str = "c1"):
    return run(gw.place_limit(ticker=TICKER, side="yes", action="buy", count=1, price=0.4, client_order_id=coid))


def test_a_400_is_a_definitive_rejection_with_kalshis_body():
    s = Script({("POST", "/portfolio/events/orders"): r(400, {"error": {"code": "insufficient_balance"}})})
    with pytest.raises(OrderRejected) as e:
        place(sdk_gw(s))
    assert e.value.status == 400 and "insufficient_balance" in e.value.body and e.value.definitive
    assert s.count("POST", "/portfolio/events/orders") == 1


def test_a_409_duplicate_returns_the_existing_order_found_by_client_order_id():
    s = Script({("POST", "/portfolio/events/orders"): r(409, {"error": {"code": "duplicate_client_order_id"}}),
                ("GET", "/portfolio/orders"): r(200, {"orders": [ORDER]})})
    got = place(sdk_gw(s))
    assert got["duplicate"] is True and got["order_id"] == OID and got["client_order_id"] == "c1"
    assert s.seen[1]["query"] == {"ticker": TICKER, "limit": "200"}


def test_a_409_whose_order_is_not_listed_yet_is_unknown_never_rejected():
    """order_already_exists means an order with this client_order_id may exist: the answer must be non-definitive
    (the application then keeps looking it up by client_order_id) and nothing is re-sent."""
    s = Script({("POST", "/portfolio/events/orders"): r(409, {"error": {"code": "order_already_exists"}}),
                ("GET", "/portfolio/orders"): r(200, {"orders": []})})
    with pytest.raises(OrderUnknown) as e:
        place(sdk_gw(s))
    assert e.value.status == 409 and e.value.definitive is False and not isinstance(e.value, OrderRejected)
    assert s.count("POST", "/portfolio/events/orders") == 1
    assert s.count("GET", "/portfolio/orders") == 10  # ~5 s of re-reads, both lists


def test_a_5xx_is_retried_with_the_same_client_order_id():
    s = Script({("POST", "/portfolio/events/orders"): [r(503, {"error": "busy"}), r(201, CREATED)]})
    got = place(sdk_gw(s))
    bodies = s.bodies("POST", "/portfolio/events/orders")
    assert len(bodies) == 2 and bodies[0] == bodies[1] and bodies[0]["client_order_id"] == "c1"
    assert got["order_id"] == OID


def test_a_lost_response_then_duplicate_recovers_the_order_placed_by_the_first_attempt():
    s = Script({("POST", "/portfolio/events/orders"): [TimeoutError(), r(409, {"error": "dup"})],
                ("GET", "/portfolio/orders"): r(200, {"orders": [ORDER]})})
    got = place(sdk_gw(s))
    bodies = s.bodies("POST", "/portfolio/events/orders")
    assert bodies[0] == bodies[1] and got["duplicate"] is True


def test_timeouts_on_every_attempt_raise_a_non_definitive_error():
    s = Script({("POST", "/portfolio/events/orders"): [aiohttp.ServerTimeoutError("read timeout")]})
    with pytest.raises(TransportFailure) as e:
        place(sdk_gw(s))
    assert getattr(e.value, "definitive", False) is False
    assert s.count("POST", "/portfolio/events/orders") == 3  # our loop: same body each time


def test_a_connection_error_is_a_transport_failure():
    s = Script({("GET", "/portfolio/positions"): [aiohttp.ClientConnectionError("down")]})
    with pytest.raises(TransportFailure):
        run(sdk_gw(s).positions())


def test_persistent_5xx_raises_a_non_definitive_http_error():
    s = Script({("POST", "/portfolio/events/orders"): r(500, {"error": "boom"})})
    with pytest.raises(KalshiHTTPError) as e:
        place(sdk_gw(s))
    assert e.value.status == 500 and getattr(e.value, "definitive", False) is False


def test_cancel_404_means_no_longer_resting():
    s = Script({("DELETE", f"/portfolio/events/orders/{OID}"): r(404, {"error": "not_found"})})
    gw = sdk_gw(s)
    assert run(gw.cancel(OID, TICKER)) == {"order_id": OID, "reduced_by": 0.0, "not_found": True}


def test_get_order_404_is_retried_then_raised():
    s = Script({("GET", f"/portfolio/orders/{OID}"): r(404, {"error": "not_found"})})
    with pytest.raises(KalshiHTTPError) as e:
        run(sdk_gw(s).get_order(OID))
    assert e.value.status == 404 and s.count("GET", f"/portfolio/orders/{OID}") == 3


def test_an_sdk_api_exception_maps_to_its_status_or_to_not_sent():
    s = Script({("POST", "/portfolio/events/orders"): [ApiException(status=0, reason="cannot prepare")],
                ("GET", "/portfolio/positions"): [ApiException(status=403, reason="forbidden")]})
    gw = sdk_gw(s)
    with pytest.raises(RequestNotSent) as e:
        place(gw)
    assert e.value.definitive
    with pytest.raises(KalshiHTTPError) as e2:
        run(gw.positions())
    assert e2.value.status == 403


ORDER_BODY = {"ticker": TICKER, "client_order_id": "c1", "side": "bid", "count": "1.00", "price": "0.4000",
              "time_in_force": "immediate_or_cancel", "self_trade_prevention_type": "taker_at_cross"}


@pytest.mark.parametrize("extra", [{"no_such_field": True}, {"reduce_onyl": True}, {"postonly": True}])
def test_a_field_the_sdk_model_would_silently_drop_is_refused_before_sending(extra):
    s = Script(CANNED)
    b = sdk_backend(s, signer=auth())
    with pytest.raises(RequestNotSent) as e:
        run(b.create_order(ORDER_BODY | extra))
    assert e.value.definitive and s.seen == [] and next(iter(extra)) in str(e.value)


def test_a_field_set_to_none_is_not_sent():
    s = Script(CANNED)
    b = sdk_backend(s, signer=auth())
    run(b.create_order(ORDER_BODY | {"order_group_id": None}))  # None means "not set", as before: allowed
    assert len(s.seen) == 1 and "order_group_id" not in s.seen[0]["body"]


def test_a_field_the_sdk_model_refuses_is_not_sent():
    s = Script(CANNED)
    b = sdk_backend(s, signer=auth())
    with pytest.raises(RequestNotSent) as e:
        run(b.create_order({"ticker": TICKER, "side": "bid", "count": 1, "price": "0.4000",  # count must be a string
                            "time_in_force": "immediate_or_cancel", "self_trade_prevention_type": "taker_at_cross"}))
    assert e.value.definitive and s.seen == []


# -- read paths: portfolio and public market data ------------------------------------------------------------------

def test_portfolio_reads_through_the_sdk_keep_their_shapes():
    s = Script(CANNED)
    pf = KalshiPortfolio(KalshiAuth("kid", ed25519.Ed25519PrivateKey.generate(), clock_ms=lambda: NOW_MS),
                         min_interval=0, sleep=no_sleep)
    assert isinstance(pf._backend, SdkBackend)
    pf._backend.api_client.rest_client = FakeRest(s)
    fills, cur = run(pf.fills(min_ts=1793206800, cursor="abc"))
    assert fills[0]["ts_ms"] == 1793206800000 + 300_000 and cur is None
    assert s.seen[0]["query"] == {"min_ts": "1793206800", "cursor": "abc", "limit": "200"}
    assert run(pf.positions()) == ([{"ticker": TICKER, "position_fp": "-3.00"}], None)
    assert run(pf.balance()) == {"balance": 1234, "balance_dollars": "12.3400"}
    assert all(x["headers"]["KALSHI-ACCESS-TIMESTAMP"] == str(NOW_MS) for x in s.seen)


def test_public_market_reads_through_the_sdk_are_unsigned_and_keep_their_shapes():
    s = Script(CANNED)
    c = KalshiClient(series_ticker="KXBTC15M", min_interval=0, sleep=no_sleep)
    assert isinstance(c._backend, SdkBackend)
    assert c._backend.api_client.configuration.host == KalshiClient.BASE_URL
    c._backend.api_client.rest_client = FakeRest(s)
    markets, cur = run(c.markets(status="open"))
    assert cur == "next" and markets[0]["open_ts"] == 1793206800000
    assert s.seen[0]["query"] == {"series_ticker": "KXBTC15M", "status": "open", "limit": "200"}
    assert run(c.market(TICKER))["close_ts"] == 1793207700000
    assert run(c.orderbook_quote(TICKER)) == {"ticker": TICKER, "yes_bid": 0.55, "no_bid": 0.42, "yes_ask": 0.58,
                                              "no_ask": 0.45}
    assert not any(k.startswith("KALSHI-") for x in s.seen for k in x["headers"])


def test_public_reads_retry_a_429_on_the_same_request():
    s = Script({("GET", f"/markets/{TICKER}"): [r(429, {"error": "slow down"}), r(200, {"market": MARKET})]})
    c = KalshiClient(series_ticker="KXBTC15M", min_interval=0, sleep=no_sleep)
    c._backend.api_client.rest_client = FakeRest(s)
    assert run(c.market(TICKER))["ticker"] == TICKER and s.count("GET", f"/markets/{TICKER}") == 2


# -- shadow stays on httpx ------------------------------------------------------------------------------------------

def test_the_shadow_environment_keeps_the_in_memory_httpx_transport():
    gw = shadow_trading(max_order_cost=25.0)
    assert isinstance(gw._backend, HttpxBackend)
    assert isinstance(gw._backend.client._transport, httpx.AsyncBaseTransport)
