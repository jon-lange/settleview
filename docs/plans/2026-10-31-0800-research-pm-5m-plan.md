# Polymarket US 5m: strategy research plan (pre-registration)

Status: draft for the user's review (user, 2026-10-31: "The 5 min one we will need to really think about strategy";
"Yes" to drafting a pre-registered plan). Research only: no orders, no live app changes. Follows AGENTS.md's honesty
rules and the common rules in docs/research/polymarket-short-term-strategies.md § "Pre-registered test specs".

## Why 5m needs its own work

- Our rules (H4, CUSHION100, LATERESID, HPANIC) were built and backtested on Kalshi 15m. Live 5m so far: HPANIC 0/3. No 5m rule has been tested.
- The 5m studies the user shared (2026-10-31) describe **Polymarket international**. We trade **Polymarket US**:
  settlement is CF Benchmarks BRTI 60 s average before close ≥ 60 s average before open (flat = Up), the same index
  as Kalshi, not Chainlink; one book per market (no split/merge, no complete-set minting); taker fee
  0.0695·C·P·(1−P) rounded to the nearest cent per order (2¢ on a 1-lot at 33–68¢; 2¢, not 3¢, on 2 @ 22¢), maker 0, no rebate at our size.
  So oracle-lag tricks, split/merge market making and rebate farming do not transfer.
- Mapping of the six shared ideas: price-the-lead → **5M-LEAD** (new, below); spot latency → not ours (co-location
  race); two-sided maker → not now (no split/merge; needs fast cancel/reprice code); late 97–99¢ → **5M-FAV** (cheap
  kill test); cheap side → already killed twice (H-PANIC, LATE_RESIDUAL) and **S4 ARGONA** stays as specified;
  book imbalance / multi-asset → no data (we record BTC only, top of book only).

## Data (checked 2026-10-31)

| Source | What | Range |
|---|---|---|
| `data/btcflow.db:index_1s` | our 1-s BTC index (BRTI proxy), ~1–2 s late per row, 2.7–3.6 s when finalized | 2026-09-28 → now (2.8M rows) |
| `data/research/polymarket_us.db:pm_ws_quotes` | real 5m/15m/1h bid/ask + depth from the WebSocket | 2026-10-29 13:06Z → **10-30 00:37Z (recorder died, unnoticed)**, again from 2026-10-31 12:57Z |
| `pm_windows` | official price to beat, settlement, result | from the settlement recorder (restarted 2026-10-31) |
| `polymarket_us_history.db` | 5m display prices (no bid/ask), 1,977 windows | 2026-10-22 → 10-29 |

Only 136 5m windows have real asks today. Every test below waits for recorded asks; the dates are set from the
restart (D0 = 2026-10-31).

## U0 — Keep the recorders alive (before anything else)

The WebSocket recorder stopped on 2026-10-30 00:37Z and nothing told us. Run both recorders
(`scripts/record_polymarket_us_ws.py`, `scripts/record_polymarket_us.py`) under the same watchdog as the app (or a
scheduled task the user approves), and add a daily line to the Telegram summary: 5m windows recorded yesterday out
of 288, and the longest gap. A window with a recorder gap > 5 s in its decision second is excluded before outcomes
are looked at (common rule).

## 5M-LEAD — price the lead (new)

Hypothesis: on PM US 5m, the leading side's ask sometimes sits below the historical chance that a lead of that size,
at that time, holds to settlement, by more than the fee.

- **Hold-rate table (built only from data before D0, so it never sees the test period).** Every 5m window
  2026-09-28 → 2026-10-30 from `index_1s`: strike = our index's 60 s average before open; outcome = our index's
  60 s average before close ≥ strike (proxy settlement). At each check time t_left ∈ {180, 150, 120, 90, 60} s,
  lead L = (index − strike) / strike in basis points, using the index value **3 s before** the check (what the live
  app would have seen). Cells: t_left × |L| bin {0–2, 2–5, 5–10, 10–20, 20+ bp} × volatility tercile (trailing
  15-min realized 1-s vol, tercile cut points from the same period). Hold rate = share of windows in the cell where
  the leading side won. A cell needs ≥ 200 windows or it is not used.
- **Proxy check (reported, not tuned).** On windows with an official result, the share where the proxy outcome
  matches; and the matching rate by |L| bin. If below 98% overall, the table is rebuilt on official
  `price_to_beat` / `settlement_price` where they exist and the run is reported both ways.
- **Rule.** At the first check time in a window where the leading side's ask a satisfies
  a + 1¢ slippage + fee(a) ≤ hold_rate − 3¢, buy the leading side with $1 and hold to settlement. One entry per
  window. Final-minute projection is out of scope (needs the fast recorder of S3), so t_left stops at 60 s.
- **Configurations (all counted in the multiple-testing bar):** (1) with the volatility split; (2) without it.
  Nothing else varies. Margin 3¢, bins and check times are fixed now.
- **Scoring.** Primary: the $1 stake the agent would use (contracts = $1 / ask, fee rounded to the nearest cent per order, as live).
  Secondary: 10-contract lots. Entries only at asks really shown in `pm_ws_quotes` at or after the check second,
  with ≥ the needed size displayed. Official `pm_windows` result. Report n, win % vs break-even %, net ¢/contract,
  t clustered by window, and how often each cell fired.
- **Pass bar.** The table comes from an earlier period, so the whole recorded-ask period is out of sample; it is
  still split chronologically into halves. Survives only if net > 0, both halves > 0, n ≥ 30 trades, and t above
  the Bonferroni bar the harness prints. A survivor is a CANDIDATE → shadow (agent proposals in Assist, skipped and
  scored on would-have P&L) → $1 live.
- **Dates.** Frequency read (no outcomes) **2026-11-03**; final read **2026-11-09** (9 days ≈ 2,600 windows).
- **Prior.** Weak-to-even: Kalshi 15m H4 (the same idea) looks fair-priced; 5m books are thinner (spread 1.22¢) and
  may lag more, which is the only reason to expect anything.

## 5M-FAV — late favourite at 95–99¢ (new, expected KILL)

- **Rule.** First snapshot at or after T−60 s where one side's ask is in [95¢, 99¢]; buy it with $1; hold. A second,
  separately counted config at T−30 s.
- **Why it is cheap to kill.** At $1 a 98¢ buy is one contract; the fee (raw 0.14¢) rounds to 0¢, so a win nets ≈ +2¢
  and a loss −98¢: break-even ≈ 98%. Report win % vs that break-even.
- **Pass bar and dates** as 5M-LEAD (standard bar; final read 2026-11-09).

## S1 PM-NEST, S4 ARGONA — re-dated

Specs unchanged (polymarket-short-term-strategies.md). The recorder outage moves their reads: S1 first read
(frequency) **2026-11-07**, train/holdout **2026-11-21**; S4 final **2026-11-09**. S1 is the one structural idea we
have (the last 5m of a 15m window settles on the same fixing, 658/658) and stays first in line.

## S3 5M-FM — still blocked

Needs a < 500 ms multi-exchange spot recorder. Not started by this plan; a separate decision for the user (it means
new WebSocket feeds from 3+ exchanges).

## What happens with results

- Every configuration run is appended to `data/research/experiments_results.csv` and every verdict goes into
  `docs/research/LEARNINGS.md` (new rows 5M-LEAD and 5M-FAV; S1/S4 updated), wins and losses alike.
- A CANDIDATE becomes an agent rule (`agent_rules`) for a **Polymarket 5m agent**, first in Assist with proposals
  scored on would-have P&L, then $1 Auto if the user agrees. Until then a 5m agent would run on model judgment only.

## Open questions for the user

1. Run the recorders under the app's watchdog (my default), or a separate scheduled task?
2. The S3 fast spot recorder (new exchange feeds): worth building now, or after these reads?
