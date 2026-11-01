"""Read-only Kalshi account link (U19): the portfolio client (Play reads the balance through it).

All Kalshi payloads here are SYNTHETIC, shaped after the documented responses of
GET /portfolio/fills, /portfolio/positions and /portfolio/balance (docs.kalshi.com).
"""

from __future__ import annotations

import asyncio

import httpx
from cryptography.hazmat.primitives.asymmetric import rsa

from btc_signal_engine.adapters.kalshi.auth import KalshiAuth
from btc_signal_engine.adapters.kalshi.portfolio import KalshiPortfolio, portfolio_from_settings

NOW = 1793210000000
B1 = 1793206800000  # 2026-10-28 17:00Z = 13:00 ET; market KXBTC15M-26OCT281315-15 (close 13:15 ET)
T1 = "KXBTC15M-26OCT281315-15"
T2 = "KXBTC15M-26OCT281330-30"  # not in kalshi_markets: open_ts parsed from the ticker
B2 = 1793207700000


def _fill(fill_id: str, ticker: str, outcome: str, yes_price: str, count: str = "10.00", created="2026-10-28T17:05:00Z",
          fee="0.17", **extra) -> dict:
    no_price = f"{1 - float(yes_price):.4f}"
    return {"fill_id": fill_id, "trade_id": fill_id, "order_id": "o-" + fill_id, "ticker": ticker,
            "market_ticker": ticker, "outcome_side": outcome, "book_side": "bid" if outcome == "yes" else "ask",
            "count_fp": count, "yes_price_dollars": yes_price, "no_price_dollars": no_price, "is_taker": True,
            "created_time": created, "fee_cost": fee, "exchange_index": 0, **extra}


SYN_FILLS = [
    _fill("f1", T1, "yes", "0.4000"),  # bought YES (UP) at 0.40
    _fill("f2", T2, "no", "0.5500", count="4.00", created="2026-10-28T17:20:00Z"),  # NO (DOWN) at 0.45
    _fill("f3", "KXETH15M-26OCT281315-15", "yes", "0.5000"),  # not the BTC series
]
SYN_POSITIONS = [{"ticker": T2, "exchange_index": 0, "total_traded_dollars": "1.8000", "position_fp": "-4.00",
                  "market_exposure_dollars": "1.8000", "realized_pnl_dollars": "0.0000", "fees_paid_dollars": "0.0700",
                  "last_updated_ts": "2026-10-28T17:20:00Z"}]
SYN_BALANCE = {"balance": 12345, "balance_dollars": "123.4500", "portfolio_value": 2000, "updated_ts": 1793209999}


# -- adapter ---------------------------------------------------------------------------
def _portfolio(handler) -> KalshiPortfolio:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    async def no_sleep(_s: float) -> None:
        return None

    return KalshiPortfolio(KalshiAuth("kid", key), transport=httpx.MockTransport(handler), min_interval=0,
                           sleep=no_sleep)


def test_fills_request_is_signed_paged_and_timestamps_normalized():
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"fills": SYN_FILLS[:1], "cursor": "next"})

    fills, cursor = asyncio.run(_portfolio(handler).fills(min_ts=1793200000, cursor="c1"))
    req = seen[0]
    assert req.method == "GET" and req.url.path == "/trade-api/v2/portfolio/fills"
    assert dict(req.url.params) == {"min_ts": "1793200000", "cursor": "c1", "limit": "200"}
    assert all(h in req.headers for h in ("KALSHI-ACCESS-KEY", "KALSHI-ACCESS-TIMESTAMP", "KALSHI-ACCESS-SIGNATURE"))
    assert cursor == "next" and fills[0]["ts_ms"] == 1793207100000


def test_positions_and_balance_use_documented_paths():
    paths: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        paths.append(req.url.path)
        if req.url.path.endswith("/positions"):
            return httpx.Response(200, json={"market_positions": SYN_POSITIONS, "event_positions": [], "cursor": ""})
        return httpx.Response(200, json=SYN_BALANCE)

    p = _portfolio(handler)
    positions, cursor = asyncio.run(p.positions())
    balance = asyncio.run(p.balance())
    assert paths == ["/trade-api/v2/portfolio/positions", "/trade-api/v2/portfolio/balance"]
    assert positions == SYN_POSITIONS and cursor is None and balance == SYN_BALANCE


def test_retries_429_then_succeeds():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(429) if len(calls) == 1 else httpx.Response(200, json=SYN_BALANCE)

    assert asyncio.run(_portfolio(handler).balance()) == SYN_BALANCE and len(calls) == 2


def test_client_exposes_no_order_methods():
    public = {n for n in dir(KalshiPortfolio) if not n.startswith("_")}
    assert public == {"fills", "positions", "balance", "aclose", "BASE_URL"}


def test_not_configured_means_no_client(tmp_path):
    assert portfolio_from_settings(None, None) is None
    assert portfolio_from_settings("kid", None) is None
    assert portfolio_from_settings(None, tmp_path / "k.pem") is None
