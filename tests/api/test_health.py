"""/api/health (R16): each venue stream's state, the writer backlog, each loop's last heartbeat and restarts, stalls
in the last hour, uptime and memory; the overall status is `degraded` (naming the part) when any part is stale."""

from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from btc_signal_engine.adapters.http.app import create_app
from btc_signal_engine.bootstrap.app import Runtime
from btc_signal_engine.bootstrap.config import Settings
from btc_signal_engine.domain.models.events import Trade
from tests.pc import paired


class StreamDouble:
    """Stands in for a live stream: the health() contract KalshiStream / PolymarketMarketStream expose."""

    def __init__(self, *, connected: bool, stale: bool, last_msg_ms: int | None) -> None:
        self.h = {"connected": connected, "stale": stale, "last_msg_ms": last_msg_ms}

    def health(self) -> dict:
        return dict(self.h)

    def close(self) -> None:
        pass


@pytest.fixture
def rt(tmp_path, monkeypatch):
    for k in ("BTCFLOW_TRADING_STREAM", "BTCFLOW_API_HOST", "BTCFLOW_ACCESS_PIN", "BTCFLOW_POLYMARKET_KEY_ID",
              "BTCFLOW_POLYMARKET_SECRET_KEY"):
        monkeypatch.delenv(k, raising=False)
    r = Runtime(Settings(_env_file=None, db_path=tmp_path / "health.db", trading_stream=False))
    yield r
    r.writer.close()


def client(rt) -> TestClient:
    return paired(TestClient(create_app(rt.services()), client=("127.0.0.1", 1),
                      base_url=f"http://127.0.0.1:{rt.settings.api_port}"), rt.settings.data_dir)


def fresh(rt) -> None:
    now = rt.clock.now_ms()
    rt.feed.on_trade(Trade(trade_id=1, ts=now, ingest_ts=now, price=80_000.0, size=0.01, buy=True))
    rt.market_stream = StreamDouble(connected=True, stale=False, last_msg_ms=now - 300)


def test_every_part_fresh_reports_live_with_every_field(rt):
    fresh(rt)
    rt.writer.insert("gaps", [])  # one queued write: the backlog is counted, not guessed
    h = client(rt).get("/api/health").json()
    assert h["status"] == "live" and h["degraded"] == []
    assert h["feed"] == "live" and "gaps_open" in h and "lag_ms" in h
    k = h["streams"]["kalshi_market"]
    assert k["venue"] == "kalshi" and k["env"] == "prod" and k["connected"] is True and k["stale"] is False
    assert 300 <= k["last_msg_age_ms"] < 5_000
    assert h["writer_pending"] == 1
    assert h["stalls_last_hour"] == 0
    assert h["uptime_s"] >= 0
    assert h["memory_mb"] is None or h["memory_mb"] > 1


def test_a_stale_kalshi_stream_degrades_and_is_named(rt):
    fresh(rt)
    rt.market_stream = StreamDouble(connected=True, stale=True, last_msg_ms=rt.clock.now_ms() - 20_000)
    h = client(rt).get("/api/health").json()
    assert h["status"] == "degraded" and h["degraded"] == ["kalshi_market"]
    assert h["streams"]["kalshi_market"]["stale"] is True


def test_a_stale_trading_stream_per_environment_is_reported(rt):
    fresh(rt)
    rt.trading_streams["demo"] = StreamDouble(connected=False, stale=True, last_msg_ms=None)
    rt.trading_streams["pm_prod"] = StreamDouble(connected=True, stale=False, last_msg_ms=rt.clock.now_ms())
    h = client(rt).get("/api/health").json()
    assert h["streams"]["trading_demo"] == {"venue": "kalshi", "env": "demo", "connected": False, "stale": True,
                                            "last_msg_age_ms": None}
    assert h["streams"]["trading_pm_prod"]["venue"] == "polymarket"
    assert h["status"] == "degraded" and h["degraded"] == ["trading_demo"]


def test_a_coinbase_feed_not_yet_live_degrades(rt):
    h = client(rt).get("/api/health").json()  # no trade received yet
    assert h["feed"] == "connecting" and h["status"] == "degraded" and "coinbase" in h["degraded"]


def test_a_loop_restarted_by_the_supervisor_shows_its_restart_count(rt):
    async def go():
        calls = 0

        async def flaky():
            nonlocal calls
            calls += 1
            raise RuntimeError("boom")
        rt.supervisor.start("flaky", flaky)
        for _ in range(100):
            await asyncio.sleep(0.01)
            if rt.supervisor.heartbeats()["flaky"]["restarts"] >= 1:
                break
        return client(rt).get("/api/health").json()

    started = time.time()
    h = asyncio.run(go())
    loop = h["loops"]["flaky"]
    assert loop["restarts"] == 1 and loop["critical"] is False
    assert 0 <= loop["last_beat_age_s"] <= time.time() - started + 1


def test_writes_waiting_in_a_failed_batch_are_counted_and_degrade_naming_the_writer(rt):
    fresh(rt)
    from sqlalchemy.exc import OperationalError
    apply = rt.writer._apply

    def down(batch):  # the database is unreachable for this one try
        rt.writer._apply = apply
        raise OperationalError("connect", {}, ConnectionRefusedError("connection refused"))

    rt.writer._apply = down
    rt.writer.insert("gaps", [{"start_ts": 1, "cause": "x", "repaired": False, "created_ts": 1}])
    asyncio.run(rt.writer.flush())  # this batch's transaction fails and waits for retry
    rt.writer.insert("gaps", [])  # a new write queued behind it
    h = client(rt).get("/api/health").json()
    assert h["writer_pending"] == 2 and h["writer_failed_batches"] == 1
    assert h["status"] == "degraded" and h["degraded"] == ["writer"]
    assert h["writer_outage_s"] is not None and h["writer_dead_lettered"] == 0


def test_a_database_outage_is_a_degraded_part_not_a_500(rt, monkeypatch):
    """Found in the P0 integration smoke: with Postgres stopped the paired PC's /api/health answered 500."""
    fresh(rt)

    def down(*a, **k):
        raise OSError("failed to resolve host 'postgres'")
    c = client(rt)  # built (and its access guard loaded) before the outage
    monkeypatch.setattr(rt.store, "select", down)
    r = c.get("/api/health")
    assert r.status_code == 200
    h = r.json()
    assert h["status"] == "degraded" and "database" in h["degraded"] and h["gaps_open"] is None


def test_without_a_session_the_healthcheck_gets_only_its_status_and_no_database_query(rt, monkeypatch):
    """The container healthcheck: loopback inside the container, no PC cookie, no PIN session (never the PC)."""
    fresh(rt)
    app = create_app(rt.services())
    monkeypatch.setattr(rt.store, "select", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no query")))
    c = TestClient(app, client=("127.0.0.1", 1), base_url=f"http://127.0.0.1:{rt.settings.api_port}")
    r = c.get("/api/health")
    assert r.status_code == 200 and r.json() == {"status": "up"}
    assert c.get("/api/play/limits").status_code in (401, 403)  # nothing else without pairing
