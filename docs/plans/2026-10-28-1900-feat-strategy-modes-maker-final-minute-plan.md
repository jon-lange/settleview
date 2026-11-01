---
title: Strategy Modes (Taker, Market Maker, Final Minute) with a Concurrent Shadow Lab - Plan
type: feat
date: 2026-10-28
artifact_contract: ce-unified-plan/v1
product_contract_source: ce-plan-bootstrap
execution: code
---

# Strategy Modes (Taker, Market Maker, Final Minute) with a Concurrent Shadow Lab - Plan

## Goal Capsule

- **Objective:** Turn the single Coach taker strategy into pluggable strategy MODES — TAKER (today's Coach), MARKET MAKER, FINAL MINUTE — run all three side by side in shadow on the same real prices with isolated simulated accounts and scorecards, and promote a mode to real pennies only when it clears a bar written down before its data exists. Log per-window context next to each mode's outcome so a mode selector (rule-based now, AI later) can be trained and judged.
- **Means:** A pure `Strategy` contract in `domain/strategy/` returning intents; taker and final-minute intents become brackets through the existing `TradingService`/`OrderGate`; maker intents go through a new `QuoteEngine` that shares the gateways, kill switch, readiness and limits. A `StrategyLab` runs every mode on its own `ShadowExchange` at once. Per-mode tables, scorecards, promotion bars, a Modes panel.
- **Authority:** The trading plan (`2026-10-28-1130-feat-kalshi-human-confirmed-trading-plan.md`) keeps authority on everything it settles (R1–R16, KTD1–KTD6). This plan adds R17–R30 and KTD7–KTD16. Where it touches a settled decision (R12 stop on every entry, KTD2 ">2 minutes left", R15 "each Coach proposal"), the change is marked **needs user approval** and is not built until approved.
- **Stop conditions:** Stop and ask before: any order sent outside a human-started, bounded session; raising any limit default; a maker quote that is not post_only; a second mode live at the same time; changing a promotion bar after its data exists; or if Kalshi's amend/decrease/batch-cancel/order-group endpoints, rate limits or KXBTC15M maker fees differ from what is assumed here.
- **Execution profile:** Builds on the trading plan's U1–U8 (shipped) and the WebSocket feeds (KTD6). No implementation starts until the user approves this plan and answers the blocking open questions.
- **Who finishes:** `ce-work` with parallel workers per the unit table, after explicit approval.

## Product Contract

### Summary

Three strategy modes share one market snapshot, one execution path and one set of hard limits. All three run in shadow at once, each with an isolated simulated account, so they can be compared on identical prices. Each mode has its own on/off, budget, loss limit, P&L and scorecard. At most one mode trades real money at a time, inside an auto session (R15) whose rules name that mode, and only after that mode passes its own pre-registered promotion bar.

### Problem Frame

The evidence says our current way of trading is the losing side: takers in Kalshi crypto lose about 1.3% per trade while makers earn about 1.3% (Becker, all Kalshi trades); a careful backtest of a model like ours lost to the market price; our first fresh-price samples agree. Two structurally different ideas may still work: **market making** (earn the spread, pay no fee; the enemy is being picked off — a paper study saw one-sided resting fills win 2%), and **Final Minute** (inside the last 60 s part of the settlement average is already known; our fair value prices that exactly via `fair_value.prob_up` with `observed`, and fees are small near the extremes; nobody has tested it publicly). We need to test all three honestly on the same prices before risking a cent, and record enough context per window for a selector to learn when each mode works.

### Requirements

**Modes**
- R17. A mode is a pure decision function: one market snapshot plus the mode's own account state in, intents out (place quote, amend, cancel, cancel all, taker bracket). Modes never call the exchange.
- R18. TAKER: today's Coach rules (`rules.entry_decision`, blend 0.5, longshot floor, 10 s confirmation, `entry_limit`, `exit_advice`) wrapped without behavior change. The Coach screen keeps working exactly as today.
- R19. MARKET MAKER: post_only two-sided quotes around a reference price with spread, size, inventory skew and limits; re-quotes on book, index or fair moves; pulls quotes on fast index moves, stale feeds and near expiry; flattens or caps inventory before the close; stays inside the Kalshi rate budget.
- R20. FINAL MINUTE: trades only from T−90 s to T−5 s (defaults), using fair value with the known part of the settlement average against Kalshi's strike, widened for the error between our index proxy and Kalshi's index. Taker IoC entries; hold to settlement.

**Shadow lab**
- R21. All modes run concurrently in shadow on the same real prices; each has its own `ShadowExchange` (positions, orders, cash), budget, loss limit and P&L. One mode's fills never touch another's account.
- R22. Per-mode scorecard: P&L after fees, trades, win rate, fill rate, adverse selection (mid move 5/30/60 s after our fill), inventory, max drawdown, P&L split into spread vs settlement, and a running significance test clustered by window.
- R23. A promotion bar per mode, written in this plan before its data exists ("Promotion bars"). Not editable after the mode's first lab day without the user's typed approval, recorded in the audit.
- R24. Everything persisted and keyed by mode; survives restarts. The lab resumes after a restart (it moves no money); a live session never resumes (R15 unchanged).

**Live**
- R25. At most one mode live at a time (recommended; open question 4). A live mode runs only inside an auto session (R15) whose rules name the mode, budget, loss limit and duration. Production needs that mode's own promotion and the FULL gate, or the live-test override within its caps. A mode with no promotion can never go live.
- R26. **(Needs user approval.)** A MAKER auto session's typed acknowledgment authorizes its resting post_only quotes, cancels and amends within the stated limits without a per-order Yes. Its rules sentence says so in plain English.
- R27. **(Needs user approval.)** A FINAL MINUTE bracket is "hold to settlement": no take profit, no stop (deviates from R12), and may be entered with under 2 minutes left (deviates from KTD2), never with 5 s or less left.

**Selector readiness**
- R28. Per window, log features (realized vol, implied vol from the mid, spread, depth, time of day, BTC move since open, distance to strike in σ units, recent fill toxicity) next to each mode's outcome.
- R29. A `ModeSelector` port with a rule-based default whose pick (and counterfactual) is logged every window. An AI selector plugs in later behind the same gates: it may only choose among promoted modes and can never raise a limit.

**Cockpit**
- R30. A Modes panel: per mode lab on/off, status line, scorecard, promotion progress, live eligibility. Coach and Trading views unchanged.

### Key Decisions

- **Maker reference price = market mid; our fair value is a guard, not an anchor** (R19). Kalshi's price is at least as good as our fair value; a maker earns the spread by being neutral. Fair value is used where it helps — our index can see Bitcoin moves before Kalshi's book: (1) pull a side when the fair change implied by an index move exceeds half our spread; (2) never quote a side worse than blended fair by more than the guard margin. Optional tilt `fair_weight` (default 0.0, max 1¢) is raised only if model-vs-market concludes "MODEL ADDS INFORMATION".
- **Final Minute is a taker** (R20). Its edge decays within seconds; a resting bid would fill mainly when wrong. The fee is smallest where it trades (0.63¢ at 90¢, 0.33¢ at 95¢). A maker variant can be a later fourth lab mode.
- **One live mode at a time** (R25). Kalshi nets positions per market; two live modes on one ticker would share a position and break per-mode P&L and reconciliation.
- **The lab runs unattended in shadow (needs user approval).** It moves no money; a lab that only runs while someone watches would take months to reach 100 windows. Lab accounts are separate from the human's shadow mode and labeled "lab" everywhere.

### Scope Boundaries

No AI/ML selector in this build (port + rule default + feature log only). No concurrent live modes. No maker on series other than KXBTC15M. No market orders; maker quotes post_only only. No standing auto (R16 stays deferred). No subaccounts (noted as a later way to isolate live modes).

## Planning Contract

### Key Technical Decisions

- **KTD7. Strategy contract (pure)** — `domain/strategy/types.py`. `MarketState` (frozen): `at`, `window_end`, `ticker`, `quote` (yes/no bid/ask, sizes, top 5 levels, age_ms, source ws|rest), `index` (S, σ per second, last N per-second prices, age_ms), `fair` (fair_up raw, blended, known part of the settlement average m/A, `kalshi_strike`, `open_avg`), `engine` (call, lean), `feeds_ok`. `ModeAccount`: position (net YES contracts), resting orders (id, side, price, remaining, placed_at), cash, realized/marked P&L, budget left, loss left, last fills. Intents: `Place(side, action, price, count, post_only, tif, tag)`, `Amend(order_ref, price, count)`, `Cancel(order_ref)`, `CancelAll(reason)`, `TakerBracket(side, stake, max_price, take_profit|None, stop|None, hold)`. `Strategy` Protocol: `name`, `decide(state, account) -> list[Intent]`, `explain(state, account) -> str`.
- **KTD8. Execution split.** `TakerBracket` → `TradingService._prepare` → `_approve_locked` (same token and OrderGate path as auto sessions). Quote intents → `QuoteEngine`, which runs a `QuoteGate` (mode ready, kill off, series, market open, > `stop_quoting_ms` left, post_only, price 1–99¢, inventory and resting-notional limits, mode budget/loss limit, rate budget), persists each order in `strategy_orders` before sending, reconciles by client_order_id through the order list (never the single-order GET), and treats cancel responses as authoritative. `TradingService` learns quote orders via an injected `known_order_ids_fn` so they never count as "external orders".
- **KTD9. Per-mode isolation via a `strategy` column.** Migration 0009 adds `strategy` (default 'taker') to `trading_brackets`, `trading_orders`, `trading_audit`, `trading_sessions`. `TradingService(…, strategy=…)` filters reads and stamps writes; settings keys prefixed by strategy except the global `killed`. Each bracket-trading lab mode gets its own `TradingService(strategy=…, shadow, lab=True)` on its own shadow gateway; the human-facing service stays `strategy="taker"`. The kill switch is global: every lab service gets `paused_fn = main.killed() or coach.paused`.
- **KTD10. Shared book, separate shadow exchanges.** A `BookCache` (adapters) fed by the production market-data WebSocket (`orderbook_delta` plus the public `trade` channel), REST orderbook fallback. Every lab `ShadowExchange(book_fn=cache.book, trades_fn=cache.trades_since)` reads the same prices. Quotes older than 2 s are never used to decide.
- **KTD11. Shadow maker fill model.** (1) Fills from the trade feed with a queue estimate: on joining a level, `queue_ahead` = size shown there; prints at our price on our side reduce the queue first, then fill us; cancellations at our level reduce the queue only from behind us; prints through our price fill us up to their size. (2) Latency: new orders live after `place_latency_ms` (150), cancels/amends effective after `cancel_latency_ms` (150); prints in between fill the old order. (3) post_only checked when the order goes live. (4) The old "book trades through" rule stays as fallback when the trade feed is down, flagged `fill_model=book`. (5) Amend, decrease, batch-cancel routes; `expiration_time` if V2 supports it (verify). (6) Settlement pays 1/0 into the account cash from Kalshi's published result. (7) Maker fee `maker_fee_rate` (default 0; verify KXBTC15M), taker `kalshi_fee`. (8) Each fill records the mid at fill and 5/30/60 s later for markout.
- **KTD12. Kalshi API for the maker** (docs.kalshi.com, verify in sandbox). Amend `POST /portfolio/events/orders/{id}/amend` (price change or size increase → back of queue; decrease keeps priority; atomic). Decrease `POST /portfolio/events/orders/{id}/decrease`. Batch cancel for cancel-all. Order groups: a contracts limit over a rolling 15 s window that cancels the group's resting orders and blocks new ones until reset — every maker quote goes in one group per market, limit = 2 × quote size, a server-side kill-on-burst. Rate limits: Basic tier ~100 write tokens/s (~10 creates/amends per s); the maker self-limits to ≤ 4 writes/s and ≤ 1 re-quote per side per 1,000 ms (cancel-all exempt). Only these paths are added to the `TradingAuth` allow-list. `cancel_order_on_pause: true` for maker quotes. Crypto is on shard 2: verify routing in sandbox.
- **KTD13. Maker parameters** (`MakerParams`, recorded per lab day). Reference r = mid − inv_skew·q + tilt, tilt = clip(fair_weight·(fair_blended − mid), ±1¢), fair_weight 0. Quotes bid = floor(r − h), ask = ceil(r + h), h = max(1¢, spread_mult·σ_p·√τ_q), σ_p = mid volatility per second from the model's delta, τ_q = 5 s; never improve the touch by more than 1¢, never cross; at a 1¢ market spread, join both touches. Size 1 contract in the lab. inv_skew 1¢ per 2 contracts; |q| ≤ `max_inventory` (5) — at the limit stop quoting the adding side; a resting-notional cap. Pick-off guard: CancelAll when |ΔS| over 1,000 ms implies |ΔP| ≥ h; re-quote after a 2,000 ms quiet cooldown. Stale feeds: CancelAll when book age > 1,500 ms, index age > 2,000 ms, seq gap, disconnect or clock-skew flag. Expiry: no new inventory from T−180 s; T−180 to T−120 s quote only the reducing side; at T−120 s sell |q| above `max_settle_inventory` (2) with a reduce_only IoC at bid − 2¢; the rest is held to settlement and reported as settlement P&L. Re-quote on: top of book change, |Δr| ≥ 1¢, a fill, or a quote older than 10 s and off the touch.
- **KTD14. Final Minute rule** (`FinalMinuteParams`). Active T−90 s to T−5 s. p = Φ((A + n·S − 60·K) / √(σ²V + 60²·σ_ε²)), K = Kalshi's strike (`floor_strike`), σ_ε = proxy-vs-Kalshi index error; v1 uses σ_ε,avg ≈ $5.0 (calibration research: |p90| basis $8.19), re-estimated nightly from settled windows with Kalshi's expiration value (verify availability; the model-vs-market study measures it). Entry: ask ≥ 25¢ (floor kept), ask ≤ 97¢, p − ask − fee ≥ 3¢, and |A + n·S − 60·K|/60 ≥ 2·σ_ε,avg so proxy noise alone never decides; holds two consecutive seconds. Limit = ask + up to 1¢ while the edge clears; one entry per window. Exit: hold to settlement (no stop — final-minute books are thin). No entry if the index is older than 1.5 s or a closing second is missing more than 2 s.
- **KTD15. Statistics.** Unit of evidence = the window. Per mode: P&L per window (settlement included), window-clustered t, day-block bootstrap 95% CI. The live running test is labeled informational; promotion is judged only at pre-registered checkpoints of 100, 200 and 400 active windows with critical t 3.0 / 2.6 / 2.2 (alpha spending) and Holm across the 3 modes. Adverse selection = average 30 s markout per fill, signed for our side.
- **KTD16. Selector port.** `ModeSelector.select(features, eligible) -> {mode|None, reason, scores}` in `application/ports.py`. `RuleSelector`: no maker when realized vol is above its 7-day p90 or the spread is 1¢ with depth under 20; final minute only when |distance to strike|/(σ·√τ) ≥ 1.5 at T−90 s; otherwise taker only if promoted; else none. Each window logs the pick and its counterfactual (the chosen mode's lab P&L).

### Implementation Units

| U-ID | Title | Exclusive files | Depends on |
|---|---|---|---|
| P1 | Persistence: migration 0009, tables | `adapters/sqlite/tables.py`, `adapters/sqlite/migrations/versions/0009_strategy_modes.py`, `tests/storage/test_strategy_tables.py` | — |
| U1 | Strategy contract, intents, account, ports | `domain/strategy/__init__.py`, `domain/strategy/types.py`, `application/ports.py` (adds `ModeSelector`, `QuotingGateway`), `tests/strategy_modes/test_types.py` | — |
| U2 | Maker quoting logic (pure) | `domain/strategy/maker.py`, `tests/strategy_modes/test_maker.py` | U1 |
| U3 | Final Minute logic (pure), odds with index error, implied vol | `domain/strategy/final_minute.py`, `domain/strategy/vol.py`, `domain/coach/fair_value.py` (optional `index_sd=0.0`, backward-compatible), `tests/strategy_modes/test_final_minute.py`, `tests/coach/test_fair_value.py` (new cases) | U1 |
| U4 | Taker mode wrapper (pure) | `domain/strategy/taker.py`, `tests/strategy_modes/test_taker.py` | U1 |
| U5 | Shadow exchange for makers, book cache, public trades | `adapters/kalshi/shadow.py`, `adapters/kalshi/book_cache.py`, `adapters/kalshi/stream.py` (trade channel), `tests/kalshi/test_shadow_maker.py`, `tests/kalshi/test_book_cache.py` | U1 |
| U6 | Trading adapter: amend, decrease, batch cancel, order groups, expiration | `adapters/kalshi/trading.py`, `tests/trading/test_adapter_maker.py` | U1 |
| U7 | TradingService per strategy: scoping, hold brackets, per-mode gate profile, known quote orders, per-mode production gate | `application/trading.py`, `tests/trading/test_strategy_scope.py`, `tests/trading/test_hold_bracket.py` | P1 |
| U8 | QuoteEngine: maker execution, QuoteGate, rate limiter, reconcile, cancel-all, inventory ledger | `application/quoting.py`, `tests/trading/test_quoting.py` | P1, U1, U5, U6 |
| U9 | StrategyLab runner: snapshot, per-mode accounts, intent routing, settlement, lab sessions | `application/strategy_lab.py`, `tests/strategy_modes/test_lab.py` | U1–U4, U7, U8 |
| U10 | Scorecards and promotion bars (pure stats) | `domain/strategy/scorecard.py`, `domain/strategy/promotion.py`, `tests/strategy_modes/test_scorecard.py`, `tests/strategy_modes/test_promotion.py` | U1 |
| U11 | Window features and RuleSelector | `domain/strategy/features.py`, `domain/strategy/selector.py`, `application/mode_features.py`, `tests/strategy_modes/test_features_selector.py` | U1, P1 |
| U12 | Bootstrap wiring and settings | `bootstrap/app.py`, `bootstrap/config.py`, `tests/test_config.py` (new cases) | U5–U11 |
| U13 | HTTP routes for modes | `adapters/http/strategy_routes.py`, `adapters/http/trading_routes.py` (strategy on auto start), `tests/api/test_strategy_routes.py` | U9, U10 |
| U14 | Cockpit Modes panel | `web/views/modes.js`, `web/index.html` (one tab and section), `web/app.js` (register view) | U13 |
| U15 | Final Minute historical check (read-only script) | `scripts/backtest_final_minute.py`, `docs/research/2026-10-2x-final-minute-backtest.md` | U3 |

**Waves.** 0: P1, U1. 1 (parallel): U2, U3, U4, U5, U6, U10, U11, U15. 2: U7, U8, then U9. 3: U12, U13, U14. 4: lab burn-in; tiny live tests only after a mode's bar passes.

#### Unit notes

- **P1.** Tables `strategy_orders`, `strategy_fills` (with mid at fill and +5/30/60 s, fill_model), `strategy_accounts` (kind lab|live, params JSON, budget, loss limit, cash, end reason), `strategy_windows` (pnl, spread_pnl, settle_pnl, fees, inventory at close, markout; PK account_id+boundary), `strategy_settings`, `mode_window_features` (features JSON, selector pick/reason). `strategy` column (default 'taker') on the four `trading_*` tables with an index on (strategy, state). Test upgrade from 0008 with rows, and downgrade.
- **U2.** `maker_intents(state, account, params)`, `reservation()`, `quote_prices()`; inventory skew, per-side kill at the limit, pick-off guard via `vol.delta`, stale-feed CancelAll, expiry schedule and flatten IoC; 1,000 ms hysteresis except cancels. Table tests: 1¢ spread (join), 6¢ spread (improve 1¢), q=+5, $40 jump in 800 ms, book age 1.6 s, T−150 s with q=+3, T−119 s with q=+4, never crossing.
- **U3.** `prob_up(..., index_sd=0.0)` adds 3600·index_sd² to the variance (old vectors unchanged); `final_minute_intents`; `vol.implied_sigma` (bisection) and `vol.delta`. Tests: known-average cases, proxy-error margin blocking a 0.6σ_ε call, floor kept, T−5 s cutoff, one entry per window, 2 s confirmation.
- **U4.** Maps `MarketState` + Coach `entry_decision`/`Confirmation` to `TakerBracket` (TP fair − 2¢, stop as today). Parity test with `CoachService._maybe_enter` on recorded states.
- **U5.** `ShadowExchange(trades_fn, place_latency_ms, cancel_latency_ms, maker_fee_rate, settle_fn)`, queue model, amend/decrease/batch-cancel/order-group routes, cash and settlement; `BookCache` fed by a public `KalshiStream` with `orderbook_delta` and `trade`; stream emits `{"type": "trade", ticker, yes_price, count, taker_book_side, ts_ms}`. Scripted tests: queue exhaustion before fill, print-through fills, cancel 100 ms before a print still fills under 150 ms latency, post_only at live time, amend vs decrease priority, order-group burst, settlement credit, isolation of two exchanges on one cache.
- **U6.** `amend`, `decrease`, `batch_cancel`, `create_order_group`/`reset_order_group`, `place_limit(..., order_group_id, expiration_ts, cancel_on_pause)`; allow-list entries for exactly these paths. Tests: request shapes vs the openapi docs, allow-list refusals, 409/404 on amend.
- **U7.** `strategy` and `lab` constructor args; every select filtered and row stamped; `kind="hold"` tickets (no TP/stop; gate 5b keeps the spread check); per-ticket `min_left_ms` from a strategy profile (floor 5 s); `known_order_ids_fn`; `gate_fns[strategy]`; lab services skip presence only when lab and shadow (needs approval). Existing `tests/trading/*` pass unchanged.
- **U8.** `QuoteEngine.apply/on_stream_event/tick/cancel_all`, inventory ledger, token bucket, audited `QuoteGate` refusals. Tests: refusals send nothing; stale feed → cancel-all; limiter defers, never drops, cancels; lost create reconciled via list; cancel authoritative; live restart cancels quotes and never resumes; inventory matches positions.
- **U9.** `StrategyLab`: builds `MarketState` per tick from the Coach evaluation, BookCache quote, index rows and `kalshi_strike`; routes intents per mode; settles windows into `strategy_windows`; per-mode lab budget and loss limit; survives restarts. Tests: three modes on one scripted path stay isolated; main kill halts all; one mode's exhaustion doesn't stop others; spread vs settlement P&L split.
- **U10.** `scorecard(...)` and `promotion(mode, card, checkpoint)` implementing the bars below; Holm across modes. Tests on synthetic window series with known t and drawdown.
- **U11.** `window_features(...)` (realized vol 5/15/60 min, implied vol at T−10/−5/−1 min, spread and depth, UTC hour/weekday, move since open in $ and σ, distance to strike in σ√τ, toxicity = last 20 maker fills' 30 s markout); `RuleSelector`; writer for `mode_window_features`.
- **U12.** Settings `BTCFLOW_MODES_LAB` (on, shadow only), enabled modes, `lab_budget_per_mode` ($100 virtual), `lab_loss_limit_per_mode` ($25 virtual/day), param overrides, shadow latencies. One `BookCache`, three `ShadowExchange`s; `trade` channel on the market stream; `strategy_loop` (0.5 s) calls `lab.tick`. A live maker uses `QuoteEngine` on the env gateway when an auto session with strategy=maker starts.
- **U13.** `GET /api/modes`, `POST /api/modes/{m}/lab`, `GET /api/modes/{m}/scorecard`, `GET /api/modes/{m}/windows`, `GET /api/modes/selector`; auto start takes `strategy` with a per-mode rules summary.
- **U14.** Modes tab: three cards (status, lab on/off, P&L after fees, windows, fill rate, markout, drawdown, t and checkpoint progress, promotion checks, live eligible), selector line, "same prices, separate accounts" note. Coach and Trading tabs untouched. Checked with ce-test-browser.
- **U15.** Read-only replay of Final Minute on public trade prints in [T−90 s, T−5 s] over the model-vs-market period, scored on Kalshi's result, with pre-registered criteria copied from the bars below. An early independent read before weeks of shadow.

## Promotion bars (pre-registered; frozen when this plan is approved)

Two steps to real pennies: **calibration live** (1-contract orders, ≤ $5 total, $2 loss limit, live-test override caps, inside an auto session) then **promoted live** (the user's normal auto limits). Both need every lab check below first.

- **TAKER:** the existing go-live gate plus window-clustered t ≥ the checkpoint's critical t (Holm) and positive in both chronological halves. If model-vs-market returns SIT OUT, taker stays lab-only regardless.
- **MARKET MAKER** (≥ 100 windows with a fill, ≥ 300 fills): net after fees > 0 with t ≥ critical t (Holm); positive in both halves; average 30 s markout > −(average half-spread captured) with day-bootstrap CI lower end > 0; settlement P&L of one-sided-fill windows reported and ≤ 50% of spread P&L in magnitude; max drawdown ≤ 40% of the loss limit; zero unreconciled orders, zero quotes resting past a cancel-all, zero 429 bursts. Calibration live also compares live fill rate and markout to the lab over the same hours (each within 50%); clearly more toxic live fills mean the shadow model is wrong — stop and fix it first.
- **FINAL MINUTE** (≥ 100 windows traded): net after fees > 0, t ≥ critical t (Holm), positive in both halves; realized win rate ≥ average entry price + fee on the window Wilson lower bound; zero losses where the margin exceeded 3·σ_ε,avg (else re-estimate σ_ε and restart the count); U15 agrees in sign.

Bars may be tightened by the user at any time and loosened only with typed approval, audited.

## Verification Contract

| Gate | Check |
|---|---|
| Unit tests | Every QuoteGate and OrderGate refusal sends nothing; every pure mode table-tested; existing `tests/trading` unchanged |
| Architecture | `lint-imports` passes; `domain/strategy` imports no I/O; `QuotingGateway` and `ModeSelector` are ports |
| No mocks in the app | Lab accounts are real simulations on real prices, labeled "lab (simulated fills on real prices)"; no demo or fake series |
| Shadow fidelity | U5 queue/latency/order-group tests pass; `fill_model` recorded per fill; cockpit shows the fallback-model share |
| Sandbox | Maker amend, decrease, batch cancel and order group verified on Kalshi demo before any production key |
| Penny prod | Each new order type (amend, decrease, batch cancel, order group) penny-tested in production before a mode uses it live |
| Burn-in | 48 h lab run: no crash, no orphan quotes after restart, three scorecards updating |
| Manual live | Per mode, one calibration-live session with the user present; kill switch pulls quotes within one second |

## Risks

- Shadow over-fills makers; calibration live exists to catch it; fallback-model fills are excluded from promotion statistics.
- Pick-off by faster players; the guard reduces but may not remove it; markout is the truth.
- Inventory held to settlement is directional; reported separately and capped.
- Index proxy error in Final Minute; the 3σ_ε loss check stops it.
- Multiple testing and peeking; checkpoints, Holm and frozen bars.
- Endpoint quirks (single-order GET staleness, 404 after writes, amend on partial fills); reconcile by list + client_order_id; cancel responses authoritative.
- Rate limits; token bucket plus the order-group server-side kill.
- Scope creep in `trading.py`; U7 limited to scoping and the hold kind; maker logic lives in `quoting.py`.

## Resolved decisions (user, 2026-10-28)

- session-settled: R26 approved — a maker auto session's typed acknowledgment authorizes its quotes, amends and cancels within its limits (kill switch and order-group burst limit still apply).
- session-settled: R27 approved — Final Minute holds to settlement with no stop and may enter down to T−5 s (this mode only).
- session-settled: the shadow lab runs unattended 24/7 (shadow only; no money).
- session-settled: one live mode at a time.
- Defaults taken for questions 5–9 until the user says otherwise: the promotion bars as written; Basic API tier assumed (verify); maker inventory into the close 2 contracts; fair tilt 0 (a 0.25 variant may be added later as a fourth lab mode); lab budgets $100 per mode and $25/day virtual. Question 10 (AI selector provider and bar) is deferred to the selector build.

## Open Questions (original list; 1–4 resolved above)

1. (Blocking for live maker) May a maker auto session's typed rules authorize resting quotes, amends and cancels without a per-order Yes (R26)?
2. (Blocking for Final Minute) May Final Minute brackets hold to settlement with no stop (deviates from R12) and enter after the 2-minute cutoff, down to T−5 s (deviates from KTD2)?
3. (Blocking for the lab) May the shadow lab run unattended 24/7, with no human present (deviates from R14 for shadow only)?
4. One live mode at a time (recommended), or concurrent live modes with per-mode budgets?
5. Promotion bar numbers (checkpoints 100/200/400 windows, critical t 3.0/2.6/2.2, maker ≥ 300 fills, calibration-live $5 cap and $2 loss limit)?
6. Kalshi API tier (sets the maker's write budget)?
7. Maker inventory allowed into the close: 2 contracts by default, or always flat?
8. Maker fair-value tilt: 0 until model-vs-market says "adds information", or a 0.25 variant as a fourth lab mode?
9. Lab virtual budgets: $100 per mode and a $25 daily loss limit?
10. Later AI selector: which provider/model, and must it beat the best single mode out of sample in the lab before choosing live?

## Definition of Done

- Three modes run concurrently in the shadow lab on the same real prices with isolated accounts, persisted scorecards and promotion progress in the Modes panel.
- No order of any mode can be sent outside a gate (OrderGate for brackets, QuoteGate for quotes); the kill switch halts every mode and pulls every quote.
- Per-window features and selector picks logged for every window.
- The live path for a promoted mode verified on Kalshi demo, penny-tested in production, then one calibration-live session with the user present.
- Existing Coach and Trading screens and their tests unchanged.
