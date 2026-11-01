---
title: Kalshi Human-Confirmed Trading - Plan
type: feat
date: 2026-10-28
artifact_contract: ce-unified-plan/v1
product_contract_source: ce-plan-bootstrap
execution: code
---

# Kalshi Human-Confirmed Trading - Plan

## Goal Capsule

- **Objective:** The app automates everything except judgment: it finds setups, sizes them, places and manages orders, and watches positions; the human makes every decision with a one-tap Yes/No while present at the screen. A stop-loss the human approved with the entry is executed by the app (Kalshi offers no stop orders). The engine never opens a position on its own.
- **Means:** An order-ticket flow on the 15m card that writes through a separate, opt-in Kalshi trading client guarded by hard limits (KTD1–KTD4).
- **Authority:** Product Contract requirements win on behavior; KTDs win on mechanism.
- **Stop conditions:** Stop and ask before any change that would let an order be sent without a fresh per-order human confirmation, before raising any limit default, or if Kalshi's order API or fee schedule differs from what this plan assumes.
- **Execution profile:** Builds on the BTC Flow Multi-Horizon Signal Engine (`docs/plans/2026-10-28-0955-feat-multi-horizon-signal-engine-plan.md`), specifically the read-only account link (U19) and live Kalshi quotes (U18). **No implementation starts until the user approves this plan.**
- **Who finishes:** `ce-work` or a human implementer after explicit approval.

---

## Product Contract

### Summary

Add a human-in-the-loop order ticket for Kalshi KXBTC15M markets. The engine proposes side, contracts and a limit price; the user confirms every order. Hard per-order, per-window and per-day limits, a kill switch, and a full audit log bound the risk.

### Problem Frame

The engine produces deterministic UP/DOWN/SKIP calls in real time, and the user wants to learn by acting on them with real positions instead of only comparing after the fact. Doing that by hand on kalshi.com is slow: by the time the user finds the market and enters an order, the price has moved. Trading also moves real money, so the design must make it impossible for a bug, a stale screen or a mis-click to trade without the user's deliberate, informed confirmation.

### Requirements

**Approvals (human decides, app executes)**
- R11. Every entry and every discretionary exit is a one-tap Yes/No card that states the action in plain words and dollars ("Buy $25 of Up at 58c or less? Our odds 66%. Stop at 43%, take profit near 80c."); Yes places the order immediately, No or no answer places nothing. Cards expire after 10 seconds or when the price moves past the limit.
- R12. Saying Yes to an entry approves its whole bracket, shown on the card in plain dollars: the entry, a take-profit price and a stop-loss. After the buy fills, the app places the take-profit as a resting sell limit order on Kalshi (a maker order: it fills even if the app is interrupted, and maker fills paid no fee on the user's real trades). The app watches the stop every second and, when it triggers, sells immediately (marketable limit at the bid, within a configured slippage cap). Whichever exit happens first, the app cancels the other. These bracket orders are the only orders sent without a fresh tap; changing either exit requires a new Yes.
- R13. Other exits the Coach suggests (value exits before the take-profit price, the last-minute time rule) are offered as Yes/No cards; an unanswered card keeps the position under its approved bracket.
- R14. New entries are offered only while a human is present: the Coach tab is open and visible and the user interacted within the last few minutes (configurable). Without presence the app offers nothing new; open positions keep their approved stop. Decisions needing attention raise a visible and audible alert.

**Auto-approve sessions (added 2026-10-28)**
- R15. Auto-approve session. Instead of a YES per proposal, the human approves bounded RULES once: mode, duration (minutes), budget (total dollars ever committed; profits do not raise it), loss limit (auto ends when the session's realized plus marked P&L reaches minus the limit), stake per trade, and whether it may open trades while the human is away for that duration. The panel states the rules in plain English and the human types "I approve these rules for this session" (in Production also "I understand this uses real money"). While on, each new Coach proposal is prepared and approved on the session's behalf through the same ticket, token binding and OrderGate as a human YES (limits, thin-market guard, price moved, series, time left, kill switch, per-trade/window/day caps, plus the session's budget and loss limit); presence is satisfied by a present human OR by being inside the granted window. Shadow and Sandbox anytime; Production only after the FULL go-live gate (never the micro-live gate). Auto ends on time, budget, loss limit, kill switch, mode change, credential problems, a restart (never resumed) or STOP; open brackets keep their take profit and stop. Every auto approval is audited as `auto_approved` with the session id and the rules. A large AUTO ON banner with a STOP button shows while it runs.
- R16. (Deferred: not in this build.) Standing auto: unattended until a human stops it. User's words: "an override, but only after certain criteria are met, where it can run completely in the background after human approval until a human stops it." Proposed unlock criteria, for the user to confirm later: (1) at least N completed bounded auto sessions in Production with no incident (no unreconciled order, no stop failure, no limit breach); (2) the live auto record is net positive after fees over at least 50 distinct windows; (3) live fills and slippage are within tolerance of shadow/paper; (4) live adverse selection is not negative. Even when unlocked it has no end time, but it keeps a daily budget, a daily loss limit, a drawdown auto-pause, a halt on stale data, feed or clock problems or a reconciliation mismatch, the kill switch, and it stays off after a restart until re-approved.

**Order ticket**
- R1. On the 15m card, when the champion calls UP or DOWN, a "Prepare order" action opens a ticket pre-filled with the market ticker, side (yes for UP, no for DOWN), contracts from the user's stake setting, and a limit price at the current ask.
- R2. The ticket shows, before confirmation: the engine's reasoning, the live bid/ask, cost including the Kalshi fee, maximum loss, payout if right, the engine's current edge and sample size on 15m, and a plain warning when that edge is not statistically positive.
- R3. Every order requires its own explicit confirmation by the user; a confirmation expires after 10 seconds or when the quote moves beyond the limit price, and the ticket must be re-confirmed.
- R4. SKIP calls never offer a ticket; the user can still open a manual ticket, which is clearly labeled as against or without the engine.

**Limits and safety**
- R5. Hard limits, configured locally and enforced server-side: max contracts per order, max dollars per 15m window, max dollars per day, and max open positions. Defaults are conservative (5 contracts, $10 per window, $25 per day, 1 open position).
- R6. A kill switch on the dashboard disables all order placement immediately and persists across restarts until turned off.
- R7. Orders are only limit orders on KXBTC15M markets currently open; any other series, market type or order type is refused.
- R8. Trading is off unless the user both configures a separate trading key and turns trading on in settings; the read-only link (U19) never gains write ability.

**Tracking and learning**
- R9. Every proposed, confirmed, rejected, sent, filled, cancelled and failed order is logged with the engine call, quote and limits in force.
- R10. Filled orders appear in My Calls and the account view, scored against the window outcome alongside the engine and baselines; the bankroll simulation can be compared with real results over the same span.

### Key Decisions

- **Human confirms every order; no automation.** Governs R3, R4, R11, R13. (session-settled: user-directed — chosen over automated trading: the user wants the human to make the decision with the engine supplying clarity fast; "execute" means the human says Yes or No and the app does it.)
- **The whole bracket (entry, take-profit, stop) is approved with one Yes.** Governs R12. (session-settled: user-directed — the human sets the rule, the app executes it; take-profit is a resting Kalshi limit order, the stop is app-monitored.)
- **Stop-loss is pre-approved at entry and executed by the app.** Governs R12. (session-settled: user-directed — chosen over asking at the stop and over a countdown auto-sell: on a 15-minute market a delayed stop can turn a small loss into a large one.)
- **A human must be present for new entries.** Governs R14. (session-settled: user-directed — the user wants to be sure a human is watching.) Amended by R15: inside an auto-approve session presence is a present human OR the window the human explicitly granted at session start.
- **Auto-approve sessions bounded by duration, budget and loss limit.** Governs R15. (session-settled: user-directed — chosen over per-trade approval for bounded sessions: "have it auto approve instead of me approving each one"; presence OR a granted window ("if I'm active and/or I say it's good for an hour"); Production only after the full go-live gate; every session needs a duration, a budget and a loss limit.) Supersedes "no automation" for sessions the human starts with the typed acknowledgment; every order still passes the same OrderGate.
- **Standing auto (R16) is deferred.** (session-settled: user-directed — the user wants an unattended-until-stopped override only after criteria are met; the four criteria in R16 are proposals for the user to confirm, and the N in criterion 1 is open.)
- **Separate opt-in trading path.** Governs R8. Keeps the read-only link incapable of trading.

### Scope Boundaries

- No automated or scheduled trading outside a human-started, bounded auto-approve session (R15); no standing auto until R16 is unlocked; no 1m/5m trading (no Kalshi markets).
- No market orders, no other series or exchanges.
- Considered and not built: position sizing by Kelly or engine confidence, because sample sizes are too small to trust; revisit after a validated out-of-sample edge exists.

---

## Planning Contract

### Key Technical Decisions

- KTD5. **Kalshi V2 order API.** Orders use `POST /portfolio/events/orders` (YES-side `bid`/`ask` with a YES price, IOC entries, post-only GTC take-profits, reduce-only IOC stop sells) and `DELETE /portfolio/events/orders/{id}`; the legacy `/portfolio/orders` create endpoint this plan first assumed is deprecated. Same semantics, different paths/body. (session-settled: user-directed — chosen over the legacy endpoints: avoid technical debt.) Sources: https://docs.kalshi.com/openapi.yaml, https://docs.kalshi.com/getting_started/order_direction.md
- KTD6. **Live feed: Kalshi WebSocket primary, REST polling backup.** Order updates (`user_orders`), fills (`fill`) and the stop monitor's prices (`orderbook_delta` snapshot + seq) come from one authenticated WebSocket per environment (shadow: the real public book polled each second plus simulated fill events); polling keeps running as the backup (take profit every 10 s while the feed is healthy, every 2 s and a REST quote each tick when it is not), and every transition is idempotent across both sources. A cancel that answered is authoritative: the take profit's final fills come from the order list or the cancel response, never from the single-order GET (stale for 20+ s on production after a cancel). The Coach's live Kalshi prices use the same order-book stream (production, market data only, read-only key) with the REST order book as fallback and a 2 s age limit; the `/markets` list (measured ~4 s stale) only discovers the current market. Live prices: WebSocket book, REST orderbook fallback; /markets only for discovery and history. This covers the trading client's gate/stop quote (`GET /markets/{ticker}/orderbook`; `/markets/{ticker}`, measured lagging up to 11 s, gives only status/open/close, cached per ticker and re-read at most every 30 s) and each window's opening `yes_ask`/`no_ask`. (session-settled: user-directed — "we should 100% use WebSockets. And use the polling as a backup.") Sources: https://docs.kalshi.com/getting_started/quick_start_websockets.md, https://docs.kalshi.com/asyncapi.yaml
- KTD1. **Separate trading client and key.** A new `KalshiTrading` adapter with its own key settings (`BTCFLOW_KALSHI_TRADING_KEY_ID`, `..._PATH`), allowed only `POST /portfolio/orders` and `DELETE /portfolio/orders/{id}`; the U19 GET-only guard remains on the read-only client.
- KTD2. **Server-side order gate.** One application service (`OrderGate`) checks, in order: trading enabled, kill switch off, market open and in series, confirmation token fresh (≤ 10s) and bound to the exact ticket (ticker, side, count, limit price), quote not beyond limit, and all limits (R5) against logged exposure. Any failure refuses the order with a reason shown on the ticket.
- KTD3. **Confirmation tokens.** Preparing a ticket returns a single-use token hashed with the ticket contents; confirming sends the token back; the gate rejects reuse, expiry or any field change.
- KTD4. **Client order IDs for idempotency.** Each confirmed ticket gets a `client_order_id`; retries reuse it so a network retry cannot double-place.

### Implementation Units

| U-ID | Title | Key files | Depends on |
|---|---|---|---|
| U1 | Trading settings, kill switch and order log tables | `adapters/sqlite/tables.py`, migration `0003` | — |
| U2 | KalshiTrading adapter (limit orders, cancel) | `adapters/kalshi/trading.py` | U1 |
| U3 | OrderGate and ticket service | `application/trading.py` | U1 |
| U4 | Ticket API and order status sync | `adapters/http/trading_routes.py` | U2, U3 |
| U5 | Yes/No approval cards on the Coach, presence heartbeat, alerts, kill switch, order history | `web/views/coach.js`, `web/views/mycalls.js` | U4 |
| U6 | Stop-loss monitor: watches approved positions each second, sells on trigger within the slippage cap, logs and notifies | `application/trading.py` | U2, U3 |
| U7 | Depth-aware paper fills: size against the recorded order book so paper results reflect what real orders would get | `application/coach_sim.py` | — |
| U8 | Auto-approve sessions (R15): `start_auto`/`offer`/`stop_auto`, OrderGate presence-or-grant and session budget/loss-limit step, auto status; cockpit panel, AUTO ON banner, auto-marked events | `application/trading.py`, `adapters/http/trading_routes.py`, `web/views/trading.js`, `web/views/coach.js`, `tests/trading/test_auto.py` | U3, U4, U6 |

Each unit's tests must cover: limit enforcement at each boundary, expired/reused/altered tokens refused, kill switch blocking, non-KXBTC15M refusal, idempotent retry, and that no code path sends an order without a valid token.

---

## Verification Contract

| Gate | Check |
|---|---|
| Unit tests | Every refusal path in KTD2 has a test that asserts no request reaches the transport |
| Demo account | End-to-end against Kalshi's demo environment before any production key is configured |
| Manual | A real 1-contract order with the user present, then kill switch verified |

## Go-live gate

Implementation starts only after a paper readout shows, at the stake the user intends: at least 100 completed Coach setups across at least 50 distinct 15-minute windows (setups within one window share an outcome, so windows are the honest sample size), net positive after fees with depth-aware fills, the adverse-selection check not negative (when the Coach saw a bargain, outcomes did not side with the market price), a win-rate interval and calibration check that support the edge, and the threshold sweep agreeing. First read (2026-10-28, 6 windows): profits came from early exits on price swings, while at settlement the market was closer than our odds in the moments we disagreed — too small a sample to conclude, but the gate must not open on one afternoon. Real orders run on the always-on home server, not the laptop.

## Definition of Done

- Orders can only be placed through a fresh, user-confirmed ticket within limits, on open KXBTC15M markets.
- Kill switch and limits are enforced server-side and survive restarts.
- Every order lifecycle event is logged and shown next to the engine's call and outcome.
- Verified end-to-end on Kalshi's demo environment.
