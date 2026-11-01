"""Quick minute-resolution test of the PNW 'cushion' strategy (video transcript). Read-only."""
import sqlite3
import math
import numpy as np
import pandas as pd
ROOT = "data"
kt = sqlite3.connect(f"file:{ROOT}/research/kalshi_trades.db?mode=ro", uri=True)
db = sqlite3.connect(f"file:{ROOT}/btcflow.db?mode=ro", uri=True)
fee = lambda p: math.ceil(0.07 * p * (1 - p) * 1e4 - 1e-9) / 1e4
m = pd.read_sql("select mkt, open_ts, close_ts, result, floor_strike from markets where result in ('yes','no')", kt)
cd = pd.read_sql("select mkt, end_ts, yes_bid, yes_ask from candles", kt)
ix = pd.read_sql("select ts, price from index_1s order by ts", db)
ts, px = ix.ts.values, ix.price.values
def btc_at(t):  # latest finalized second strictly before t - 1s (no lookahead)
    i = np.searchsorted(ts, t - 1000) - 1
    return px[i] if i >= 0 and t - 1000 - ts[i] <= 10_000 else None
cd = cd.sort_values("end_ts"); groups = dict(tuple(cd.groupby("mkt")))
days = sorted(pd.to_datetime(m.open_ts, unit="ms").dt.date.unique()); hold_days = set(days[-10:])
def run(cushion=50, lo=0.65, hi=0.80, tp=0.20, stop_gap=20, use_tp=True, use_stop=True):
    rows = []
    for r in m.itertuples():
        c = groups.get(r.mkt)
        if c is None or r.floor_strike is None: continue
        K = float(r.floor_strike); entered = None
        for q in c.itertuples():
            left = (r.close_ts - q.end_ts) / 60000
            if entered is None:
                if not (7 <= left <= 9): continue
                S = btc_at(q.end_ts)
                if S is None: continue
                side = "UP" if cushion <= S - K else ("DOWN" if cushion <= K - S else None)
                if side is None: continue
                ask = q.yes_ask if side == "UP" else (None if q.yes_bid is None else 1 - q.yes_bid)
                if ask is None or not (lo <= ask <= hi): continue
                paid = min(0.99, round(ask + 0.01, 4)); entered = (side, paid, fee(paid)); continue
            side, paid, f_in = entered
            bid = q.yes_bid if side == "UP" else (None if q.yes_ask is None else 1 - q.yes_ask)
            if use_tp and bid is not None and bid >= paid + tp:          # resting maker sell fills
                rows.append((r.mkt, r.open_ts, round((paid + tp) - paid - f_in, 4), "tp")); break
            S = btc_at(q.end_ts)
            if use_stop and S is not None and left >= 0.5 and ((side == "UP" and stop_gap >= S - K) or (side == "DOWN" and stop_gap >= K - S)):
                if bid is None: continue
                sell = max(0.01, round(bid - 0.01, 4))
                rows.append((r.mkt, r.open_ts, round(sell - paid - f_in - fee(sell), 4), "stop")); break
        else:
            if entered:
                side, paid, f_in = entered
                won = (r.result == "yes") == (side == "UP")
                rows.append((r.mkt, r.open_ts, round((1.0 if won else 0.0) - paid - f_in, 4), "settle"))
            continue
        if entered and (not rows or rows[-1][0] != r.mkt):
            side, paid, f_in = entered
            won = (r.result == "yes") == (side == "UP")
            rows.append((r.mkt, r.open_ts, round((1.0 if won else 0.0) - paid - f_in, 4), "settle"))
    d = pd.DataFrame(rows, columns=["mkt", "open_ts", "pnl", "how"])
    d["hold"] = pd.to_datetime(d.open_ts, unit="ms").dt.date.isin(hold_days)
    return d
def report(name, d):
    for part, sub in (("ALL 30d", d), ("train", d[~d.hold]), ("HOLDOUT", d[d.hold])):
        n = len(sub)
        if not n: print(f"  {name:34s} {part:8s} n=0"); continue
        mu, sd = sub.pnl.mean(), sub.pnl.std(ddof=1)
        t = mu / (sd / math.sqrt(n)) if n > 1 and sd > 0 else float("nan")
        share = sub.how.value_counts(normalize=True).round(2).to_dict()
        print(f"  {name:34s} {part:8s} n={n:4d} win={100*(sub.pnl>0).mean():5.1f}%  net={100*mu:+6.2f}c/contract  t={t:+5.2f}  exits={share}")
print("PNW cushion strategy, minute resolution, 1c slippage, exact fees, Kalshi official results")
report("v1 base: $50 cushion, 65-80c, TP+20, stop $20", run())
report("v4 hold to settlement", run(use_tp=False, use_stop=False))
report("TP only (no stop)", run(use_stop=False))
report("stop only (no TP)", run(use_tp=False))
report("bigger cushion $100", run(cushion=100))
