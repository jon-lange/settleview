import asyncio

import httpx

from btc_signal_engine.adapters.kalshi.client import KalshiClient


def test_markets_carry_ms_timestamps_promised_by_the_port():
    body = {"markets": [{"ticker": "T", "open_time": "2026-10-28T16:30:00Z", "close_time": "2026-10-28T16:45:00Z"}],
            "cursor": ""}
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=body))
    client = KalshiClient(httpx.AsyncClient(transport=transport, base_url=KalshiClient.BASE_URL), min_interval=0)
    markets, cursor = asyncio.run(client.markets(status="open"))
    assert markets[0]["open_ts"] == 1793205000000 and markets[0]["close_ts"] == 1793205900000
    assert cursor is None


def test_kalshi_numbers_with_thousands_separators_parse():
    from btc_signal_engine.adapters.kalshi.backfill import market_row
    row = market_row({"ticker": "T", "open_time": "2026-09-01T16:00:00Z", "close_time": "2026-09-01T16:15:00Z",
                      "status": "finalized", "floor_strike": 77360.0, "expiration_value": "77,362.10", "result": "yes"})
    assert row["expiration_value"] == 77362.10
