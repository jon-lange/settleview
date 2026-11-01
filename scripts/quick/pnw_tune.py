"""PNW cushion threshold tuning (pre-registered in docs/research/pnw-tuning.md). Read-only on the databases.

  docker compose -f deploy/dev/compose.yaml run --rm research python scripts/quick/pnw_tune.py sweep     # 1,600 configs -> experiments_results.csv; prints
                                                               # TRAIN-only tables; freezes <= 3 candidates
  docker compose -f deploy/dev/compose.yaml run --rm research python scripts/quick/pnw_tune.py holdout   # frozen candidates + live setting, holdout once

`sweep` scores every config with the harness (scripts/experiments.py evaluate, minute resolution, slips 0/1/2c) and
appends all rows, so every config counts in the Bonferroni line. It only prints / saves train numbers.
"""
from __future__ import annotations

import itertools
import json
import sys
from datetime import datetime, UTC
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import experiments as X  # noqa: E402

CUSH = [25, 35, 50, 75, 100]
WIN = [(10, 8), (9, 7), (8, 6), (7, 5), (9, 5)]
BAND = [(60, 80), (65, 80), (65, 85), (70, 90)]
TP = [10, 15, 20, None]
STOP = [10, 20, 30, None]
LIVE = (50, (9, 7), (65, 80), 20, 20)
TRAIN_OUT = X.OUT / "pnw_tune_train.csv"
FROZEN = X.OUT / "pnw_tune_candidates.json"


def entry_spec(c, w, b):
    return f"pnw_tune:cushion={c},start={w[0]},end={w[1]},lo={b[0]},hi={b[1]}"


def exit_spec(tp, st):
    parts = ([f"mtp{tp}"] if tp is not None else []) + ([f"bstop{st}"] if st is not None else [])
    return "_".join(parts) or "hold"


def load():
    return X.load(X.OUT / "kalshi_trades.db", ROOT / "data" / "btcflow.db", tapes=False)


def sweep():
    D = load()
    run_ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%MZ")
    exits = [exit_spec(t, s) for t in TP for s in STOP]
    keep = []
    for c, w, b in itertools.product(CUSH, WIN, BAND):
        e = entry_spec(c, w, b)
        rows = X.evaluate(D, "pnw_tune", e, X.parse_entry(e), exits, None, run_ts)
        X.append(rows)
        for r in rows:  # keep TRAIN only for the analysis
            if r["slip_c"] == 1 and r["split"] == "train" and r["res"] == "minute":
                keep.append({**r, "cushion": c, "win_start": w[0], "win_end": w[1], "lo": b[0], "hi": b[1],
                             "tp": r["exit"].split("_")[0][3:] if r["exit"].startswith("mtp") else "none",
                             "stop": r["exit"].split("bstop")[1] if "bstop" in r["exit"] else "none"})
    T = pd.DataFrame(keep)
    T["per_day"] = T.n / 20
    T.to_csv(TRAIN_OUT, index=False)
    print(f"appended {len(T)} configs; train table -> {TRAIN_OUT}")
    analyse(T)


def key_of(c, w, b, tp, st):
    return f"{entry_spec(c, w, b)}|{exit_spec(tp, st)}"


def analyse(T: pd.DataFrame):
    T = T.set_index("config")
    live = key_of(*LIVE)
    L = T.loc[live]
    cols = ["n", "per_day", "win", "be", "net_c", "t", "share_tp", "share_stop", "share_settle"]
    print("\nLIVE (train): " + ", ".join(f"{c}={L[c]:.2f}" for c in cols))
    axes = [("cushion", CUSH, 0), ("window", WIN, 1), ("band", BAND, 2), ("tp", TP, 3), ("stop", STOP, 4)]
    for name, vals, pos in axes:
        print(f"\n-- axis {name} (others at live), TRAIN --")
        for v in vals:
            p = list(LIVE)
            p[pos] = v
            r = T.loc[key_of(*p)]
            print(f"  {str(v):10s} n={int(r.n):4d} /day={r.per_day:5.1f} win={r.win:5.1f}% be={r.be:5.1f}% "
                  f"net={r.net_c:+6.2f}c t={r.t:+5.2f}  TP/stop/settle={r.share_tp:.2f}/{r.share_stop:.2f}/{r.share_settle:.2f}"
                  + ("   <- live" if v == LIVE[pos] else ""))
    # neighbourhood mean t
    grid = {}
    for c, w, b, tp, st in itertools.product(CUSH, WIN, BAND, TP, STOP):
        grid[(CUSH.index(c), WIN.index(w), BAND.index(b), TP.index(tp), STOP.index(st))] = key_of(c, w, b, tp, st)
    sizes = [len(CUSH), len(WIN), len(BAND), len(TP), len(STOP)]
    nb = {}
    for ix, k in grid.items():
        ts = [T.loc[k, "t"]]
        for a in range(5):
            for d in (-1, 1):
                j = list(ix)
                j[a] += d
                if 0 <= j[a] < sizes[a]:
                    ts.append(T.loc[grid[tuple(j)], "t"])
        nb[k] = float(np.nanmean(ts))
    T["nb_t"] = pd.Series(nb)
    print(f"\nTRAIN overview over 1,600 configs: net>0 in {(T.net_c > 0).sum()}, t>2 in {(T.t > 2).sum()}, "
          f"median net {T.net_c.median():.2f}c, max t {T.t.max():.2f}, min t {T.t.min():.2f}")
    print("\nTop 10 train t (n>=100):")
    print(T[T.n >= 100].sort_values("t", ascending=False).head(10)[cols + ["nb_t"]].to_string(float_format=lambda v: f"{v:.2f}"))
    print("\nTop 10 neighbourhood t (n>=100):")
    print(T[T.n >= 100].sort_values("nb_t", ascending=False).head(10)[cols + ["nb_t"]].to_string(float_format=lambda v: f"{v:.2f}"))
    picks = []

    def pick(df, by):
        for k, r in df.sort_values(by, ascending=False).iterrows():
            if k in picks:
                continue
            if r.net_c > 0:
                picks.append(k)
            return
    pick(T[T.n >= 100], "t")
    pick(T[T.n >= 100], "nb_t")
    pick(T[T.n >= L.n], "net_c")
    print("\nFROZEN candidates (train only):")
    for k in picks:
        r = T.loc[k]
        print(f"  {k}: train n={int(r.n)} win={r.win:.1f}% be={r.be:.1f}% net={r.net_c:+.2f}c t={r.t:+.2f} nb_t={r.nb_t:.2f}")
    if FROZEN.exists():
        print(f"(candidates already frozen in {FROZEN}; not overwriting)")
    else:
        FROZEN.write_text(json.dumps({"frozen_utc": datetime.now(UTC).isoformat(), "candidates": picks,
                                      "live": live}, indent=1))
    T.to_csv(TRAIN_OUT)


def holdout():
    fz = json.loads(FROZEN.read_text())
    D = load()
    days = sorted(D.mk.day.unique())
    hd = days[20:]
    h_half = np.where(D.mk.day.isin(set(hd[:5])), 1, np.where(D.mk.day.isin(set(hd[5:])), 2, 0))
    for cfg in fz["candidates"] + [fz["live"]]:
        e, ex = cfg.split("|")
        k, side = X.parse_entry(e)(D)
        out = X.sim_minute(D, k, side, X.Exit.parse(ex), X.PRIMARY_SLIP)
        res = X.split_rows(D, out)
        o = np.argsort(D.mk.open_ts.to_numpy()[out["idx"]], kind="stable")
        idx, pnl = out["idx"][o], out["pnl"][o]
        H = res["holdout"]
        hh = [X.summarize(pnl[h_half[idx] == q]) for q in (1, 2)]
        tag = "LIVE" if cfg == fz["live"] else "CAND"
        print(f"\n[{tag}] {cfg}")
        print(f"  holdout n={H['n']} win={H['win']:.1f}% be={H['be']:.1f}% net={H['net_c']:+.2f}c t={H['t']:+.2f} "
              f"TP/stop/settle={H.get('share_tp', 0):.2f}/{H.get('share_stop', 0):.2f}/{H.get('share_settle', 0):.2f}")
        print(f"  holdout 5-day halves: n={hh[0]['n']} net={hh[0].get('net_c', float('nan')):+.2f}c | "
              f"n={hh[1]['n']} net={hh[1].get('net_c', float('nan')):+.2f}c")
        print(f"  harness 15-day halves: n={res['half1']['n']} net={res['half1']['net_c']:+.2f}c | "
              f"n={res['half2']['n']} net={res['half2']['net_c']:+.2f}c; train net={res['train']['net_c']:+.2f}c "
              f"t={res['train']['t']:+.2f}")


if __name__ == "__main__":
    {"sweep": sweep, "holdout": holdout}[sys.argv[1]]()
