from btc_signal_engine.adapters.coinbase.aggregator import book_metrics
from btc_signal_engine.adapters.coinbase.stream import LocalBook, trade_from_ccxt, trade_from_match

MATCH = {
    "type": "match", "trade_id": 1111596487, "product_id": "BTC-USD", "side": "sell",
    "size": "0.0123", "price": "80489.87", "time": "2026-10-28T12:00:00.123456Z",
}


def test_coinbase_match_side_sell_is_aggressor_buy():
    t = trade_from_match(MATCH, ingest_ts=1_793_188_800_400)
    assert t.buy is True
    assert (t.trade_id, t.price, t.size) == (1111596487, 80489.87, 0.0123)
    assert t.ts == 1_793_188_800_123  # 2026-10-28T12:00:00.123Z
    assert t.ingest_ts == 1_793_188_800_400
    assert t.backfilled is False


def test_coinbase_match_side_buy_is_aggressor_sell():
    assert trade_from_match({**MATCH, "side": "buy"}, ingest_ts=0).buy is False


def test_ccxt_trade_uses_raw_maker_side_not_ccxt_side():
    # ccxt reports the taker side ("buy") for this match; we derive it from the raw maker side.
    ccxt_trade = {"id": "1111596487", "side": "buy", "price": 80489.87, "amount": 0.0123, "info": MATCH}
    assert trade_from_ccxt(ccxt_trade, ingest_ts=5).buy is True


def test_local_book_applies_snapshot_and_l2_updates_best_first():
    book = LocalBook()
    book.load(bids=[["100.00", "1"], ["99.99", "2"]], asks=[["100.02", "3"], ["100.10", "4"]])
    book.apply("buy", "100.01", "0.5")   # new best bid
    book.apply("sell", "100.02", "0")    # best ask removed
    assert list(book.bids())[:2] == [(100.01, 0.5), (100.00, 1.0)]
    assert list(book.asks()) == [(100.10, 4.0)]


def test_book_metrics_depth_within_10_and_25_bps_of_mid():
    # mid = 10_000; 10 bps = 10.0, 25 bps = 25.0
    bids = [(9_999.0, 1.0), (9_991.0, 2.0), (9_980.0, 4.0), (9_970.0, 8.0)]
    asks = [(10_001.0, 1.5), (10_012.0, 2.5), (10_024.0, 3.5), (10_030.0, 9.0)]
    m = book_metrics(bids, asks, ts=42)
    assert (m.bid, m.ask, m.ts) == (9_999.0, 10_001.0, 42)
    assert (m.bid_depth_10, m.ask_depth_10) == (3.0, 1.5)
    assert (m.bid_depth_25, m.ask_depth_25) == (7.0, 7.5)
    assert (m.bid_size, m.ask_size) == (1.0, 1.5)  # size resting at the best levels
