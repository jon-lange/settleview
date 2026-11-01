# Model vs market: does our fair value know anything Kalshi's price does not? (2026-10-28)

**Question:** does our fair-value model contain information that Kalshi's own price does not, out of sample, after real fees and realistic execution? If it does not, no amount of threshold tuning can create a taker edge, and we should stop trading it.

## Pre-registered criteria

Written before any result was computed. Not edited afterwards.

- Holdout = the last third of days by date. Fit on the first two thirds. Bootstrap clusters = days (CIs) or windows (t-stats).
- "MODEL ADDS INFORMATION" iff, on the holdout at a given tau, the logistic combo outcome ~ logit(market mid) + logit(model) has lower log loss than the market alone, with the 95% day-bootstrap CI of the difference excluding 0, AND the model coefficient's 95% CI excludes 0 (positive).
- "TAKER STRATEGY VIABLE" iff the live rule's holdout net cents/contract at 1c slippage is > 0 with window-clustered t > 2, AND it is positive in both chronological halves of the full period.
- Otherwise the verdict is SIT OUT for taker trading with this model.

## Operational definitions (fixed before any result was computed)

These pin down choices the criteria leave open. They were written at the same time as the criteria, after measuring only data volume (prints per market) and API behaviour.

- **Period and days.** Settled KXBTC15M markets whose window opens from 2026-09-29 00:00 UTC through 2026-10-28 23:45 UTC, result `yes` or `no` (scalar or void results excluded). A window's day is the UTC date of its open. With 30 days, train = the first 20 (2026-09-29 to 2026-10-18) and holdout = the last 10 (2026-10-19 to 2026-10-28). The chronological halves for the strategy test are the first 15 and last 15 days.
- **Market mid at tau.** As in Step 2 of the brief: YES ask proxy = latest taker-buys-YES print in [tau-30s, tau); YES bid proxy = latest taker-buys-NO print in [tau-30s, tau), in YES terms. mid = their average when both exist, else the one print that exists (flagged). A window with no print in [tau-30s, tau) is dropped at that tau.
- **Model at tau.** `fair_from_index` from `btc_signal_engine/domain/coach/fair_value.py`, evaluated at `at = tau - 1s` with index rows strictly before that, and our own open average (`window_average` over the 60 seconds before the open) as the strike.
- **Probabilities** are clipped to [0.001, 0.999] (Kalshi's price range) before log loss and logits.
- **"The market alone"** is the nested benchmark: a logistic outcome ~ a + b·logit(mid), fit on the same training days. The verdict uses the combo's log loss minus this benchmark's on the holdout. The raw mid is reported too.
- **CIs.** The log-loss difference CI is a joint day-cluster bootstrap (resample training days and refit both models; resample holdout days and evaluate), 2,000 replicates, percentile interval. The model coefficient's CI is a day-cluster bootstrap of the training fit, 2,000 replicates, percentile interval. Each tau is judged on its own; a pass at one tau with the neighbouring taus failing is flagged as possibly chance (8 taus are tested).
- **Strategy test.** Each whole second s with close - 13 min <= s < close - 2 min: fair at s - 1s; quotes from the same print proxies over [s-30s, s) (NO ask = 1 - YES bid proxy, NO bid = 1 - YES ask proxy). The live rule is `entry_decision` from `rules.py` at commit bcc339a (blend 0.5, floor 25c, zone 35c +3c, min edge 5c, 2-minute cutoff) with the engine gate off, 10-second `Confirmation`, and limit = `entry_limit` on the blended fair (up to +2c). The old rule is no blend, no floor or zone, min edge 5c, limit = the seen ask (rules.py before 13d602a). Once a side is confirmed at s, it fills at the first taker-buys print for that side in [s, s+2s) priced at or under the limit; it pays that print plus 0, 1 or 2 cents (capped at 99c) and the exact taker fee. With no fill, the scan continues; at most one fill per window. Primary exit: hold to settlement, scored on Kalshi's result. Secondary exit: the live `exit_advice` each second after entry, selling at the held side's bid proxy minus the same slippage and paying the taker fee. One trade per window at most, so the window-clustered t is mean / (sd / sqrt(n)).
- **Sampling.** Prints run about 40,000 per market (about 110M in the period), far over the ~5M threshold. Full tapes are fetched for a random 40% of markets, stratified by UTC day (seed 20261028); the strategy test runs on those. For every market, the panel fetch takes the latest prints before each tau (pages of 1,000, at most 45s back, paging further only while a taker side is still missing within 30s), so it always contains the latest print of each taker side in [tau-30s, tau) when one exists. Nothing after tau is fetched for the panel, since no test may look ahead.

## Amendment (2026-10-29 00:15 UTC, before any strategy result was seen)

Kalshi's public trades endpoint allows about 4 requests a second from one IP (HTTP 429 above that; page size does not change it), so the planned 40% full-tape sample (about 41,000 requests) would take about 3 hours. The full tapes are therefore time-boxed: they are fetched in a fixed random order of the stratified 40% sample and fetching stops at 02:20 UTC, so the markets fetched by then are a random subset of that sample. The strategy test runs on that subset. Sampled markets left without a full tape get the same direct panel fetch as every other market, so the information test still covers every market. Only a 3-window code test of the strategy simulator had been run when this was decided.

## Interim results: tests A to C (2026-10-29 01:22 UTC, 64% of windows; superseded by the final results below)

Coverage at this point: 1,821 of 2,848 windows have both a model value and a market mid at every tau (1,750 direct panel fetches plus 115 full tapes). The other ~1,000 are sampled windows whose tape or panel had not been fetched yet; they are a random subset, so this is an unbiased 64% sample. Holdout n = 611 per tau. The route check found 480 of 480 quote proxies identical between full tapes and direct panel fetches on 30 windows.

**A. Information (holdout, nested logistic combo vs market-only logistic):**

| tau (min) | LL market | LL model | combo b2 (model) [95% CI] | dLL x1000 [95% CI] | criterion |
|---|---|---|---|---|---|
| 12 | 0.603 | 0.614 | -0.38 [-0.88, 0.19] | -0.97 [-4.64, 4.11] | fail |
| 10 | 0.568 | 0.572 | 0.23 [-0.06, 0.53] | -0.71 [-4.02, 2.86] | fail |
| 8 | 0.498 | 0.500 | 0.07 [-0.19, 0.41] | -0.54 [-3.04, 2.16] | fail |
| 6 | 0.412 | 0.429 | 0.09 [-0.14, 0.43] | 0.38 [-0.84, 4.42] | fail |
| 4 | 0.330 | 0.367 | -0.14 [-0.40, 0.15] | -1.66 [-4.90, 2.88] | fail |
| 3 | 0.284 | 0.314 | -0.02 [-0.19, 0.17] | -0.12 [-2.17, 2.59] | fail |
| 2 | 0.206 | 0.239 | -0.06 [-0.23, 0.09] | -0.26 [-1.52, 1.39] | fail |
| 1 | 0.083 | 0.123 | -0.08 [-0.42, 0.23] | 0.38 [-1.19, 6.71] | fail |

The market's own log loss beats the model's at every tau on the holdout and on all days, and every fixed blend is worse than the market alone except a 0.75-market blend at 8 to 10 minutes (by under 0.001). On Kalshi's own top of book at tau (1-minute candles, all 2,848 windows) the model coefficient is again within noise of zero (-0.31 to +0.09). When model and market disagree by 10c or more (1,417 cases), the market has the lower Brier at every tau, and buying the model's side at the mid would have lost 2.8c before fees (t = -1.8).

**B. Index fidelity:** our close average minus Kalshi's settlement value has median -$1.82 (p5 -$9.76, p95 +$5.77, |error| p90 $8.21, max $24.05); our open average minus floor_strike is almost identical (-$1.81). Our proxy outcome disagrees with Kalshi's result in 2.1% of windows (58 of 2,804), rising to 8.0% when the official close is within $25 of the strike, 17.5% within $10 and 28.3% within $5.

**C. Volatility:** median ratio of our sigma to the market-implied sigma is 1.03 to 1.04 at 10 to 12 minutes, rising to 1.11 to 1.25 at 6 to 1 minutes. It is highest at 12:00 to 15:59 UTC (1.17) and in high recent volatility (1.21), and 0.97 in low volatility. Outcomes themselves fit our sigma at 12 to 6 minutes (best multiplier k = 0.99 to 1.01) and want 5 to 12% more at 4 to 1 minutes.

## Verdict (final, 2026-10-29 02:47 UTC, all 2,848 windows)

- **MODEL ADDS INFORMATION: no, at every tau.** On the holdout, the market's own mid has lower log loss than the model at all 8 taus. The logistic combo does not beat the market-only logistic anywhere: every 95% CI of the difference contains 0, and every model coefficient CI contains 0 (point estimates -0.30 to +0.08). Kalshi's top of book at tau (1-minute candles) gives the same answer.
- **TAKER STRATEGY VIABLE: no.** The live rule at 1c slippage held to settlement lost 20.5c/contract on the holdout (n = 11, t = -1.15) and 17.2c on all sampled windows (n = 32, t = -1.88). It was negative in both halves. The old rule lost 9.0c/contract (n = 304, t = -3.69), 6.5c on the holdout (n = 98).
- **SIT OUT** for taker trading with this model. The brief asks for next hypotheses only if the model adds information, and it does not, so none are listed.

## Final results

Generated by `python scripts/model_vs_market.py analyze` (full copy in `data/research/results.md`; per-window panel in `data/research/panel.csv`, strategy trades in `data/research/strategy_trades.csv`).


### Data coverage

Markets: 2848 settled KXBTC15M windows, 2026-09-29 to 2026-10-28 (30 UTC days; train 2026-09-29 to 2026-10-18, holdout 2026-10-19 to 2026-10-28). Excluded results: {}. App cross-check: {'result': 0, 'floor_strike': 0, 'expiration_value': 0, 'missing_in_app': 0}.

Full tapes (last 820s of each window): 435 markets, 14,775,924 prints (33,968 per market), 0 block trades dropped. Direct panel fetches: 2443 markets, 18,592,204 prints, 0 block trades dropped.

Route check on 30 markets with both a full tape and a direct panel fetch: 480/480 quote proxies identical.

| tau (min) | markets | prints fetched | model ok | two-sided mid | one-sided mid | no print in 30s | used (model+mid) | median ask-print age (s) | median bid-print age (s) | candle mid |
|---|---|---|---|---|---|---|---|---|---|---|
| 12 | 2848 | 2848 | 2825 | 2848 | 0 | 0 | 2825 | 0.0710 | 0.0750 | 2848 |
| 10 | 2848 | 2848 | 2825 | 2848 | 0 | 0 | 2825 | 0.0730 | 0.0780 | 2848 |
| 8 | 2848 | 2848 | 2825 | 2848 | 0 | 0 | 2825 | 0.0810 | 0.0800 | 2848 |
| 6 | 2848 | 2848 | 2825 | 2848 | 0 | 0 | 2825 | 0.0760 | 0.0810 | 2848 |
| 4 | 2848 | 2848 | 2825 | 2848 | 0 | 0 | 2825 | 0.0730 | 0.0790 | 2848 |
| 3 | 2848 | 2848 | 2825 | 2847 | 1 | 0 | 2825 | 0.0710 | 0.0750 | 2841 |
| 2 | 2848 | 2848 | 2825 | 2819 | 29 | 0 | 2825 | 0.0685 | 0.0730 | 2674 |
| 1 | 2848 | 2848 | 2825 | 2455 | 393 | 0 | 2825 | 0.0820 | 0.0840 | 1861 |

### A. Information

Holdout days. Log loss (LL; lower is better; probabilities clipped to [0.001, 0.999]) and Brier. Blend w = fixed weight on the market mid, the rest on the model (nothing fitted).

| tau | n | LL market | LL model | LL blend 0.25 | LL blend 0.5 | LL blend 0.75 | Brier market | Brier model | Brier blend 0.25 | Brier blend 0.5 | Brier blend 0.75 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 12 | 942 | 0.6090 | 0.6192 | 0.6151 | 0.6122 | 0.6102 | 0.2115 | 0.2147 | 0.2135 | 0.2125 | 0.2119 |
| 10 | 942 | 0.5788 | 0.5807 | 0.5784 | 0.5774 | 0.5775 | 0.1983 | 0.1982 | 0.1977 | 0.1976 | 0.1978 |
| 8 | 942 | 0.5156 | 0.5168 | 0.5147 | 0.5138 | 0.5141 | 0.1723 | 0.1732 | 0.1724 | 0.1721 | 0.1720 |
| 6 | 942 | 0.4414 | 0.4519 | 0.4463 | 0.4429 | 0.4413 | 0.1443 | 0.1476 | 0.1461 | 0.1450 | 0.1444 |
| 4 | 942 | 0.3523 | 0.3763 | 0.3631 | 0.3572 | 0.3537 | 0.1116 | 0.1173 | 0.1150 | 0.1133 | 0.1122 |
| 3 | 942 | 0.2996 | 0.3205 | 0.3091 | 0.3031 | 0.3000 | 0.0940 | 0.1002 | 0.0975 | 0.0956 | 0.0944 |
| 2 | 942 | 0.2190 | 0.2561 | 0.2351 | 0.2264 | 0.2211 | 0.0675 | 0.0753 | 0.0720 | 0.0696 | 0.0681 |
| 1 | 942 | 0.0857 | 0.1303 | 0.1062 | 0.0958 | 0.0890 | 0.0243 | 0.0371 | 0.0317 | 0.0277 | 0.0253 |

All days (nothing here is fitted, so every day counts):

| tau | n | LL market | LL model | LL blend 0.25 | LL blend 0.5 | LL blend 0.75 | Brier market | Brier model | Brier blend 0.25 | Brier blend 0.5 | Brier blend 0.75 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 12 | 2825 | 0.5975 | 0.6075 | 0.6036 | 0.6008 | 0.5988 | 0.2064 | 0.2103 | 0.2089 | 0.2078 | 0.2070 |
| 10 | 2825 | 0.5531 | 0.5589 | 0.5553 | 0.5534 | 0.5527 | 0.1877 | 0.1894 | 0.1884 | 0.1878 | 0.1875 |
| 8 | 2825 | 0.5004 | 0.5064 | 0.5027 | 0.5007 | 0.4999 | 0.1661 | 0.1686 | 0.1675 | 0.1667 | 0.1662 |
| 6 | 2825 | 0.4404 | 0.4534 | 0.4468 | 0.4430 | 0.4409 | 0.1436 | 0.1472 | 0.1456 | 0.1445 | 0.1438 |
| 4 | 2825 | 0.3553 | 0.3791 | 0.3677 | 0.3614 | 0.3573 | 0.1136 | 0.1204 | 0.1178 | 0.1159 | 0.1145 |
| 3 | 2825 | 0.2992 | 0.3225 | 0.3108 | 0.3043 | 0.3005 | 0.0937 | 0.1003 | 0.0976 | 0.0956 | 0.0943 |
| 2 | 2825 | 0.2337 | 0.2677 | 0.2501 | 0.2414 | 0.2360 | 0.0726 | 0.0810 | 0.0776 | 0.0751 | 0.0734 |
| 1 | 2825 | 0.1120 | 0.1575 | 0.1321 | 0.1214 | 0.1147 | 0.0327 | 0.0463 | 0.0409 | 0.0368 | 0.0341 |

Logistic combo outcome ~ a + b1 logit(mid) + b2 logit(model), fit on train days, scored on holdout days. dLL = combo minus the market-only logistic (nested benchmark; negative = the model adds information), in thousandths of a nat per window, with the joint day-bootstrap 95% CI. Also shown against the raw mid.

| tau | n train | n hold | market-only slope | combo b1 (mid) | combo b2 (model) | b2 95% CI | b2 refit on holdout | dLL x1000 | 95% CI | dLL vs raw mid x1000 | 95% CI | criterion |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 12 | 1883 | 942 | 1.14 | 1.45 | -0.3004 | [-0.739, 0.199] | -0.2515 | -0.3554 | [-3.96, 4.65] | 1.30 | [-3.13, 8.28] | fail |
| 10 | 1883 | 942 | 1.13 | 1.07 | 0.0620 | [-0.244, 0.382] | 0.3580 | -0.4990 | [-3.75, 3.46] | 3.32 | [-1.51, 11.76] | fail |
| 8 | 1883 | 942 | 1.06 | 0.9789 | 0.0839 | [-0.140, 0.324] | 0.4005 | -0.7346 | [-2.98, 1.86] | 0.5579 | [-2.63, 6.69] | fail |
| 6 | 1883 | 942 | 1.00 | 1.01 | -0.0076 | [-0.213, 0.239] | 0.0990 | 0.0273 | [-0.99, 2.16] | -0.0427 | [-1.49, 3.96] | fail |
| 4 | 1883 | 942 | 0.9958 | 1.11 | -0.1138 | [-0.311, 0.114] | -0.0876 | -0.1384 | [-1.35, 2.14] | 0.9485 | [-0.72, 5.53] | fail |
| 3 | 1883 | 942 | 1.00 | 1.04 | -0.0393 | [-0.193, 0.132] | 0.0091 | 0.0974 | [-0.92, 2.15] | 1.60 | [-0.39, 5.91] | fail |
| 2 | 1883 | 942 | 0.9492 | 1.09 | -0.1446 | [-0.286, 0.006] | -0.1600 | -0.7898 | [-3.46, 1.98] | 0.2008 | [-3.74, 5.24] | fail |
| 1 | 1883 | 942 | 0.9947 | 1.08 | -0.0914 | [-0.281, 0.154] | 0.0230 | 0.2159 | [-0.80, 3.16] | 1.70 | [-1.26, 7.05] | fail |

Robustness (not part of the criteria): Kalshi's own top of book at tau (1-minute candle close) instead of print proxies. CI resamples holdout days only, coefficients fixed from train.

| tau | n hold | LL candle mid | LL model | combo b2 (model) | dLL x1000 | 95% CI |
|---|---|---|---|---|---|---|
| 12 | 942 | 0.6091 | 0.6192 | -0.3065 | -0.3040 | [-3.21, 2.82] |
| 10 | 942 | 0.5785 | 0.5807 | 0.0699 | -0.5420 | [-1.41, 0.33] |
| 8 | 942 | 0.5157 | 0.5168 | 0.0894 | -0.7839 | [-1.58, -0.07] |
| 6 | 942 | 0.4412 | 0.4519 | -0.0124 | 0.0441 | [-0.08, 0.16] |
| 4 | 942 | 0.3523 | 0.3763 | -0.1106 | -0.1279 | [-1.21, 1.05] |
| 3 | 940 | 0.3002 | 0.3212 | -0.0369 | 0.0886 | [-0.45, 0.61] |
| 2 | 853 | 0.2418 | 0.2827 | -0.1428 | -0.8628 | [-3.55, 1.46] |
| 1 | 550 | 0.1464 | 0.2224 | -0.0929 | 0.3651 | [-1.43, 2.43] |

Calibration by market mid bucket (cents), all days, all taus pooled:

| bucket | n | avg market mid | avg model | went Up |
|---|---|---|---|---|
| 0-10 | 4522 | 0.0281 | 0.0365 | 0.0305 |
| 10-25 | 2529 | 0.1724 | 0.1968 | 0.1645 |
| 25-35 | 1678 | 0.2986 | 0.3113 | 0.2861 |
| 35-65 | 4969 | 0.4995 | 0.4982 | 0.4741 |
| 65-75 | 1713 | 0.6999 | 0.6812 | 0.6807 |
| 75-90 | 2558 | 0.8255 | 0.7967 | 0.8163 |
| 90-100 | 4631 | 0.9724 | 0.9599 | 0.9786 |

Calibration by model bucket (cents), all days, all taus pooled:

| bucket | n | avg model | avg market mid | went Up |
|---|---|---|---|---|
| 0-10 | 4344 | 0.0241 | 0.0354 | 0.0343 |
| 10-25 | 2426 | 0.1739 | 0.1687 | 0.1760 |
| 25-35 | 1763 | 0.2999 | 0.2828 | 0.2785 |
| 35-65 | 5487 | 0.5012 | 0.5036 | 0.4777 |
| 65-75 | 1788 | 0.6998 | 0.7210 | 0.6969 |
| 75-90 | 2469 | 0.8246 | 0.8363 | 0.8287 |
| 90-100 | 4323 | 0.9759 | 0.9672 | 0.9706 |

By market-mid bucket, taus 12-6 min:

| bucket | n | avg market mid | avg model | went Up |
|---|---|---|---|---|
| 0-10 | 856 | 0.0566 | 0.0565 | 0.0572 |
| 10-25 | 1569 | 0.1762 | 0.1851 | 0.1714 |
| 25-35 | 1220 | 0.2983 | 0.3041 | 0.2869 |
| 35-65 | 3834 | 0.4998 | 0.4997 | 0.4742 |
| 65-75 | 1252 | 0.6996 | 0.6884 | 0.6829 |
| 75-90 | 1672 | 0.8225 | 0.8083 | 0.8289 |
| 90-100 | 897 | 0.9447 | 0.9396 | 0.9599 |

By market-mid bucket, taus 4-1 min:

| bucket | n | avg market mid | avg model | went Up |
|---|---|---|---|---|
| 0-10 | 3666 | 0.0215 | 0.0319 | 0.0243 |
| 10-25 | 960 | 0.1662 | 0.2161 | 0.1531 |
| 25-35 | 458 | 0.2995 | 0.3305 | 0.2838 |
| 35-65 | 1135 | 0.4981 | 0.4934 | 0.4740 |
| 65-75 | 461 | 0.7009 | 0.6616 | 0.6746 |
| 75-90 | 886 | 0.8311 | 0.7748 | 0.7923 |
| 90-100 | 3734 | 0.9791 | 0.9648 | 0.9831 |

Disagreement subset (all days): 'outcome on model side' = the window went the way the model leaned relative to the mid. Edge at mid = average of sign(model - mid) x (outcome - mid) in cents, before fees and spread; t clustered by window.

| |model - mid| >= | tau | n | outcome on model side | edge at mid (c) | t | Brier model | Brier market | more accurate |
|---|---|---|---|---|---|---|---|---|
| 5c | 12 | 769 | 0.4811 | -2.04 | -1.27 | 0.2101 | 0.1994 | market |
| 5c | 10 | 881 | 0.4960 | 1.68 | 1.12 | 0.2030 | 0.1990 | market |
| 5c | 8 | 857 | 0.4259 | 1.11 | 0.7650 | 0.1870 | 0.1807 | market |
| 5c | 6 | 954 | 0.3553 | -0.0487 | -0.0363 | 0.1822 | 0.1712 | market |
| 5c | 4 | 952 | 0.3151 | -2.42 | -1.82 | 0.1880 | 0.1685 | market |
| 5c | 3 | 914 | 0.3118 | 1.05 | 0.7872 | 0.1837 | 0.1640 | market |
| 5c | 2 | 856 | 0.2780 | -0.2241 | -0.1691 | 0.1769 | 0.1504 | market |
| 5c | 1 | 543 | 0.2670 | -1.82 | -1.20 | 0.1954 | 0.1250 | market |
| 5c | all | 6726 | 0.3686 | -0.2530 | -0.3212 | 0.1901 | 0.1715 | market |
| 10c | 12 | 114 | 0.5439 | -1.33 | -0.3180 | 0.2236 | 0.1993 | market |
| 10c | 10 | 183 | 0.5137 | 2.96 | 0.8313 | 0.2398 | 0.2325 | market |
| 10c | 8 | 161 | 0.4099 | 1.39 | 0.3850 | 0.2258 | 0.2094 | market |
| 10c | 6 | 249 | 0.3052 | -2.64 | -0.9645 | 0.2177 | 0.1879 | market |
| 10c | 4 | 332 | 0.3012 | -3.63 | -1.57 | 0.2208 | 0.1778 | market |
| 10c | 3 | 401 | 0.3017 | -0.9746 | -0.4649 | 0.2187 | 0.1763 | market |
| 10c | 2 | 430 | 0.2488 | -2.35 | -1.19 | 0.2193 | 0.1690 | market |
| 10c | 1 | 364 | 0.2390 | -2.95 | -1.53 | 0.2380 | 0.1352 | market |
| 10c | all | 2234 | 0.3192 | -1.67 | -1.27 | 0.2246 | 0.1779 | market |

### B. Index fidelity

| error ($) | n | mean | median | p5 | p95 | |e| median | |e| p90 | |e| p99 | |e| max |
|---|---|---|---|---|---|---|---|---|---|
| our close avg - official settlement value | 2825 | -1.84 | -1.82 | -9.76 | 5.77 | 3.29 | 8.21 | 13.98 | 24.05 |
| our open avg - Kalshi floor_strike | 2825 | -1.83 | -1.81 | -9.72 | 5.77 | 3.29 | 8.19 | 13.98 | 24.05 |

Outcome mismatches with Kalshi's official result. 'Ours' = our close avg >= our open avg (what the model forecasts); 'our close vs Kalshi strike' isolates the close-side error.

| subset | n | ours != Kalshi (rate) | count | our close vs Kalshi strike != Kalshi |
|---|---|---|---|---|
| all | 2804 | 0.0207 | 58 | 0.0146 |
| |official close - strike| < $25 | 725 | 0.0800 | 58 | 0.0566 |
| < $10 | 332 | 0.1747 | 58 | 0.1205 |
| < $5 | 166 | 0.2831 | 47 | 0.1988 |

Kalshi's result agrees with expiration_value >= floor_strike in 2804/2804 windows. Windows without our open or close average (index coverage rule): 23 open, 23 close.

### C. Volatility: ours vs market-implied

ratio = our sigma / the sigma that makes our model reproduce the market mid (Phi^-1(mid) / our z). Below 1 = we assume less volatility than the market prices. Rows with mid in [5c, 95c], |Phi^-1(mid)| >= 0.1, model and market on the same side of 50% (13840 of 15631; opposite sides: 923).

| tau | n | median ratio | p25 | p75 | share < 1 |
|---|---|---|---|---|---|
| 12 | 2390 | 1.03 | 0.8292 | 1.27 | 0.4628 |
| 10 | 2435 | 1.02 | 0.8355 | 1.27 | 0.4719 |
| 8 | 2363 | 1.06 | 0.8729 | 1.30 | 0.4262 |
| 6 | 2146 | 1.11 | 0.9092 | 1.36 | 0.3574 |
| 4 | 1683 | 1.13 | 0.8997 | 1.43 | 0.3660 |
| 3 | 1366 | 1.17 | 0.9205 | 1.52 | 0.3360 |
| 2 | 1008 | 1.22 | 0.9045 | 1.66 | 0.3333 |
| 1 | 449 | 1.17 | 0.7622 | 2.04 | 0.3898 |

By hour of day (UTC, all taus):

| hours | n | median ratio | p25 | p75 | share < 1 |
|---|---|---|---|---|---|
| 00-03 UTC | 2381 | 1.02 | 0.8066 | 1.28 | 0.4742 |
| 04-07 UTC | 2256 | 1.08 | 0.8795 | 1.38 | 0.3998 |
| 08-11 UTC | 2175 | 1.14 | 0.9234 | 1.46 | 0.3411 |
| 12-15 UTC | 2279 | 1.14 | 0.9430 | 1.44 | 0.3282 |
| 16-19 UTC | 2335 | 1.09 | 0.9040 | 1.37 | 0.3773 |
| 20-23 UTC | 2414 | 0.9982 | 0.7936 | 1.31 | 0.5025 |

By our recent realized volatility (terciles of sigma / price):

| tercile | n | median ratio | p25 | p75 | share < 1 |
|---|---|---|---|---|---|
| low recent vol | 4614 | 0.9660 | 0.7651 | 1.28 | 0.5384 |
| mid recent vol | 4613 | 1.05 | 0.8810 | 1.29 | 0.4214 |
| high recent vol | 4613 | 1.21 | 0.9940 | 1.50 | 0.2573 |

Outcome-fitted sigma multiplier k (Phi(z / k) with the lowest log loss, all days; above 1 = outcomes were more random than our sigma implied):

| tau | n | best k |
|---|---|---|
| 12 | 2825 | 1.04 |
| 10 | 2825 | 1.03 |
| 8 | 2825 | 1.01 |
| 6 | 2825 | 1.03 |
| 4 | 2825 | 1.07 |
| 3 | 2825 | 1.05 |
| 2 | 2825 | 1.10 |
| 1 | 2825 | 1.19 |

### D. Tradeable P&L (full-tape sample)

435 windows with full tapes. At most one fill per window; fills need a taker print for our side within 2s at or under the limit; paid = print + slippage; exact taker fee (1 contract).

| rule | exit | slippage | days | n | win % | avg cost incl. fee (= breakeven win % if held) | net c/contract | t (window-clustered) |
|---|---|---|---|---|---|---|---|---|
| live | hold | 0c | all | 32 | 0.4688 | 0.6307 | -16.20 | -1.77 |
| live | hold | 0c | train | 21 | 0.4762 | 0.6207 | -14.45 | -1.35 |
| live | hold | 0c | holdout | 11 | 0.4545 | 0.6499 | -19.54 | -1.10 |
| live | hold | 0c | half 1 | 13 | 0.4615 | 0.6161 | -15.46 | -1.11 |
| live | hold | 0c | half 2 | 19 | 0.4737 | 0.6407 | -16.70 | -1.34 |
| live | hold | 1c | all | 32 | 0.4688 | 0.6405 | -17.18 | -1.88 |
| live | hold | 1c | train | 21 | 0.4762 | 0.6305 | -15.43 | -1.44 |
| live | hold | 1c | holdout | 11 | 0.4545 | 0.6597 | -20.52 | -1.15 |
| live | hold | 1c | half 1 | 13 | 0.4615 | 0.6260 | -16.44 | -1.18 |
| live | hold | 1c | half 2 | 19 | 0.4737 | 0.6505 | -17.68 | -1.42 |
| live | hold | 2c | all | 32 | 0.4688 | 0.6504 | -18.16 | -1.98 |
| live | hold | 2c | train | 21 | 0.4762 | 0.6403 | -16.41 | -1.53 |
| live | hold | 2c | holdout | 11 | 0.4545 | 0.6695 | -21.50 | -1.21 |
| live | hold | 2c | half 1 | 13 | 0.4615 | 0.6358 | -17.42 | -1.25 |
| live | hold | 2c | half 2 | 19 | 0.4737 | 0.6603 | -18.67 | -1.50 |
| live | exit | 0c | all | 32 | 0.4375 | 0.6307 | -5.99 | -1.55 |
| live | exit | 0c | train | 21 | 0.3810 | 0.6207 | -8.89 | -2.04 |
| live | exit | 0c | holdout | 11 | 0.5455 | 0.6499 | -0.4700 | -0.0621 |
| live | exit | 0c | half 1 | 13 | 0.3077 | 0.6161 | -10.42 | -1.97 |
| live | exit | 0c | half 2 | 19 | 0.5263 | 0.6407 | -2.96 | -0.5474 |
| live | exit | 1c | all | 32 | 0.4062 | 0.6405 | -7.99 | -2.07 |
| live | exit | 1c | train | 21 | 0.3810 | 0.6305 | -10.88 | -2.50 |
| live | exit | 1c | holdout | 11 | 0.4545 | 0.6597 | -2.47 | -0.3272 |
| live | exit | 1c | half 1 | 13 | 0.3077 | 0.6260 | -12.41 | -2.35 |
| live | exit | 1c | half 2 | 19 | 0.4737 | 0.6505 | -4.96 | -0.9178 |
| live | exit | 2c | all | 32 | 0.3438 | 0.6504 | -9.98 | -2.59 |
| live | exit | 2c | train | 21 | 0.2857 | 0.6403 | -12.86 | -2.96 |
| live | exit | 2c | holdout | 11 | 0.4545 | 0.6695 | -4.47 | -0.5920 |
| live | exit | 2c | half 1 | 13 | 0.2308 | 0.6358 | -14.39 | -2.73 |
| live | exit | 2c | half 2 | 19 | 0.4211 | 0.6603 | -6.96 | -1.29 |
| old | hold | 0c | all | 304 | 0.3651 | 0.4446 | -7.95 | -3.27 |
| old | hold | 0c | train | 206 | 0.3786 | 0.4699 | -9.12 | -3.17 |
| old | hold | 0c | holdout | 98 | 0.3367 | 0.3915 | -5.47 | -1.21 |
| old | hold | 0c | half 1 | 154 | 0.3831 | 0.4568 | -7.37 | -2.20 |
| old | hold | 0c | half 2 | 150 | 0.3467 | 0.4321 | -8.54 | -2.42 |
| old | hold | 1c | all | 304 | 0.3651 | 0.4547 | -8.95 | -3.69 |
| old | hold | 1c | train | 206 | 0.3786 | 0.4799 | -10.13 | -3.52 |
| old | hold | 1c | holdout | 98 | 0.3367 | 0.4016 | -6.49 | -1.44 |
| old | hold | 1c | half 1 | 154 | 0.3831 | 0.4669 | -8.38 | -2.50 |
| old | hold | 1c | half 2 | 150 | 0.3467 | 0.4422 | -9.55 | -2.71 |
| old | hold | 2c | all | 304 | 0.3651 | 0.4648 | -9.96 | -4.10 |
| old | hold | 2c | train | 206 | 0.3786 | 0.4900 | -11.13 | -3.87 |
| old | hold | 2c | holdout | 98 | 0.3367 | 0.4118 | -7.50 | -1.66 |
| old | hold | 2c | half 1 | 154 | 0.3831 | 0.4769 | -9.38 | -2.80 |
| old | hold | 2c | half 2 | 150 | 0.3467 | 0.4523 | -10.56 | -2.99 |
| old | exit | 0c | all | 304 | 0.5362 | 0.4446 | -4.43 | -6.02 |
| old | exit | 0c | train | 206 | 0.5534 | 0.4699 | -4.20 | -4.73 |
| old | exit | 0c | holdout | 98 | 0.5000 | 0.3915 | -4.92 | -3.72 |
| old | exit | 0c | half 1 | 154 | 0.5714 | 0.4568 | -3.59 | -3.49 |
| old | exit | 0c | half 2 | 150 | 0.5000 | 0.4321 | -5.30 | -5.02 |
| old | exit | 1c | all | 304 | 0.4013 | 0.4547 | -6.43 | -8.74 |
| old | exit | 1c | train | 206 | 0.4223 | 0.4799 | -6.20 | -6.98 |
| old | exit | 1c | holdout | 98 | 0.3571 | 0.4016 | -6.91 | -5.23 |
| old | exit | 1c | half 1 | 154 | 0.4416 | 0.4669 | -5.59 | -5.44 |
| old | exit | 1c | half 2 | 150 | 0.3600 | 0.4422 | -7.29 | -6.92 |
| old | exit | 2c | all | 304 | 0.2961 | 0.4648 | -8.42 | -11.46 |
| old | exit | 2c | train | 206 | 0.3301 | 0.4900 | -8.19 | -9.24 |
| old | exit | 2c | holdout | 98 | 0.2245 | 0.4118 | -8.91 | -6.75 |
| old | exit | 2c | half 1 | 154 | 0.3442 | 0.4769 | -7.58 | -7.39 |
| old | exit | 2c | half 2 | 150 | 0.2467 | 0.4523 | -9.28 | -8.82 |

Secondary-exit reasons (count):

| rule | stop | take_value | time |
|---|---|---|---|
| live | 18 | 13 | 1 |
| old | 141 | 163 | 0 |

Breakdown, hold to settlement at 1c slippage, all days:

| rule | subset | n | win % | avg print price | net c/contract | t |
|---|---|---|---|---|---|---|
| live | UP | 13 | 0.2308 | 0.6038 | -39.85 | -3.00 |
| live | DOWN | 19 | 0.6316 | 0.6232 | -1.67 | -0.1465 |
| live | 35-65c | 16 | 0.4375 | 0.5075 | -9.68 | -0.7540 |
| live | price > 65c | 16 | 0.5000 | 0.7231 | -24.68 | -1.87 |
| old | UP | 132 | 0.3258 | 0.4462 | -14.34 | -3.91 |
| old | DOWN | 172 | 0.3953 | 0.4205 | -4.82 | -1.50 |
| old | price < 35c | 152 | 0.1447 | 0.2186 | -9.58 | -3.36 |
| old | 35-65c | 70 | 0.4857 | 0.5057 | -4.69 | -0.7789 |
| old | price > 65c | 82 | 0.6707 | 0.7632 | -11.44 | -2.20 |

## Reading the results

- **A.** The market beats the model by 0.010 nats at 12 minutes and by 0.045 at 1 minute (holdout). The gap widens toward the close, where the market reacts to BRTI and we see only Coinbase, 1 second late. Fixed blends with weight 0.5 to 0.75 on the market tie or beat the market by under 0.002 nats at 8 to 10 minutes. That is a tiny in-sample wobble inside the CIs above, not a usable edge. When model and market disagree by 10c or more (2,234 cases), the market has the lower Brier at every tau, and buying the model's side at the mid lost 1.7c before fees and spread (t = -1.27).
- **B.** Our Coinbase index sits a steady $1.8 below BRTI at both the open and the close, so the strike basis mostly cancels. But the close error has a $3.3 median absolute value and a $24 maximum. Our proxy outcome therefore disagrees with Kalshi's result in 2.1% of all windows and 28% of windows that settle within $5 of the strike. Every one of the 58 mismatches settled within $25 of the strike. That is an irreducible handicap of a Coinbase-only model near the money.
- **C.** Our sigma is close to what the market implies 10 to 12 minutes out (median ratio 1.02 to 1.03). It is 11 to 22% higher than the market's from 6 to 2 minutes, and higher in busy hours (08:00 to 15:59 UTC) and after volatile stretches (1.21). It is slightly lower in quiet stretches (0.97). Outcomes alone want our sigma scaled by 1.01 to 1.04 at 12 to 6 minutes and by 1.05 to 1.19 at 4 to 1 minutes. So the market's lower implied sigma near the close is not more accurate than ours on outcomes; each is off in its own way, and neither error leaves the model with information the market lacks.
- **D.** The live rule seldom fires: 32 fills in 435 windows. Its losses come mostly from buying UP (-40c, 3 wins in 13) and favourites above 65c. The old rule fires in 70% of windows and loses about 9c a contract, 3 to 4 standard errors below zero. The live exit rule (stops and take-value) cuts the live rule's loss to 8c and still loses. For the old rule the exits lose 6.4c with t = -8.7, so stops and take-profits mostly pay the spread twice.

## Caveats

- **Market price proxy.** The criteria use print proxies: the latest taker print on each side in [tau-30s, tau). Prints are dense, with a median age of 0.07 to 0.08s at tau, and mids are two-sided in 98% of cases (86% at 1 minute). The candle top-of-book check agrees, so the result does not hinge on the proxy.
- **Latency.** The model sees the index up to tau - 1s; the market up to tau. That 1-second handicap is by design and matters most in the last 2 minutes. It cannot explain the 12-to-6-minute result, where the model is also no better.
- **Holdout size.** The holdout is 10 days, so the day-cluster CIs rest on 10 clusters. 8 taus were tested. Nothing came close to passing at any tau, so neither point changes the verdict.
- **Strategy sample.** The full tapes are a time-boxed random 435 windows (15%) of the planned 40% sample (see the amendment). The live rule's n = 32 (11 holdout) is too small to estimate its loss precisely, but it cannot meet the pre-registered bar: it is negative everywhere. Fills assume our 1-contract order fills like the observed print within 2s; the fee is per contract at C = 1; the engine gate is not replicated; and the secondary exit sells at a print-proxy bid without queue risk.
- **Tape span.** The "full" tapes cover the last 13 min 40 s of each window, the span the strategy test reads. The first 80 s were not fetched.
- **Interim looks.** Partial results were seen before the final run: code tests on a 12-market scratch database (whose candle robustness table already covered all markets) and on 252/287-market snapshots, and the interim report at 64% coverage. No criterion, definition or analysis choice changed after that. The changes were presentation only (Brier for every blend, an all-days table, labels in the disagreement table) and a window-clustered standard error in the disagreement table, which had mixed per-window means with per-observation means.
- **One regime.** This is one month (2026-09-29 to 2026-10-28) of one market.

## Data and runtime

- Kalshi public API, unauthenticated reads only. Results come from `GET /markets` (series KXBTC15M, status settled; `expiration_value` arrives as a string and commas are stripped). Prints come from `GET /markets/trades` (ticker, min_ts/max_ts, cursor, limit 1000; `yes_price_dollars`, `count_fp`, `taker_outcome_side`). Top of book comes from `GET /markets/candlesticks` (batch, 1-minute; capped at 10,000 candles per call). `GET /historical/cutoff` gave trades_created_ts = 2026-08-29, before the period, so every print came from the live endpoint.
- Throttling: the trades endpoint returned HTTP 429 above about 4.5 requests/s per IP regardless of page size, while `/markets` was unthrottled at 8/s. The fetcher averaged 3.6 to 3.8 requests/s with HTTP 429 on under 0.3% of requests.
- Volume: prints averaged about 39,400 per market on 22 probe markets, so about 112M in the period. Stored: 14.8M prints from 435 full tapes plus 18.6M panel prints from 2,443 direct panel fetches, in `data/research/kalshi_trades.db` (views `trades_v` and `panel_trades_v` expose ticker, ts_ms, yes_price, count, taker_side). There were no block trades. The app's copy of results, strikes and settlement values matched Kalshi's on all 2,848 markets. 23 windows have no open or close average under our index coverage rule.
- Runtime: about 3.5 hours of wall clock, nearly all of it API throttling. Panel fetch 63 + 26 min, full tapes 76 min (time box), candles 0.5 min, analysis 1.5 min.
- Reproduce: `python scripts/model_vs_market.py fetch-markets`, `fetch-candles`, `fetch-panel`, `fetch-full --full-until <UTC time>`, `fetch-panel --panel-missing`, `analyze`.
