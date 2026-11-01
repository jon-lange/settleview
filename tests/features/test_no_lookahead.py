from hypothesis import given, settings
from hypothesis import strategies as st

from btc_signal_engine.domain.features.state import FUTURE_SLACK_MS, FeatureState
from btc_signal_engine.domain.models.events import BookSecond, Trade

T0 = 1_699_999_200_000
SPAN_S = 1_500


@st.composite
def trades(draw, lo_ms, hi_ms):
    ts = draw(st.integers(lo_ms, hi_ms - 1))
    return Trade(trade_id=draw(st.integers(0, 10**9)), ts=ts, ingest_ts=ts,
                 price=draw(st.floats(90.0, 110.0)), size=draw(st.floats(0.001, 5.0)), buy=draw(st.booleans()))


@st.composite
def books(draw, lo_s, hi_s):
    ts = draw(st.integers(lo_s, hi_s - 1)) * 1_000
    bid = draw(st.floats(90.0, 110.0))
    sizes = draw(st.one_of(st.none(), st.tuples(st.floats(0.001, 10), st.floats(0.001, 10))))
    return BookSecond(ts=ts, ingest_ts=ts + 1_000, bid=bid, ask=bid + draw(st.floats(0.01, 5.0)),
                      bid_depth_10=draw(st.floats(0, 10)), ask_depth_10=draw(st.floats(0, 10)),
                      bid_depth_25=0.0, ask_depth_25=0.0,
                      bid_size=None if sizes is None else sizes[0], ask_size=None if sizes is None else sizes[1])


@st.composite
def scenario(draw):
    t_s = draw(st.integers(1, SPAN_S))
    T = T0 + t_s * 1_000
    past = draw(st.lists(st.one_of(trades(T0, T), books(0, t_s).map(lambda b: _shift(b))), max_size=120))
    future = draw(st.lists(st.one_of(trades(T, T + FUTURE_SLACK_MS),
                                     books(t_s, t_s + FUTURE_SLACK_MS // 1_000).map(_shift)), max_size=60))
    return T, past, future


def _shift(b: BookSecond) -> BookSecond:
    return BookSecond(ts=T0 + b.ts, ingest_ts=T0 + b.ingest_ts, bid=b.bid, ask=b.ask, bid_depth_10=b.bid_depth_10,
                      ask_depth_10=b.ask_depth_10, bid_depth_25=b.bid_depth_25, ask_depth_25=b.ask_depth_25,
                      bid_size=b.bid_size, ask_size=b.ask_size)


def _feed(state, events):
    for e in events:
        if isinstance(e, Trade):
            state.on_trade(e)
        else:
            state.on_book(e)


@settings(max_examples=150, deadline=None)
@given(scenario())
def test_events_at_or_after_T_never_change_snapshot_T(case):
    T, past, future = case
    only_past = FeatureState()
    _feed(only_past, past)
    expected = only_past.snapshot(T)

    _feed(only_past, future)
    assert only_past.snapshot(T) == expected

    future_first = FeatureState()
    _feed(future_first, future)
    _feed(future_first, past)
    assert future_first.snapshot(T) == expected
