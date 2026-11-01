from btc_signal_engine.domain.models.events import IndexSecond, Trade
from btc_signal_engine.domain.settlement.index import index_seconds

T0 = 1_700_000_000_000


def tr(i, ts, price):
    return Trade(trade_id=i, ts=ts, ingest_ts=ts, price=price, size=0.1, buy=True)


def test_last_trade_in_second_wins_and_quiet_seconds_carry_forward():
    rows = index_seconds([tr(1, T0 + 100, 100.0), tr(2, T0 + 900, 101.0), tr(3, T0 + 2_500, 102.0)], T0, T0 + 4_000)
    assert [(r.ts - T0, r.price, r.last_trade_ts - T0) for r in rows] == [
        (0, 101.0, 900), (1000, 101.0, 900), (2000, 102.0, 2500), (3000, 102.0, 2500)]


def test_out_of_order_trade_does_not_move_price_backwards():
    rows = index_seconds([tr(2, T0 + 800, 101.0), tr(1, T0 + 200, 100.0)], T0, T0 + 1_000)
    assert rows[0].price == 101.0


def test_carry_from_previous_window_fills_leading_quiet_seconds():
    carry = IndexSecond(ts=T0 - 1_000, price=99.0, last_trade_ts=T0 - 1_500)
    rows = index_seconds([], T0, T0 + 2_000, carry=carry)
    assert [r.price for r in rows] == [99.0, 99.0]
    assert rows[0].last_trade_ts == T0 - 1_500


def test_mid_is_diagnostic_only():
    rows = index_seconds([tr(1, T0 + 10, 100.0)], T0, T0 + 1_000, mids={T0: 250.0})
    assert rows[0].price == 100.0 and rows[0].mid == 250.0
