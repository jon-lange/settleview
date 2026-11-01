import asyncio

from btc_signal_engine.application.kalshi_sync import KalshiSync
from btc_signal_engine.application.settlement import SettlementService
from btc_signal_engine.domain.models.events import IndexSecond

B = 1_700_000_100_000 - 1_700_000_100_000 % 900_000  # a 15m boundary


def seed_index(store, start, end, price_fn, fresh=True):
    store.add_index_seconds([IndexSecond(ts, price_fn(ts), ts if fresh else ts - 10_000) for ts in range(start, end, 1000)])


class FakeKalshi:
    def __init__(self, open_markets=(), settled=(), book=None):
        self.open, self.settled = list(open_markets), list(settled)
        self.book = book or {}

    async def orderbook_quote(self, ticker):
        return {"ticker": ticker, "yes_bid": None, "no_bid": None, "yes_ask": None, "no_ask": None, **self.book}

    async def markets(self, *, status, cursor=None, limit=200):
        return (self.open if status == "open" else self.settled), None

    async def market(self, ticker):
        raise NotImplementedError


def test_proxy_settles_a_5m_window_up(store):
    svc = SettlementService(store, store)
    seed_index(store, B - 60_000, B + 300_000, lambda ts: 100.0 if ts < B else 101.0)
    svc.open_window(300, B, 100.0, now=B)
    assert svc.settle_due(now=B + 300_000 + 2_000) == 1
    w = store.select("windows")[0]
    assert (w["status"], w["actual"], w["source"], w["open_avg"], w["close_avg"]) == ("RESOLVED", "UP", "proxy", 100.0, 101.0)


def test_window_with_stale_close_minute_becomes_unresolved_after_grace(store):
    svc = SettlementService(store, store)
    seed_index(store, B - 60_000, B + 240_000, lambda ts: 100.0)
    seed_index(store, B + 240_000, B + 300_000, lambda ts: 100.0, fresh=False)
    svc.open_window(300, B, 100.0, now=B)
    assert svc.settle_due(now=B + 302_000) == 0  # still waiting
    svc.settle_due(now=B + 330_000)
    assert store.select("windows")[0]["status"] == "UNRESOLVED"


def test_kalshi_result_overrides_proxy(store):  # AE3
    svc = SettlementService(store, store)
    seed_index(store, B - 60_000, B + 900_000, lambda ts: 100.0 if ts < B else 100.5)
    svc.open_window(900, B, 100.0, now=B)
    svc.settle_due(now=B + 902_000)
    assert store.select("windows")[0]["actual"] == "UP"
    market = {"ticker": "KXBTC15M-TEST", "open_ts": B, "close_ts": B + 900_000, "status": "finalized",
              "floor_strike": 100.02, "expiration_value": "100.47", "result": "no"}
    applied = asyncio.run(KalshiSync(FakeKalshi(settled=[market]), store, svc).sync_settled(now=B + 960_000))
    assert applied == 1
    w = store.select("windows")[0]
    assert (w["actual"], w["source"], w["proxy_actual"], w["kalshi_result"]) == ("DOWN", "kalshi", "UP", "no")
    assert store.select("kalshi_markets")[0]["result"] == "no"
    assert store.select("calibration") == []  # proxy-vs-Kalshi calibration rows are no longer written


def test_settled_sync_reads_the_store_off_the_event_loop(store, monkeypatch):
    """The periodic settled sync's `windows` reads run in a worker thread (P0 #5: no store read on the loop)."""
    import threading

    svc = SettlementService(store, store)
    svc.open_window(900, B, 100.0, now=B)
    market = {"ticker": "KXBTC15M-T", "open_ts": B, "close_ts": B + 900_000, "result": "yes", "floor_strike": 100.0}
    seen, select = [], store.select

    def spy(table, *a, **kw):
        seen.append(threading.current_thread().name)
        return select(table, *a, **kw)

    monkeypatch.setattr(store, "select", spy)
    assert asyncio.run(KalshiSync(FakeKalshi(settled=[market]), store, svc).sync_settled(now=B + 960_000)) == 1
    assert seen and threading.current_thread().name not in seen


def test_quote_needs_both_sides_and_gives_up_after_30s(store):
    svc = SettlementService(store, store)
    svc.open_window(900, B, 100.0, now=B)
    market = {"ticker": "T", "open_ts": B, "floor_strike": 100.0}  # the list only discovers; the book prices
    sync = KalshiSync(FakeKalshi(open_markets=[market], book={"yes_ask": 0.52}), store, svc)  # one-sided
    assert asyncio.run(sync.capture_quote(B, now=B + 5_000)) is False
    assert asyncio.run(sync.capture_quote(B, now=B + 40_000)) is True  # gave up
    assert store.select("windows")[0]["yes_ask"] is None
    two_sided = {"yes_ask": 0.52, "no_ask": 0.49}
    assert asyncio.run(KalshiSync(FakeKalshi(open_markets=[market], book=two_sided), store, svc).capture_quote(
        B, now=B + 5_000))
    w = store.select("windows")[0]
    assert (w["yes_ask"], w["no_ask"], w["kalshi_ticker"]) == (0.52, 0.49, "T")
