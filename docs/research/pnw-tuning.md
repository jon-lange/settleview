# PNW cushion strategy: threshold tuning (scenario analysis)

Question: is the live PNW cushion rule (`domain/strategy/pnw.py` PnwParams) too conservative or too aggressive, and
can its thresholds be tuned without fooling ourselves?

## Pre-registration (written 2026-10-29, before any of the grid below was run)

**Live rule (baseline):** decision at the 1-minute candle closes 9, 8, 7 min before close (first qualifying minute);
BTC index >= $50 past Kalshi's floor_strike -> that side; its ask 65–80c; buy at ask + 1c; resting maker sell at
ask + 20c (cap 99c); taker sell at the bid - 1c once BTC (index, minute marks) is back within $20 of the strike;
else hold to settlement; one trade per window. In the harness this is `pnw_tune:cushion=50,start=9,end=7,lo=65,hi=80`
with exit `mtp20_bstop20` (identical to the earlier `pnw_cushion_tp20:variant=v1|mtp20_bstop20`; checked below).

**Grid (full product, nothing trimmed): 5 x 5 x 4 x 4 x 4 = 1,600 configurations.**

| axis | values (in this order) | live |
|---|---|---|
| cushion $ (BTC past strike) | 25, 35, 50, 75, 100 | 50 |
| entry window, minutes left (start, end) | (10,8), (9,7), (8,6), (7,5), (9,5) | (9,7) |
| price band c (lo, hi) | (60,80), (65,80), (65,85), (70,90) | (65,80) |
| TP gap c (maker sell at ask + x) | 10, 15, 20, none (hold) | 20 |
| index stop gap $ (sell when BTC within $x of strike) | 10, 20, 30, none | 20 |

Exit names: `mtp{tp}_bstop{gap}`, `mtp{tp}` (no stop), `bstop{gap}` (no TP), `hold`.

**Scoring:** `scripts/experiments.py` unchanged (minute resolution, real candle bid/ask, 1c slippage primary,
exact taker fee rounded up, maker TP fills at its price with no fee, Kalshi official result, one trade per window,
t clustered by window). Train = first 20 UTC days (2026-09-29 .. 2026-10-18), holdout = last 10. Every one of the
1,600 configs is appended to `data/research/experiments_results.csv` (family `pnw_tune`) and so counted in the
report's Bonferroni correction. Driver: `scripts/quick/pnw_tune.py`.

**Sensitivity (train only):** one-at-a-time moves along each axis from the live setting, reporting train n,
trades/day, win %, break-even %, net c/contract, t.

**Candidate selection (train only; at most 3, frozen before any holdout number is read):**

- C1: highest train t among configs with train n >= 100 (>= 5 trades/day).
- C2: highest *neighbourhood* train t: the mean train t of the config and every config one step away on a single
  axis (axes ordered as listed above), among configs with train n >= 100 — favours plateaus over spikes.
- C3: highest train net c/contract among configs with train n >= the live setting's train n (at least as active).
- A pick must have train net > 0; a duplicate pick passes to the next-best config of that rule. If fewer than three
  qualify, fewer are tested.

**Pass bar (holdout, run once):** holdout n >= 30; holdout t above the Bonferroni line printed by
`experiments.py report` (one-sided 5% family-wise over every config ever tried, including these 1,600); net > 0 in
both chronological halves (the harness's 15-day halves, and the holdout's own two 5-day halves). Meeting it makes a
config a CANDIDATE only (it would then need new days after 2026-10-28 and the shadow lab). A change to live
PnwParams is justified only if a candidate passes; "better than live on the holdout" alone is not enough.

The live setting is also reported on the holdout for comparison.

## Results (run 2026-10-29)

Check: `pnw_tune` at the live defaults picks exactly the same 687 entries as `pnw_cushion_tp20:variant=v1`, and its
holdout result (n=222, −1.97c) matches the earlier harness run.

### Train only (first 20 days), one axis at a time around the live setting (minute, 1c slippage)

Live (train): n=465 (23.2/day), won 57.2% vs 62.9% break-even, **−2.68c/contract, t −2.34**; exits 56% TP / 44% stop
(winners that never hit +20c earlier still reach it at the final candle, so "settle" is ~0%).

| axis | value | n | /day | win % | BE % | net c | t |
|---|---|---|---|---|---|---|---|
| cushion $ | 25 | 927 | 46.4 | 50.3 | 59.4 | −3.88 | −5.11 |
| | 35 | 743 | 37.1 | 52.5 | 61.3 | −3.96 | −4.45 |
| | **50 (live)** | 465 | 23.2 | 57.2 | 62.9 | −2.68 | −2.34 |
| | 75 | 179 | 8.9 | 62.6 | 65.2 | −1.32 | −0.69 |
| | 100 | 70 | 3.5 | 58.6 | 67.2 | −4.52 | −1.38 |
| window (min left) | 10–8 | 599 | 29.9 | 54.4 | 62.5 | −3.76 | −3.74 |
| | **9–7 (live)** | 465 | 23.2 | 57.2 | 62.9 | −2.68 | −2.34 |
| | 8–6 | 356 | 17.8 | 56.7 | 63.0 | −2.98 | −2.23 |
| | 7–5 | 262 | 13.1 | 59.2 | 65.0 | −2.89 | −1.76 |
| | 9–5 | 535 | 26.8 | 57.8 | 63.4 | −2.70 | −2.48 |
| band c | 60–80 | 474 | 23.7 | 56.8 | 62.7 | −2.77 | −2.45 |
| | **65–80 (live)** | 465 | 23.2 | 57.2 | 62.9 | −2.68 | −2.34 |
| | 65–85 | 701 | 35.0 | 60.2 | 66.4 | −2.93 | −3.13 |
| | 70–90 | 881 | 44.0 | 66.3 | 71.4 | −2.48 | −2.97 |
| TP gap c | 10 | 465 | 23.2 | 63.2 | 79.4 | −6.11 | −6.75 |
| | 15 | 465 | 23.2 | 60.2 | 70.2 | −4.27 | −4.15 |
| | **20 (live)** | 465 | 23.2 | 57.2 | 62.9 | −2.68 | −2.34 |
| | none | 465 | 23.2 | 54.6 | 58.3 | −1.90 | −1.50 |
| index stop $ | 10 | 465 | 23.2 | 61.5 | 66.3 | −2.53 | −2.04 |
| | **20 (live)** | 465 | 23.2 | 57.2 | 62.9 | −2.68 | −2.34 |
| | 30 | 465 | 23.2 | 50.5 | 58.4 | −3.27 | −3.10 |
| | none | 465 | 23.2 | 77.8 | 81.3 | −3.22 | −1.77 |

Live entry with no TP and no stop (hold): 75.9% vs 77.6%, −1.65c, t −0.85.

Reading it: the live rule sits on a flat, uniformly negative plateau, not a cliff. Looser (smaller cushion, earlier
window, wider band) = more trades and the same or worse per trade; tighter = fewer trades, still negative. Every
TP/stop overlay costs relative to holding (median over all 100 entries: hold −1.9c, best overlay bstop20 −2.6c,
mtp10 −6.5c) — a tighter TP buys win rate at a worse break-even. Over all 1,600 configs on train: 21 had net > 0,
**none had t > 2** (max t 0.81), median −4.1c. That is what pure noise around a ~−3c cost of trading looks like.

### Frozen candidates (selected on train only) and the single holdout run

| | config | train n / net c / t | holdout n | win % | BE % | net c | t | holdout 5-day halves | 15-day halves |
|---|---|---|---|---|---|---|---|---|---|
| C1 (best train t) | cushion 75, 9–5 min, 65–80c, hold | 203 / +1.67 / +0.60 | 82 | 72.0 | 78.4 | **−6.34** | −1.28 | −5.33 / −7.57 | +2.82 / −3.79 |
| C2 (best neighbourhood) | cushion 75, 8–6 min, 60–80c, hold | 115 / +0.57 / +0.15 | 42 | 73.8 | 79.6 | **−5.80** | −0.84 | −7.04 / −4.44 | −2.03 / −0.27 |
| C3 (best net, n >= live) | cushion 35, 7–5 min, 60–80c, hold | 508 / +0.17 / +0.09 | 268 | 72.0 | 76.7 | **−4.59** | −1.68 | −4.99 / −4.13 | +1.27 / −3.98 |
| live | cushion 50, 9–7 min, 65–80c, mtp20_bstop20 | 465 / −2.68 / −2.34 | 222 | 58.1 | 62.4 | **−1.97** | −1.21 | −1.93 / −2.02 | −2.78 / −2.13 |

Bonferroni line after this run (3,566 configs ever tried): holdout t must exceed **4.19**. No candidate is even
positive. All three train "winners" flipped to −4.6 to −6.3c on the holdout — the train edge was selection noise.
The best-looking train configs were all hold-to-settlement variants, which have the highest variance and so the
easiest spurious t.

### Fresh out-of-sample data

`data/btcflow.db` holds 39 settled KXBTC15M markets after the 30-day set (2026-10-29 00:00–11:45 UTC), with live
`kalshi_quotes` for 32 of them (not the candle format the harness uses). At ~23 trades/day the live rule would fire
in roughly 8–10 of them — far too few to say anything (sd per trade ~40c, so ±13c at n=10). Not tuned on, not
scored. A useful fresh check needs weeks of recorded windows and would itself only re-test the live rule.

### Verdict

**KILLED (tuning).** No threshold change survives; the live PnwParams are not on a cliff and are not obviously
too conservative or too aggressive — every neighbouring setting loses about the same 2–4c/contract. There is no
evidence-based reason to change PnwParams; changing them would swap one losing setting for another chosen on noise.

## Exit study: "let trades ride" (pre-registered 2026-10-29, before any of these exits was run)

Question from the user: the live stop is too sensitive; use our signals and let trades ride longer. Live so far
(10 trades): 3 index stops, 2 of which would have won if held; TP capped every winner; holding all 10 would have
made about 1.8x what the live exits made. Ten trades cannot separate rules (sd ~40c/trade), so this is a backtest of EXITS only.

**Entry (fixed, not tuned):** live PNW, `pnw_tune:cushion=50,start=9,end=7,lo=65,hi=80` (687 trades over 30 days).

**Exit family (28 configs; each stop rule with and without the +20c resting maker TP `mtp20`):**

| group | exits | rule |
|---|---|---|
| baselines | `hold`, `mtp20`, `bstop20`, `mtp20_bstop20` (current) | as in the harness |
| index stop at / past the strike | `bstop0`, `bstop-10` (+ `mtp20_` versions) | taker sell at the bid once BTC (index, minute marks) is on the wrong side of the strike / $10 past it on the wrong side |
| time-gated $20 index stop | `gstop20n3`, `n2`, `n1` (+ TP) | the $20 index stop only counts at minute marks with <= N minutes left (candle closes 15-N .. 15 min after the open); earlier breaches are ignored |
| contract-price ("clearly lost") stop | `bidle40`, `bidle30`, `bidle20` (+ TP) | sell when the held side's candle-close bid <= 40 / 30 / 20c |
| position dollar stop | `dl20`, `dl40` (+ TP) | sell when the held side's bid <= (1 − x) × entry price paid (mark-to-bid loss >= 20% / 40% of the contract price) |
| model / signal exit | `medge3`, `mfair35` (+ TP) | D.F model fair odds for our side (computed 1 s before the minute mark): sell when fair < bid − 3c (selling is +EV by the model) / when fair < 35%. D.F exists only through 2 min left, so these exits cannot fire in the last 2 minutes |

Trimmed vs the suggestion: nothing dropped; "−$10 past the strike" = `bstop-10`. Sales are taker at the minute
bid − 1c with the exact fee; when the TP and a stop trigger in the same minute the stop is assumed first (harness
convention). Implementation: `scripts/quick/pnw_exits.py` (a generic copy of `sim_minute`'s maker-TP/stop branch
with an arbitrary stop mask; it must reproduce `experiments.sim_minute` exactly for `hold`, `mtp20`, `bstop20`,
`mtp20_bstop20`, `bstop0`, `bstop-10` or the run is void). All 28 configs x slippage 0/1/2c are appended to
`experiments_results.csv` (family `pnw_exit`) and count in the Bonferroni line.

**Train report (first 20 days, 1c):** n, win %, break-even %, net c/contract, t, mean loss on losing trades, worst
trade, share of trades where the stop fired, share of stopped trades that would have WON if held; plus paired
difference vs hold and vs current (same entries, per-window difference, t clustered by window).

**Picks (train only, frozen before any holdout number):** up to 3 non-baseline configs, the highest train net
c/contract, at most one per group (index / gated / bid / dollar / model). Ties broken by train t.

**Pass bar (holdout, run once):**
- *Strategy-level (CANDIDATE):* holdout n >= 30, holdout t above the report's Bonferroni line, net > 0 in both
  15-day halves and both 5-day holdout halves. (Expected: nothing passes; the entry itself loses.)
- *Exit-level (which exit to use if the user keeps running PNW):* paired holdout difference pick − current, and
  pick − hold, one-sided p < 0.05/3 (Bonferroni over the 3 picks). Passing this means "less bad than the current
  exit", not "profitable". If no pick passes, the recommendation follows the pre-existing evidence ordering only
  as a weak statement, labelled as such.
- Also reported for hold vs current (train and holdout): max drawdown, per-trade P&L distribution, worst trade,
  and dollars for a $10 stake.

### Exit study results (run 2026-10-29)

Check passed: the generic simulator reproduces `experiments.sim_minute` exactly (P&L and exit reason, 0/1/2c) for
`hold`, `mtp20`, `bstop20`, `mtp20_bstop20`, `bstop0`, `bstop-10`. 28 configs appended (family `pnw_exit`);
Bonferroni line now **4.19** (3,590 configs).

**Train (first 20 days, n=465 each, 1c).** "fired" = share of trades the stop sold; "fired&won" = share of those
that would have won if held. Paired d = per-window difference vs hold / vs current (c/contract, t).

| exit | win % | BE % | net c | t | avg loss c | worst c | max DD $/ct | fired % | fired&won % | d vs hold (t) | d vs current (t) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| hold | 75.9 | 77.6 | −1.65 | −0.85 | −76.2 | −82 | 13.3 | 0 | – | 0 | +1.02 (0.64) |
| mtp20 (TP only) | 77.9 | 81.3 | −3.22 | −1.77 | −76.4 | −82 | 18.9 | 0 | – | −1.56 (−2.48) | −0.54 |
| bstop20 | 54.6 | 58.3 | −1.90 | −1.50 | −29.7 | −79 | 10.7 | 48.6 | 50.9 | −0.24 | +0.78 (1.70) |
| **mtp20_bstop20 (current)** | 57.2 | 62.9 | −2.68 | −2.34 | −29.6 | −79 | 14.4 | 44.1 | 49.8 | −1.02 | 0 |
| bstop0 | 64.5 | 66.7 | −1.42 | −0.94 | −43.7 | −82 | 9.9 | 35.1 | 35.6 | +0.24 | +1.26 (1.26) |
| **bstop-10** | 68.6 | 69.4 | **−0.59** | −0.37 | −49.9 | −82 | 8.4 | 29.3 | 27.2 | +1.07 (1.01) | +2.09 (1.81) |
| mtp20_bstop-10 | 70.5 | 73.3 | −1.85 | −1.28 | −48.5 | −82 | 12.8 | 28.0 | 27.7 | −0.20 | +0.83 |
| gstop20n3 | 63.9 | 66.4 | −1.60 | −1.03 | −42.3 | −82 | 11.3 | 40.0 | 40.9 | +0.05 | +1.08 |
| gstop20n2 | 66.7 | 69.4 | −1.89 | −1.14 | −48.4 | −82 | 11.6 | 37.9 | 38.6 | −0.24 | +0.79 |
| **gstop20n1** | 72.0 | 73.2 | −0.90 | −0.51 | −58.1 | −82 | 8.5 | 31.6 | 34.7 | +0.75 (1.01) | +1.78 (1.29) |
| **bidle40** | 69.5 | 70.8 | −0.97 | −0.59 | −53.0 | −82 | 8.1 | 28.2 | 22.9 | +0.68 | +1.71 (1.44) |
| bidle30 | 71.8 | 73.8 | −1.63 | −0.92 | −61.7 | −82 | 13.6 | 24.1 | 17.0 | +0.03 | +1.05 |
| bidle20 | 74.2 | 75.7 | −1.39 | −0.75 | −68.5 | −82 | 12.3 | 20.4 | 8.4 | +0.27 | +1.29 |
| dl20 | 54.8 | 60.0 | −2.78 | −2.09 | −32.6 | −82 | 14.8 | 43.9 | 48.0 | −1.13 | −0.10 |
| dl40 | 67.3 | 69.1 | −1.25 | −0.79 | −49.0 | −82 | 8.7 | 30.5 | 28.2 | +0.41 | +1.43 (1.30) |
| medge3 (model) | 54.0 | 63.4 | −3.44 | −2.91 | −23.2 | −82 | 17.2 | 69.3 | 73.9 | −1.78 | −0.76 |
| mfair35 (model) | 70.8 | 73.5 | −2.27 | −1.27 | −60.8 | −82 | 15.2 | 21.1 | 24.5 | −0.61 | +0.41 |

Every `mtp20_` version is 0.9–1.4c worse than the same rule without the TP (other mtp20_ rows: gstop n3 −2.83,
n2 −3.20, n1 −2.27; bidle40 −2.30, 30 −3.03, 20 −2.85; dl20 −3.45, dl40 −2.51; medge3 −4.02, mfair35 −3.59;
bstop0 −2.39). The model exits are the worst stops (the model has no information beyond the price, H-MM1).
Every rule is still negative on train; none has t > 0.

**Frozen picks (train, best net per group):** `bstop-10`, `gstop20n1`, `bidle40`.

**Holdout (last 10 days, run once, 1c).** Exit-level bar: paired t > 2.13 (one-sided 0.05/3).

| exit | n | win % | BE % | net c | t | avg loss c | max DD $/ct | fired % / fired&won % | 5-day halves | 15-day halves | vs hold c (t) | vs current c (t) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| bstop-10 | 222 | 69.8 | 70.0 | **−0.15** | −0.06 | −51.1 | 3.6 | 29 / 20 | +0.45 / −0.89 | −0.04 / −0.84 | +3.39 (**2.54**) | +1.82 (1.14) |
| gstop20n1 | 222 | 72.1 | 75.2 | −2.64 | −0.97 | −64.4 | 7.7 | 34 / 34 | −3.12 / −2.04 | −0.08 / −2.84 | +0.90 (1.15) | −0.67 (−0.34) |
| bidle40 | 222 | 70.7 | 71.4 | −0.51 | −0.22 | −54.6 | 4.6 | 27 / 13 | −0.10 / −1.03 | −0.47 / −1.18 | +3.02 (**2.55**) | +1.46 (0.85) |
| hold | 222 | 74.3 | 77.9 | −3.54 | −1.21 | −77.2 | 10.1 | – | −3.34 / −3.78 | −0.19 / −4.33 | 0 | −1.57 (−0.70) |
| current | 222 | 58.1 | 62.4 | −1.97 | −1.21 | −28.3 | 5.7 | 45 / 44 | −1.93 / −2.02 | −2.78 / −2.13 | +1.57 (0.70) | 0 |

(After the freeze I also printed `mtp20_bstop-10` on the holdout for the distribution table: −2.38c. Not a pick, not
used for any decision; disclosed for completeness.)

**Per-trade P&L distribution, c/contract (p5 / p25 / median / p75 / p95; share of trades losing >= 50c):**

| exit | split | mean | sd | p5 | p25 | p50 | p75 | p95 | worst | <= −50c | max DD, $10 stake |
|---|---|---|---|---|---|---|---|---|---|---|---|
| hold | train | −1.65 | 42.2 | −80 | +18 | +20 | +23 | +29 | −82 | 24.1% | $183 |
| current | train | −2.68 | 24.7 | −44 | −25 | +17 | +18 | +18 | −79 | 2.8% | $197 |
| bstop-10 | train | −0.59 | 34.5 | −69 | −36 | +19 | +23 | +28 | −82 | 13.3% | $115 |
| hold | holdout | −3.54 | 43.5 | −81 | −70 | +19 | +24 | +28 | −82 | 25.7% | $138 |
| current | holdout | −1.97 | 24.2 | −42 | −23 | +17 | +18 | +18 | −76 | 3.2% | $78 |
| bstop-10 | holdout | −0.15 | 34.6 | −68 | −36 | +19 | +23 | +28 | −82 | 14.9% | $50 |

($10 stake ≈ 13.7 contracts at ~73c; one losing held trade ≈ −$10 to −$11; one current-rule stop ≈ −$3 to −$6.)

### Exit study verdict

- **No exit makes PNW profitable** (best holdout −0.15c, t −0.06; Bonferroni bar 4.19): strategy-level KILLED stands.
- **"Let it ride to settlement" is KILLED as an improvement**: hold beat the current exit on train (+1.0c, t 0.6)
  but lost to it on the holdout (−1.6c, t −0.7), with 8x as many trades losing >= 50c. Both the current stop and
  holding are about equally bad; holding just swaps many small losses for fewer full losses.
- **Dropping the +20c TP** helped on train in every pair (0.9–1.4c) — consistent with H-PNW-T — not separately
  tested on the holdout beyond hold vs mtp20.
- **`bstop-10` (exit only when BTC is $10 past the strike on the wrong side, no TP)** was the train pick that also led
  on the holdout (−0.15c) and beat hold significantly (t 2.54), but did NOT clear the bar vs the current exit
  (+1.8c, t 1.14). **OPEN (weak lead)**: it is a defensible "looser stop" but not proven better than today's.

## Exit study 2: fixed-price take profit + patient stop (pre-registered 2026-10-29, before any of these was run)

User's clarification: keep a take profit, but at a FIXED price (~85c, "if we're profiting, take it, don't wait for
the close"), not entry + 20c; and make the stop more patient ("let it play out a minute"; sell only when the
evidence says it won't recover). Live exit is now `bstop-10` with no TP (from Exit study 1).

**Entry (fixed):** live PNW, `pnw_tune:cushion=50,start=9,end=7,lo=65,hi=80` (same as above).

**Family: 4 TPs x 5 stops = 20 configs.**
- TP: `ftp85`, `ftp90`, `ftp95` = resting MAKER sell at 85 / 90 / 95c (fee 0, no slippage), filled at the first
  minute mark after entry whose held-side candle-close bid >= the level (the simulator's existing maker-TP fill rule;
  entry asks are 65-80c so the level is always above entry); or no TP.
- Stop (taker sell at minute bid − slippage, exact fee): `bstop-10` (BTC $10 past the strike on the wrong side,
  current live), `bstop0` (strike cross), `bstop-10c` = **confirmed**: BTC beyond −$10 at TWO consecutive minute
  marks (this minute and the previous one); sale at the second one (approximates "let it play out a minute"),
  `bstop-20` (BTC $20 past the strike on the wrong side), or no stop.
- Same-minute TP and stop: stop assumed first (harness convention).
- Already run in Exit study 1 (re-run here only for the table and appended again under family `pnw_exit2`, so they
  count twice in the Bonferroni denominator — conservative): `hold`, `bstop-10`, `bstop0`. New: 17.
- All 20 x slippage 0/1/2c appended to `experiments_results.csv`. Script: `scripts/quick/pnw_exits_fixedtp.py`
  (reuses `pnw_exits.sim`; must reproduce `experiments.sim_minute` for hold / bstop-10 / bstop0 or the run is void).

**Train report (first 20 days, 1c):** n, win % vs break-even %, net c/contract, t, avg win, avg loss, worst loss,
% stopped, % of stopped that would have won if held, % hitting TP, paired difference vs current live.

**Holdout set (frozen before any holdout number):** at most 2 picks = highest train net c/contract among the 18
configs other than the two always-run ones (ties by train t), plus always `ftp85_bstop-10` (user's preference) and
`bstop-10` (current live). Run once.

**Pass bar (holdout):** strategy-level CANDIDATE = n >= 30, t above the report's Bonferroni line, positive in both
15-day and both 5-day holdout halves (expected: nothing). Exit-level = paired difference vs current live with
one-sided p < 0.05/3 (paired t > 2.13; 3 comparisons). Failing that, the answer to "does a fixed 85c TP help" and
"does the confirmed stop help" is read from the paired train+holdout differences as a weak statement only.

### Exit study 2 results (run 2026-10-29)

Check passed (sim == `experiments.sim_minute` for hold / bstop-10 / bstop0 / bstop-20, 0/1/2c). 20 configs x 3 slips
appended (family `pnw_exit2`); Bonferroni line **4.19** (3,607 configs). TP fill = held-side candle-close bid >= level
at a minute mark (existing rule; intra-minute touches are not counted, so real fills would be somewhat more frequent).

**Train (first 20 days, n=465 each, 1c).** d live = paired difference vs `bstop-10` (current live), c (t).

| exit | win % | BE % | net c | t | avg win c | avg loss c | worst c | stopped % | stopped&would-win % | TP % | d live (t) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **bstop-10 (live)** | 68.6 | 69.4 | **−0.59** | −0.37 | 22.0 | −49.9 | −82 | 29.3 | 27.2 | 0 | 0 |
| bstop0 | 64.5 | 66.7 | −1.42 | −0.94 | 21.8 | −43.7 | −82 | 35.1 | 35.6 | 0 | −0.83 (−1.35) |
| bstop-10c (confirmed) | 71.8 | 73.7 | −1.54 | −0.86 | 21.9 | −61.4 | −82 | 20.4 | 20.0 | 0 | −0.95 (−1.39) |
| bstop-20 | 70.8 | 72.0 | −0.95 | −0.56 | 21.9 | −56.3 | −82 | 24.3 | 23.0 | 0 | −0.37 (−0.72) |
| hold | 75.9 | 77.6 | −1.65 | −0.85 | 22.0 | −76.2 | −82 | 0 | – | 0 | −1.07 (−1.01) |
| **ftp85_bstop-10 (user)** | 75.5 | 86.9 | **−6.08** | −5.50 | 7.0 | −46.2 | −77 | 23.9 | 27.0 | 75.5 | −5.50 (−5.42) |
| ftp85_bstop0 | 72.3 | 85.8 | −6.48 | −6.19 | 6.8 | −41.2 | −77 | 27.7 | 34.9 | 72.3 | −5.89 (−5.25) |
| ftp85_bstop-10c | 78.3 | 89.2 | −7.01 | −5.51 | 6.9 | −57.2 | −81 | 18.5 | 20.9 | 78.3 | −6.42 (−5.68) |
| ftp85_bstop-20 | 77.2 | 88.3 | −6.58 | −5.49 | 6.9 | −52.2 | −79 | 20.4 | 23.2 | 77.2 | −5.99 (−5.56) |
| ftp85 | 82.2 | 91.5 | −7.74 | −5.24 | 7.0 | −75.6 | −82 | 0 | – | 82.2 | −7.15 (−5.44) |
| ftp90_bstop-10 | 72.7 | 79.6 | −4.07 | −3.22 | 12.0 | −46.8 | −82 | 26.5 | 26.8 | 72.7 | −3.48 (−4.06) |
| ftp90_bstop0 | 69.3 | 77.8 | −4.60 | −3.85 | 11.9 | −41.7 | −82 | 30.5 | 34.5 | 69.3 | −4.01 (−3.95) |
| ftp90_bstop-10c | 75.7 | 83.1 | −5.24 | −3.60 | 12.0 | −58.8 | −82 | 20.0 | 20.4 | 75.7 | −4.65 (−4.51) |
| ftp90_bstop-20 | 74.8 | 81.7 | −4.47 | −3.28 | 11.9 | −53.3 | −82 | 22.2 | 22.3 | 74.8 | −3.88 (−4.02) |
| ftp90 | 79.8 | 86.3 | −5.76 | −3.49 | 12.0 | −76.0 | −82 | 0 | – | 79.8 | −5.18 (−4.10) |
| ftp95_bstop-10 | 70.1 | 74.0 | −2.57 | −1.78 | 17.0 | −48.5 | −82 | 28.2 | 27.5 | 69.7 | −1.98 (−3.52) |
| ftp95_bstop0 | 66.5 | 71.8 | −3.18 | −2.33 | 16.9 | −42.9 | −82 | 32.7 | 34.2 | 66.2 | −2.60 (−3.21) |
| ftp95_bstop-10c | 73.3 | 78.1 | −3.70 | −2.26 | 17.0 | −60.5 | −82 | 20.4 | 20.0 | 73.3 | −3.11 (−3.64) |
| ftp95_bstop-20 | 72.3 | 76.5 | −3.06 | −1.97 | 17.0 | −55.2 | −82 | 23.2 | 23.2 | 72.0 | −2.47 (−3.33) |
| ftp95 | 77.4 | 81.7 | −4.02 | −2.21 | 17.0 | −76.2 | −82 | 0 | – | 77.4 | −3.43 (−2.99) |

Monotone in the TP level: every fixed TP is worse than no TP for every stop, by ~2c (95c), ~3.5c (90c) and ~5.5c
(85c). An 85c TP caps a ~73c entry's win at ~+7c (vs ~+22c riding) while the break-even win rate jumps to ~87%.
The confirmed stop fires less (20% vs 29%) but its stops are only slightly less often wrong (20% vs 27% would have
won) and each loss is bigger (−61 vs −50c): −0.95c vs live on train.

**Frozen picks (train, best net excl. the two required):** `bstop-20`, `bstop0`.

**Holdout (last 10 days, run once, n=222, 1c).** Exit-level bar: paired t > 2.13 vs live.

| exit | win % | BE % | net c | t | avg win / loss c | stopped % / would-win % | TP % | 5-day halves | 15-day halves | vs live c (t) |
|---|---|---|---|---|---|---|---|---|---|---|
| bstop-20 (pick) | 71.2 | 72.4 | −1.00 | −0.40 | 21.9 / −57.5 | 25 / 16 | 0 | −0.91 / −1.11 | −0.12 / −1.81 | −0.85 (−1.28) |
| bstop0 (pick) | 64.0 | 66.8 | −1.89 | −0.85 | 21.8 / −43.9 | 36 / 32 | 0 | −0.98 / −3.02 | −1.26 / −1.87 | −1.74 (−1.72) |
| ftp85_bstop-10 (user) | 77.0 | 87.2 | **−5.45** | −3.47 | 6.8 / −46.6 | 23 / 16 | 77 | −5.98 / −4.78 | −6.54 / −5.22 | **−5.30 (−3.49)** |
| bstop-10 (live) | 69.8 | 70.0 | −0.15 | −0.06 | 21.9 / −51.1 | 29 / 20 | 0 | +0.45 / −0.89 | −0.04 / −0.84 | 0 |

### Exit study 2 verdict

- **Fixed 85c take profit: KILLED, clearly harmful.** −5.3c/contract vs riding on the holdout (paired t −3.49),
  replicating train (−5.5c, t −5.4). It wins more often (77%) but needs 87% to break even. On a $10 stake
  (~14 contracts) that is about −$0.75 per trade vs the live rule. 90c and 95c TPs are also worse than no TP (train).
- **Confirmed ("patient") stop: no help.** Train −0.95c vs live (t −1.4); not a pick, not run on the holdout.
  Waiting a minute avoids some false stops but lets the true losers get ~11c worse.
- No pick beats live on the holdout; nothing passes Bonferroni 4.19. Keep the current live exit (`bstop-10`, no TP).

## Exit study 3: whipsaw grace period / re-check on the $10 stop (pre-registered 2026-10-29, before any of these was run)

Trigger (user, live trade 2026-10-29 10:51 UTC): entry Up at 60c; within 18 s BTC fell > $65 to $15 below the
strike, `bstop-10` sold at 50c (about −19% of the stake), then BTC reversed and Up traded back to ~80c. "We need to adjust for
this whipsaw activity." Question: does a grace period right after entry, or a short re-check before selling, save
more on whipsaws (stop fired, the side then won) than it costs on stops that were right (ridden to $0 instead)?

**Entry (fixed):** live PNW, `pnw_tune:cushion=50,start=9,end=7,lo=65,hi=80`. **Reference exit:** `bstop-10` (live).

**Family (23 rules):**
- `G(g,h)`: for the first g seconds after the entry fill the stop fires only if BTC is $h or more past the strike on
  the wrong side (UP: S < K − h; h = none = no stop at all during the grace); after g seconds the normal `bstop-10`
  (S < K − 10) applies. g ∈ {30, 60, 90, 120, 180} s × h ∈ {20, 30, 50, none} = 20.
- `R(d)`: when BTC is past the $10 level at second t, re-check at t + d; sell only if it is still past the level then
  (fires at the first second t' with dist(t') < −10 and dist(t' − d) < −10, t' − d >= the first stop-check second).
  d ∈ {10, 20, 30} s = 3. (The minute-resolution "confirmed" stop `bstop-10c` = a 60 s re-check, already tested:
  −0.95c vs live on train.)
Every G / R fire is a subset of `bstop-10`'s fires at the same or a later time, so the comparison is clean.

**View A — minute resolution (all 2,848 windows; train n = 465, holdout 222; `pnw_exits.sim` machinery):** the stop
is checked at minute marks only (as in Exit studies 1–2). Grace = the first floor(g/60) minute marks after the entry
mark: g = 30 → 0 marks (identical to `bstop-10`), 60 and 90 → 1, 120 → 2, 180 → 3. So only 12 distinct rules,
but all 20 labels are appended (counted). A minute-mark stop already ignores intra-minute spikes (the 10:51 whipsaw
lasted < 1 min and might not show at a mark), so this view UNDERSTATES whipsaws; it is an approximation.
R(d) cannot be represented at minute marks and is not run in this view.

**View B — second resolution (435 full-tape windows in `data/research/kalshi_trades.db`, last 13:40 of each window;
PNW enters at 9–7 min left so its whole life is covered):** BTC = per-second index from `data/btcflow.db:index_1s`
(verified complete, 2,592,000 seconds for 09-29..10-28), read with the harness's 1 s lag (`Data.btc`). Contract
prices from the tape proxy exactly as `experiments.PrintSim`: entry fills at the first taker-buy print of our side
at <= ask + 1c within 2 s of the decision (no print = no trade); the stop is checked every second from the fill;
the sale is the first opposite-taker print >= 1 s after the trigger, − slippage, exact fee. My simulator must
reproduce `experiments.PrintSim.run` exactly for `hold` and `bstop-10` (0/1/2c) or the run is void. Split = the
harness's (train = first 20 days, holdout = last 10); expected n ≈ 70 train / 35 holdout — small.
All 23 rules + `bstop-10` + `hold` appended with family `pnw_grace_tape` (config suffix `@tape`); view A's 20 with
family `pnw_grace_min`. 45 labels x slip 0/1/2c, all counted in the Bonferroni denominator.

**Train report (1c, both views):** n, win %, break-even %, net c/contract, t, avg loss, worst loss, % stopped, % of
stops that would have won if held, paired d vs `bstop-10` (t), and the whipsaw ledger vs `bstop-10` (same entries):
*saved* = `bstop-10` fired, held side won, rule never fired (n, c gained); *extra full losses* = `bstop-10` fired,
side lost, rule never fired (n, c lost); *delayed* = both fired, rule later (n, net c).

**Picks (train only, frozen in `data/research/pnw_grace_picks.json` before any holdout number; at most 2):**
- P1 = highest train net c among the 23 view-B rules (ties by t), only if its paired train d vs `bstop-10` > 0.
- P2 = highest train net c among the 15 view-A rules with >= 1 grace mark (g >= 60), only if paired train d > 0 and
  it is not the same rule as P1 (else the next best).

**Holdout (run once):** each pick in both views where it exists, plus `bstop-10` and `hold`.
- *Exit-level bar (switch the live exit):* paired holdout difference pick − `bstop-10` with one-sided p < 0.05/2
  (t > 1.96) in the pick's selection view, AND the same sign in the other view when it exists. Otherwise: keep
  `bstop-10`.
- *Strategy-level (CANDIDATE):* n >= 30, holdout t above the report's Bonferroni line, positive in both 15-day and
  both 5-day holdout halves (expected: nothing; the entry itself loses).
Script: `scripts/quick/pnw_grace.py`.

### Exit study 3 results (run 2026-10-29)

Checks passed: minute sim == `experiments.sim_minute` and tape sim == `experiments.PrintSim.run` for hold / bstop-10
(0/1/2c); minute G30* == bstop-10. 45 labels x 3 slips appended (families `pnw_grace_min`, `pnw_grace_tape`);
Bonferroni line **4.20** (3,672 configs). Tape view: 76 train / 29 holdout PNW entries with a tape fill (holdout
below the 30-trade floor). Tables: `data/research/pnw_grace_train_A.csv`, `..._B.csv`.

**The whipsaw problem is real at second resolution.** On the tapes the per-second `bstop-10` fires on 37% of train
trades and 46% of those would have won if held (minute marks: 29% / 27%). But most of those whipsaws do not resolve
inside a grace window; they come back later, after the stop has also caught genuine losers.

**Train, 1c, whipsaw ledger vs `bstop-10`** (saved = live stopped, side won, rule never sold; extra loss = live
stopped, side lost, rule rode to $0; delayed = both sold, rule later — usually at a worse bid):

| rule | view | net c | d vs live c (t) | stopped % / would-win % | saved n (c) | extra full losses n (c) | delayed n (c) |
|---|---|---|---|---|---|---|---|
| bstop-10 (live) | A minute | −0.59 | 0 | 29 / 27 | – | – | – |
| G60/90 h20 | A | −0.56 | +0.02 (0.15) | 29 / 27 | 1 (+64) | 0 | 4 (−53) |
| G60/90 hnone | A | −0.71 | −0.12 (−0.73) | 29 / 27 | 1 (+64) | 0 | 12 (−121) |
| G120 h20 / hnone | A | −0.73 / −1.12 | −0.14 / −0.53 (−1.99) | 29 / 26 | 2 (+123) | 0 | 14 (−191) / 39 (−372) |
| **G180 h20** | A | **−0.23** | **+0.36 (1.06)** | 28 / 24 | 6 (+355) | 0 | 15 (−189) |
| G180 h30 / h50 / hnone | A | −0.50 / −0.80 / −0.92 | +0.09 / −0.22 / −0.34 | 27–28 / 23 | 7–8 (+416…+497) | 1 (−38) | 28–56 (−336…−616) |
| hold | A | −1.65 | −1.07 (−1.01) | 0 | 37 (+2,237) | 99 (−2,734) | – |
| bstop-10 (live) | B tape | −0.31 | 0 | 37 / 46 | – | – | – |
| G30 h20…none | B | −0.30…−0.17 | +0.01…+0.14 | 37 / 46 | 0 | 0 | 1 (+1…+11) |
| G60 hnone | B | +0.52 | +0.82 (0.86) | 36 / 44 | 1 (+72) | 0 | 2 (−10) |
| G60–120 h20/30/50 | B | −0.24…−0.99 | +0.06…−0.68 | 37 / 46 | 0 | 0 | 3–8 (+5…−52) |
| G180 h20 | B | +0.24 | +0.55 (0.60) | 36 / 44 | 1 (+67) | 0 | 12 (−25) |
| G180 hnone | B | +0.53 | +0.84 (0.59) | 34 / 42 | 2 (+139) | 0 | 11 (−76) |
| R10 | B | +0.23 | +0.54 (0.38) | 34 / 42 | 2 (+126) | 0 | 26 (−85) |
| **R20** | B | **+0.89** | **+1.20 (0.64)** | 32 / 38 | 4 (+250) | 0 | 24 (−159) |
| R30 | B | +0.24 | +0.55 (0.31) | 33 / 40 | 3 (+188) | 0 | 25 (−146) |
| hold | B | +2.74 | +3.05 (0.84) | 0 | 13 (+814) | 15 (−582) | – |

Every rule is within noise of live on train (best |t| 1.06). Grace/re-check rarely creates extra full losses
(0–1), but every saved whipsaw is paid for by many delayed stops that sell lower.

**Frozen picks (train):** P1 = `R20` (best tape rule), P2 = `G180h20` (best minute rule with a grace mark).

**Holdout (run once, 1c):**

| rule | view | n | win % | BE % | net c | vs live c (t) | saved / extra / delayed (c) |
|---|---|---|---|---|---|---|---|
| G180h20 (P2) | A minute | 222 | 70.3 | 70.6 | −0.26 | −0.11 (−0.33) | 1 (+66) / 0 / 7 (−91) |
| bstop-10 (live) | A | 222 | 69.8 | 70.0 | −0.15 | 0 | – |
| hold | A | 222 | 74.3 | 77.9 | −3.54 | −3.39 (−2.54) | 13 (+661) / 52 (−1,414) / – |
| R20 (P1) | B tape | 29 | 79.3 | 70.3 | +6.50 | −2.26 (−1.81) | 0 / 0 / 6 (−65) |
| G180h20 (P2) | B | 29 | 79.3 | 65.7 | +8.52 | −0.24 (−1.00) | 0 / 0 / 2 (−7) |
| bstop-10 (live) | B | 29 | 79.3 | 65.0 | +8.75 | 0 | – |
| hold | B | 29 | 82.8 | 78.8 | +4.00 | −4.76 (−1.38) | 1 (+53) / 5 (−191) / – |

### Exit study 3 verdict

**KILLED (grace period and re-check).** Both train picks lost to live `bstop-10` on the holdout in every view
(R20 −2.3c, t −1.8; G180h20 −0.1 / −0.2c); neither comes near the exit-level bar (t > 1.96 better). The holdout
had no whipsaws inside the grace windows to save, only delayed stops. Strategy level: nothing near Bonferroni 4.20;
the tape holdout (29 trades) is below the 30-trade floor and its +8.75c for live is small-sample luck.
The 10:51 trade is a real pattern (on second data ~46% of fired stops would have won), but no simple delay rule
separates those from real losses: they look the same in the first seconds/minutes. Keep `bstop-10`.
Caveat: view A approximates 60–180 s graces as 1–3 minute marks (and cannot see 30 s or R(d)); view B uses
print-proxied bids (a single print can move the proxy) with n = 76 / 29.

## Exit study 4: volatility-scaled / probability index stops (pre-registered 2026-10-29, before any of these was run)

Trigger (user): "we really need better exit criteria" — the fixed $10 threshold ignores time left: $10 past the
strike with 8 min left is noise, with 1 min left it is decisive. Question: does a stop whose threshold scales with the
BTC move still possible before the close beat `bstop-10`?

**Entry (fixed):** live PNW, `pnw_tune:cushion=50,start=9,end=7,lo=65,hi=80`. **Reference:** `bstop-10` (live), and
`hold`. Strike = Kalshi `floor_strike` (as `bstop-10`). BTC = `data/btcflow.db:index_1s`, read with the harness's
1 s lag (`Data.btc`: a decision at second s uses the latest index second with ts < s − 1 s). dist(s) = side · (BTC − K)
(negative = wrong side). seconds_left(s) = (close − s)/1000.

**Volatility:** σ_1s(s) = sqrt(mean of squared 1-second index $ changes over the trailing 15 minutes, using only index
seconds with ts < s − 1 s) — realized 1-second volatility, no look-ahead. (Checked before registering, as a data
property not a result: on this index the raw 1-second σ and the bounce-robust 1-minute-return σ have median ratio
0.99 per 15-min block, so bid-ask bounce does not inflate it here.) σ_rem(s) = σ_1s(s) · sqrt(seconds_left(s)).

**Family (14 rules):**
- `VS(z)`: sell when dist(s) ≤ −z · σ_rem(s); z ∈ {0.25, 0.5, 0.75, 1.0, 1.5}.
- `P(p)`: sell when the repo's random-walk probability that the window finishes on our side falls below p;
  p ∈ {0.35, 0.25, 0.15, 0.10}. Model = `btc_signal_engine/domain/coach/fair_value.fair_from_index` exactly (the
  Coach's odds: S = mean of the last 10 index seconds, σ from 1-minute log returns over the last 30 min, settlement
  on the 60-second closing average with already-observed closing seconds, called with at = s − 1 s), with the
  strike = Kalshi floor_strike instead of our open average (so it is the same strike the stop and entry use).
  Vectorised in the script and asserted equal to the domain function on a random sample of (window, second) pairs.
- `H(z)`: sell when dist(s) ≤ −max(z · σ_rem(s), $10), i.e. never tighter than live; z as VS.

**View S — PRIMARY (all windows, train n = 465, holdout 222):** every rule (and `bstop-10`, `hold`) is checked every
second s from the entry minute mark + 1 s to the close. Contract price: Kalshi's minute bid candles are the only bid
source for every window, so the sale = the held side's candle-close bid at the first minute mark ≥ trigger + 1 s
(first mark with a quote; none → held to settlement), − slippage, exact fee. Limits: the sale lags the trigger by
1–60 s (mean ~30 s), so it misses intra-minute bid dynamics in both directions (a fast whipsaw may have already
recovered by the mark, a crash may have gone further); the same rule applies to `bstop-10`, so paired comparisons
share the bias. Machinery check: restricting checks to minute marks with sale at that mark must reproduce
`experiments.sim_minute` for `hold` / `bstop-10` (0/1/2c), or the run is void.
**View T — secondary (435 full tapes; ~76 train / 29 holdout entries):** as Exit study 3 view B (tape-proxy entry
and sale = first opposite-taker print ≥ 1 s after the trigger); must reproduce `PrintSim.run` for hold / bstop-10.

All 14 rules + `bstop-10` + `hold` x 2 views x slip 0/1/2c appended (families `pnw_vs_sec`, `pnw_vs_tape`; config
suffixes `@sec`, `@tape`), all counted in the Bonferroni denominator. Script: `scripts/quick/pnw_volstop.py`.

**Train report (1c, both views):** n, win % vs break-even %, net c/contract, t, avg / worst loss, % stopped, % of
stops that would have won if held (whipsaw rate), paired d vs `bstop-10` (t), ledger vs `bstop-10`: saved (live
stopped, side won, rule never sold), extra full losses (live stopped, side lost, rule never sold), and stops the live
rule did not make (rule sold, live never did: n, c).

**Picks (train only, view S, frozen in `data/research/pnw_volstop_picks.json` before any holdout number; ≤ 2):**
P1 = highest train net c among the 14 rules (ties by t), only if paired train d vs `bstop-10` > 0; P2 = the best rule
of a different family (VS / P / H) with paired train d > 0. If none qualifies, no pick (keep `bstop-10`).

**Holdout (run once, both views):** picks + `bstop-10` + `hold`.
- *Switch bar:* paired holdout d pick − `bstop-10` in view S with one-sided p < 0.05/2 (t > 1.96), AND the same sign
  in view T. Otherwise keep `bstop-10`.
- *Strategy level (CANDIDATE):* n ≥ 30, t above the report's Bonferroni line, positive in both 15-day and both 5-day
  holdout halves (expected: nothing; the entry itself is ~break-even).

### Exit study 4 results (run 2026-10-29)

Checks passed: vectorised P(up) == `fair_value.fair_from_index` (strike = floor_strike) on 300 random (window, second)
samples (max |diff| 5.5e-7, cumsum rounding); view S restricted to minute marks == `experiments.sim_minute` and
view T == `PrintSim.run` for hold / bstop-10 (0/1/2c). 16 labels x 2 views x 3 slips appended (families `pnw_vs_sec`,
`pnw_vs_tape`); Bonferroni line **4.20** (3,702 configs). Tables: `data/research/pnw_volstop_train_S.csv`, `..._T.csv`.

Note: per-second `bstop-10` with the next-minute-mark bid scores −2.02c on train (minute-mark `bstop-10`: −0.59c): it
fires on 37% of trades and 40% of those would have won — the second-level whipsaw problem again.

**Train, view S (primary, n = 465, 1c).** Ledger vs `bstop-10`: saved = live stopped, side won, rule never sold;
extra = live stopped, side lost, rule rode to $0; later = both sold, rule later.

| rule | win % | BE % | net c | t | avg / worst loss c | stopped % | stops that would have won % | d vs live c (t) | saved n (c) | extra losses n (c) | later n (c) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **bstop-10 (live)** | 62.2 | 65.4 | −2.02 | −1.35 | −40.7 / −82 | 37.2 | 40.5 | 0 | – | – | – |
| VS0.25 | 65.4 | 67.3 | −1.27 | −0.82 | −44.8 / −82 | 34.8 | 36.4 | +0.74 (1.57) | 17 (+688) | 1 (−36) | 112 (−328) |
| VS0.5 | 69.5 | 72.6 | −2.51 | −1.44 | −57.8 / −82 | 28.2 | 29.0 | −0.50 (−0.59) | 35 (+1,605) | 10 (−393) | 117 (−1,440) |
| VS0.75 | 72.9 | 75.2 | −1.99 | −1.08 | −66.3 / −82 | 22.4 | 20.2 | +0.03 (0.03) | 50 (+2,427) | 20 (−695) | 101 (−1,716) |
| VS1.0 | 74.2 | 76.1 | −1.74 | −0.92 | −70.0 / −82 | 18.1 | 15.5 | +0.28 (0.25) | 58 (+2,828) | 32 (−1,015) | 83 (−1,684) |
| VS1.5 | 75.1 | 77.3 | −2.15 | −1.10 | −74.9 / −82 | 10.8 | 16.0 | −0.14 (−0.12) | 62 (+3,103) | 61 (−2,024) | 50 (−1,141) |
| P0.35 | 68.4 | 71.0 | −1.95 | −1.16 | −53.3 / −82 | 29.9 | 28.8 | +0.07 (0.10) | 30 (+1,280) | 5 (−163) | 138 (−1,113) |
| P0.25 | 71.8 | 74.4 | −2.20 | −1.21 | −63.7 / −82 | 23.9 | 19.8 | −0.19 (−0.19) | 48 (+2,197) | 14 (−464) | 111 (−1,819) |
| **P0.15** | 74.8 | 76.1 | **−1.18** | −0.63 | −70.1 / −82 | 18.1 | 8.3 | +0.84 (0.74) | 63 (+3,066) | 26 (−910) | 84 (−1,765) |
| P0.10 | 74.8 | 76.7 | −1.71 | −0.90 | −72.2 / −82 | 16.3 | 9.2 | +0.30 (0.26) | 63 (+3,066) | 34 (−1,134) | 76 (−1,790) |
| H0.25…H1.5 | = VS(z) within 0.05c (the σ-scaled level is almost always wider than $10 while a stop matters) | | | | | | | | | | |
| hold | 75.9 | 77.6 | −1.65 | −0.85 | −76.2 / −82 | 0 | – | +0.36 (0.29) | 70 (+3,521) | 103 (−3,352) | – |

View T (tapes, n = 76, train): `bstop-10` −0.31c; VS/H0.75–1.0 +4.3…+4.6c (d vs live +4.6…+4.9c, t 1.43), P0.15
+2.97c (t vs live 0.98), VS0.25 −0.86c, hold +2.74c. Every rule within noise of live in both views (best |t| 1.57).
The trade-off is the same in every rule: looser σ-scaled stops save whipsaws but give back most of it on later sales
at worse bids and on losers ridden to $0.

**Frozen picks (train, view S):** P1 = `P0.15` (best net), P2 = `VS0.25` (best of another family with d > 0).

**Holdout (run once, 1c).**

| rule | view | n | win % | BE % | net c | vs live c (t) | vs hold c (t) | saved / extra / later (c) | 5-day halves | 15-day halves |
|---|---|---|---|---|---|---|---|---|---|---|
| P0.15 (P1) | S | 222 | 73.9 | 76.6 | −2.59 | **−1.68 (−1.05)** | +0.95 (1.66) | 23 (+1,042) / 14 (−505) / 45 (−910) | −2.42 / −2.80 | +0.11 / −3.37 |
| VS0.25 (P2) | S | 222 | 67.6 | 68.4 | −0.58 | +0.33 (0.45) | +2.96 (1.97) | 6 (+261) / 1 (−65) / 50 (−122) | +0.06 / −1.37 | −0.77 / −1.33 |
| bstop-10 (live) | S | 222 | 64.0 | 65.4 | −0.91 | 0 | +2.63 (1.57) | – | +0.32 / −2.44 | −1.75 / −1.56 |
| hold | S | 222 | 74.3 | 77.9 | −3.54 | −2.63 (−1.57) | 0 | – | −3.34 / −3.78 | −0.19 / −4.33 |
| P0.15 | T | 29 | 82.8 | 77.2 | +5.13 | −3.62 (−1.30) | +1.13 (1.44) | 0 / 0 / 6 (−105) | | |
| VS0.25 | T | 29 | 79.3 | 66.9 | +8.04 | −0.72 (−1.69) | +4.04 (1.22) | 0 / 0 / 4 (−21) | | |
| bstop-10 (live) | T | 29 | 79.3 | 65.0 | +8.75 | 0 | +4.76 (1.38) | – | | |

### Exit study 4 verdict

**KILLED (volatility-scaled and probability stops).** Neither pick clears the switch bar (paired t > 1.96 vs live in
view S with the same sign on the tapes): `P0.15` lost to live on the holdout in both views (−1.7c, t −1.05; tapes
−3.6c); `VS0.25` was +0.33c (t 0.45) in view S but −0.72c on the tapes (wrong sign) — and VS0.25 is nearly the live
rule anyway (its σ-scaled level is ≈ $10 for most of the trade's life). Nothing near Bonferroni 4.20; no strategy-
level candidate (the entry itself is ~break-even). The intuition is right that a $10 breach at 8 min left is often
noise (40% of per-second stops would have won), but scaling the threshold by σ√t mostly converts those whipsaws into
bigger losses on the trades that really were lost; the market's bid already prices time-left, so the sale price
falls with the same information the rule waits for. Keep `bstop-10`.
Limits: view S sells at the next minute-mark candle bid (1–60 s after the trigger); view T has exact-ish bids but only
76 / 29 trades.

## Band study 5: entry price band under a fixed 85c take profit (pre-registered 2026-10-29, before any of these was run)

User question: "is 80c the right entry? imagine if we entered at 60" — the user keeps a fixed 85c TP, so an 80c entry
has ~5c of upside room and a 60c entry ~25c. Live rule now: PNW entry (9–7 min left, BTC >= $50 past floor_strike),
ask in [65, 80], buy at ask + 1c; exit `ftp85_bstop-10` (resting maker sell at 85c, fee 0; else taker sell at the
minute bid − 1c once BTC is $10 past the strike on the wrong side; else settle).

**Entries:** `pnw_tune:cushion=50,start=9,end=7,lo=LO,hi=HI` for bands [55,65], [60,70], [65,75], [70,80], [60,75],
[60,80], live [65,80], plus the union [55,80] (used only for the per-price breakdown). Note the band filter applies at
each of the 3 decision minutes (first qualifying minute), so bands are not strict subsets of each other's trades.
**Exits:** `ftp85_bstop-10` (primary, user's choice) and `bstop-10` (RIDE: no TP, reference).
Simulator: `pnw_exits_fixedtp.sim` (validated vs `experiments.sim_minute`; the check is re-run here, else void).
Same-minute TP and stop: stop first. TP fill = held-side candle-close bid >= 85c at a minute mark after entry.
**Counting:** 8 bands x 2 exits x slips 0/1/2c appended to `experiments_results.csv` (family `pnw_band5`), all in the
Bonferroni denominator (the live [65,80] configs re-appended: conservative). Script: `scripts/quick/pnw_band_tp85.py`.

**Train report (first 20 days, 1c):** per band x exit: n, trades/day, win % vs break-even %, net c/contract, t, TP %,
stopped %, avg win / avg loss; paired difference vs live [65,80]+TP85 on the union of windows where either config
trades (a window where a config does not trade counts 0): d = c per window (a total-P&L comparison, since bands
differ in trade count), t clustered by window. **Per-price breakdown (train, union [55,80] entry, by entry ask in 2c
buckets 55–56 … 79–80):** n, win %, break-even %, net c/contract, TP %, for TP85 and RIDE — descriptive only.

**Picks (train only, TP85 exit, frozen in `data/research/pnw_band5_picks.json` before any holdout number; <= 2):**
P1 = highest train net c/contract among the 6 non-live bands with train n >= 60 (3/day), ties by t; P2 = highest
train paired d vs live (c per window) among the remaining non-live bands, only if d > 0. A pick needs train net c
above live's train net c (else no pick).

**Holdout (run once, 1c):** picks + live [65,80], each with TP85 and RIDE.
- *Band switch bar:* holdout paired d (pick+TP85 − live+TP85, per window) with one-sided p < 0.05/2 (t > 1.96,
  Bonferroni over 2 picks), same sign as train, AND pick's holdout net c/contract > live's. Otherwise "no evidence
  the band should change".
- *Strategy level (CANDIDATE):* n >= 30, t above the report's Bonferroni line, positive in both 15-day halves and both
  5-day holdout halves (expected: nothing; Exit study 2 had live+TP85 at −5.45c on the holdout).

### Band study 5 results (run 2026-10-29)

Check passed (sim == `experiments.sim_minute`, 0/1/2c). 8 bands x 2 exits x 3 slips appended (family `pnw_band5`);
Bonferroni line **4.20** (3,716 configs). Tables: `data/research/pnw_band5_train.csv`,
`data/research/pnw_band5_perprice_train.csv`.

**Low entries barely exist.** With BTC >= $50 past the strike at 9–7 min left, the leading side's ask is almost
always >= 70c: on train only 13 of 474 union entries were below 65c (0.65/day), 50 below 70c. "Enter at 60" is not a
setting that would trade; [55,80] picks the same trades as [60,80].

**Per-price, train (union [55,80] entry, 1c):**

| ask c | n | TP85: win % / BE % / net c | RIDE: win % / BE % / net c |
|---|---|---|---|
| 59–64 | 13 | 54 / 57 / −2.5 (pooled) | 54 / 42 / +5.0 (pooled) |
| 65–66 | 11 | 36 / 70 / −18.7 | 36 / 55 / −13.3 |
| 67–68 | 23 | 61 / 70 / −4.7 | 52 / 57 / −3.1 |
| 69–70 | 27 | 44 / 77 / −19.0 | 44 / 61 / −12.3 |
| 71–72 | 51 | 75 / 83 / −5.1 | 69 / 68 / +0.3 |
| 73–74 | 52 | 77 / 83 / −3.4 | 69 / 67 / +2.0 |
| 75–76 | 82 | 74 / 87 / −6.7 | 68 / 69 / −0.6 |
| 77–78 | 98 | 83 / 90 / −4.0 | 73 / 72 / +0.9 |
| 79–80 | 117 | 84 / 94 / −5.2 | 76 / 75 / +0.8 |

TP85 costs ~2–7c/contract vs riding in every bucket (bigger at 71–80c, where 85c caps the win at 4–13c but the
stop loss is still ~−48c). Lower entries do NOT rescue TP85: the 65–70c buckets are the worst of all (they are
windows where the market doubts the $50 cushion; 36–61% wins), and below 65c there are too few trades to say anything.

**Train, by band (1c).** d = paired vs live [65,80]+TP85, c per window over the union of windows (0 = no trade).

| band | exit | n | /day | win % | BE % | net c | t | TP % | d live c/window (t) |
|---|---|---|---|---|---|---|---|---|---|
| 55–65 | TP85 | 23 | 1.2 | 60.9 | 61.9 | −0.53 | −0.10 | 61 | +5.94 (5.49) |
| 60–70 | TP85 | 94 | 4.7 | 54.3 | 71.2 | −9.17 | −3.13 | 54 | +4.15 (4.33) |
| 65–75 | TP85 | 232 | 11.6 | 67.7 | 80.9 | −7.29 | −4.11 | 68 | +2.45 (3.56) |
| 70–80 | TP85 | 437 | 21.9 | 78.0 | 88.2 | −5.54 | −4.95 | 78 | +0.87 (2.31) |
| 60–75 | TP85 | 243 | 12.2 | 67.1 | 79.6 | −6.85 | −3.96 | 67 | +2.46 (3.55) |
| 60–80 | TP85 | 474 | 23.7 | 74.9 | 86.3 | −5.99 | −5.46 | 75 | −0.02 (−0.15) |
| **65–80 (live)** | TP85 | 465 | 23.3 | 75.5 | 86.9 | **−6.08** | −5.50 | 75 | 0 |
| 55–65 | RIDE | 23 | 1.2 | 56.5 | 50.4 | +4.25 | 0.56 | 0 | +6.17 |
| 60–70 | RIDE | 94 | 4.7 | 51.1 | 56.7 | −3.94 | −1.04 | 0 | +5.19 |
| 65–75 | RIDE | 232 | 11.6 | 62.5 | 64.9 | −1.72 | −0.72 | 0 | +5.22 |
| 70–80 | RIDE | 437 | 21.9 | 70.7 | 70.8 | −0.04 | −0.02 | 0 | +6.05 |
| 60–75 | RIDE | 243 | 12.2 | 62.1 | 63.8 | −1.17 | −0.50 | 0 | +5.37 |
| 60–80 | RIDE | 474 | 23.7 | 68.1 | 68.8 | −0.47 | −0.30 | 0 | +5.50 |
| 65–80 (live) | RIDE | 465 | 23.3 | 68.6 | 69.4 | −0.59 | −0.37 | 0 | +5.50 (5.42) |

Every TP85 band loses on train; the per-window d > 0 for narrow bands only says "trading less of a losing rule loses
less" (55–65 trades 23 times in 20 days). Every RIDE band beats its TP85 twin by 4–5c/contract.

**Frozen picks (train):** P1 = 70–80 (best net c with n >= 60), P2 = 55–65 (best per-window d).

**Holdout (run once, 1c):**

| band | exit | n | win % | BE % | net c | t | 5-day halves | 15-day halves | vs live+TP85 c/window (t) |
|---|---|---|---|---|---|---|---|---|---|
| 70–80 (P1) | TP85 | 213 | 77.5 | 88.1 | −5.70 | −3.56 | −6.14 / −5.16 | −6.54 / −4.64 | −0.03 (−0.07) |
| 55–65 (P2) | TP85 | 9 | 55.6 | 69.0 | −8.87 | −0.74 | −4.61 / −11.00 | −6.96 / +0.29 | +5.02 (3.06) |
| **65–80 (live)** | TP85 | 222 | 77.0 | 87.2 | **−5.45** | −3.47 | −5.98 / −4.78 | −6.54 / −5.22 | 0 |
| 70–80 | RIDE | 213 | 70.0 | 70.8 | −0.60 | −0.25 | +0.18 / −1.58 | −0.07 / −0.37 | +4.87 (3.13) |
| 55–65 | RIDE | 9 | 55.6 | 56.2 | −0.53 | −0.04 | | | +5.35 (3.21) |
| 65–80 (live) | RIDE | 222 | 69.8 | 70.0 | −0.15 | −0.06 | +0.45 / −0.89 | −0.04 / −0.84 | +5.30 (3.49) |

### Band study 5 verdict

**No band change justified (KILLED: "a lower entry band fixes the 85c TP").** P1 70–80 = live within 0.03c/window.
P2 55–65 clears the per-window paired t (3.06 > 1.96) but only because it trades 9 times in 10 days instead of 222
(not trading a −5c rule "saves" 5c/window); per contract it was worse (−8.87c vs −5.45c, n = 9), so it fails the
pre-registered bar. Nothing near Bonferroni 4.20; no strategy-level candidate. The real lever is the exit, not the
band: in every band and every price bucket, riding (`bstop-10`, no TP) beats TP85 by ~4–5c/contract (holdout live
+5.30c, paired t 3.49, replicating Exit study 2). If the user keeps TP85, [65,80] is as good as any band tested
(70–80 identical; low bands almost never fire with a $50 cushion and were not better per trade).
