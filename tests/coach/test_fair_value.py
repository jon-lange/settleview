"""Coach fair-odds model. All prices here are synthetic test inputs, not market data."""

import math

import pytest

from btc_signal_engine.domain.coach.fair_value import SIGMA_FLOOR_FRAC, prob_up, sigma_per_second

T = 1_800_000_000_000 - 1_800_000_000_000 % 900_000  # a synthetic 15m-aligned boundary
H = 300


def p(price, at, strike=80_000.0, observed=(), h=H, sigma=8.0):
    return prob_up(price=price, strike=strike, sigma=sigma, at=at, window_end=T + h * 1000, observed=observed)


def test_half_at_the_open_when_price_equals_strike():
    assert p(80_000.0, T) == pytest.approx(0.5)
    assert p(80_000.0, T, h=60) == pytest.approx(0.5)
    assert p(80_000.0, T, h=900) == pytest.approx(0.5)


def test_increases_with_price_above_strike():
    probs = [p(80_000.0 + d, T + 60_000) for d in (0, 5, 10, 20, 40, 80)]
    assert all(b > a for a, b in zip(probs, probs[1:], strict=False))
    assert p(79_990.0, T + 60_000) < 0.5


def test_increases_as_time_runs_out_when_above_strike():
    probs = [p(80_010.0, T + s * 1000) for s in (0, 60, 120, 200, 239)]
    assert all(b > a for a, b in zip(probs, probs[1:], strict=False))


def test_observed_closing_seconds_count():
    # inside the closing minute: 50 seconds already observed well above the strike,
    # price now slightly below: the average is almost locked in above the strike.
    at = T + H * 1000 - 10_000
    observed = [80_030.0] * 50
    assert p(79_999.0, at, observed=observed) > 0.99
    observed_low = [79_970.0] * 50
    assert p(80_001.0, at, observed=observed_low) < 0.01


def test_fully_observed_is_certain_and_tie_counts_up():
    at = T + H * 1000
    assert p(1.0, at, observed=[80_000.0] * 60) == 1.0  # tie -> UP (KTD6 kalshi tie rule)
    assert p(1.0, at, observed=[79_999.99] * 60) == 0.0


def test_matches_closed_form_before_the_closing_minute():
    # 5m window, 100s in: closing seconds are 141..200 seconds ahead (offsets g..g+59, g = 141)
    at, S, K, sig = T + 100_000, 80_012.0, 80_000.0, 3.0
    n, g = 60, 141
    var = n * n * g + (n - 1) * n * (2 * n - 1) / 6
    z = 60 * (S - K) / (sig * math.sqrt(var))
    expected = 0.5 * (1 + math.erf(z / math.sqrt(2)))
    assert p(S, at, sigma=sig) == pytest.approx(expected)


def test_sigma_from_rv_and_floor():
    assert sigma_per_second(80_000.0, 0.006) == pytest.approx(80_000.0 * 0.006 / math.sqrt(60))
    assert sigma_per_second(80_000.0, 0.0) == pytest.approx(80_000.0 * SIGMA_FLOOR_FRAC)
    assert sigma_per_second(80_000.0, None) is None


# -- fair odds from recorded index rows (synthetic rows) --------------------------------------
from btc_signal_engine.domain.coach.fair_value import HISTORY_MS, fair_from_index, minute_sigma  # noqa: E402


def test_fair_from_index_matches_model_and_needs_data():
    end = T + 900_000
    at = T + 300_000
    rows = [(ts, 80_000.0 + 3.0 * ((ts // 60_000) % 3)) for ts in range(at - HISTORY_MS, at, 1000)]
    f = fair_from_index(at, rows, 79_990.0, end)
    prices = [p for _, p in rows]
    sig = minute_sigma(at, rows, len(rows))
    assert f["price"] == prices[-1] and f["sigma"] == pytest.approx(sig)
    assert f["fair_up"] == pytest.approx(prob_up(price=prices[-1], strike=79_990.0, sigma=sig, at=at, window_end=end))
    assert fair_from_index(at, rows[-300:], 79_990.0, end) is None  # 5 minutes: not enough for volatility
    assert fair_from_index(at + 10_000, rows, 79_990.0, end) is None  # latest price older than 5s


def test_fair_from_index_uses_observed_closing_seconds():
    end = T + 900_000
    at = end - 5_000
    rows = [(ts, 80_050.0) for ts in range(at - HISTORY_MS, at, 1000)]
    f = fair_from_index(at, rows, 80_000.0, end)
    assert f["fair_up"] > 0.999


def test_bid_ask_bounce_does_not_inflate_volatility():
    # Synthetic: a flat $80,000 market whose last trade alternates between bid and ask ($10 apart) every second.
    from btc_signal_engine.domain.coach.fair_value import fair_from_index
    at = 1_793_212_200_000 + 300_000
    rows = [(ts, 80_005.0 if (ts // 1000) % 2 else 79_995.0) for ts in range(at - 1_900_000, at, 1000)]
    f = fair_from_index(at, rows, 80_000.0, at + 600_000)
    assert f["sigma"] < 3.0  # per-second dollars: near the floor, not the ~$10/s the bounce would imply


def test_trend_measured_on_minute_returns():
    from btc_signal_engine.domain.coach.fair_value import fair_from_index
    at = 1_793_212_200_000 + 300_000
    # Synthetic: steady $5 moves every minute up and down (random-walk-like at the minute scale).
    rows = []
    for ts in range(at - 1_900_000, at, 1000):
        minute = (ts - (at - 1_900_000)) // 60_000
        rows.append((ts, 80_000.0 + (25.0 if minute % 2 else -25.0)))
    f = fair_from_index(at, rows, 80_000.0, at + 600_000)
    assert 3.0 < f["sigma"] < 10.0  # $50 per minute ~ $6.5/s


# -- index error (index_sd, Final Minute KTD14): synthetic inputs ------------------------------


def test_index_sd_zero_is_the_original_model():
    at = T + H * 1000 - 20_000
    obs = [80_004.0] * 40
    for price in (79_990.0, 80_000.0, 80_020.0):
        base = prob_up(price=price, strike=80_000.0, sigma=6.0, at=at, window_end=T + H * 1000, observed=obs)
        assert prob_up(price=price, strike=80_000.0, sigma=6.0, at=at, window_end=T + H * 1000, observed=obs,
                       index_sd=0.0) == base
    assert p(1.0, T + H * 1000, observed=[80_000.0] * 60) == 1.0


def test_index_sd_adds_3600_sd_squared_to_the_variance():
    at, S, K, sig, sd_eps = T + H * 1000 - 30_000, 80_006.0, 80_000.0, 4.0, 5.0
    obs = [80_008.0] * 30
    n, g = 30, 1
    var = n * n * g + (n - 1) * n * (2 * n - 1) / 6
    gap = sum(obs) + n * S - 60 * K
    z = gap / math.sqrt(sig * sig * var + 3600 * sd_eps * sd_eps)
    want = 0.5 * (1 + math.erf(z / math.sqrt(2)))
    got = prob_up(price=S, strike=K, sigma=sig, at=at, window_end=T + H * 1000, observed=obs, index_sd=sd_eps)
    assert got == pytest.approx(want)
    assert 0.5 < got < prob_up(price=S, strike=K, sigma=sig, at=at, window_end=T + H * 1000, observed=obs)


def test_index_sd_keeps_a_fully_known_average_uncertain():
    end = T + H * 1000
    got = prob_up(price=1.0, strike=80_000.0, sigma=6.0, at=end, window_end=end, observed=[80_005.0] * 60,
                  index_sd=5.0)
    assert got == pytest.approx(0.5 * (1 + math.erf(1.0 / math.sqrt(2))))  # $5 above with a $5 error: Phi(1)


# -- the settlement average's distribution (the Play cone) agrees with prob_up: synthetic inputs ---------------
def test_close_avg_dist_matches_prob_up_and_shrinks_with_time():
    from btc_signal_engine.domain.coach.fair_value import close_avg_dist
    end = T + 900_000
    for at, obs in ((T + 100_000, ()), (end - 30_000, [80_010.0] * 30)):
        mean, sd = close_avg_dist(price=80_020.0, sigma=6.0, at=at, window_end=end, observed=obs)
        z = (mean - 80_000.0) / sd
        assert prob_up(price=80_020.0, strike=80_000.0, sigma=6.0, at=at, window_end=end, observed=obs) == \
            pytest.approx(0.5 * (1 + math.erf(z / math.sqrt(2))))
    # before the closing minute: sd = sigma * sqrt(V) / 60 with V from the module doc (n = 60, g = seconds to it + 1)
    at = T + 100_000
    g = (end - 60_000 - at) / 1000 + 1
    _, sd = close_avg_dist(price=80_000.0, sigma=6.0, at=at, window_end=end)
    assert sd == pytest.approx(6.0 * math.sqrt(3600 * g + 59 * 60 * 119 / 6) / 60)
    assert sd < 6.0 * math.sqrt((end - at) / 1000)  # averaging: narrower than the spot band at the close
    sds = [close_avg_dist(price=80_000.0, sigma=6.0, at=end - s * 1000, window_end=end,
                          observed=[80_000.0] * max(0, 60 - s))[1] for s in (600, 300, 90, 60, 30, 5, 0)]
    assert all(a > b for a, b in zip(sds, sds[1:], strict=False)) and sds[-1] == 0.0  # monotone in time left; certain at the close


def test_fair_from_index_carries_the_cone_numbers():
    end = T + 900_000
    at = T + 300_000
    rows = [(ts, 80_000.0 + 3.0 * ((ts // 60_000) % 3)) for ts in range(at - HISTORY_MS, at, 1000)]
    f = fair_from_index(at, rows, 79_990.0, end)
    from btc_signal_engine.domain.coach.fair_value import close_avg_dist
    assert (f["mean_close"], f["sd_close"]) == close_avg_dist(price=f["price"], sigma=f["sigma"], at=at, window_end=end)
    assert f["window_end"] == end and f["at"] == at


# -- the bisections (review fix plan 2026-10-31 P1, B5#6): the same numbers as the original walks -------------------
def _walk_sigma(at, rows, end):
    """minute_sigma as it was before the bisection (the reference)."""
    from btc_signal_engine.domain.coach.fair_value import MIN_VOL_RETURNS, VOL_MINUTES
    samples, i = [], end - 1
    for k in range(VOL_MINUTES + 1):
        mark = at - k * 60_000
        while i >= 0 and rows[i][0] >= mark:
            i -= 1
        if i < 0:
            break
        samples.append(rows[i][1])
    rets = [math.log(a / b) for a, b in zip(samples, samples[1:], strict=False) if a > 0 and b > 0]
    if len(rets) < MIN_VOL_RETURNS:
        return None
    price = rows[end - 1][1]
    return max(price * math.sqrt(sum(r * r for r in rets) / len(rets) / 60), price * SIGMA_FLOOR_FRAC)


def test_the_bisected_sigma_and_closing_seconds_match_the_original_walks():
    import random

    from btc_signal_engine.domain.coach.fair_value import fair_from_index, minute_sigma
    rng = random.Random(11)
    end_w = T + 900_000
    rows, price = [], 80_000.0
    for ts in range(T - 1_900_000, end_w, 1000):
        price += rng.gauss(0, 6)
        if rng.random() > 0.08:  # gaps, as recorded data has
            rows.append((ts, round(price, 2)))
    for at in [*range(T - 1_000, end_w, 37_000), *range(end_w - 61_000, end_w + 2_000, 1_000)]:
        end = next((i for i, r in enumerate(rows) if r[0] >= at), len(rows))
        assert minute_sigma(at, rows, end) == _walk_sigma(at, rows, end)
        f = fair_from_index(at, rows, 80_000.0, end_w)
        if f is None:
            continue
        # the closing seconds: missing rows carried forward from the previous row (or S), as before
        close_start, observed = end_w - 60_000, []
        if at > close_start:
            by_ts = {ts: p for ts, p in rows[:end] if ts >= close_start}
            prev = next((p for ts, p in reversed(rows[:end]) if ts < close_start), f["price"])
            for s in range(close_start, min(at, end_w), 1000):
                prev = by_ts.get(s, prev)
                observed.append(prev)
        assert f["fair_up"] == prob_up(price=f["price"], strike=80_000.0, sigma=f["sigma"], at=at, window_end=end_w,
                                       observed=observed)
