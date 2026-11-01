# HTTP API contract

Clients:
- **The web app** (`web/`), in the paired PC browser or on a phone signed in with the PIN: Play (`web/views/play.js`,
  `#play`, with Ask Settleview and the Record tab `#record`), Perps (`web/views/perps.js`, `#perps`), the Agent tab
  (`web/views/agent.js`, `#agent`) and the assist proposal cards (`web/views/proposal.js`). `web/app.js` sends the
  presence heartbeat.
- **External agents** (an MCP client you run, inside an external agent session you launched): only the tool routes,
  on the agent listener (`docs/api-agents.md`, "External agents' tool API").
- **The container healthcheck**: `GET /api/health` without a session.

The Telegram bot is not an HTTP client: it runs inside the app and calls the same application code (its pairing and
alert settings are the `/api/telegram/*` routes below). FastAPI's own `GET /api/docs` and `GET /api/openapi.json` are
served to the paired PC only (a phone gets 403 "Only from the PC itself.").

All timestamps are integer UTC milliseconds; prices are dollars per contract (0-1) on the bracket's own side (YES for
`UP`, NO for `DOWN`); BTC prices are USD floats. Request bodies are JSON unless a route says "form".

The research-era routes (`/api/live`, `/api/picks`, `/api/decisions`, `/api/performance`, `/api/simulate`,
`/api/rulesets`, `/api/jobs`, `/api/whatif`, `/api/replay`, `/api/glossary`, `/api/kalshi/*`, `/api/strategies/*`,
`/api/modes/*`, the Coach cockpit routes and the trading auto / micro-live session routes) were removed with their
screens. Their history stays in the database.

Agent routes (any number of agent sessions, one market each; lasting agents and their journal across runs):
[`docs/api-agents.md`](api-agents.md). The Agents tab (`web/views/agent.js`) reads the fleet
([`GET /api/agent/sessions`](api-agents.md#get-apiagentsessionsended0-pc-and-phone), `GET /api/agent/agents`), one
agent's run ([`/api/agent/sessions/{id}/...`](api-agents.md#one-session-apiagentsessionsid), its own stream), All
activity across runs ([`GET /api/agent/agents/{id}/journal`](api-agents.md#agents-lasting-identities-and-their-journal-across-runs))
and the fleet's audit (`GET /api/agent/journal/all`); it launches with `POST /api/agent/launch` (one `market`), stops
one run or all (`POST /api/agent/stop_all`), sets the fleet limits (`POST /api/agent/global_limits`, PC only) and
retires an agent from Record (`POST /api/agent/agents/{id}/archive`, PC only). It also reads `GET /api/agent/status`
and the scorecard, retires and reactivates rules, edits the prompts, saves launch presets and shows assist proposals
for you to approve or skip (api-agents.md: "Status, scorecard and rules", "Prompts", "Assist proposals", "Presets").
The single-session wrappers were removed with the fleet UI (see api-agents.md, "Removed").

## Play (strategies "you" on Kalshi 15m, "you_poly" on Polymarket US 15m, "you_poly5" on Polymarket US 5m)

Read-only routes, except the trade limits and the market protection (`POST /api/play/limits`,
`POST /api/play/market_band`). All are open to the PC and a signed-in phone. Orders use the trading routes below with the tab's strategy
(the payload's `strategy`): prepare with `price_mode` (`market`, or `limit` with `max_price`), `take_profit` (0.99 =
ride) and no stop, then approve; the optional BTC stop is set after entry (`/api/trading/stop`); EXIT NOW =
`prepare_exit` + approve. `venue` (`kalshi|polymarket`) and `span` (`5m|15m|1h`) are required: missing or outside
those values = 422. A pair this server has no tab for (Kalshi 5m, any 1h) answers 200 with `available: false` and a
`reason`. `stake` is optional, above 0 and at most 10,000.

- `GET /api/play/now?venue=kalshi|polymarket&span=15m|5m&stake=`: `now` and `server_now_ms` (the server's clock),
  `venue`, `span`, `venues` {tab key: has a trading service}, `strategy` (the tab's trading strategy, null when
  unavailable), `available`; `window` {start, end, seconds_left, ticker,
  entry_cutoff_ms, entry_open, phase, settle_op}; `market` (the venue's live quote: Kalshi = the Coach's live quote,
  WebSocket book with the REST book as fallback; Polymarket = the WebSocket cache only, never the REST BBO; DOWN = the
  short side 1 - YES; stale -> `ready` false); `call` {action, headline, reason, lean from the mid at 55 / 45 %,
  mid_up, fair_up, model_lean, agree}; `signals` [market, model, 1m / 5m / 15m pressure, crowd last minute]; `btc`
  {btc, strike, gap, leader, flip_move, source}; `btc_series`, `btc_levels`; `entry.UP|DOWN` {bid, ask, ideal_entry,
  max_price, take_profit, suggested_stop, stop_distance, buyable, contracts, max_loss, payoffs, `market` (a market buy's
  worst case and whether it fits)}; `exit`; `stake` {default, max, cap, cash_cap, sentence, value, fee_rate,
  whole_contracts, fee_rounding} (`fee_rounding`: `"up_0.0001"` = up to $0.0001 per fill, Kalshi; `"nearest_cent"` = to the nearest cent per order, Polymarket US); `limits`; `mode`; `positions` (bracket views with `advice`, `outlook`, `stop_level`,
  `entry_btc`); `cone` (the likely close range from the Coach's model); `balances`; `open_elsewhere`; `spans`;
  `ready`, `reason`. A tab missing on this server answers `available: false` and a reason. `limits` carries
  `day_used` (today's committed dollars, one total across both venues and perps) and `day_tz` (the zone the day runs
  in).
- `GET /api/play/stream?venue=&span=&stake=` (text/event-stream; the Play screen's live view since 2026-11-01): the
  `/api/play/now` payload pushed once a second. Frames: `event: view` with data `{"seq", "full", "view"}` — the first
  frame and any frame whose top-level keys changed are the whole view (`full: true`, replace), later ones only the
  changed top-level fields (`full: false`, merge); `event: stale` `{"seq", "reason"}` when a read fails (the screen
  greys its slide and sends nothing until the next `view`). A healthy stream sends a frame every second; the screen
  treats 4 s of silence as dead and reconnects (3 s). One stream per screen, tab and amount; more than 64 open streams
  answer 503 `{"detail"}`. Same parameters and 422s as `/api/play/now`; the session is checked when it opens
  (docs/solutions/2026-11-01-play-stream-stale-and-signed-out.md).
- `GET /api/play/scoreboard?venue=&span=&mode=prod|shadow|sandbox|all&last=N&both=true`: `record`, `streak`, `pnl`
  {today, week, all} (today = since local midnight, week = today and the six local days before, in the zone named by
  `day_tz`, the same days as Record), `calls` {n, win_rate, avg_price (the bare price), break_even (what a contract
  cost WITH the entry fee: entry_cost / filled, averaged per trade; null without a known cost), break_even_n (trades
  with a known cost), edge (= win_rate − break_even), mostly_luck (< 50 calls), verdict}, `attempts` {n, record, streak,
  points (each with `break_even`), calibration} for the last N (`last` omitted = all), `recent`, `day_tz`. Totals are
  over every trade (no newest-500 cap since 2026-11-01). Default mode: each tab's current mode; `both` = every tab.
- `GET /api/play/limits` → `{per_trade, per_day, set_at, readable, error, confirm_phrase, balance_cap: {tab: {...}},
  history: [{ts, reason, detail}] (the last 10 changes), market_band: {cents, choices: [5, 10, 15, 25], readable,
  error}}`. `readable: false` (with `error`) when the stored limits cannot be read: entries are refused until they are
  saved again.
- `POST /api/play/limits` body `{per_trade, per_day, confirm?}`: both required, each a dollar amount above 0 or null
  (no limit); `confirm` at most 100 characters. A missing field, a non-number, 0 or less is 422. Raising or removing a
  limit needs `confirm` exactly `confirm_phrase` ("I understand this raises my limit"), from the PC only: a phone may
  lower a limit, and a raise or removal from a phone answers 403 `{"detail": "Only from the PC itself."}`. Answer:
  `{ok: true, changed, ...the GET fields}`. Refusals are 200 `{"ok": false, "code": "invalid|confirm_required",
  "reason"}` (`confirm_required` adds `raises` and `confirm_phrase`). People only (403 `person_required`).
- `POST /api/play/market_band` body `{cents}` (required): 5, 10, 15, 25 or null (up to 99c). How far over the ask a
  Market buy may fill, shared by every Play tab. A phone may narrow it; widening it (more cents or null), or any change
  while the stored value is unreadable, answers a phone 403 "Only from the PC itself.". Answer: `{ok: true, changed,
  market_band, ...the GET /api/play/limits fields}`; another integer is 200 `{"ok": false, "code": "invalid"}`; a
  missing or non-integer `cents` is 422. People only (403 `person_required`).

## Ask Settleview (Play's copilot line)

`adapters/http/copilot_routes.py`, logic in `application/copilot.py`. Read only, except the record of what was shown
with an order. Every sentence comes from code templates; no model is called and nothing is sent. Open to the PC and a
signed-in phone.

- `GET /api/copilot/now?venue=&span=&mode=quiet|full&warn=<keys>|none&stake=`: `venue`, `span` and `stake` as
  `/api/play/now`. `mode` is required (`off` is not accepted: Off means the screen does not ask). `warn` is required
  (1-200 characters): the warnings switched on, comma separated, from `against_btc`, `late`, `band_50_75`, or `none`.
  An unknown key or a missing parameter is 422. A pair this server has no Play tab for answers 200 `available: false`
  with a `reason`, as `/api/play/now` does: Polymarket 1h (a market with no tab) and, since 2026-11-01, Kalshi 5m and
  1h (no such market: `market` null; these were 422 before). Answer: `{mode, warnings_on, market, available, reason
  (null while available), window_end, ticker, lights, line: {none, UP, DOWN}, warnings: {UP, DOWN}, fill, hold, btc}`.
  With `available: false` the rest is empty (`lights` [], the lines and warnings null). `lights` (full only): one per
  active rule that trades this market, `{rule, market, label, state: grey|amber|green, progress (0-1), side, ask,
  line, details, match, record, backtest, record_text, backtest_text}`. `line.none|UP|DOWN`: the one line for no side
  picked or that side, `{kind: ready|setup|warning|context, tone, text, details, rule?, progress?, side?, keys?}` or
  null (quiet shows warnings only). `warnings.UP|DOWN`: `{kind: "warning", tone, side, keys, text, details}` or null:
  only for a switched-on warning whose bucket of your own record is losing. `fill` (full, a green rule): `{rule, side,
  ask, max_price, stop_index_below, stop_index_above, stop, window_end, ticker, break_even_pct}`, what "Fill in ticket"
  puts on the slide. `hold` (full, an open position): `{kind: "hold", tone, rule, text, details}` or null. `btc`:
  `{btc, strike, gap, left_ms}`. The screen uses an answer only while `available`, `mode` and `window_end` match what
  it shows.
- `POST /api/copilot/shown` body `{strategy (≤32), bracket_id (≤64), shown: {mode: quiet|full, side: UP|DOWN, ask?
  (0-1), lights? [{rule, state}] (≤12), line? {kind: ready|setup|warning|context|hold, text (≤300), rule?}, warning?
  {keys (≤8), text (≤300)}, filled? {rule, side, max_price (0-1 exclusive), stop?}, btc?, strike?, btc_gap?,
  left_ms?}}`: what the screen showed when the slide sent that order (one `trading_audit` row, event `SHOWN`). →
  `{ok: true, repeat: false}`; a second call for the same bracket is a no-op `{ok: true, repeat: true}`. An unknown
  bracket for that strategy is 200 `{"ok": false, "code": "unknown_bracket"}`; an unknown rule, light state or warning
  key, or a malformed body, is 422.

## Record (the web app's Record tab)

- `GET /api/record/summary?range=today|week|all` → `{ok, range, start_ms, now_ms, net, trades, wins, you {net, trades,
  wins}, agent {net, trades, wins}, api {usd, wakes, sessions}, by_rule [{rule, trades, wins, net}], by_agent
  [{agent_id, name, market, market_label, archived, runs, trades, wins, net, api_usd, net_after_api}], day_tz}`. Read only; open
  to the PC and a signed-in phone like every `/api` route. The Telegram daily summary's own computation
  (`application/record_summary.py`): closed, filled Play brackets (yours and every agent session's) counted on the
  local day they closed, net after every fee (proceeds − entry cost − take-profit and exit fees); `you` = no session,
  `agent` = a session's; `api` = the agent sessions started in the range; `by_rule` = each rule that traded plus each
  rule a session in the range ran with (0 trades); `by_agent` = each agent (all its runs) with a trade closed or a
  session started in the range, best net after API first (`api_usd`: its sessions started in the range). Every agent
  session counts, however many ran at once. Ranges: `today` (since local midnight), `week` (today and the six
  local days before), `all` (everything). Days run in one zone, `day_tz` (the app's `TZ`, default UTC;
  docs/operations.md "Running in Docker: time zone"), the same day as Play, the day limit and the daily summary. Perps
  are not included (their record is `GET /api/perps/record`). `range` is required: missing or unknown is 422.

## Coach (the evaluation behind Play)

- `GET /api/coach/series?window=current|<window start ms>` → recorded data for one Kalshi 15m window: `{"horizon",
  "boundary", "window_end", "now", "ticker", "strike" (our open average), "kalshi_strike", "result", "step_ms": 5000,
  "points": [{"ts", "fair_up", "price", "yes_bid", "yes_ask", "no_bid", "no_ask"}], "coverage", "note"}`. Points run
  every 5 s from the window start up to now only; a Kalshi value is the latest recorded quote no older than 10 s,
  otherwise `null` (gaps stay gaps). 422 for a non-boundary or future window. Play's odds pane reads it.
- `GET /api/coach/pause` → `{"paused", "since", "sentence"}`; `POST /api/coach/pause` body `{"paused": true|false}` →
  same. Persisted. While paused every phone service refuses new entries: it is the second half of Play's kill switch.
  Pausing works from the PC or a phone; un-pausing (`false`) is PC only (a phone gets 403 "Only from the PC itself.").

## Trading (human-confirmed orders)

Routes in `adapters/http/trading_routes.py`, logic in `application/trading.py`. The human approves each bracket with
one slide: the entry (an immediate-or-cancel buy at the approved max price, or at 99c for a market buy), the take
profit (a resting maker sell, placed after the entry fills) and the optional BTC stop (watched server-side; on trigger
the take profit is cancelled, then the held contracts are sold reduce-only at the bid minus the slippage cap). Nothing
else is ever sent without a fresh Yes, and new entries need a present human (heartbeat). No route returns key ids,
key paths or key material.

Every route but the shared ones (heartbeat, kill, resume) needs `strategy` (body field or query parameter; missing =
422, empty or unknown = `{"ok": false, "code": "unknown_strategy"}`); a ticket only approves on the strategy that
prepared it. Environments (`env`): `demo` (mode `sandbox`, Kalshi demo exchange), `shadow` / `pm_shadow` (the
production code path against a simulated exchange on the real public book; no keys, no money), `prod` / `pm_prod`
(real money).

Refusals are HTTP 200 `{"ok": false, "code", "reason"}` with `reason` in plain English; malformed bodies are 422.
Codes include `mode_changed`, `disabled`, `not_configured`, `not_reconciled`, `killed`, `unknown_orders`,
`not_present`, `wrong_series`, `no_quote`, `no_offer`, `market_closed`, `too_late`, `bad_token`, `expired`, `altered`,
`price_moved`, `thin_market`, `limits_unreadable`, `balance_unknown`, `over_trade_limit`, `over_cash`,
`over_order_cap`, `max_open`, `over_window_limit`, `over_day_limit`, `confirm`, `invalid`, `not_found`, `no_bid`,
`already_closed`, `already_closing`; send failures return `rejected` or `unknown` (being reconciled).

Who may call: the PC and a signed-in phone (an agent session's orders take the same application path from inside the
app, never these routes). `heartbeat` and `mode` are people only (403 `person_required`); `resume` is PC only; `kill`
works from either.

- `POST /api/trading/prepare` body `{"strategy" (≤32), "ticker" (≤64), "side": "UP|DOWN", "stake" (above 0, ≤10,000),
  "price_mode": "limit|market" (absent = limit), "max_price"? (0-1 exclusive; required for limit, omitted for market),
  "take_profit" (0-1 exclusive), "stop"? (0-1 exclusive), "stop_index_below"?, "stop_index_above"? (BTC levels above
  0), "window_end"?, "fair_up"?}` → `{"ok": true, "token", "ticket"}`. The token is single-use and expires after
  10 s.
- `POST /api/trading/prepare_exit` body `{"strategy", "bracket_id"}` → a `kind: "exit"` ticket: sell everything held
  at the current bid or better.
- `POST /api/trading/approve` body `{"strategy", "token", "decision": "yes|no", "ticket": <the ticket as shown>}` →
  `{"ok": true, "bracket"}` (or `declined`). Any change to a bound field refuses with `altered`. The Play slide sends
  the ticket with the slide's own side, amount, order type and max price in those fields, and only after checking the
  prepared ticket against what the slide showed (market, window, side, amount, order type, max price or a market buy's
  worst case and count, the BTC stop; for a sell the position, count and floor): a ticket that differs is shown on the
  confirm sheet with what changed, never approved from the slide.
- `POST /api/trading/take_profit` body `{"strategy", "bracket_id", "price"}` (the auto-sell; above 0, at most 0.99;
  0.99 = ride) and
  `POST /api/trading/stop` body `{"strategy", "bracket_id", "level" | null}` (set, move or remove the BTC stop; no order
  is sent for it). The tap is the Yes.
- `POST /api/trading/heartbeat` body `{"visible": true|false}` → `{"present", "last_seen", "timeout_s"}` (every
  service). The web app sends one (web/app.js) every 30 s while Play or Perps is on screen and the page is visible,
  and when either comes into view; the screens send none of their own.
- `POST /api/trading/kill` → `{"killed": true}` (persisted, shared: no new entries in any mode on any tab; open
  positions keep their exits; the perps service is killed too); `POST /api/trading/resume` (PC only; a phone gets 403
  "Only from the PC itself.") → `{"killed"}` (still `true` while the Coach pause is on). Both reach every Play service
  and the perps service; the answer is the first Play service's.
- `POST /api/trading/mode` body `{"strategy", "mode": "sandbox|shadow|prod", "confirm"? (≤200)}`. `prod` needs
  `confirm` exactly `"I understand this uses real money"` (from the PC or a phone), trading enabled and a production
  key; a switch reconciles first. People only.
- `GET /api/trading/status?strategy=` → `{"mode", "env", "label", "modes", "enabled", "strategy", "venue",
  "configured", "locked", "reasons", "killed", "present", "ready", "limits", "exposure", "open": [<bracket>],
  "settling": [<bracket>], "settle_warnings", "external", "stream", "recent"}`. The Play and Agent screens read
  `killed`.

## Perps (Kalshi's BTC perpetual)

Routes in `adapters/http/perp_routes.py`, logic in `application/perp_trading.py` (the View: `application/perp_view.py`).
The Perps screen (`web/views/perps.js`) is the only user. Open to the PC and a signed-in phone, the stream included
(its session is checked when it opens). One position at a time; the human approves every write with a slide. Kill,
resume and the presence heartbeat are the shared `/api/trading/*` routes above.

Refusals are HTTP 200 `{"ok": false, "code", "reason"}`; a malformed body (missing field, wrong type, unknown value) is
422. With perps switched off on this server (no service) the View answers `enabled: false` and every write refuses
`perps_off`. Codes include `invalid`, `perps_off`, `busy`, `mode_off`, `disabled`, `not_configured`, `not_reconciled`,
`killed`, `not_present`, `exchange_paused`, `quote_stale`, `no_offer`, `unknown_order`, `foreign_position`,
`foreign_orders`, `diverged`, `close_first`, `closing`, `position_open`, `no_position`, `position_changed`,
`trigger_changed`, `limits_unreadable`, `over_trade_limit`, `over_day_limit`, `balance_unknown`,
`balance_too_small`, `risk_limit_unknown`, `over_risk_limit`, `price_out_of_band`, `stop_past_liquidation`,
`stop_wrong_side`, `take_profit_wrong_side`, `mark_unknown`, `bad_token`, `expired`, `altered`, `mode_changed`,
`confirm`; send failures return `rejected` or `unknown` (being resolved), a failed bracket write `trigger_rejected`.

`busy`: `approve`, `adopt` and `mode` answer `{"ok": false, "code": "busy", "reason"}` (nothing sent or changed) when
another perps approval is still sending and holds the service lock for more than 2 s (`PerpConfig.lock_wait_s`); the
request never waits for the whole send (up to 30 s, `send_deadline_s`). Try again once the other order is answered.

- `GET /api/perps/view` → the View: `{server_ms, enabled, enabled_reason, mode (off|perp_demo|perp_prod),
  modes_available, modes_unavailable {mode: why}, ready, ready_reason, killed, exchange {trading_active, reason,
  maintenance_note}, market {ticker, tick, contract_btc, count_step, taker_fee, maker_fee, max_leverage_long,
  max_leverage_short}, market_reason, quote {bid, ask, bid_size, ask_size, last, mark, reference, age_ms},
  quote_reason, funding {rate, next_ms}, funding_reason, balance {equity, available, at_ms}, balance_reason, position,
  trigger {state, stop_loss, take_profit, updated_time, reason}, unknown_order, foreign [{subaccount, kind, size,
  avg_entry, unrealized_pnl}], banners [{code, severity: red|amber, text}], can {entry, entry_reason, exit,
  exit_reason, trigger, trigger_reason}, outcome {kind, text, at_ms} | null, limits {per_trade, per_day,
  committed_today, text}, settings {band, amber, red}, warnings_note}`. A value nobody has read is null with its
  `*_reason` beside it. `position`: `{id, direction (long|short), size, avg_entry, state, collateral, leverage, mark,
  unrealized_pnl, realized_pnl, fees, funding_total, liquidation_price, liquidation_source, distance, band,
  opened_ms}` or null (`collateral` and `leverage` null for an adopted position). `quote.age_ms` is the age of the
  book's last update, not of the newest message; a quote older than 2 s is null.
- `GET /api/perps/stream` (text/event-stream): the whole View first, then `event: view` frames with only the changed
  top-level fields (at most one per 100 ms; merge them), `event: ping` every 15 s.
- `GET /api/perps/record?last=N` (1-200, default 10) → `{env, reason, rows: [{id, direction, avg_entry,
  committed_collateral, leverage_chosen, close_reason, realized_pnl, fees, funding_total, opened_ms, closed_ms}], wins,
  losses, pnl}`: the active environment's last N closed positions, newest first, P&L from Kalshi's fills (fees and
  funding included). Off: `env` null, `rows` [] and the reason.
- `POST /api/perps/prepare` body `{action: entry|add, side: long|short, collateral (>0), leverage (>0), stop_loss,
  take_profit, fill: market|all_or_nothing|limit, limit_price, band}`: every key required; `stop_loss`,
  `take_profit`, `limit_price` (>0) and `band` (≥0, a fraction; null = the default 0.5%) may be null. A band, or a
  limit price, more than `max_band` (10%) from the touch is refused `price_out_of_band`. A limit price goes with `fill: "limit"` only; an add carries no stop or take profit (`invalid`). →
  `{ok: true, token, ticket, expires_ms}`: the token is single-use and expires after 10 s. `ticket`: `{action, side,
  env, ticker, count, btc, notional, notional_worst, protective_price, protective_text, time_in_force, fill,
  limit_price, band, fee_est, worst_case, at_risk_balance, at_risk_text, liquidation_est, liquidation_label, funding
  {rate, next_ms, est_amount}, collateral, leverage, max_leverage, stop_loss, take_profit, new_avg_entry, new_size,
  new_liquidation_est, remaining, est_realized_pnl, chips, bound {position_sign, position_size,
  trigger_updated_time}, order_side, expires_ms}`.
- `POST /api/perps/prepare_exit` body `{action: reduce|close, count}`: `count` (>0, whole contract steps, less than
  held) for reduce, null for close (the whole exchange position) → the same answer; the ticket adds `remaining` and
  `est_realized_pnl`.
- `POST /api/perps/prepare_trigger` body `{stop_loss?, take_profit?}` (each >0 or null): a key left out keeps that
  leg, null clears it, both null removes the bracket → the same answer, a ticket with `action: "trigger"` and
  `remove`.
- `POST /api/perps/approve` body `{token (1-200), shown: <the ticket exactly as shown>}` → `{ok: true, outcome, view}`
  (`view` is the fresh View) or a refusal (`bad_token`, `expired`, `altered`, `mode_changed`, a gate code); a refused
  send may still carry `outcome`. The send finishes and is recorded even if the request is dropped.
- `POST /api/perps/adopt` body `{size_shown}` (the signed size the screen showed) → `{ok: true, outcome, view}`: take
  Kalshi's position in the perps subaccount as Settleview's. Refused `position_changed` (Kalshi now shows another
  size), `no_position`, `unknown_order`.
- `POST /api/perps/mode` body `{mode: perp_demo|perp_prod|off, confirm? (≤200)}` → `{ok: true, mode, ready, view}`.
  `perp_prod` needs `confirm` exactly `"I understand this uses real money"` (PC or phone) and a production key;
  leaving production with a position open is refused `position_open`. People only (403 `person_required`).

## Who is asking (the PC, a phone, an agent)

Every route below is for a person: the **paired PC browser** (actor `pc`: the PC cookie `settleview_pc` on the PC
listener, `BTCFLOW_PC_PORT`; without one, on a loopback client of the main port) or a **phone** signed in with the PIN
(actor `phone:<8>`). "PC only" means the paired PC browser; a phone gets 403 `{"detail": "Only from the PC itself."}`.
Network position alone is never the PC (docs/operations.md, "Running in Docker: who counts as the PC"):

- In the PC's place without the PC cookie: pages answer 401 with the "Pair this browser" page, `/api/*` answers 401
  `{"detail", "code": "pair_required"}`. A phone without a valid session gets 401 `{"detail": "Sign in first (PIN)."}`.
  The web app turns any 401 into one prompt ("This browser is not paired" → /pair, or "You are signed out" → /login)
  and closes Play's live stream (a stream's session is checked only when it opens).
- External agents' tool routes (`/api/agent/tools`, `/api/agent/call/{tool}`, `/api/agent/events/wait`) are served on
  the agent listener (`BTCFLOW_AGENT_PORT`) only; elsewhere they answer 403 `{"ok": false, "code": "agent_port"}`. The
  agent listener answers 404 to every other path.
- People-only routes refuse a request without a person's actor with 403 `{"ok": false, "code": "person_required"}`:
  `POST /api/trading/heartbeat`, `POST /api/trading/mode`, `POST /api/perps/mode`, `POST /api/play/limits`,
  `POST /api/play/market_band`, `POST /api/agent/proposals/approve` and `/skip` (the access middleware already
  requires a person on the PC and phone listeners; the routes check again).

### Pairing (PC listener only)

- `GET /pair?code=` → 200, the "Pair this browser" form (the code prefilled; nothing is paired by a GET). Already
  paired: 303 to `/#play`. On the phone port: 403.
- `POST /pair` form `code` (the one-time code from `python -m btc_signal_engine.ops pair-pc`; case, spaces and dashes
  ignored; the body is read up to 2 KB, more answers 413) → 303 to `/#play` with `Set-Cookie: settleview_pc=...;
  HttpOnly; SameSite=Strict; Max-Age=34560000` (400 days). Refusals: 401 with the form and the reason ("Wrong pairing
  code.", "That pairing code has expired ...", "No pairing code is waiting ..."); 429 after 5 wrong codes (the pending
  code is cancelled); 403 cross-site (counts no failure).

## Phone access

PC only: `GET /api/access` → `{pin_set, reset_needed, signed_in, listening_on_network, api_host, port, lan_ip,
lan_url, in_container, session_hours, note, how_to_find_ip}`; `lan_url` is `BTCFLOW_PUBLIC_URL`, else the first
`BTCFLOW_ALLOWED_HOSTS` name, else (not in a container) the detected LAN address, else null. `POST /api/access/pin`
{pin, pin_again} (each at most 16 characters; the PIN is 6-8 digits, else `ok: false` with the reason),
`POST /api/access/signout` (every phone), `POST /api/access/clear` → `{ok, reason, ...the GET /api/access fields}`.
`POST /api/access/pc-signout` (every PC browser, this one too: a new PC secret; the answer clears this browser's PC
cookie) → `{ok, reason}`; 409 `{ok: false, reason}` when PC pairing is not configured.
Phones: `GET|POST /login` (form `pin`; the body is read up to 2 KB, more answers 413; see docs/operations.md).
`GET /login` also sets `btcflow_device` (a random, signed id; HttpOnly, SameSite=Strict, `Path=/login`, a year) unless
the browser holds a valid one: wrong PINs are counted per address plus device, so phones behind one gateway address
are locked out one by one; a POST without it (or with a forged one) counts under the bare address. A wrong PIN answers
401, a lockout 429; the POST never sets the device cookie.

## Other

- `GET /api/health` (no session needed: the container healthcheck; without a PC or phone session the answer is only
  `{"status": "up"}`, with no database query) → `{"status", "degraded", "feed", "lag_ms", "last_trade_ts", "clock_offset_ms", "gaps_open",
  "streams", "writer_pending", "writer_failed_batches", "loops", "stalls_last_hour", "perps", "recorders", "uptime_s",
  "memory_mb"}` (an operations check, not used by Play; fields in docs/operations.md). `recorders` (2026-10-31): the
  research recorders' freshness, read-only; it never changes `status`. A silent `pm-quotes` may carry `reason` "the
  venue is sending trades but no quotes". Polymarket's `trading_pm_prod` stream is latched with hysteresis and
  `polymarket_market` is instantaneous (docs/operations.md, "Polymarket book freshness"). During a database outage it
  still answers 200: `gaps_open` null and `database` in `degraded`.

## Telegram (alerts and the remote)

Routes in `adapters/http/telegram_routes.py`, logic in `application/notifier.py` (`RemoteControl`). Always registered
(without a bot token the status says `configured: false`). Never returns the bot token. Refusals are 200 `{"ok":
false, "code", "reason"}`; PC-only routes answer a phone 403 "Only from the PC itself.". The Agent tab's Telegram card
uses them. The saved launch presets are agent routes (`docs/api-agents.md`, "Presets").

- `GET /api/telegram/status` (PC and phone) → `{ok, configured (a token is set), running (polling now), paired,
  bot_username, user_label, alerts: {proposals, trades, activity, pauses, session_end, settle_warnings, setups, data,
  daily}, daily_at ("HH:MM", the daily summary's local time, default "22:00"), muted: [session ids]}`. `bot_username`:
  the bot's @username without the `@`, from `BTCFLOW_TELEGRAM_BOT_USERNAME`, else from Telegram's `getMe` once the bot
  has started; null when no token is set or the name is not known yet (the Agent tab then says "your bot"). Alert
  defaults: on for `proposals`, `trades`, `activity`, `session_end`, `data` (the recorders' freshness) and `daily`; off
  for `pauses`, `settle_warnings` and `setups`.
- `POST /api/telegram/pair` (PC only, no body) → `{ok, code, expires_ts}`: a one-time 8-character code for
  `/start <code>` in the bot, valid 10 minutes; it replaces any pending code. `not_configured` without a bot token.
- `POST /api/telegram/unpair` (PC only, no body) → `{ok, was_paired}`.
- `POST /api/telegram/alerts` (PC only) body `{<kind>: true|false, ..., daily_at?: "HH:MM"}` (at least one key) →
  `{ok, alerts, daily_at}`. An unknown kind is refused `unknown_alert` (never dropped); a non-boolean value, a
  malformed `daily_at` (00:00-23:59) or an empty body is `invalid`. A change keeps the muted agents (stored with the
  alert choices; before 2026-11-01 any alert change unmuted them all).
- `POST /api/telegram/mute` (PC and phone) body `{session_id, muted: true|false}` → `{ok, muted: [session ids]}`: that
  agent session's trade and activity alerts off or on (its pauses, end and proposals still come). `not_found` for an
  unknown session, and for any id while agent mode is off (there are no sessions); `invalid` for a missing session id
  or a non-boolean `muted`.
