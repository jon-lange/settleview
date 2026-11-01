"""Calibration of the Coach's fair odds against real settled KXBTC15M results (read-only analysis)."""
import sqlite3
import sys
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from btc_signal_engine.domain.coach.fair_value import HISTORY_MS, fair_from_index  # noqa: E402
from btc_signal_engine.domain.models.events import IndexSecond  # noqa: E402
from btc_signal_engine.domain.settlement.settlement import window_average  # noqa: E402

DB = str(ROOT / "data" / "btcflow.db")
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
first_index = c.execute("select min(ts) from index_1s").fetchone()[0]
markets = c.execute("""select open_ts, close_ts, result, floor_strike, expiration_value from kalshi_markets
                       where status='finalized' and result in ('yes','no') and open_ts >= ? order by open_ts""",
                    (first_index + HISTORY_MS + 60_000,)).fetchall()
CHECKS = (12, 8, 5, 3, 1)  # minutes before close
preds = defaultdict(list)  # minutes_left -> [(p, outcome)]
basis = []
skipped = 0
for open_ts, close_ts, result, strike, _expv in markets:
    rows = c.execute("select ts, price, last_trade_ts from index_1s where ts >= ? and ts < ? order by ts",
                     (open_ts - HISTORY_MS, close_ts)).fetchall()
    if len(rows) < 2000:
        skipped += 1
        continue
    oa = window_average([IndexSecond(t, p, lt) for t, p, lt in rows if open_ts - 60_000 <= t < open_ts], open_ts)
    if oa is None:
        skipped += 1
        continue
    if strike:
        basis.append(oa - strike)
    pairs = [(t, p) for t, p, _ in rows]
    y = 1 if result == "yes" else 0
    for m in CHECKS:
        at = close_ts - m * 60_000
        f = fair_from_index(at, [r for r in pairs if r[0] < at], oa, close_ts)
        if f is not None:
            preds[m].append((f["fair_up"], y))

print(f"windows used: {len(markets) - skipped} (skipped {skipped}) | basis our open_avg - kalshi strike: "
      f"median ${sorted(basis)[len(basis)//2]:.2f}, |p90| ${sorted(abs(b) for b in basis)[int(0.9*len(basis))]:.2f}")
for m in CHECKS:
    ps = preds[m]
    brier = sum((p - y) ** 2 for p, y in ps) / len(ps)
    base = sum(y for _, y in ps) / len(ps)
    clim = sum((base - y) ** 2 for _, y in ps) / len(ps)
    print(f"\n{m} min before close: n={len(ps)}  Brier {brier:.4f} vs coin-flip-ish baseline {clim:.4f}  (skill {1 - brier / clim:+.1%})")
    buckets = defaultdict(list)
    for p, y in ps:
        buckets[min(9, int(p * 10))].append((p, y))
    for b in range(10):
        if buckets[b]:
            xs = buckets[b]
            print(f"   model {b*10:>2}-{b*10+10:<3}%  n={len(xs):>4}  avg model {sum(p for p, _ in xs)/len(xs):.0%}  actually Up {sum(y for _, y in xs)/len(xs):.0%}")
