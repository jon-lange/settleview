"""FeatureState.copy: the coach computes its snapshot from a copy in a worker thread while the loop keeps ingesting."""

from btc_signal_engine.domain.features.state import FeatureState
from btc_signal_engine.domain.models.events import Trade

T0 = 1_700_000_000_000


def _feed(state: FeatureState, start: int, n: int) -> None:
    for i in range(start, start + n):
        state.on_trade(Trade(trade_id=i, ts=T0 + i * 700, ingest_ts=T0 + i * 700, price=60_000 + (i % 7) * 3.5,
                             size=0.01 * (1 + i % 3), buy=i % 2 == 0))


def test_a_copy_is_unaffected_by_later_ingestion():
    state, reference = FeatureState(), FeatureState()
    _feed(state, 0, 3_000)
    _feed(reference, 0, 3_000)
    snap = state.copy()
    at = T0 + 3_000 * 700
    # Later trades land in the same (open) second and minute rows the copy holds, and in new ones.
    _feed(state, 3_000, 200)
    assert snap.snapshot(at) == reference.snapshot(at)
    assert state.snapshot(at + 200 * 700) != snap.snapshot(at + 200 * 700)
