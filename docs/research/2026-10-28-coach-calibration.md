# Coach fair-odds calibration — 2026-10-28

**Question:** when the Coach says "our odds Up: X%", do windows go Up about X% of the time?

**Data:** 2,825 settled Kalshi KXBTC15M markets (2026-09-29 → 2026-10-28) with real Kalshi results, and our per-second Coinbase index rebuilt from 12.0M real trades. The model is the corrected one (1-minute-return volatility, 10-second price smoothing). Reproduce with `python scripts/calibrate_coach.py`.

| Minutes before close | Brier score | Coin-flip baseline | Skill |
|---|---|---|---|
| 12 | 0.209 | 0.250 | +16% |
| 8 | 0.168 | 0.250 | +33% |
| 5 | 0.135 | 0.250 | +46% |
| 3 | 0.100 | 0.250 | +60% |
| 1 | 0.044 | 0.250 | +82% |

Reliability is tight at every checkpoint. For example, at 5 minutes the 70–80% bucket went Up 79% (n=232) and the 80–90% bucket 85% (n=264).

**Basis:** our open average minus Kalshi's strike has a median of −$1.82, and |p90| is $8.19. Coinbase is a good proxy for Kalshi's index at the window open.

**Findings:**
- The odds are honest (calibrated). This says nothing yet about beating Kalshi's prices, which are probably also calibrated. Edge exists only where Kalshi's price departs from honest odds by more than fees. That is measured by the Coach's paper record and the threshold sweep on recorded Kalshi quotes, which have only been collected since 2026-10-28.
- Fat tails: 12 minutes out, the model's 0–10% bucket averages 7% but went Up 14% (n=51). BTC jumps more than a Brownian path assumes. This is a candidate refinement (Student-t or a jump term) once there are more samples.
