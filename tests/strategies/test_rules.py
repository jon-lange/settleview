import pytest

from btc_signal_engine.domain.features import FEATURES
from btc_signal_engine.domain.models.decisions import Call
from btc_signal_engine.domain.strategies.base import Context
from btc_signal_engine.domain.strategies.baselines import baseline_call
from btc_signal_engine.domain.strategies.ruleset import build_spec, evaluate


def feats(**kw):
    """A calm, fully-available synthetic snapshot; override what a scenario needs."""
    f = {name: None for name in FEATURES}
    f.update({
        "last_trade_age": 0.5, "trades_60": 200, "spread": 0.01, "rv_60": 0.0005,
        "buy_vol_15": 5.0, "sell_vol_15": 5.0, "buy_vol_60": 5.0, "sell_vol_60": 5.0,
        "buy_vol_180": 5.0, "sell_vol_180": 5.0, "imbalance_15": 0.0,
        "momentum_15": 0.0, "momentum_60": 0.0, "momentum_180": 0.0,
        "ema_align_1m": 0, "ema_align_5m": 0, "ema_align_15m": 0, "ema_votes_up": 0, "ema_votes_down": 0,
        "range_width_pct_10": 0.5, "range_width_pct_20": 0.5, "range_width_pct_45": 0.5,
        "range_high_10": 100.5, "range_high_20": 100.5, "range_high_45": 100.5,
        "range_low_10": 99.5, "range_low_20": 99.5, "range_low_45": 99.5,
        "range_avg_vol_10": 1.0, "range_avg_vol_20": 1.0, "range_avg_vol_45": 1.0,
        "last_close_1m": 100.0, "last_volume_1m": 1.0, "known_component": 0.0,
    })
    f.update(kw)
    return f


MOM = build_spec("momentum", ["momentum"], [60, 300, 900])


def test_momentum_up_when_pressure_price_and_recent_flow_agree():
    f = feats(buy_vol_60=6.5, sell_vol_60=3.5, momentum_60=0.0004, imbalance_15=0.3, ema_align_15m=0)
    e = evaluate(MOM, f, {}, Context(horizon=300))
    assert e.call is Call.UP and "buy share 65% >= 58%" in e.conditions


def test_same_snapshot_abstains_on_15m_when_trend_is_bearish():
    f = feats(buy_vol_180=6.5, sell_vol_180=3.5, momentum_180=0.0004, imbalance_15=0.3, ema_align_15m=-1)
    assert evaluate(MOM, f, {}, Context(horizon=900)).call is Call.SKIP


def test_horizons_use_their_own_params():
    # 1m reads 15s flow (strong), 15m reads 180s flow (balanced): different calls on one snapshot.
    f = feats(buy_vol_15=8, sell_vol_15=2, momentum_15=0.0003, imbalance_15=0.6, ema_align_5m=1)
    assert evaluate(MOM, f, {}, Context(horizon=60)).call is Call.UP
    assert evaluate(MOM, f, {}, Context(horizon=900)).call is Call.SKIP


def test_filter_vetoes_stale_and_warmup():
    e = evaluate(MOM, feats(last_trade_age=12), {}, Context(horizon=60))
    assert e.call is Call.SKIP and e.flags == ("stale",)
    e = evaluate(MOM, feats(), {"momentum_15": "warmup"}, Context(horizon=60))
    assert e.call is Call.SKIP and e.flags == ("warmup",)


def test_backfilled_snapshot_without_book_is_not_vetoed_but_flagged():
    f = feats(spread=None, buy_vol_60=7, sell_vol_60=3, momentum_60=0.0004, imbalance_15=0.3)
    e = evaluate(MOM, f, {"spread": "book_unavailable"}, Context(horizon=300))
    assert e.call is Call.UP and "book_unavailable" in e.flags


def test_unanimous_with_conflicting_votes_skips_and_first_match_takes_first():
    both = ["momentum", "trend_alignment"]
    f = feats(buy_vol_60=7, sell_vol_60=3, momentum_60=0.0004, imbalance_15=0.3, ema_votes_down=2, ema_align_15m=-1)
    f_trend_down = dict(f, momentum_60=-0.0004)  # momentum rule now abstains, trend rule says DOWN
    first = build_spec("x", both, [300])
    unan = build_spec("x", both, [300], combine="unanimous")
    assert evaluate(first, f_trend_down, {}, Context(horizon=300)).call is Call.DOWN
    assert evaluate(unan, f_trend_down, {}, Context(horizon=300)).call is Call.SKIP


def test_breakout_up_on_5m():
    f = feats(range_width_pct_20=0.08, range_high_20=100.2, range_low_20=100.12, range_avg_vol_20=1.0,
              last_close_1m=100.3, last_volume_1m=1.6)
    e = evaluate(build_spec("b", ["breakout"], [300]), f, {}, Context(horizon=300))
    assert e.call is Call.UP


def test_divergence_always_abstains_but_notes_it():
    f = feats(buy_vol_60=7, sell_vol_60=3, momentum_60=-0.0004)
    e = evaluate(build_spec("d", ["divergence"], [60]), f, {}, Context(horizon=60))
    assert e.call is Call.SKIP and "divergence detected" in e.conditions


def test_baselines():
    assert baseline_call("known_component", feats(known_component=3.2), Context(60))[0] is Call.UP
    assert baseline_call("known_component", feats(known_component=-0.01), Context(60))[0] is Call.DOWN
    assert baseline_call("known_component", feats(known_component=0.0), Context(60))[0] is Call.UP  # tie = UP
    assert baseline_call("repeat_previous", feats(), Context(60, prev_actual="DOWN"))[0] is Call.DOWN
    assert baseline_call("momentum_60s", feats(momentum_60=-0.001), Context(60))[0] is Call.DOWN


# -- momentum: k of 4 conditions --------------------------------------------------------------------------------
# 65% buying over 60s and price up, but the last 15s net selling: 3 of the 4 UP conditions hold.
THREE_OF_FOUR = dict(buy_vol_60=6.5, sell_vol_60=3.5, momentum_60=0.0004, imbalance_15=-0.2, ema_align_15m=0)


def test_momentum_default_needs_all_four_conditions():
    assert MOM["params"]["momentum"]["300"]["min_conditions"] == 4
    assert evaluate(MOM, feats(**THREE_OF_FOUR), {}, Context(horizon=300)).call is Call.SKIP


def test_momentum_min_conditions_3_fires_on_3_of_4():
    spec = build_spec("m", ["momentum"], [300], params={"momentum": {"300": {"min_conditions": 3}}})
    e = evaluate(spec, feats(**THREE_OF_FOUR), {}, Context(horizon=300))
    assert e.call is Call.UP
    assert "3 of 4" in e.explanation and len(e.conditions) == 3


def test_momentum_k_of_n_tie_abstains():
    # balanced flow, price up, last 15s selling, no trend: 2 conditions each way
    spec = build_spec("m", ["momentum"], [300], params={"momentum": {"300": {"min_conditions": 2}}})
    f = feats(buy_vol_60=5, sell_vol_60=5, momentum_60=0.0004, imbalance_15=-0.2, ema_align_15m=0)
    assert evaluate(spec, f, {}, Context(horizon=300)).call is Call.SKIP


def test_stored_momentum_spec_without_min_conditions_behaves_as_all_four():
    old = build_spec("momentum", ["momentum"], [60, 300, 900])
    for h in old["params"]["momentum"].values():
        del h["min_conditions"]  # specs recorded before RULE_VERSION 2 lack the param
    for f in (feats(**THREE_OF_FOUR), feats(buy_vol_60=6.5, sell_vol_60=3.5, momentum_60=0.0004, imbalance_15=0.3)):
        assert evaluate(old, f, {}, Context(horizon=300)) == evaluate(MOM, f, {}, Context(horizon=300))
    from btc_signal_engine.domain.strategies.rules import SIGNAL_RULES
    rule = SIGNAL_RULES["momentum"]
    assert rule.checklist(feats(**THREE_OF_FOUR), old["params"]["momentum"]["300"]) == \
        rule.checklist(feats(**THREE_OF_FOUR), MOM["params"]["momentum"]["300"])


def test_momentum_four_of_four_keeps_its_original_sentence():
    f = feats(buy_vol_60=6.5, sell_vol_60=3.5, momentum_60=0.0004, imbalance_15=0.3, ema_align_15m=0)
    e = evaluate(MOM, f, {}, Context(horizon=300))
    assert e.explanation.startswith("Buyers drove 65% of volume over 60s, price rose")


# -- evidence (strategy F) ------------------------------------------------------------------------------------
EVI = build_spec("evidence", ["evidence"], [60, 300, 900])
STRONG_UP = dict(imbalance_60=0.4, momentum_60=0.0010, rv_60=0.0005, ofi_30=15.0, microprice_bps=0.0004,
                 spread=0.01, last_price=100_000.0, intensity_ratio=0.7, ema_votes_up=3, ema_votes_down=0, kc_z=1.0)


def mirror(d):
    out = dict(d, imbalance_60=-d["imbalance_60"], momentum_60=-d["momentum_60"], ofi_30=-d["ofi_30"],
               microprice_bps=-d["microprice_bps"], intensity_ratio=1 - d["intensity_ratio"],
               ema_votes_up=d["ema_votes_down"], ema_votes_down=d["ema_votes_up"], kc_z=-d["kc_z"])
    return out


def test_evidence_calls_up_on_agreeing_evidence_and_down_on_its_mirror():
    e = evaluate(EVI, feats(**STRONG_UP), {}, Context(horizon=300))
    assert e.call is Call.UP
    assert e.explanation.startswith("Mostly ") and e.explanation.endswith("point up.")
    e = evaluate(EVI, feats(**mirror(STRONG_UP)), {}, Context(horizon=300))
    assert e.call is Call.DOWN and e.explanation.endswith("point down.")


def test_evidence_abstains_on_mixed_or_weak_evidence():
    mixed = dict(STRONG_UP, momentum_60=-0.0010, ofi_30=-15.0, kc_z=-1.0)  # price and book flow disagree with flow
    e = evaluate(EVI, feats(**mixed), {}, Context(horizon=300))
    assert e.call is Call.SKIP and "score" in e.explanation
    weak = dict(imbalance_60=0.05, momentum_60=0.00005, rv_60=0.0005, kc_z=0.05, intensity_ratio=0.52)
    assert evaluate(EVI, feats(**weak), {}, Context(horizon=300)).call is Call.SKIP


def test_evidence_threshold_is_the_bar_the_score_must_clear():
    from btc_signal_engine.domain.strategies.rules import SIGNAL_RULES
    rule = SIGNAL_RULES["evidence"]
    f = feats(**dict(STRONG_UP, kc_z=0.0))
    score = rule.score(f, EVI["params"]["evidence"]["300"])
    assert 1.5 < score < 4.0
    for thr, call in ((score - 0.05, Call.UP), (score + 0.05, Call.SKIP)):
        spec = build_spec("e", ["evidence"], [300], params={"evidence": {"300": {"threshold": thr}}})
        assert evaluate(spec, f, {}, Context(horizon=300)).call is call


def test_evidence_book_weight_zero_ignores_order_book_inputs():
    from btc_signal_engine.domain.strategies.rules import SIGNAL_RULES
    rule = SIGNAL_RULES["evidence"]
    p = dict(EVI["params"]["evidence"]["300"], book_weight=0.0)
    a = rule.score(feats(**STRONG_UP), p)
    b = rule.score(feats(**dict(STRONG_UP, ofi_30=-30.0, microprice_bps=-0.0005)), p)
    assert a == pytest.approx(b)


def test_evidence_without_book_data_still_scores_from_trades_only():
    f = feats(**dict(STRONG_UP, ofi_30=None, microprice_bps=None, spread=None))
    e = evaluate(EVI, f, {"ofi_30": "book_unavailable", "microprice_bps": "book_unavailable",
                          "spread": "book_unavailable"}, Context(horizon=300))
    assert e.call is Call.UP and "book_unavailable" in e.flags


def test_evidence_checklist_lists_every_input_with_its_direction():
    from btc_signal_engine.domain.strategies.rules import SIGNAL_RULES
    rule = SIGNAL_RULES["evidence"]
    p = EVI["params"]["evidence"]["300"]
    items = rule.checklist(feats(**STRONG_UP), p)
    assert len(items) == 7
    assert all(up and not down for _, up, down in items)
    items = rule.checklist(feats(**mirror(STRONG_UP)), p)
    assert all(down and not up for _, up, down in items)
    items = rule.checklist(feats(**dict(STRONG_UP, ofi_30=None)), p)
    assert any("no book data" in text and not up and not down for text, up, down in items)
