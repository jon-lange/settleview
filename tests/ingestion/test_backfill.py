"""Coinbase REST backfill and gap repair against a mocked HTTP transport (no network)."""

from __future__ import annotations

import asyncio
import json
import random
from datetime import UTC, datetime
from pathlib import Path

import httpx

from btc_signal_engine.adapters.coinbase.backfill import backfill, repair_gaps
from btc_signal_engine.adapters.coinbase.rest import CoinbaseRest
from btc_signal_engine.domain.models.events import Trade
from btc_signal_engine.domain.settlement.index import index_seconds

FIXTURES = Path(__file__).parents[1] / "fixtures"
T0 = 1_793_000_000_000  # 2026-10-26, a whole hour in ms


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "000Z"


def _wire(t: Trade) -> dict:
    """A synthetic trade in Coinbase REST shape; `side` is the maker side."""
    return {"trade_id": t.trade_id, "side": "sell" if t.buy else "buy", "size": f"{t.size:.8f}",
            "price": f"{t.price:.2f}", "time": _iso(t.ts)}


class FakeCoinbase:
    """Serves trades by id cursor (newest first) and candles; records every request."""

    def __init__(self, trades: list[dict], fail_once: dict[str, int] | None = None) -> None:
        self.trades = sorted(trades, key=lambda t: -t["trade_id"])
        self.fail_once = dict(fail_once or {})  # after-cursor -> HTTP status to return once
        self.requests: list[dict[str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        if request.url.path.endswith("/candles"):
            self.requests.append({"path": "candles", **params})
            start = int(datetime.fromisoformat(params["start"]).timestamp())
            return httpx.Response(200, json=[[start, 1.0, 2.0, 1.5, 1.7, 3.0]])
        self.requests.append(params)
        status = self.fail_once.pop(params.get("after", ""), None)
        if status:
            return httpx.Response(status, json={"message": "slow down"})
        after = int(params["after"]) if "after" in params else None
        limit = int(params.get("limit", 1000))
        page = [t for t in self.trades if after is None or t["trade_id"] < after][:limit]
        return httpx.Response(200, json=page)

    def trade_requests(self) -> list[dict[str, str]]:
        return [r for r in self.requests if r.get("path") != "candles"]


def _rest(fake: FakeCoinbase, sleeps: list[float] | None = None) -> CoinbaseRest:
    async def sleep(s: float) -> None:
        if sleeps is not None:
            sleeps.append(s)

    client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler), base_url=CoinbaseRest.BASE_URL)
    return CoinbaseRest(client, sleep=sleep)


def _count(store, table: str) -> int:
    return len(store.select(table))


def test_recorded_page_writes_three_rows_and_rerun_writes_zero(store):
    page = json.loads((FIXTURES / "coinbase_trades_page_recorded.json").read_text())
    fake = FakeCoinbase(page)
    now = T0 + 10**9  # 10,000 days back reaches past every fixture trade: page until history is empty
    asyncio.run(backfill(store, _rest(fake), days=10_000, now_ms=now))
    rows = store.select("trades", order_by=["trade_id"])
    assert len(rows) == 3
    assert all(r["backfilled"] and r["ingest_ts"] == r["ts"] for r in rows)
    by_id = {r["trade_id"]: r for r in rows}
    assert by_id[1111596345]["buy"] is True  # maker side "sell" => aggressor bought
    assert by_id[1111596347]["buy"] is False
    assert by_id[1111596345]["ts"] == 1793208638766  # 2026-10-28T17:30:38.766261Z, truncated to ms
    assert _count(store, "index_1s") == 2  # seconds :38 and :39
    candles = store.select("candles")
    assert candles and all(c["source"] == "rest" and c["granularity"] == 60 for c in candles)

    before = len(fake.trade_requests())
    written = asyncio.run(backfill(store, _rest(fake), days=10_000, now_ms=now))
    assert written["trades"] == 0
    assert _count(store, "trades") == 3
    assert len(fake.trade_requests()) == before + 1  # one probe past the oldest id, which is empty


def _synthetic(ids: range, start_ts: int, step_ms: int, seed: int = 1) -> list[Trade]:
    rng = random.Random(seed)
    price = 80_000.0
    out = []
    for n, i in enumerate(ids):
        price += rng.choice((-1, 1)) * rng.random() * 5
        out.append(Trade(i, start_ts + n * step_ms, start_ts + n * step_ms, round(price, 2),
                         round(rng.random(), 8), rng.random() < 0.5, backfilled=True))
    return out


def test_id_jump_gap_requests_exactly_the_missing_range_and_closes_gap(store):
    all_trades = _synthetic(range(1000, 1501), T0, 100)
    known = [all_trades[0], all_trades[-1]]
    store.add_trades(known)
    store.add_index_seconds(index_seconds(known, T0, T0 + 51_000))  # what live would have written
    store.insert("gaps", [{"start_ts": known[0].ts, "end_ts": known[1].ts, "cause": "id_jump",
                           "from_trade_id": 1000, "to_trade_id": 1500, "repaired": False, "created_ts": T0}])
    fake = FakeCoinbase([_wire(t) for t in all_trades[1:-1]])

    repaired = asyncio.run(repair_gaps(store, _rest(fake)))

    assert repaired == 1
    assert fake.requests == [{"after": "1500", "limit": "499"}]
    assert _count(store, "trades") == 501
    assert store.select("gaps")[0]["repaired"] is True
    assert store.index_range(T0, T0 + 51_000) == index_seconds(all_trades, T0, T0 + 51_000)


def test_time_gap_fetches_trades_covering_the_window(store):
    all_trades = _synthetic(range(100, 201), T0, 1000)  # one trade per second
    known = [all_trades[0], all_trades[-1]]
    store.add_trades(known)
    store.add_index_seconds(index_seconds(known, T0, T0 + 101_000))  # what live would have written
    store.insert("gaps", [{"start_ts": T0 + 1000, "end_ts": T0 + 99_000, "cause": "stale",
                           "repaired": False, "created_ts": T0}])
    fake = FakeCoinbase([_wire(t) for t in all_trades[1:-1]])

    asyncio.run(repair_gaps(store, _rest(fake)))

    assert fake.trade_requests()[0]["after"] == "200"  # pages back from the first trade after the gap
    assert _count(store, "trades") == 101
    assert store.select("gaps")[0]["repaired"] is True
    assert store.index_range(T0, T0 + 101_000) == index_seconds(all_trades, T0, T0 + 101_000)


def test_429_backs_off_and_retries_without_skipping_a_page(store, monkeypatch, jitter_top):
    trades = _synthetic(range(1, 10), T0, 1000)
    fake = FakeCoinbase([_wire(t) for t in trades], fail_once={"7": 429})
    sleeps: list[float] = []
    from btc_signal_engine.adapters.coinbase import backfill as bf

    monkeypatch.setattr(bf, "PAGE_SIZE", 3)
    # jitter_top: the top of the jitter, the full 1 s first backoff
    asyncio.run(backfill(store, _rest(fake, sleeps), days=10_000, now_ms=T0 + 10**9))

    assert [r.get("after") for r in fake.trade_requests()] == [None, "7", "7", "4", "1"]
    assert sorted(r["trade_id"] for r in store.select("trades")) == list(range(1, 10))
    assert any(s >= 1.0 for s in sleeps)  # a real back-off, not just the rate-limit spacing


def test_rebuilt_hour_matches_index_seconds_over_the_same_trades(store):
    rng = random.Random(7)
    ts_list = sorted(T0 + rng.randrange(0, 3_600_000) for _ in range(2500))
    trades = [Trade(10_000 + n, ts, ts, round(80_000 + rng.uniform(-50, 50), 2), 0.01, rng.random() < 0.5, True)
              for n, ts in enumerate(ts_list)]
    fake = FakeCoinbase([_wire(t) for t in trades])

    asyncio.run(backfill(store, _rest(fake), days=10_000, now_ms=T0 + 10**9))

    end = ts_list[-1] - ts_list[-1] % 1000 + 1000  # backfill indexes up to its newest trade's second
    expected = index_seconds(trades, T0, end)
    got = store.index_range(T0, T0 + 3_600_000)
    assert got == expected
    traded = {t.ts - t.ts % 1000 for t in trades}
    assert {r.ts for r in got} >= traded
