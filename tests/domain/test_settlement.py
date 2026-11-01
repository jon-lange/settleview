from btc_signal_engine.domain.models.decisions import Call, Result
from btc_signal_engine.domain.models.events import IndexSecond
from btc_signal_engine.domain.settlement.fees import contract_return, kalshi_fee
from btc_signal_engine.domain.settlement.scoring import score
from btc_signal_engine.domain.settlement.settlement import actual_direction, settle_window, window_average

B = 1_700_000_040_000  # a whole-minute boundary


def flat(start, end, price, stale_from=None):
    """Per-second rows; seconds at/after `stale_from` keep an old last_trade_ts."""
    rows = []
    for ts in range(start, end, 1000):
        last = ts if stale_from is None or ts < stale_from else stale_from - 6000
        rows.append(IndexSecond(ts=ts, price=price, last_trade_ts=last))
    return rows


def test_window_average_is_mean_of_the_60_seconds_before_end():
    rows = flat(B - 60_000, B - 30_000, 100.0) + flat(B - 30_000, B, 102.0)
    assert window_average(rows, B) == 101.0


def test_coverage_rule_needs_54_fresh_seconds():
    good = flat(B - 60_000, B, 100.0, stale_from=B - 6_000)  # 54 fresh
    bad = flat(B - 60_000, B, 100.0, stale_from=B - 7_000)  # 53 fresh
    assert window_average(good, B) == 100.0
    assert window_average(bad, B) is None


def test_averages_round_to_cents_before_comparison():
    assert actual_direction(80000.00, 80000.00, "kalshi") == (Call.UP, True)
    rows = flat(B - 60_000, B, 80000.004)
    assert window_average(rows, B) == 80000.0


def test_tie_is_up_under_kalshi_and_tie_under_push():
    assert actual_direction(100.0, 100.0, "kalshi") == (Call.UP, True)
    assert actual_direction(100.0, 100.0, "push") == (None, True)
    assert score(Call.UP, "RESOLVED", Call.UP, True) is Result.WIN
    assert score(Call.DOWN, "RESOLVED", Call.UP, True) is Result.LOSS
    assert score(Call.UP, "RESOLVED", None, True) is Result.TIE


def test_settle_window_uses_open_and_close_minutes():
    h = 300
    rows = flat(B - 60_000, B, 100.0) + flat(B, B + h * 1000, 105.0)
    s = settle_window(h, B, rows)
    assert (s.open_avg, s.close_avg) == (100.0, 105.0)


def test_scoring_states():
    assert score(Call.SKIP, "RESOLVED", Call.UP, False) is Result.NOT_SCORED
    assert score(Call.MISSED, None, None, False) is Result.NOT_SCORED
    assert score(Call.UP, "PENDING", None, False) is Result.PENDING
    assert score(Call.UP, "UNRESOLVED", None, False) is Result.UNRESOLVED
    assert score(Call.DOWN, "RESOLVED", Call.DOWN, False) is Result.WIN


def test_kalshi_fee_and_returns():
    # matches real fills: 156.54 @ 35c paid $2.4929; 10 @ 43c paid ~$0.1716
    assert kalshi_fee(156.54, 0.35) == 2.4929
    assert abs(kalshi_fee(10, 0.43) - 0.1716) <= 0.0001
    assert kalshi_fee(10, 0.40) == 0.168
    assert round(contract_return(True, 0.55), 4) == round(1 - 0.55 - kalshi_fee(1, 0.55), 4)
    assert round(contract_return(False, 0.55), 4) == round(-0.55 - kalshi_fee(1, 0.55), 4)


# Real production fills (2026-10-28, from GET /portfolio/fills): (contracts, price, fee Kalshi charged).
REAL_TAKER_FILLS = [(1.76, 0.62, 0.0291), (1.76, 0.52, 0.0308), (3.4, 0.28, 0.0480), (0.05, 0.82, 0.0006),
                    (0.05, 0.19, 0.0006), (0.64, 0.67, 0.0100), (0.64, 0.36, 0.0104), (0.53, 0.44, 0.0092),
                    (1.76, 0.67, 0.0273), (1.76, 0.54, 0.0307), (1.41, 0.49, 0.0247), (1.41, 0.66, 0.0222)]


def test_taker_fee_matches_every_real_production_fill_rounded_up_to_4dp():
    from btc_signal_engine.domain.settlement.fees import kalshi_fee
    for c, p, charged in REAL_TAKER_FILLS:
        assert kalshi_fee(c, p) == charged, (c, p, charged)
