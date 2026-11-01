"""Golden features: a fixed synthetic fixture's snapshot must equal the stored JSON (KTD10).

If this fails because feature output changed on purpose, bump FEATURE_LIB_VERSION and regenerate with
`docker compose -f deploy/dev/compose.yaml run --rm dev python -m tests.features.test_golden` (in the dev container,
as the tests run).
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from btc_signal_engine.domain.features import FEATURE_LIB_VERSION
from btc_signal_engine.domain.features.state import FeatureState
from btc_signal_engine.domain.models.events import BookSecond, Trade

GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "golden_features.json"
T0 = 1_699_999_200_000  # synthetic: aligned to a 15-minute boundary
AS_OF = T0 + 15 * 3_600_000


def synthetic_state() -> FeatureState:
    """15h of sparse synthetic trades, a dense final 15 minutes, and book seconds in the final minute."""
    state = FeatureState()
    ts, i = T0, 0
    while ts < AS_OF:
        dense = ts >= AS_OF - 900_000
        price = 30_000 + 200 * math.sin(i / 500) + 30 * math.sin(i / 37) + 0.01 * i
        state.on_trade(Trade(trade_id=i, ts=ts, ingest_ts=ts, price=round(price, 2),
                             size=0.01 + (i * 7 % 13) / 100, buy=(i * 5) % 3 != 0))
        ts += 250 if dense else 20_000
        i += 1
    for s in range(60):
        sec = AS_OF - 60_000 + s * 1_000
        bid = 30_000 + 10 * math.sin(s / 7)
        state.on_book(BookSecond(ts=sec, ingest_ts=sec + 1_000, bid=round(bid, 2), ask=round(bid + 0.5 + s % 3, 2),
                                 bid_depth_10=1 + s % 5, ask_depth_10=2 + s % 4, bid_depth_25=10, ask_depth_25=10,
                                 bid_size=0.25 + s % 4, ask_size=0.5 + s % 3))
    return state


def current() -> dict:
    features, availability = synthetic_state().snapshot(AS_OF)
    return {"_note": "SYNTHETIC fixture, not market data", "feature_lib_version": FEATURE_LIB_VERSION,
            "as_of": AS_OF, "features": features, "availability": availability}


def test_golden_snapshot_matches_stored_json():
    stored = json.loads(GOLDEN.read_text())
    assert stored["feature_lib_version"] == FEATURE_LIB_VERSION, "regenerate the golden file for the new version"
    got = current()
    assert got["availability"] == stored["availability"]
    assert set(got["features"]) == set(stored["features"]), "feature set changed: bump FEATURE_LIB_VERSION"
    for name, want in stored["features"].items():
        have = got["features"][name]
        if want is None:
            assert have is None, name
        else:
            assert have == pytest.approx(want, rel=1e-9, abs=1e-12), f"{name} changed: bump FEATURE_LIB_VERSION"


if __name__ == "__main__":
    GOLDEN.write_text(json.dumps(current(), indent=2, sort_keys=True) + "\n")
    print(f"wrote {GOLDEN}")
