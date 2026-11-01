from btc_signal_engine.application.engine.lean import compute_lean
from btc_signal_engine.domain.features.state import FeatureState
from btc_signal_engine.domain.models.events import BookSecond, Trade
from btc_signal_engine.domain.strategies.ruleset import build_spec

T = 1_793_209_620_000
SPEC = build_spec("momentum", ["momentum"], [60])


def feed(state, start, end, buy_ratio, step):
    for i, ts in enumerate(range(start, end, 1000)):
        state.on_trade(Trade(i + 1 + start // 1000, ts + 10, ts + 50, 80_000 + step * i, 0.1, (i % 10) < buy_ratio * 10))
        state.on_book(BookSecond(ts, ts + 1000, 79_999.99, 80_000.01, 5, 5, 9, 9))


def test_one_sided_rising_flow_leans_up_with_conditions_met(store):
    state = FeatureState()
    feed(state, T - 1_800_000, T, buy_ratio=0.8, step=0.05)
    lean = compute_lean(state, SPEC, 60, T)
    assert lean["side"] == "UP" and lean["up_met"] >= 3 and lean["total"] == 4
    assert 0 < lean["strength"] <= 1


def test_lean_writes_nothing(store):
    state = FeatureState()
    feed(state, T - 600_000, T, buy_ratio=0.5, step=0.0)
    compute_lean(state, SPEC, 60, T)
    assert store.select("decisions") == []


def test_lean_moves_with_new_trades_within_the_minute():
    state = FeatureState()
    feed(state, T - 1_800_000, T, buy_ratio=0.8, step=0.05)
    before = compute_lean(state, SPEC, 60, T)
    for i in range(20):  # a burst of aggressive selling, price falling
        ts = T + i * 1000
        state.on_trade(Trade(10_000_000 + i, ts + 10, ts + 50, 80_000 - 5 * i, 2.0, False))
    after = compute_lean(state, SPEC, 60, T + 20_000)
    assert before["side"] == "UP" and after["side"] == "DOWN"
