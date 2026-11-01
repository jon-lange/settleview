# LLM paper-trader autoresearch loop

**Goal:** find trader instructions under which an LLM (blind to outcomes) beats Kalshi's price after fees on KXBTC15M.

**Protocol (fixed):** blind snapshots at 8:00 left (BTC path vs strike, 1-min vol, Kalshi asks, last 4 outcomes); answer keys kept outside the trader's readable folder; entry at seen ask + 1¢, exact fee, hold to settlement; Brier vs market mid on every window. Pools by day: tune = days 1–15, validation = days 16–20, sealed test = days 21–30 minus already-used windows (and, preferably, live shadow data after Oct 28). Each round: incumbent and challenger decide the SAME fresh tune windows; a challenger is kept only if it earns more and forecasts no worse, then must also beat the incumbent on validation. New rules come from post-mortems of losing trades on TUNE windows only; a rule is proposed only for a recurring pattern.

Scripts: `scripts/quick/llm_blind/` (build_pool.py, score_tag.py). Instructions: `research/llm_trader/prompts/`.

## Rounds

| Round | Windows | Version | Trades | Net ¢/contract | t | Brier (market) | Decision |
|---|---|---|---|---|---|---|---|
| baseline | 200 holdout (days 21–30) | ad-hoc | 55 | +1.4 | 0.25 | 0.1674 (0.1650) | — |
| r01 | 100 tune | v0 | 31 | +18.0 | 2.63 | 0.1495 (0.1539) | incumbent |
| r01 | same | v1 (market-anchored) | 1 | — | — | 0.1528 (0.1539) | dropped: almost never trades |
| v01 | 100 validation | v0 | 27 | **−10.8** | −1.44 | 0.1731 (0.1619) | r01 result did not validate |
| r02 | 100 tune (fresh) | v0 | 17 | +6.6 | 0.86 | 0.1291 (0.1324) | — |
| r02 | same | **v2** (v0 + rule L1) | 23 | **+13.5** | 1.88 | 0.1267 (0.1324) | won tune |
| v01 | 100 validation | v2 | 19 | **−18.0** | −1.70 | 0.1733 (0.1619) | **REJECTED**: worse than v0 (−10.8) on validation |

## Rules from loss post-mortems

- **L1 (from r01, 6 of 8 losses):** don't fade a fresh lead on inflated volatility — the 1-min RMS was inflated by the very jump that created the lead, making 2–25¢ long shots look cheap. Use calmer-minute volatility; buy a cheap side only if the lead is small vs calm vol and the path is already turning back. Status: won r02 tune (+13.5¢ vs +6.6¢) but **failed validation** (−18.0¢ vs v0 −10.8¢) — rejected.

## Observations

- The LLM independently rebuilds a driftless random walk from the snapshot (same as our model).
- LLM beat the market's forecast in days 1–15 samples but lost to it in days 16–30. Market Brier at 8 min: 0.162 (days 1–10), 0.164 (11–20), 0.172 (21–30) with unchanged spreads — late October was harder for everyone, not a market that got smarter. Tuning on early days risks fitting a different regime; prefer recent windows and live shadow data for the final check.

- After r02: every version beats the market's forecast on days 1–15 samples and loses on days 16–30, and the one rule that won tuning failed validation. Tuning on early days does not transfer. Next: re-split to recent days (tune 16–23, validate 24–27, sealed 28–30 + live shadow), and give the snapshot sub-minute detail (last 60 s) so lag-seeking instructions have something to see.
