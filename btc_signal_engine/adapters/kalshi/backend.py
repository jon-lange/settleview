"""The Kalshi REST transport under our clients: Kalshi's official SDK for prod/demo, httpx for the shadow exchange.

`KalshiTrading`, `KalshiPortfolio` and `KalshiClient` keep every guard, retry loop and payload mapping; they only ask
a backend for one named operation (`create_order`, `get_orders`, ...) and read back a response with `status_code`,
`text`, `json()` and `raise_for_status()`. Two backends implement the same operations:

* `SdkBackend`: `kalshi_python_async` (pinned 3.35.0) API classes, used for prod and demo. Requests are built by the
  SDK's typed models from the same field values our httpx path sends (fixed-point count strings, dollar price
  strings, client_order_id, ...). Its responses are read raw (`*_without_preload_content`): the body is parsed by us
  exactly as before, so a model that does not match a newer Kalshi answer can never turn a placed order into an
  error. Nothing below us re-sends a write: the SDK's aiohttp_retry wrapper is off (`Configuration(retries=None)`),
  and aiohttp's own silent one-time resend of an idempotent request (GET/PUT/DELETE) on a stale pooled connection is
  off too (`ClientSession._retry_connection = False`, checked loudly: see `_new_session`). The one exception is a
  READ: a GET that fails because its pooled keep-alive socket turned out to be closed (ServerDisconnectedError /
  ClientOSError, never a ClientConnectorError) is sent once more, at once, on a brand-new connection
  (`_NoRedirectSession`, logged). POST/PUT/DELETE are never re-sent below us; our own loop is the only retry for them
  and it reuses the same client_order_id. Connections: idle pooled sockets are dropped after KEEPALIVE_S (30 s),
  below Kalshi's own idle close (measured 2026-10-29 on external-api.kalshi.com: a socket idle 45 s was still open,
  one idle 60 s was closed), and host names are resolved by the OS (`ThreadedResolver`: getaddrinfo, as httpx does),
  not aiohttp's default c-ares resolver: on this PC c-ares intermittently stalled ~2 s and failed with "Timeout
  while contacting DNS servers" (every Kalshi SDK transport failure with a logged cause in data_run.log, 2026-10-29),
  while the OS resolver answered in 2-24 ms. Redirects are not followed
  (`allow_redirects=False`): a 3xx comes back to us as a non-definitive error, so KALSHI-ACCESS-* headers never go
  to another host. Every SDK request model is checked to carry exactly the fields we gave it (pydantic would drop an
  unknown one silently); a mismatch is refused before sending. Requests are signed by our auth object (its
  allow-list and its clock, `time.time()` unless injected), never by the SDK's own `KalshiAuth`.
* `HttpxBackend`: the original httpx requests, kept for the shadow exchange (an in-memory httpx transport that fakes
  Kalshi's HTTP; the SDK speaks aiohttp and cannot be pointed at it) and for tests built on it.

The WebSocket (stream.py) is ours: the SDK has none.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from typing import Any, Protocol
from urllib.parse import urlsplit

import aiohttp
import httpx
from kalshi_python_async import ApiClient, Configuration, rest
from kalshi_python_async.api import MarketApi, OrdersApi, PortfolioApi
from kalshi_python_async.exceptions import ApiException, OpenApiException
from kalshi_python_async.models import CreateOrderV2Request
from pydantic import ValidationError

log = logging.getLogger(__name__)

KEEPALIVE_S = 30.0  # idle pooled sockets are closed after this; Kalshi closes them itself after 45-60 s idle
# Requests sent through every SdkBackend since the last `take_request_counts()` (operation -> count), so the app can
# log how many real Kalshi REST calls it makes (aiohttp logs none, unlike httpx).
REQUESTS: Counter = Counter()


def take_request_counts() -> dict[str, int]:
    """The SDK requests counted since the previous call (operation -> count), and reset."""
    out = dict(REQUESTS)
    REQUESTS.clear()
    return out


class Signer(Protocol):
    def headers(self, method: str, path: str) -> dict[str, str]: ...


class TransportFailure(RuntimeError):
    """The SDK's connection failed or timed out. The request may have reached Kalshi: not definitive (like an httpx
    TransportError, it is retried with the same body)."""


class RequestNotSent(RuntimeError):
    """The SDK refused to build the request (a field its model does not accept). Nothing was sent."""

    definitive = True
    status = 0


class KalshiHTTPError(RuntimeError):
    """`raise_for_status()` of an SDK response: an HTTP error status, with Kalshi's body."""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"Kalshi HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body


class Reply:
    """An SDK response read raw: the shape of `httpx.Response` our clients use."""

    def __init__(self, status_code: int, content: bytes) -> None:
        self.status_code = status_code
        self.content = content

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.content)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise KalshiHTTPError(self.status_code, self.text)


def _new_session(maxsize: int, ssl_context: Any, *, fresh: bool = False) -> aiohttp.ClientSession:
    """The aiohttp session the SDK would build (rest.py), with aiohttp's silent reconnect-and-resend turned off, idle
    sockets dropped after KEEPALIVE_S and the OS resolver. `fresh` = a session that never reuses a socket (each
    request opens its own connection and closes it after): the one-time GET retry after a stale pooled socket. The
    resend switch is private: if a future aiohttp drops it, this raises instead of quietly resending (aiohttp is
    pinned, and a test asserts the attribute is honoured)."""
    pool: dict[str, Any] = {"force_close": True} if fresh else {"keepalive_timeout": KEEPALIVE_S}
    connector = aiohttp.TCPConnector(limit=maxsize, ssl=ssl_context, resolver=aiohttp.ThreadedResolver(), **pool)
    session = aiohttp.ClientSession(connector=connector, trust_env=True)
    if getattr(session, "_retry_connection", None) is not True:
        raise RuntimeError("aiohttp no longer has ClientSession._retry_connection; re-check its resend behaviour")
    session._retry_connection = False
    return session


# What aiohttp raises when a reused keep-alive socket was already closed by the server (aiohttp's own resend catches
# the same pair). A ClientConnectorError (a NEW connection could not be opened: DNS, refused, TLS) is a subclass of
# ClientOSError and is never retried here.
_STALE = (aiohttp.ServerDisconnectedError, aiohttp.ClientOSError)


class _NoRedirectSession:
    """What rest.RESTClientObject calls as its pool manager: the session, but every request with redirects off. A
    GET whose pooled socket turns out to be closed is sent once more, immediately, on a brand-new connection (the
    `fresh` session); nothing else is ever re-sent here."""

    def __init__(self, session: aiohttp.ClientSession, fresh: aiohttp.ClientSession | None = None) -> None:
        self.session = session
        self.fresh = fresh

    async def request(self, **args: Any) -> aiohttp.ClientResponse:
        try:
            return await self.session.request(allow_redirects=False, **args)
        except aiohttp.ClientConnectorError:
            raise
        except _STALE as exc:
            if str(args.get("method", "")).upper() != "GET" or self.fresh is None:
                raise  # a write is never re-sent below our own retry loop
            log.warning("Kalshi GET %s: the pooled connection was closed (%s: %s); retrying once on a new "
                        "connection", urlsplit(str(args.get("url") or "")).path, type(exc).__name__, exc)
            return await self.fresh.request(allow_redirects=False, **args)

    async def close(self) -> None:
        await self.session.close()
        if self.fresh is not None:
            await self.fresh.close()


class _RestClient(rest.RESTClientObject):
    """The SDK's REST client with our session (no resend, no redirects). Its retry wrapper needs `retries` set; it
    is None, so `request` always goes through this pool manager."""

    async def request(self, *args: Any, **kw: Any):
        if self.retries is not None:
            raise RuntimeError("the Kalshi SDK must run with retries=None")
        if self.pool_manager is None:
            self.pool_manager = _NoRedirectSession(_new_session(self.maxsize, self.ssl_context),
                                                   _new_session(self.maxsize, self.ssl_context, fresh=True))
        return await super().request(*args, **kw)


class _SdkAuth:
    """The SDK's auth hook (`ApiClient.kalshi_auth.create_auth_headers(method, url)`), answered by our signer: its
    allow-list and its clock. The signed path is the URL's path (with /trade-api/v2, without the query)."""

    def __init__(self, signer: Signer) -> None:
        self._signer = signer

    def __repr__(self) -> str:
        return "_SdkAuth(<redacted>)"

    def create_auth_headers(self, method: str, url: str) -> dict[str, str]:
        return self._signer.headers(method.upper(), urlsplit(url).path)


class Backend(Protocol):
    """One method per Kalshi REST operation our clients use. Arguments are the request's own field values."""

    async def create_order(self, body: dict) -> Any: ...
    async def cancel_order(self, order_id: str, market_ticker: str) -> Any: ...
    async def get_order(self, order_id: str) -> Any: ...
    async def get_orders(self, **params: Any) -> Any: ...
    async def get_positions(self, **params: Any) -> Any: ...
    async def get_fills(self, **params: Any) -> Any: ...
    async def get_balance(self) -> Any: ...
    async def get_market(self, ticker: str) -> Any: ...
    async def get_orderbook(self, ticker: str) -> Any: ...
    async def get_markets(self, **params: Any) -> Any: ...
    async def aclose(self) -> None: ...


class HttpxBackend:
    """The original httpx requests (method, path, query, JSON body), byte for byte."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def create_order(self, body: dict) -> httpx.Response:
        return await self.client.request("POST", "/portfolio/events/orders", json=body)

    async def cancel_order(self, order_id: str, market_ticker: str) -> httpx.Response:
        return await self.client.request("DELETE", f"/portfolio/events/orders/{order_id}",
                                         params={"market_ticker": market_ticker})

    async def get_order(self, order_id: str) -> httpx.Response:
        return await self.client.request("GET", f"/portfolio/orders/{order_id}")

    async def get_orders(self, **params: Any) -> httpx.Response:
        return await self.client.request("GET", "/portfolio/orders", params=params)

    async def get_positions(self, **params: Any) -> httpx.Response:
        return await self.client.request("GET", "/portfolio/positions", params=params)

    async def get_fills(self, **params: Any) -> httpx.Response:
        return await self.client.request("GET", "/portfolio/fills", params=params)

    async def get_balance(self) -> httpx.Response:
        return await self.client.request("GET", "/portfolio/balance")

    async def get_market(self, ticker: str) -> httpx.Response:
        return await self.client.request("GET", f"/markets/{ticker}")

    async def get_orderbook(self, ticker: str) -> httpx.Response:
        return await self.client.request("GET", f"/markets/{ticker}/orderbook")

    async def get_markets(self, **params: Any) -> httpx.Response:
        return await self.client.request("GET", "/markets", params=params)

    async def aclose(self) -> None:
        await self.client.aclose()


def _model(cls, **fields: Any):
    """An SDK request model from our field values. A value it refuses, or a field it would drop (pydantic ignores
    unknown keys) or add, means nothing is sent: the serialized keys must be exactly the non-None keys we gave."""
    try:
        model = cls(**fields)
    except ValidationError as exc:
        raise RequestNotSent(f"Kalshi SDK refused the request: {exc}") from exc
    given = {k for k, v in fields.items() if v is not None}
    sent = set(model.to_dict())
    if sent != given:
        raise RequestNotSent(f"Kalshi SDK {cls.__name__} would not send exactly our fields "
                             f"(dropped {sorted(given - sent)}, added {sorted(sent - given)})")
    return model


def _ints(params: dict[str, Any]) -> dict[str, Any]:
    """Query timestamps as int (the SDK wants strict ints; a stored REAL such as 1793206800.0 is converted, not
    dropped)."""
    return {k: (int(v) if k in ("min_ts", "max_ts") and v is not None else v) for k, v in params.items()}


class SdkBackend:
    """Kalshi's official async SDK. `signer` None = unsigned (public market data)."""

    def __init__(self, host: str, *, signer: Signer | None, timeout: float) -> None:
        config = Configuration(host=host, retries=None)  # None: no aiohttp_retry wrapper, our loop is the only retry
        self.api_client = ApiClient(config)
        self.api_client.rest_client = _RestClient(config)  # no resend, no redirects (see _new_session)
        if signer is not None:
            self.api_client.kalshi_auth = _SdkAuth(signer)
        self.timeout = float(timeout)  # total seconds per request (the SDK's own default is 5 minutes)
        self.orders = OrdersApi(self.api_client)
        self.portfolio = PortfolioApi(self.api_client)
        self.markets = MarketApi(self.api_client)

    def __repr__(self) -> str:
        return f"SdkBackend({self.api_client.configuration.host!r})"

    async def _call(self, fn, *args: Any, **kw: Any) -> Reply:
        """Run one `*_without_preload_content` SDK call and read its raw response."""
        REQUESTS[getattr(fn, "__name__", "call").removesuffix("_without_preload_content")] += 1
        try:
            resp = await fn(*args, _request_timeout=self.timeout, **kw)
            try:
                reply = Reply(resp.status, await resp.read())
            finally:
                resp.release()
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise TransportFailure(f"{type(exc).__name__}: {exc}") from exc
        except RuntimeError as exc:  # exactly aiohttp's closed session; our own guards subclass RuntimeError
            if type(exc) is RuntimeError and str(exc) == "Session is closed":
                raise TransportFailure("aiohttp session is closed") from exc
            raise
        except ApiException as exc:  # raised before any HTTP status exists (e.g. a body it cannot encode)
            if exc.status:
                return Reply(int(exc.status), (exc.body or "").encode("utf-8"))
            raise RequestNotSent(str(exc.reason or exc)) from exc
        except (ValidationError, OpenApiException) as exc:  # a model refused a field value: nothing was sent
            raise RequestNotSent(f"Kalshi SDK refused the request: {exc}") from exc
        if 300 <= reply.status_code < 400:  # not followed; where it went is unknown, so not definitive
            raise KalshiHTTPError(reply.status_code, reply.text)
        return reply

    async def create_order(self, body: dict) -> Reply:
        return await self._call(self.orders.create_order_v2_without_preload_content,
                                create_order_v2_request=_model(CreateOrderV2Request, **body))

    async def cancel_order(self, order_id: str, market_ticker: str) -> Reply:
        return await self._call(self.orders.cancel_order_v2_without_preload_content, order_id=order_id,
                                market_ticker=market_ticker)

    async def get_order(self, order_id: str) -> Reply:
        return await self._call(self.orders.get_order_without_preload_content, order_id=order_id)

    async def get_orders(self, **params: Any) -> Reply:
        return await self._call(self.orders.get_orders_without_preload_content, **_ints(params))

    async def get_positions(self, **params: Any) -> Reply:
        return await self._call(self.portfolio.get_positions_without_preload_content, **params)

    async def get_fills(self, **params: Any) -> Reply:
        return await self._call(self.portfolio.get_fills_without_preload_content, **_ints(params))

    async def get_balance(self) -> Reply:
        return await self._call(self.portfolio.get_balance_without_preload_content)

    async def get_market(self, ticker: str) -> Reply:
        return await self._call(self.markets.get_market_without_preload_content, ticker=ticker)

    async def get_orderbook(self, ticker: str) -> Reply:
        return await self._call(self.markets.get_market_orderbook_without_preload_content, ticker=ticker)

    async def get_markets(self, **params: Any) -> Reply:
        return await self._call(self.markets.get_markets_without_preload_content, **params)

    async def aclose(self) -> None:
        await self.api_client.close()
