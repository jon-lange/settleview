"""Kalshi PERPS (margin) client (real money): our own guarded REST client over httpx (KTD2).

This is the documented exception to "Kalshi REST goes through the official SDK": `kalshi_python_async` 3.35.0 has no
margin endpoints. The SDK backend's transport rules hold here too (backend.py): our single retry loop (`_send`) is the
only retry and re-sends the SAME body (same client_order_id); httpx's transport is built with
`retries=0` (httpcore re-sends nothing; it only moves a request that was never written to another pooled connection);
redirects are not followed (a 3xx is a non-definitive `MarginUnknown`, so KALSHI-ACCESS-* headers never go to another
host); idle pooled sockets are dropped after KEEPALIVE_S (30 s), below Kalshi's own idle close (45-60 s, measured
2026-10-29).

Verified (LEARNINGS "Kalshi perps (margin) API facts", demo reads 2026-10-30, and Kalshi's perps spec
`perps_openapi.yaml` / `perps_asyncapi.yaml` from docs.kalshi.com, read 2026-10-30):

* Hosts: REST https://external-api.kalshi.com/trade-api/v2 (perp_prod), https://external-api.demo.kalshi.co/trade-api/v2
  (perp_demo); WebSocket external-api-margin-ws[.demo].kalshi.co, path /trade-api/ws/v2/margin, signed like a REST GET
  of that path. The BTC ticker is KXBTCPERP on prod and KXBTCPERP1 on demo (domain/markets.py `allowed_perp_tickers`).
* Whole contracts only for BTC (`fractional_trading_enabled` false; the spec's general step is 0.01); tick $0.0001
  per contract; one contract = 0.0001 BTC.
* Create: POST /margin/orders (CreateMarginOrderRequest): client_order_id, side bid|ask, count (FixedPointCount string,
  0-2 decimals), price (FixedPointDollars string, 2-4 decimals), time_in_force fill_or_kill | good_till_canceled |
  immediate_or_cancel, self_trade_prevention_type taker_at_cross | maker (required; we send taker_at_cross: with no
  resting orders of ours, a self-cross can only be our own taker, which is then cancelled), reduce_only (rejected
  unless IOC or FOK), subaccount. Response 201: order_id, client_order_id, fill_count, remaining_count (IOC: final),
  average_fill_price and average_fee_paid (only when something filled). 409 is the spec's ConflictError. The spec
  sets no maxLength on a margin client_order_id (client_trigger_id has 64); we only require a non-empty string.
* Order-scoped calls (cancel DELETE /margin/orders/{id}, POST .../decrease, every /margin/cross exit-trigger call)
  take `subaccount` as a query parameter that DEFAULTS TO 0 (primary): this client always sends the configured one.
* MarginOrder (GET /margin/orders, /margin/orders/{id}) has no status and no subaccount field; the list filters by
  ticker, subaccount, status, min_ts, max_ts (Unix seconds), limit (default 10000), cursor.
* Exit triggers: PUT /margin/cross/positions/{ticker}/exit_trigger without count / anchor_order_id /
  client_trigger_id is the whole-position bracket (KTD6); a leg left out is cleared, a body with neither leg is
  rejected. PUT .../exit_trigger/{trigger_id} edits one bracket's legs (at least one).
* Settleview trades from ONE configured perps subaccount: the primary account 0 in production (the user's API tier
  cannot create numbered subaccounts, 2026-10-30, so KD4 changed). There are no transfers: the subaccount transfer
  endpoint is off the allow-list, like subaccount creation.
* Production reads omit `subaccount_type` on positions and balances although the spec marks it required: nothing here
  requires it.
* GET /margin/funding_history requires start_date AND end_date (YYYY-MM-DD, inclusive, UTC).
* Timestamps: REST date-times are RFC3339 strings; WebSocket timestamps (ts_ms, next_funding_time_ms) are integer
  milliseconds; the market schedule (next_close_ts / next_open_ts) and funding-rate history queries use integer
  seconds. `to_epoch_ms` is the one parser for all three.

Confirmed in production (real-money check 2026-10-30, LEARNINGS):

* a duplicate client_order_id answers 409 `order_already_exists`; a 23-character id is accepted;
* an IOC 0.5% through the touch is accepted (not refused by price banding) and fills at the touch;
* the fills list's `min_ts` is Unix seconds; the fee is 0.12% of notional rounded up to $0.0001;
* a stop past the liquidation price on a trigger write answers 400 `stop_loss_past_liquidation`.

Still per spec only: that the order lists show finished IOC/FOK orders, so a 409 can be resolved by list-and-match.

Parsed values: Kalshi's fixed-point strings (prices, dollars, counts) become `Decimal` everywhere (exact, on the
tick); fields the spec gives as JSON numbers (funding rates, leverage, fee rates) stay float as sent; every date-time
"X_time" becomes "X_ts" in epoch milliseconds. A value that cannot be parsed raises; nothing is defaulted.

Safety: `MarginAuth` signs only an allow-list of (method, path) pairs and only for its own environment's host;
anything else raises before a request exists. Excluded on purpose: cancel-all (DELETE /margin/orders), subaccount
creation, subaccount transfers, amend, order groups, FCM paths, intra-exchange transfers and every
/margin/isolated/* path. Every write is
checked first (`MarginViolation`, nothing sent): ticker allow-listed for the env, subaccount exactly the configured
one, price positive and on the tick, count positive and a whole multiple of the
contract's count step, IOC or FOK only (KTD5: no resting orders in v1), every exit reduce_only, an entry's notional
(count x price) within the perps-only hard ceiling. Key material is never in repr or logs.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from btc_signal_engine.adapters.kalshi.auth import KalshiSigner, PrivateKey, now_ms
from btc_signal_engine.adapters.retry import header, retry_after_s, retry_delay
from btc_signal_engine.domain.markets import PERP_ENVS, PERPS, PerpSpec, allowed_perp_tickers
from btc_signal_engine.domain.order_resolution import definitive_status
from btc_signal_engine.domain.timestamps import iso_to_ms

log = logging.getLogger(__name__)

PREFIX = "/trade-api/v2"
BASE_URLS = {
    "perp_prod": "https://external-api.kalshi.com/trade-api/v2",
    "perp_demo": "https://external-api.demo.kalshi.co/trade-api/v2",
}
WS_URLS = {
    "perp_prod": "wss://external-api-margin-ws.kalshi.com/trade-api/ws/v2/margin",
    "perp_demo": "wss://external-api-margin-ws.demo.kalshi.co/trade-api/ws/v2/margin",
}
KEEPALIVE_S = 30.0  # idle pooled sockets are closed after this; Kalshi closes them itself after 45-60 s idle
_ID = r"[A-Za-z0-9\-_]+"
_SEGMENT = re.compile(rf"^{_ID}$")
_TRIGGER = rf"{PREFIX}/margin/cross/positions/{_ID}/exit_trigger"
ALLOWED = (
    # Reads under /margin/ (markets, book, status, risk, balance, positions, orders, fills, funding, cross triggers),
    # never the isolated or FCM families.
    ("GET", re.compile(rf"^{PREFIX}/margin(?!/(?:isolated|fcm)(?:/|$))(?:/{_ID})+$")),
    ("GET", re.compile(rf"^{PREFIX}/account/limits/perps$")),
    ("POST", re.compile(rf"^{PREFIX}/margin/orders$")),
    ("DELETE", re.compile(rf"^{PREFIX}/margin/orders/{_ID}$")),  # cancel ONE order; never the collection
    ("POST", re.compile(rf"^{PREFIX}/margin/orders/{_ID}/decrease$")),
    ("PUT", re.compile(rf"^{_TRIGGER}(?:/{_ID})?$")),
    ("DELETE", re.compile(rf"^{_TRIGGER}(?:/{_ID})?$")),
    ("GET", re.compile(r"^/trade-api/ws/v2/margin$")),  # the WebSocket handshake; it carries no order commands
)
_RETRY = 3
# Order-list reads to find the order behind a 409 / lost reply on create (waits 0.5, 1.0, 1.5, 2.0 s between: ~5 s);
# the event-contract lists were seen lagging a placement by seconds (trading.py).
_FIND_TRIES = 5
_TIFS = ("immediate_or_cancel", "fill_or_kill")  # KTD5: no resting orders in v1
_STP = "taker_at_cross"
_CENT_STEP = Decimal("0.01")  # the spec's count granularity


class MarginViolation(RuntimeError):
    """A request the margin client must never make (path, host, ticker, subaccount, size, price, time in force).
    Nothing is sent."""

    definitive = True  # the application reads this: the order / trigger write certainly did not happen
    status = 0


class MarginError(RuntimeError):
    definitive: bool

    def __init__(self, status: int, body: str, what: str) -> None:
        super().__init__(f"{what} (HTTP {status}): {body[:300]}")
        self.status = status
        self.body = body


class MarginRejected(MarginError):
    """Kalshi answered 400/401/403/404/422 (domain/order_resolution.DEFINITIVE_4XX): the request had no effect. Any
    other 4xx (408, 425, 499, ...) is a MarginUnknown: it may have been acted on."""

    definitive = True

    def __init__(self, status: int, body: str) -> None:
        super().__init__(status, body, "Kalshi rejected the request")


class MarginUnknown(MarginError):
    """The outcome is not known: a lost reply, timeouts or 5xx/429 on every attempt, a redirect (status 3xx, not
    followed), or a create answered 409 whose order the lists do not show. Never treated as a rejection: the write may
    have happened and must be reconciled (R18). status 0 = no HTTP answer at all."""

    definitive = False

    def __init__(self, status: int, body: str) -> None:
        super().__init__(status, body, "outcome unknown")


# -- signing -----------------------------------------------------------------------------------------------------------
class MarginAuth(KalshiSigner):
    """Signs only allow-listed margin requests (ALLOWED: never cancel-all, subaccount create or transfer, or any
    /margin/isolated/* path), and only to its own environment's REST host. The margin stream calls `headers` for its
    handshake."""

    violation = MarginViolation
    refuses = "margin client refuses"

    def __init__(self, key_id: str, private_key: PrivateKey, *, env: str, clock_ms: Callable[[], int] = now_ms) -> None:
        if env not in PERP_ENVS:
            raise ValueError(f"margin env must be one of {PERP_ENVS}")
        self.env = env
        super().__init__(key_id, private_key, hosts=(httpx.URL(BASE_URLS[env]).host,), clock_ms=clock_ms)

    def __repr__(self) -> str:
        return f"MarginAuth(env={self.env!r}, <redacted>)"

    def allows(self, method: str, path: str) -> bool:
        return any(method == m and rx.match(path) for m, rx in ALLOWED)


# -- values ------------------------------------------------------------------------------------------------------------


def to_epoch_ms(value: Any, *, unit: str) -> int:
    """One timestamp parser -> epoch milliseconds. unit "rfc3339": a date-time string with an offset (REST; fractional
    seconds beyond microseconds are truncated); "ms": integer milliseconds (WebSocket); "s": integer seconds (market
    schedule, funding-rate queries). Anything else raises: no guessing a unit from the magnitude."""
    if unit == "rfc3339":
        return iso_to_ms(value)  # domain/timestamps.py: the one date-time parser
    if unit in ("ms", "s"):
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"not an integer timestamp: {value!r}")
        return value if unit == "ms" else value * 1000
    raise ValueError(f"unknown timestamp unit {unit!r}")


_OPTIONAL_LIMITS = ("default_notional_value_risk_limit", "member_notional_value_risk_limit",
                    "effective_account_notional_value_risk_limit")


def _dec(x: Any) -> Decimal | None:
    """A Kalshi fixed-point string (or JSON number) -> Decimal; None stays None; anything unreadable raises."""
    if x is None:
        return None
    if isinstance(x, bool):
        raise ValueError(f"not a number: {x!r}")
    try:
        d = Decimal(repr(x)) if isinstance(x, float) else Decimal(x)
    except (InvalidOperation, TypeError):
        raise ValueError(f"not a fixed-point number: {x!r}") from None
    if not d.is_finite():
        raise ValueError(f"not a finite number: {x!r}")
    return d


def _ts_key(k: str) -> str:
    return k if k.endswith("_ts") else k.removesuffix("_time") + "_ts"


def _parse(obj: dict, dec: tuple[str, ...] = (), ts: tuple[str, ...] = ()) -> dict:
    """A copy of a Kalshi object with its fixed-point fields as Decimal and its RFC3339 fields as "_ts" epoch ms."""
    out = dict(obj)
    for k in dec:
        if k in out:
            out[k] = _dec(out[k])
    for k in ts:
        if k in out:
            v = out.pop(k)
            out[_ts_key(k)] = None if v is None else to_epoch_ms(v, unit="rfc3339")
    return out


_ORDER = {"dec": ("price", "fill_count", "remaining_count"),
          "ts": ("expiration_time", "created_time", "last_update_time")}
_FILL = {"dec": ("count", "price", "entry_price", "fees", "realized_pnl"), "ts": ("created_time",)}
_POSITION = {"dec": ("position", "entry_price", "unrealized_pnl", "margin_used", "fees")}
_SUB_BALANCE = {"dec": ("position_value", "account_equity", "maintenance_margin", "initial_margin",
                        "resting_orders_margin", "available_balance")}
_RISK_POSITION = {"dec": ("position", "mark_price", "position_notional", "maintenance_margin_required",
                          "estimated_liquidation_price")}
_FUNDING = {"dec": ("mark_price", "funding_amount", "quantity"), "ts": ("funding_time",)}
_ESTIMATE = {"dec": ("mark_price", "premium_index"), "ts": ("computed_time", "next_funding_time", "premium_index_ts")}
_TRIGGER_FIELDS = {"dec": ("stop_loss_price", "take_profit_price", "trail_amount", "watermark_price",
                           "effective_stop_price", "count", "filled_count"), "ts": ("created_time", "updated_time")}
_MARKET = {"dec": ("contract_size", "underlying_multiplier", "tick_size", "price", "volume",
                   "volume_notional_value_dollars", "open_interest", "open_interest_notional_value_dollars",
                   "volume_24h", "volume_24h_notional_value_dollars", "bid", "bid_size_fp", "ask", "ask_size_fp")}


def parse_market(m: dict) -> dict:
    out = _parse(m, **_MARKET)
    for k in ("settlement_mark_price", "liquidation_mark_price", "reference_price"):  # TickerPrice {price, ts_ms}
        if out.get(k) is not None:
            out[k] = {"price": _dec(out[k]["price"]), "ts_ms": to_epoch_ms(out[k]["ts_ms"], unit="ms")}
    sched = out.get("schedule")
    if sched is not None:  # next_close_ts / next_open_ts are Unix SECONDS in the spec: converted to ms
        out["schedule"] = {**sched, **{k: (None if sched.get(k) is None else to_epoch_ms(sched[k], unit="s"))
                                       for k in ("next_close_ts", "next_open_ts") if k in sched}}
    return out


def _levels(levels: list) -> list[tuple[Decimal, Decimal]]:
    return [(_dec(p), _dec(q)) for p, q in levels]


def _need_segment(x: Any, what: str) -> str:
    """A value that goes into a URL path: letters, digits, '-' and '_' only (no '/', '?', '..')."""
    if not isinstance(x, str) or not _SEGMENT.match(x):
        raise MarginViolation(f"{what} {x!r} is not a plain identifier")
    return x


def _amount(x: Any, what: str) -> Decimal:
    """A wire amount given as Decimal, str or int (never a float: binary floats are not on any tick)."""
    if isinstance(x, (bool, float)) or not isinstance(x, (Decimal, str, int)):
        raise MarginViolation(f"{what} must be a Decimal, str or int (got {type(x).__name__})")
    try:
        d = Decimal(x)
    except InvalidOperation:
        raise MarginViolation(f"{what} {x!r} is not a number") from None
    if not d.is_finite() or d <= 0:
        raise MarginViolation(f"{what} must be positive (got {x!r})")
    return d


def _query_date(x: Any, what: str) -> str:
    if isinstance(x, date) and not isinstance(x, datetime):
        return x.isoformat()
    if isinstance(x, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", x):
        date.fromisoformat(x)  # raises on an impossible date
        return x
    raise ValueError(f"{what} must be a date or 'YYYY-MM-DD' (got {x!r})")


def _query_int(x: Any, what: str) -> int | None:
    if x is None:
        return None
    if not isinstance(x, int) or isinstance(x, bool):
        raise ValueError(f"{what} must be an int (got {x!r})")
    return x


# -- client ------------------------------------------------------------------------------------------------------------
class MarginClient:
    """Perps REST client for exactly one env (perp_demo | perp_prod) and one configured subaccount (0 = primary)."""

    def __init__(self, auth: MarginAuth, *, env: str, subaccount: int, max_notional: Decimal | int | float | str,
                 transport: httpx.AsyncBaseTransport | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, timeout: float = 10.0) -> None:
        """`transport` None = the real network (httpx with retries=0); tests pass a mock transport.
        `max_notional`: the perps-only hard ceiling on an entry's count x price ($), a bug guard sized for leveraged
        notional (separate from the event contracts' trading_order_hard_ceiling)."""
        if env not in PERP_ENVS:
            raise ValueError(f"margin env must be one of {PERP_ENVS}")
        if auth.env != env:
            raise ValueError(f"the signer is for {auth.env}, not {env}")
        if not isinstance(subaccount, int) or isinstance(subaccount, bool) or not 0 <= subaccount <= 63:
            raise ValueError("the Settleview perps subaccount must be 0 (primary) to 63")
        ceiling = _dec(max_notional)
        if ceiling is None or ceiling <= 0:
            raise ValueError("the perps notional ceiling must be a positive amount")
        self.env = env
        self.subaccount = subaccount
        self.base_url = BASE_URLS[env]
        self._max_notional = ceiling
        self._tickers = allowed_perp_tickers(env)
        self._specs: dict[str, PerpSpec] = {p.ticker(env): p for p in PERPS}
        if transport is None:
            transport = httpx.AsyncHTTPTransport(retries=0, limits=httpx.Limits(keepalive_expiry=KEEPALIVE_S))
        self._http = httpx.AsyncClient(base_url=self.base_url, auth=auth, transport=transport, timeout=timeout,
                                       follow_redirects=False)
        self._sleep = sleep

    def __repr__(self) -> str:
        return f"MarginClient(env={self.env!r}, subaccount={self.subaccount})"

    @property
    def max_notional(self) -> Decimal:
        return self._max_notional

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- guards -----------------------------------------------------------------------------------------------------
    def _ticker(self, ticker: Any) -> PerpSpec:
        if ticker not in self._tickers:
            raise MarginViolation(f"only {'/'.join(sorted(self._tickers))} may be traded on {self.env} "
                                  f"(got {ticker!r})")
        return self._specs[ticker]

    def _price(self, spec: PerpSpec, x: Any, what: str = "price") -> str:
        d = _amount(x, what)
        tick = Decimal(str(spec.tick))
        if d % tick != 0:
            raise MarginViolation(f"{what} {x!r} is not on the ${tick} tick")
        return str(d.quantize(tick))

    def _count(self, spec: PerpSpec, x: Any) -> str:
        d = _amount(x, "count")
        step = Decimal(str(spec.count_step))
        if d % _CENT_STEP != 0 or d % step != 0:
            raise MarginViolation(f"count {x!r} is not a whole multiple of {step} contracts")
        return str(d.quantize(_CENT_STEP))

    # -- transport ---------------------------------------------------------------------------------------------------
    async def _send(self, method: str, path: str, *, params: dict | None = None,
                    body: dict | None = None) -> httpx.Response:
        """One request, retried on a transport error, 429 or 5xx with the SAME body (the only retry anywhere). Returns
        a 2xx or 4xx response; a 3xx (not followed) or exhausted retries raise MarginUnknown."""
        q = {k: v for k, v in (params or {}).items() if v is not None}
        status, text = 0, ""
        for attempt in range(_RETRY):
            try:
                resp = await self._http.request(method, path, params=q or None, json=body)
            except httpx.TransportError as exc:  # may have reached Kalshi: same body again, never a second write
                status, text = 0, f"{type(exc).__name__}: {exc}"
                log.warning("margin %s %s transport error (%s); retry %d", method, path, text[:200], attempt + 1)
                if attempt + 1 < _RETRY:
                    await self._sleep(retry_delay(attempt))
                continue
            if 300 <= resp.status_code < 400:  # not followed; where it went is unknown, so not definitive
                raise MarginUnknown(resp.status_code, resp.text)
            if resp.status_code == 429 or resp.status_code >= 500:  # full jitter; a 429 honours its Retry-After
                status, text = resp.status_code, resp.text
                wait = retry_delay(attempt, status=status, retry_after=retry_after_s(header(resp.headers)))
                log.warning("margin %s %s HTTP %d; retry %d in %.2f s", method, path, status, attempt + 1, wait)
                if attempt + 1 < _RETRY:
                    await self._sleep(wait)
                continue
            return resp
        raise MarginUnknown(status, text or "request failed")

    async def _json(self, method: str, path: str, **kw: Any) -> Any:
        resp = await self._send(method, path, **kw)
        if resp.status_code >= 400:
            if definitive_status(resp.status_code):
                raise MarginRejected(resp.status_code, resp.text)
            raise MarginUnknown(resp.status_code, resp.text)  # 408 / 425 / 499 ...: a write may have happened
        return resp.json() if resp.content else {}

    # -- public and account reads -------------------------------------------------------------------------------------
    async def markets(self, *, status: str | None = None) -> list[dict]:
        d = await self._json("GET", "/margin/markets", params={"status": status})
        return [parse_market(m) for m in d["markets"]]

    async def market(self, ticker: str) -> dict:
        d = await self._json("GET", f"/margin/markets/{_need_segment(ticker, 'ticker')}")
        return parse_market(d["market"])

    async def orderbook(self, ticker: str, *, depth: int | None = None) -> dict:
        """{"bids": [(price, count)], "asks": [...]}, best first (the spec's order)."""
        d = await self._json("GET", f"/margin/markets/{_need_segment(ticker, 'ticker')}/orderbook",
                             params={"depth": _query_int(depth, "depth")})
        ob = d["orderbook"]
        return {"bids": _levels(ob.get("bids") or []), "asks": _levels(ob.get("asks") or [])}

    async def enabled(self) -> bool:
        """Kalshi's margin switch for this key's account (GET /margin/enabled); anything but a boolean raises."""
        got = (await self._json("GET", "/margin/enabled")).get("enabled")
        if not isinstance(got, bool):
            raise ValueError(f"/margin/enabled answered {got!r}, not true or false")
        return got

    async def exchange_status(self) -> dict:
        return _parse(await self._json("GET", "/margin/exchange/status"), ts=("exchange_estimated_resume_time",))

    async def risk_parameters(self) -> dict:
        return await self._json("GET", "/margin/risk_parameters")

    async def fee_tiers(self) -> dict:
        return await self._json("GET", "/margin/fee_tiers")

    async def account_limits(self) -> dict:
        return await self._json("GET", "/account/limits/perps")

    async def notional_risk_limit(self) -> dict:
        """The account's notional risk limits. Production sends "" for a limit that is not set (the account default,
        2026-10-30, beside a per-market $500): read as None here, for those limit fields only, never as a number."""
        raw = await self._json("GET", "/margin/notional_risk_limit")
        raw = {k: (None if k in _OPTIONAL_LIMITS and v == "" else v) for k, v in raw.items()}
        d = _parse(raw,
                   dec=("default_notional_value_risk_limit", "total_current_usage", "member_notional_value_risk_limit",
                        "effective_account_notional_value_risk_limit"))
        for k in ("notional_value_risk_limits_by_market_ticker", "current_usage_by_market_ticker"):
            if d.get(k) is not None:
                d[k] = {t: _dec(v) for t, v in d[k].items()}
        return d

    async def balance(self, *, compute_available_balance: bool) -> dict:
        """Every subaccount's balance (also how a configured subaccount is validated: there is no list endpoint).
        available_balance is 0 unless `compute_available_balance` (10x the rate-limit cost)."""
        d = await self._json("GET", "/margin/balance",
                             params={"compute_available_balance": bool(compute_available_balance)})
        return {**_parse(d, dec=("settled_funds",)),
                "subaccount_balances": [_parse(b, **_SUB_BALANCE) for b in d["subaccount_balances"]]}

    async def positions(self, *, subaccount: int | None = None, ticker: str | None = None) -> list[dict]:
        """Positions of every subaccount (cross, isolated, auto_rebalance) unless `subaccount` is given."""
        d = await self._json("GET", "/margin/positions",
                             params={"subaccount": _query_int(subaccount, "subaccount"), "ticker": ticker})
        return [_parse(p, **_POSITION) for p in d["positions"]]

    async def risk(self) -> dict:
        d = await self._json("GET", "/margin/risk")
        return {**_parse(d, dec=("total_position_notional", "total_maintenance_margin")),
                "positions": [_parse(p, **_RISK_POSITION) for p in d["positions"]]}

    async def orders(self, *, ticker: str | None = None, subaccount: int | None = None, status: str | None = None,
                     min_ts: int | None = None, max_ts: int | None = None, limit: int | None = None,
                     cursor: str | None = None) -> dict:
        """{"orders": [...], "cursor": str}. min_ts / max_ts are Unix seconds (the spec's query)."""
        d = await self._json("GET", "/margin/orders", params={
            "ticker": ticker, "subaccount": _query_int(subaccount, "subaccount"), "status": status,
            "min_ts": _query_int(min_ts, "min_ts"), "max_ts": _query_int(max_ts, "max_ts"),
            "limit": _query_int(limit, "limit"), "cursor": cursor})
        return {"orders": [_parse(o, **_ORDER) for o in d["orders"]], "cursor": d.get("cursor")}

    async def order(self, order_id: str) -> dict:
        d = await self._json("GET", f"/margin/orders/{_need_segment(order_id, 'order id')}")
        return _parse(d["order"], **_ORDER)

    async def fills(self, *, subaccount: int | None = None, min_ts: int | None = None, max_ts: int | None = None,
                    limit: int | None = None, cursor: str | None = None) -> dict:
        """{"fills": [...], "cursor": str}; every subaccount unless `subaccount`. min_ts / max_ts are Unix seconds."""
        d = await self._json("GET", "/margin/fills", params={
            "subaccount": _query_int(subaccount, "subaccount"), "min_ts": _query_int(min_ts, "min_ts"),
            "max_ts": _query_int(max_ts, "max_ts"), "limit": _query_int(limit, "limit"), "cursor": cursor})
        return {"fills": [_parse(f, **_FILL) for f in d["fills"]], "cursor": d.get("cursor")}

    async def funding_estimate(self, ticker: str) -> dict:
        d = await self._json("GET", "/margin/funding_rates/estimate",
                             params={"ticker": _need_segment(ticker, "ticker")})
        return _parse(d, **_ESTIMATE)

    async def funding_history(self, *, start_date: date | str, end_date: date | str, ticker: str | None = None,
                              subaccount: int | None = None) -> list[dict]:
        """Funding payments over an inclusive UTC date range (both dates are required by Kalshi)."""
        d = await self._json("GET", "/margin/funding_history", params={
            "start_date": _query_date(start_date, "start_date"), "end_date": _query_date(end_date, "end_date"),
            "ticker": ticker, "subaccount": _query_int(subaccount, "subaccount")})
        return [_parse(f, **_FUNDING) for f in d["funding_history"]]

    # -- orders --------------------------------------------------------------------------------------------------------
    async def create_order(self, *, ticker: str, side: str, count: Decimal | str | int, price: Decimal | str,
                           client_order_id: str, time_in_force: str, reduce_only: bool, purpose: str) -> dict:
        """Place one IOC/FOK limit order in the configured subaccount. `purpose` "entry" (open or add) or "exit"
        (reduce or close: must be reduce_only; exempt from the notional ceiling, since it can only shrink the
        position). Returns {order_id, client_order_id, fill_count, remaining_count, average_fill_price,
        average_fee_paid, duplicate} (Decimals; the averages None when nothing filled or when the order was found in
        the lists, which carry no averages).

        A retry reuses `client_order_id`. A 409, or a create whose every attempt was lost, is resolved by finding the
        order by client_order_id in the subaccount's order list (re-read for ~5 s); not found -> MarginUnknown
        (definitive False), never a rejection."""
        spec = self._ticker(ticker)
        if side not in ("bid", "ask"):
            raise MarginViolation(f"side must be bid or ask (got {side!r})")
        if purpose not in ("entry", "exit"):
            raise MarginViolation(f"purpose must be entry or exit (got {purpose!r})")
        if time_in_force not in _TIFS:
            raise MarginViolation(f"time_in_force must be one of {_TIFS} (no resting orders; got {time_in_force!r})")
        if not isinstance(reduce_only, bool):
            raise MarginViolation("reduce_only must be a bool")
        if purpose == "exit" and not reduce_only:
            raise MarginViolation("an exit must be reduce_only")
        if not isinstance(client_order_id, str) or not client_order_id:
            raise MarginViolation("client_order_id is required")
        count_s, price_s = self._count(spec, count), self._price(spec, price)
        notional = Decimal(count_s) * Decimal(price_s)
        if purpose == "entry" and notional > self._max_notional:
            raise MarginViolation(f"order notional ${notional:.2f} exceeds the ${self._max_notional:.2f} perps ceiling")
        body = {"ticker": ticker, "client_order_id": client_order_id, "side": side, "count": count_s,
                "price": price_s, "time_in_force": time_in_force, "self_trade_prevention_type": _STP,
                "reduce_only": reduce_only, "subaccount": self.subaccount}
        try:
            resp = await self._send("POST", "/margin/orders", body=body)
        except MarginUnknown as exc:  # lost reply / 5xx on every attempt / redirect: it may have been placed
            return await self._resolve(ticker, client_order_id, exc.status, exc.body)
        if resp.status_code == 409:
            return await self._resolve(ticker, client_order_id, 409, resp.text)
        if resp.status_code >= 400:
            if definitive_status(resp.status_code):
                raise MarginRejected(resp.status_code, resp.text)
            return await self._resolve(ticker, client_order_id, resp.status_code, resp.text)  # it may exist
        d = _parse(resp.json(), dec=("fill_count", "remaining_count", "average_fill_price", "average_fee_paid"))
        return {"order_id": d["order_id"], "client_order_id": d.get("client_order_id", client_order_id),
                "fill_count": d["fill_count"], "remaining_count": d["remaining_count"],
                "average_fill_price": d.get("average_fill_price"), "average_fee_paid": d.get("average_fee_paid"),
                "duplicate": False}

    async def _resolve(self, ticker: str, client_order_id: str, status: int, text: str) -> dict:
        try:
            found = await self._find_order(ticker, client_order_id)
        except MarginError as exc:
            raise MarginUnknown(status, f"{text[:200]} (order lists unreadable: {exc})") from exc
        if found is None:
            raise MarginUnknown(status, text)
        return {"order_id": found["order_id"], "client_order_id": client_order_id,
                "fill_count": found["fill_count"], "remaining_count": found["remaining_count"],
                "average_fill_price": None, "average_fee_paid": None, "duplicate": True, "order": found}

    async def _find_order(self, ticker: str, client_order_id: str) -> dict | None:
        for attempt in range(_FIND_TRIES):
            got = await self.orders(ticker=ticker, subaccount=self.subaccount)
            for o in got["orders"]:
                if o.get("client_order_id") == client_order_id and o.get("ticker") == ticker:
                    return o
            if attempt + 1 < _FIND_TRIES:
                await self._sleep(0.5 * (attempt + 1))
        return None

    async def cancel_order(self, order_id: str) -> dict:
        """Cancel ONE order in the configured subaccount. {order_id, client_order_id, reduced_by}."""
        d = await self._json("DELETE", f"/margin/orders/{_need_segment(order_id, 'order id')}",
                             params={"subaccount": self.subaccount})
        return _parse(d, dec=("reduced_by",))

    async def decrease_order(self, order_id: str, *, reduce_to: Decimal | str | int) -> dict:
        """Decrease an order TO a count (reduce_to only: a retried reduce_by after a lost reply could reduce twice)."""
        _need_segment(order_id, "order id")
        to = _dec(reduce_to) if not isinstance(reduce_to, float) else None
        if to is None or to < 0 or to % _CENT_STEP != 0:
            raise MarginViolation(f"reduce_to {reduce_to!r} must be a non-negative count in 0.01 steps")
        d = await self._json("POST", f"/margin/orders/{order_id}/decrease", params={"subaccount": self.subaccount},
                             body={"reduce_to": str(to.quantize(_CENT_STEP))})
        return _parse(d, dec=("remaining_count",))

    # -- exit triggers (cross, whole position: KTD6) -------------------------------------------------------------------
    def _trigger_path(self, ticker: str, trigger_id: str | None = None) -> str:
        self._ticker(ticker)
        path = f"/margin/cross/positions/{ticker}/exit_trigger"
        return path if trigger_id is None else f"{path}/{_need_segment(trigger_id, 'trigger id')}"

    def _legs(self, ticker: str, stop_loss_price: Any, take_profit_price: Any) -> dict:
        spec = self._ticker(ticker)
        legs = {}
        if stop_loss_price is not None:
            legs["stop_loss_price"] = self._price(spec, stop_loss_price, "stop_loss_price")
        if take_profit_price is not None:
            legs["take_profit_price"] = self._price(spec, take_profit_price, "take_profit_price")
        if not legs:
            raise MarginViolation("an exit trigger needs a stop-loss or take-profit leg (remove it with delete)")
        return legs

    @staticmethod
    def _kind(kind: str | None) -> str | None:
        if kind not in (None, "bracket", "trailing"):
            raise ValueError(f"unknown exit trigger kind {kind!r}")
        return kind

    async def exit_triggers(self, ticker: str, *, kind: str | None = None) -> list[dict]:
        d = await self._json("GET", self._trigger_path(ticker),
                             params={"subaccount": self.subaccount, "kind": self._kind(kind)})
        return [_parse(t, **_TRIGGER_FIELDS) for t in d["exit_triggers"]]

    async def set_exit_trigger(self, ticker: str, *, stop_loss_price: Decimal | str | None = None,
                               take_profit_price: Decimal | str | None = None) -> dict:
        """Replace the whole-position bracket: a leg left out is CLEARED by Kalshi, so the caller always passes both
        current legs. Never sends count, anchor_order_id or client_trigger_id."""
        body = {"kind": "bracket", **self._legs(ticker, stop_loss_price, take_profit_price)}
        d = await self._json("PUT", self._trigger_path(ticker), params={"subaccount": self.subaccount}, body=body)
        return _parse(d, **_TRIGGER_FIELDS)

    async def update_exit_trigger(self, ticker: str, trigger_id: str, *, stop_loss_price: Decimal | str | None = None,
                                  take_profit_price: Decimal | str | None = None) -> dict:
        """Edit one bracket's legs (a leg left out is cleared)."""
        body = self._legs(ticker, stop_loss_price, take_profit_price)
        d = await self._json("PUT", self._trigger_path(ticker, trigger_id), params={"subaccount": self.subaccount},
                             body=body)
        return _parse(d, **_TRIGGER_FIELDS)

    async def delete_exit_triggers(self, ticker: str, *, kind: str | None = None) -> None:
        """Cancel the position's triggers (Kalshi answers success even when none is live)."""
        await self._json("DELETE", self._trigger_path(ticker),
                         params={"subaccount": self.subaccount, "kind": self._kind(kind)})

    async def delete_exit_trigger(self, ticker: str, trigger_id: str) -> None:
        await self._json("DELETE", self._trigger_path(ticker, trigger_id), params={"subaccount": self.subaccount})
