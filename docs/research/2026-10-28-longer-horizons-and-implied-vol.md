# Longer horizons and implied volatility: two last hypotheses for a model edge (2026-10-28)

**Context.** On KXBTC15M our driftless random-walk fair value adds no information beyond Kalshi's price (`2026-10-28-model-vs-market.md`), our 1-minute/30-minute sigma runs 11-25% above the market-implied sigma in the last 6 minutes, and popular human rules lose about the fee (`2026-10-28-human-strategies.md`). Two hypotheses remain:

- **H-A.** Longer-duration Kalshi BTC markets (hourly, daily) are less efficient than the 15-minute ones, so the same model has a better chance there.
- **H-B.** Options-implied volatility (Deribit) is a better sigma than our backward-looking one, and with it the model adds information beyond the market price.

Script: `scripts/horizons_iv.py` (fetch-kalshi, fetch-deribit, analyze). Read-only on the app: `data/btcflow.db` and `data/research/kalshi_trades.db` are opened with `mode=ro`; new data go to `data/research/horizons.db`; nothing is ordered.

## Pre-registered criteria

Written before any model or market number for these tests was computed (only API structure, ticker naming and data volume had been looked at). Not edited afterwards; any later change goes in a dated amendment.

### Common to both

- **Days and holdout.** Same 30 UTC days as the 15-minute study, 2026-09-29 to 2026-10-28. Train = first 20 days (2026-09-29 to 2026-10-18), holdout = last 10 (2026-10-19 to 2026-10-28). A market's day is the UTC date of its close (15-minute panel: of its open, as in the original study). Bootstrap clusters = days, 2,000 replicates, percentile intervals.
- **"ADDS INFORMATION"** (identical to the 15-minute study): at a given checkpoint, the logistic combo outcome ~ a + b1·logit(market mid) + b2·logit(model) fit on train days has lower holdout log loss than the nested market-only logistic outcome ~ a + b·logit(mid) fit on the same days, with the joint day-bootstrap 95% CI of the difference excluding 0, AND the 95% day-bootstrap CI of b2 excluding 0 on the positive side. Each checkpoint is judged on its own; a pass whose neighbouring checkpoints both fail is flagged as possibly chance (8 checkpoints per horizon are tested).
- Probabilities are clipped to [0.001, 0.999] before log loss and logits. Brier is reported alongside.

### H-A: hourly and daily markets

- **Series.** Primary: KXBTCD ("Bitcoin price above/below", one strike per market, YES iff the 60-second BRTI average before the close is above `floor_strike`). Secondary (reported, not part of the verdict): KXBTC ("Bitcoin range", YES iff the average is in [`floor_strike`, `cap_strike`]; open-ended edges use the one strike).
- **Horizons, by market duration (close - open).** Hourly = exactly 1 hour. Daily = 20 to 30 hours (the 5 pm EDT events, e.g. `KXBTCD-26OCT2817B`). Other durations (e.g. the 2-day `...17` events) are excluded.
- **Markets.** Settled, result `yes`/`no`, closing in the 30 days. Only strikes near the money are fetched, chosen from information at the open: hourly, `floor_strike` or `cap_strike` within ±3% of our index price at the event's open; daily, within ±8%. (Strikes further out are certain at every checkpoint.)
- **Checkpoints (tau = time before close).** Hourly: 50, 40, 30, 20, 15, 10, 5, 2 minutes (1-minute candles). Daily: 20, 16, 12, 8, 6, 4, 2, 1 hours (1-hour candles).
- **Market mid at tau.** Kalshi candlestick `yes_bid.close` and `yes_ask.close` from `/markets/candlesticks`: 1-minute candles use the latest candle with end_period_ts in (tau - 5 min, tau]; 1-hour candles must end exactly at tau. Usable iff 0 < bid <= ask < 1; mid = (bid + ask)/2.
- **Rows scored.** "In play" = market mid in [0.05, 0.95] at tau (market information only). The verdict uses in-play rows. All two-sided rows are reported as a secondary table.
- **Model at tau.** Exactly the Coach's math (`fair_from_index`): at = tau - 1 s, index rows strictly before `at`, S = mean of the last 10 index seconds, sigma = per-second dollar sigma from 1-minute log returns over the last 30 minutes (floored). P(YES) = Phi((60·S - 60·K) / (sigma·sqrt(V))), V = 60²·g + 59·60·119/6, g = (close - 60 s - at)/1000 + 1 (the 60-second settlement average, driftless random walk). K = Kalshi's strike; no index-basis correction (our index runs about $2 below BRTI, against hourly sigmas of hundreds of dollars). Range markets: P = P(avg > floor) - P(avg > cap).
- **H-A verdict.** SUPPORTED if, for KXBTCD hourly or daily, ADDS INFORMATION passes at two or more adjacent checkpoints. WEAK / POSSIBLY CHANCE if only isolated passes. NOT SUPPORTED if no checkpoint passes. Also reported: raw holdout log loss and Brier of model vs market mid per checkpoint, and fixed blends.

### H-B: implied volatility

- **Source.** Deribit public API. DVOL (BTC 30-day implied volatility index, annualised %), 1-minute candles from `public/get_volatility_index_data`. Short-dated option IVs from historical option trades (`history.deribit.com`, `public/get_last_trades_by_currency_and_time`, field `iv`).
- **Conversion.** Per-second dollar sigma = S · IV/100 / sqrt(365 · 86,400). DVOL value used at `at` = the close of the latest DVOL minute candle whose start is <= at - 120 s (no look-ahead).
- **Variants.**
  - **V1 raw DVOL.**
  - **V2 scaled DVOL (primary):** k · V1 sigma, with one scalar k fitted on train days only, by minimising the model's pooled log loss over all checkpoints of the panel being tested (15-minute panel; separately for each H-A horizon).
  - **V3 blend:** sigma = sqrt((sigma_ours² + sigma_V2²)/2).
  - **V4 short-dated option IV (raw):** median `iv` of Deribit BTC option trades in [at - 60 min, at) on the nearest expiry with at least 2 hours left, strike within ±2% of the trade's `index_price`; missing if fewer than 3 such trades (rows without V4 are dropped from V4's comparison only).
- **Fair value with an IV sigma.** Same formula as above with sigma replaced, i.e. P_IV = Phi(z_ours · sigma_ours / sigma_IV).
- **15-minute panel.** The same KXBTC15M windows, checkpoints (12, 10, 8, 6, 4, 3, 2, 1 minutes) and market-mid definition as the 15-minute study (print proxies in [tau - 30 s, tau) from `panel_trades` or the full tape; a window with no print in that interval is dropped at that tau). Robustness: Kalshi 1-minute candle mid at tau on all windows.
- **"IV BEATS OUR SIGMA"** at a checkpoint iff the holdout log loss of the V2 fair minus that of our fair is < 0 with a holdout-day bootstrap 95% CI excluding 0 (k held at its train value).
- **"IV MODEL ADDS INFORMATION"**: the ADDS INFORMATION criterion with logit(V2 fair) in place of logit(model).
- **H-B verdict.** IV IMPROVES OUR MODEL if IV BEATS OUR SIGMA passes at two or more adjacent 15-minute checkpoints; IV MODEL ADDS INFORMATION if that criterion passes at two or more adjacent checkpoints. V1, V3, V4 are reported as secondary. H-A is redone with V1/V2 when the hourly and daily data allow; that redo is secondary.

## Results (2026-10-29 ~01:30 UTC; nothing above this heading was edited after the numbers were seen)

Full tables (every variant, blends, all-two-sided rows, Brier): `data/research/horizons_results.md` and `.json`. Row-level panels: `data/research/horizons_panel.csv`, `data/research/horizons_panel_15m.csv`. Code checks: the closed-form model reproduces `fair_from_index` to 7e-14 on 200 sampled hourly/daily rows, and the vectorised S/sigma reproduces it to 3e-11 on the 15-minute panel.

### Data coverage and access

- **Kalshi.** Current tickers confirmed via `/series`: KXBTCD (hourly above/below; the 5 pm EDT event is the "daily", opening 23-25 hours before), KXBTC (hourly ranges, $100 wide), KXBTC15M. No hourly up/down series exists. Settled markets came from `/markets` (no `/historical/*` call was needed: every market in the period is still served by the live endpoints), candles from `/markets/candlesticks` (1-minute for hourly events, 1-hour for daily). 1,666 requests at 1.5/s, 4 HTTP 429s, alongside the 15-minute tape job.
- **Events.** 674 hourly KXBTCD events in the 30 days (23 non-5-pm hours a day, a few missing), 26 daily. The 4 Friday 5 pm events are weekly (169-hour) markets and were excluded under the duration rule, as was the 48-hour `KXBTCD-26OCT2817`. So the daily test has 26 events, 9 of them in the holdout. That is far too few for this test to have power.
- **Deribit.** Not geo-blocked. DVOL 1-minute: 44,720 minutes covering the whole period. Historical option trades (with `iv`) from `history.deribit.com`: 185,791 trades. The live host's time-ranged trade endpoint returns nothing for past dates, so use the history host. Short-dated IV (V4) exists for about 98% of checkpoints.
- **15-minute panel.** 2,825 windows with our fair value. A print-proxy mid is available for 2,263 windows (1,858 direct panels plus 405 full tapes; the fetch job was still running), so n = 735 holdout rows per tau. The candle mid covers all windows.

| series | horizon | events | markets | two-sided rows | in-play rows | model ok | DVOL ok | short IV ok | holdout events |
|---|---|---|---|---|---|---|---|---|---|
| KXBTC | daily | 26 | 1406 | 2072 | 1423 | 11248 | 11248 | 11030 | 9 |
| KXBTC | hourly | 674 | 34153 | 35275 | 23399 | 273224 | 273122 | 267372 | 226 |
| KXBTCD | daily | 26 | 1410 | 3803 | 2173 | 11280 | 11280 | 11066 | 9 |
| KXBTCD | hourly | 674 | 33479 | 40141 | 22693 | 267832 | 267732 | 262095 | 226 |

### H-A: hourly and daily markets

**KXBTCD hourly, in-play rows (mid in [0.05, 0.95]), holdout.** The market's log loss is lower than the model's at every checkpoint. The best fixed blend (0.75 on the market) only ties the market.

| tau | n train | n hold | hold days | LL market | LL model | LL blend 0.75 | Brier market | Brier model |
|---|---|---|---|---|---|---|---|---|
| 50 | 3307 | 1631 | 10 | 0.4528 | 0.4644 | 0.4538 | 0.1455 | 0.1484 |
| 40 | 2980 | 1496 | 10 | 0.4482 | 0.4701 | 0.4515 | 0.1430 | 0.1486 |
| 30 | 2565 | 1282 | 10 | 0.4304 | 0.4512 | 0.4332 | 0.1365 | 0.1415 |
| 20 | 2023 | 1011 | 10 | 0.4484 | 0.4602 | 0.4488 | 0.1450 | 0.1472 |
| 15 | 1689 | 853 | 10 | 0.4650 | 0.4752 | 0.4652 | 0.1498 | 0.1531 |
| 10 | 1322 | 647 | 10 | 0.4660 | 0.4767 | 0.4662 | 0.1510 | 0.1538 |
| 5 | 831 | 406 | 10 | 0.5120 | 0.5263 | 0.5112 | 0.1681 | 0.1729 |
| 2 | 431 | 219 | 10 | 0.4574 | 0.4773 | 0.4576 | 0.1474 | 0.1529 |

Logistic combo vs market-only (the pre-registered test):

| tau | prob | n hold | b1 (mid) | b2 (model) | b2 95% CI | dLL x1000 | 95% CI | criterion |
|---|---|---|---|---|---|---|---|---|
| 50 | model | 1631 | 1.06 | -0.0156 | [-0.382, 0.456] | 0.0103 | [-0.71, 3.48] | fail |
| 40 | model | 1496 | 1.11 | -0.0130 | [-0.284, 0.347] | -0.1145 | [-2.39, 5.13] | fail |
| 30 | model | 1282 | 1.17 | -0.1008 | [-0.399, 0.449] | -0.2814 | [-2.80, 5.73] | fail |
| 20 | model | 1011 | 0.7271 | 0.4392 | [0.006, 0.889] | 0.1940 | [-4.44, 6.39] | fail |
| 15 | model | 853 | 0.8096 | 0.3714 | [0.030, 0.736] | 0.1491 | [-4.98, 5.90] | fail |
| 10 | model | 647 | 1.12 | -0.0703 | [-0.514, 0.481] | 0.2993 | [-3.43, 7.10] | fail |
| 5 | model | 406 | 0.9695 | 0.2043 | [-0.292, 0.776] | 0.4806 | [-2.82, 10.94] | fail |
| 2 | model | 219 | 1.20 | -0.2127 | [-0.568, 0.157] | 3.84 | [-1.88, 17.09] | fail |

At 20 and 15 minutes the model coefficient's CI excludes 0 (b2 = 0.44 and 0.37), but the holdout log loss gets worse (+0.19 and +0.15 x1000, CIs straddling 0). The weight fitted on training days does not carry to the holdout, so both checkpoints fail the criterion. The all-two-sided rows tell the same story: no pass, dLL from -0.58 to +0.45 x1000.

**KXBTCD daily (in-play; 9 holdout days, 37-178 rows per checkpoint).**

| tau | n train | n hold | hold days | LL market | LL model | LL blend 0.75 | Brier market | Brier model |
|---|---|---|---|---|---|---|---|---|
| 20 | 257 | 178 | 9 | 0.4265 | 0.4567 | 0.4294 | 0.1329 | 0.1386 |
| 16 | 249 | 158 | 9 | 0.4460 | 0.4897 | 0.4443 | 0.1425 | 0.1465 |
| 12 | 158 | 141 | 9 | 0.3967 | 0.3851 | 0.3911 | 0.1251 | 0.1209 |
| 8 | 194 | 143 | 9 | 0.3693 | 0.3420 | 0.3614 | 0.1155 | 0.1125 |
| 6 | 152 | 118 | 9 | 0.3565 | 0.4021 | 0.3660 | 0.1105 | 0.1276 |
| 4 | 121 | 86 | 9 | 0.3574 | 0.4004 | 0.3675 | 0.1100 | 0.1235 |
| 2 | 77 | 54 | 9 | 0.3312 | 0.3528 | 0.3352 | 0.0995 | 0.1076 |
| 1 | 50 | 37 | 9 | 0.3596 | 0.3357 | 0.3526 | 0.1134 | 0.1116 |

| tau | prob | n hold | b1 (mid) | b2 (model) | b2 95% CI | dLL x1000 | 95% CI | criterion |
|---|---|---|---|---|---|---|---|---|
| 20 | model | 178 | 1.11 | -0.1195 | [-2.051, 1.171] | 0.5935 | [-44.25, 296.55] | fail |
| 16 | model | 158 | 2.32 | -0.6125 | [-1.464, 1.443] | -1.74 | [-50.38, 128.69] | fail |
| 12 | model | 141 | 0.7775 | 0.4541 | [-3.535, 4.774] | -7.40 | [-53.90, 533.60] | fail |
| 8 | model | 143 | 0.7246 | 0.3845 | [-1.181, 1.905] | -14.42 | [-45.57, 112.94] | fail |
| 6 | model | 118 | 2.35 | -0.9520 | [-2.426, 0.500] | 12.01 | [-18.23, 204.99] | fail |
| 4 | model | 86 | 0.2020 | 1.14 | [-0.145, 4.451] | 24.99 | [-2.44, 216.20] | fail |
| 2 | model | 54 | 0.6361 | 0.4243 | [-0.545, 3.794] | 7.45 | [-19.98, 748.22] | fail |
| 1 | model | 37 | 0.7561 | 0.6024 | [-1.723, 13.515] | -0.5742 | [-46.19, 361.34] | fail |

The raw model beats the market mid at 12 h, 8 h and 1 h on the holdout and loses at the other 5 checkpoints. The combo test fails everywhere, and its CIs span tens to hundreds of thousandths of a nat. With 9 independent days, a daily edge cannot be found or ruled out.

**KXBTC ranges (secondary).** Hourly: the model's log loss is within 0.001-0.02 of the market's and always worse. The combo fails at all 8 checkpoints (dLL from -0.26 to +2.38 x1000). Daily: also no pass; the samples are tiny.

**Sigma vs the market (KXBTCD).** Our 30-minute sigma is 7-11% above the hourly market's implied sigma at 50-30 minutes and 15-35% above it at 20-2 minutes. That is the same over-dispersion seen on KXBTC15M. Raw DVOL is 35-56% above the market's implied sigma.

Hourly:

| tau | n | ours / market | DVOL / market |
|---|---|---|---|
| 50 | 4261 | 1.07 | 1.37 |
| 40 | 3873 | 1.07 | 1.35 |
| 30 | 3324 | 1.11 | 1.37 |
| 20 | 2602 | 1.15 | 1.41 |
| 15 | 2185 | 1.17 | 1.43 |
| 10 | 1707 | 1.21 | 1.46 |
| 5 | 1051 | 1.28 | 1.53 |
| 2 | 550 | 1.35 | 1.56 |

Daily (noisy, small n):

| tau | n | ours / market | DVOL / market |
|---|---|---|---|
| 20 | 379 | 0.8899 | 1.16 |
| 16 | 351 | 0.6790 | 1.10 |
| 12 | 258 | 0.9440 | 1.10 |
| 8 | 293 | 0.8212 | 0.9663 |
| 6 | 237 | 1.31 | 1.07 |
| 4 | 179 | 1.17 | 1.21 |
| 2 | 120 | 0.9383 | 1.34 |
| 1 | 73 | 0.8941 | 1.45 |

### H-B: implied volatility

**Levels.** Over these 30 days, raw DVOL is about 1.49x our realized 1-minute sigma at every checkpoint, and about 1.45-1.5x the sigma implied by the 15-minute market at 12 to 3 minutes. The sigma from short-dated option trades is much closer to the market's (1.07-1.13x at 12 to 3 minutes). In other words, the 15-minute market prices short-horizon vol close to our realized estimate, not to option IV. The train-fitted scale for DVOL (V2) is k = 0.753 (0.90 hourly, 0.72 daily).

| tau | n | ours / market | DVOL / market | short IV / market | DVOL / ours |
|---|---|---|---|---|---|
| 12 | 1442 | 1.03 | 1.45 | 1.07 | 1.49 |
| 10 | 1660 | 1.03 | 1.47 | 1.09 | 1.49 |
| 8 | 1802 | 1.04 | 1.49 | 1.11 | 1.49 |
| 6 | 1913 | 1.07 | 1.53 | 1.13 | 1.50 |
| 4 | 2030 | 1.02 | 1.50 | 1.10 | 1.49 |
| 3 | 2032 | 1.03 | 1.48 | 1.09 | 1.49 |
| 2 | 2072 | 0.9486 | 1.39 | 1.02 | 1.50 |
| 1 | 2127 | 0.6845 | 1.03 | 0.7608 | 1.49 |

**Does IV beat our sigma? (15-minute panel, holdout, IV fair minus our fair, x1000; positive = IV worse.)**

| tau | n hold | V1 | 95% CI | V2 | 95% CI | V2 criterion | V3 | 95% CI | V4 | 95% CI |
|---|---|---|---|---|---|---|---|---|---|---|
| 12 | 735 | 10.37 | [-0.70, 21.79] | 16.80 | [7.42, 25.17] | fail | 1.25 | [-3.51, 5.21] | 10.42 | [0.80, 19.39] |
| 10 | 735 | 12.23 | [-0.72, 24.76] | 18.95 | [4.94, 32.27] | fail | -0.7360 | [-7.86, 5.42] | 15.14 | [3.37, 26.93] |
| 8 | 735 | 22.45 | [2.12, 45.16] | 21.10 | [0.50, 40.42] | fail | 0.5634 | [-10.61, 10.41] | 16.37 | [-1.19, 35.22] |
| 6 | 735 | 21.72 | [-0.37, 48.23] | 15.50 | [-6.69, 38.64] | fail | -0.9944 | [-14.43, 12.24] | 12.04 | [-8.63, 33.27] |
| 4 | 735 | 11.68 | [-7.16, 28.26] | 0.4814 | [-20.75, 22.09] | fail | -8.52 | [-21.04, 3.87] | -0.6129 | [-17.61, 15.19] |
| 3 | 735 | 21.04 | [4.41, 35.85] | 9.00 | [-10.77, 27.74] | fail | -3.40 | [-14.95, 6.35] | 6.85 | [-12.08, 22.77] |
| 2 | 735 | 15.60 | [-1.81, 28.89] | 4.87 | [-16.55, 22.05] | fail | -4.41 | [-20.20, 5.79] | 2.11 | [-17.84, 18.31] |
| 1 | 735 | 0.6891 | [-26.09, 17.90] | -13.41 | [-36.51, 1.10] | fail | -12.28 | [-31.35, -1.12] | -14.56 | [-39.35, 0.08] |

No. Scaled DVOL (V2, primary) is significantly *worse* than our sigma at 12, 10 and 8 minutes (+17 to +21 x1000) and not distinguishable at 6 to 1 minutes. At 1 minute the point estimate favours IV (-13.4 [-36.5, +1.1]), but it misses the criterion and stands alone. Raw DVOL (V1) and short-dated option IV (V4) are also worse or equal. The 50/50 variance blend (V3) is the only variant near zero at most checkpoints; it is better at 1 minute (-12.3 [-31.4, -1.1]), an isolated secondary result. On hourly KXBTCD, V2 is worse than ours at 50, 15, 10 and 2 minutes (significant) and never better. On daily KXBTCD, V2 beats ours at 6 h (-60 [-124, -17]) and nowhere else, with 9 days. That pass is isolated and flagged as possibly chance.

**Does an IV-based fair add information beyond the market? (15-minute panel, print mid, holdout.)**

| tau | prob | n hold | b1 (mid) | b2 (model) | b2 95% CI | dLL x1000 | 95% CI | criterion |
|---|---|---|---|---|---|---|---|---|
| 12 | model | 735 | 1.53 | -0.4109 | [-0.988, 0.156] | -0.1958 | [-4.34, 6.24] | fail |
| 12 | V2 | 735 | 1.09 | 0.0111 | [-0.172, 0.216] | 0.1280 | [-1.93, 3.89] | fail |
| 10 | model | 735 | 1.05 | 0.0538 | [-0.188, 0.336] | -0.3927 | [-3.11, 2.46] | fail |
| 10 | V2 | 735 | 0.9830 | 0.1110 | [-0.066, 0.342] | 0.4423 | [-1.99, 5.55] | fail |
| 8 | model | 735 | 0.9999 | 0.0530 | [-0.186, 0.343] | -0.4612 | [-3.02, 2.34] | fail |
| 8 | V2 | 735 | 1.02 | 0.0301 | [-0.138, 0.268] | 0.0441 | [-1.56, 3.79] | fail |
| 6 | model | 735 | 0.8904 | 0.0883 | [-0.126, 0.384] | 0.3030 | [-0.92, 4.20] | fail |
| 6 | V2 | 735 | 0.7830 | 0.2081 | [0.028, 0.446] | 2.72 | [-0.54, 10.50] | fail |
| 4 | model | 735 | 1.17 | -0.1651 | [-0.374, 0.085] | -1.25 | [-3.99, 2.30] | fail |
| 4 | V2 | 735 | 0.9898 | 0.0058 | [-0.137, 0.174] | 0.0432 | [-1.28, 2.53] | fail |
| 3 | model | 735 | 1.03 | -0.0436 | [-0.206, 0.150] | -0.2318 | [-2.04, 2.31] | fail |
| 3 | V2 | 735 | 0.9989 | -0.0113 | [-0.171, 0.164] | -0.1448 | [-2.77, 3.33] | fail |
| 2 | model | 735 | 1.05 | -0.1302 | [-0.268, 0.019] | -1.84 | [-5.38, 1.27] | fail |
| 2 | V2 | 735 | 0.9183 | 0.0003 | [-0.158, 0.187] | 0.0064 | [-3.90, 5.68] | fail |
| 1 | model | 735 | 1.21 | -0.1659 | [-0.417, 0.070] | 0.8233 | [-0.97, 6.40] | fail |
| 1 | V2 | 735 | 1.12 | -0.0902 | [-0.371, 0.156] | 0.6851 | [-1.22, 6.17] | fail |

Short-dated option IV (V4, rows where it exists):

| tau | prob | n hold | b1 (mid) | b2 (model) | b2 95% CI | dLL x1000 | 95% CI | criterion |
|---|---|---|---|---|---|---|---|---|
| 12 | V4 | 728 | 1.10 | -0.0042 | [-0.211, 0.279] | -0.0306 | [-1.76, 3.77] | fail |
| 10 | V4 | 729 | 0.9904 | 0.1119 | [-0.119, 0.428] | 0.2144 | [-2.59, 5.61] | fail |
| 8 | V4 | 728 | 0.9830 | 0.0775 | [-0.097, 0.323] | -0.0120 | [-2.20, 3.03] | fail |
| 6 | V4 | 729 | 0.7951 | 0.2020 | [0.019, 0.469] | 2.24 | [-0.63, 9.56] | fail |
| 4 | V4 | 730 | 1.06 | -0.0654 | [-0.198, 0.101] | -0.4270 | [-1.64, 1.18] | fail |
| 3 | V4 | 730 | 1.01 | -0.0328 | [-0.175, 0.122] | -0.3469 | [-2.56, 2.25] | fail |
| 2 | V4 | 729 | 1.02 | -0.1072 | [-0.269, 0.079] | -1.94 | [-6.94, 1.92] | fail |
| 1 | V4 | 730 | 1.09 | -0.0620 | [-0.379, 0.207] | 0.3655 | [-1.38, 5.16] | fail |

No checkpoint passes, for our sigma or for any IV variant (V1, V3, V4 and the candle-mid robustness check included). The coefficient CI excludes 0 only at 6 minutes (V1 to V4) and at 10 minutes (V3). At 6 minutes the holdout log loss gets worse (+1.4 to +2.7 x1000); at 10 minutes V3 improves it by 0.86 x1000, with a CI of [-4.83, +3.22]. Hourly KXBTCD with V2: no pass, dLL from -3.6 to +0.9 x1000.

## Verdicts

- **H-A (longer-duration markets are less efficient): NOT SUPPORTED.** On KXBTCD hourly, the pre-registered criterion fails at all 8 checkpoints. The market's log loss is lower than the model's at every checkpoint, and the combo's holdout gain ranges from -0.28 to +3.84 thousandths of a nat, with every CI spanning 0. The hourly market behaves like the 15-minute market: it already contains everything our random walk knows, and our sigma is again too high near the close. Daily: no pass, but with 9 holdout days the test has no power, so treat daily as untested rather than refuted. KXBTC ranges: no pass.
- **H-B (implied vol beats our sigma): NOT SUPPORTED.** On the 15-minute panel, IV BEATS OUR SIGMA fails at every checkpoint. Scaled DVOL is significantly worse at 8-12 minutes, and short-dated option IV is worse at 10-12 minutes. **IV MODEL ADDS INFORMATION: NOT SUPPORTED** (0 of 8 checkpoints, for every variant). Redone on hourly and daily markets, it is the same picture. The one isolated pass (daily 6 h, IV better than ours) is flagged as possibly chance.
- **Implication.** Neither a longer horizon nor a forward-looking sigma gives the random-walk fair value an edge over Kalshi's price on any BTC series tested. There is no evidence for building a taker strategy on this model in KXBTCD/KXBTC either.

## Caveats

- One month of data, one volatility regime (DVOL 34.5-40.1). Holdout = 10 days (hourly) and 9 days (daily). The daily test is underpowered.
- Market price = candle close mid of Kalshi's top of book, not a fill price. A mid with a wide spread can be stale. The in-play filter uses only the market's own price.
- Strikes in one event are strongly correlated (one BTC path). Day-clustered bootstraps account for that, but the effective sample is roughly events x checkpoints, not rows.
- No index-basis correction for hourly/daily strikes (our index runs about $2 below BRTI). That is negligible against hourly sigmas of hundreds of dollars.
- The 15-minute print-mid panel covers 2,263 of 2,848 windows, because the tape job was still running. The missing ones are a random subset, and the candle-mid robustness check on all windows agrees.
- The sigma-ratio method here (z_market/z_ours on rows with |mid - 0.5| >= 0.1) is not identical to the 15-minute study's test C, so the levels differ slightly from that doc.

