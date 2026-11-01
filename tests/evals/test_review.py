"""The error-analysis pack: a stratified, capped, covering sample; one readable section per wake with blank label lines;
a label sheet that is never overwritten; and the tally of filled-in labels."""

from __future__ import annotations

import csv
from collections import Counter

from evals import review
from tests.evals.test_export_redaction import source  # noqa: F401 - the planted database fixture

HOUR = 3_600_000


def _item(i: int, trig: str, *, day: str = "2026-10-31", market: str = "kalshi_15m", action: str = "NO_TRADE",
          won=None, session: str = "s01", ts: int | None = None) -> dict:
    return {"item_id": f"w-{i:04d}", "source": {"journal_id": i, "session": session},
            "meta": {"ts": ts if ts is not None else i * 1_000_000, "day": day, "decision_market": market,
                     "markets": [market], "decision_action": action, "pass_reason": "NO_VALIDATED_EDGE",
                     "primary_trigger": trig, "local_time": f"{day} 10:00 CDT", "model": "claude-haiku-5-5"},
            "outcome": {"trades": [] if won is None else [{"won": won, "net": 0.1 if won else -0.1}]},
            "input": {"events": [{"type": trig, "class": "normal"}], "memory": {}, "observe": None},
            "recorded": {"tools": [], "decision": None, "final_text": "ok", "status": "done", "turns": 1,
                         "est_usd": 0.003}, "context": {}}


def test_the_sample_is_stratified_capped_and_covers_every_value():
    pool = ([_item(i, "agent_alarm") for i in range(300)] + [_item(1000 + i, "price_move") for i in range(150)]
            + [_item(2000 + i, "settled", won=i % 3 == 0) for i in range(30)]
            + [_item(3000, "session_ending", day="2026-10-30", market="polymarket_5m", action="HOLD")]
            + [_item(4000 + i, "window_ready", session="s02", ts=5 * HOUR + i) for i in range(10)])  # one window
    got = review.stratified(pool, 50, seed=3)
    assert len(got) == 50 and got == review.stratified(pool, 50, seed=3)  # repeatable
    trig = Counter(i["meta"]["primary_trigger"] for i in got)
    assert trig["session_ending"] == 1 and trig["settled"] >= 3 and trig["agent_alarm"] > trig["price_move"]
    windows = Counter(review._window(i) for i in got)
    assert max(windows.values()) <= 2  # at most 2 wakes from one (session, 15-minute window)
    keys = [review._keys(i) for i in got]
    assert {"2026-10-30", "2026-10-31"} <= {k["day"] for k in keys}
    assert {"loss", "win", "none"} <= {k["outcome"] for k in keys} and "polymarket_5m" in {k["market"] for k in keys}


def test_the_pack_reads_one_section_per_wake_and_never_overwrites_labels(source, tmp_path):  # noqa: F811
    r = review.build(source, 100, 7, tmp_path)
    md = r["review"].read_text(encoding="utf-8")
    assert r["n"] == 2 and md.count("First failure: ") == 2 and md.count("Pass/fail: ") == 2
    for part in ("**What woke it:**", "**What it saw:**", "**Tool calls:**", "**Decision:**", "**Told the user:**",
                 "**Outcome:**"):
        assert part in md
    assert "Observe kalshi_15m: UP mid 0.61" in md  # the second wake's exact input (agent_wake_inputs)
    with r["sheet"].open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert [x["journal_id"] for x in rows] and set(review.LABEL_COLUMNS) == set(rows[0])
    rows[0].update(pass_fail="fail", failure_category="claimed a stale price", first_failure="used memory")
    rows[1].update(pass_fail="pass")
    with r["sheet"].open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=review.LABEL_COLUMNS)
        w.writeheader()
        w.writerows(rows)
    review.build(source, 100, 7, tmp_path)  # again: the filled-in sheet stays
    t = review.tally(r["sheet"])
    assert t.startswith("2/2 wakes labelled") and "claimed a stale price" in t
