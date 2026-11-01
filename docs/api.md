# HTTP API contract

The Play app (`web/views/play.js`, `#play`) is the only client. All timestamps are integer UTC milliseconds; prices are
dollars per contract (0-1) on the bracket's own side (YES for `UP`, NO for `DOWN`); BTC prices are USD floats.

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
retires an agent from Record (`POST /api/agent/agents/{id}/archive`, PC only). The single-session wrappers were
removed with the fleet UI (see api-agents.md, "Removed").

## Play (strategies "you" on Kalshi 15m, "you_poly" on Polymarket US 15m, "you_poly5" on Polymarket US 5m)

Read-only routes, except the phone's own trade limits. Orders use the trading routes below with the tab's strategy
(the payload's `strategy`): prepare with `price_mode` (`market`, or `limit` with `max_price`), `take_profit` (0.99 =
ride) and no stop, then approve; the optional BTC stop is set after entry (`/api/trading/stop`); EXIT NOW =
`prepare_exit` + approve. `venue` and `span` are required (missing or unknown = 422).

- `GET /api/play/now?venue=kalshi|polymarket&span=15m|5m&stake=`: `window` {start, end, seconds_left, ticker,
  entry_cutoff_ms, entry_open, phase, settle_op}; `market` (the venue's live quote: Kalshi = the Coach's live quote,
  WebSocket book with the REST book as fallback; Polymarket = the WebSocket cache only, never the REST BBO; DOWN = the
  short side 1 - YES; stale -> `ready` false); `call` {action, headline, reason, lean from the mid at 55 / 45 %,
  mid_up, fair_up, model_lean, agree}; `signals` [market, model, 1m / 5m / 15m pressure, crowd last minute]; `btc`
  {btc, strike, gap, leader, flip_move, source}; `btc_series`, `btc_levels`; `entry.UP|DOWN` {bid, ask, ideal_entry,
  max_price, take_profit, suggested_stop, stop_distance, buyable, contracts, max_loss, payoffs, `market` (a market buy's
  worst case and whether it fits)}; `exit`; `stake` {default, max, cap, cash_cap, sentence, value, fee_rate,
  whole_contracts, fee_rounding}; `limits`; `mode`; `positions` (bracket views with `advice`, `outlook`, `stop_level`,
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
- `GET /api/play/limits` → `{per_trade, per_day, error, balance_cap: {tab: {...}}, history}`; `POST /api/play/limits`
  body `{per_trade, per_day, confirm?}` (both required; null = no limit). Raising or removing a limit needs `confirm`
  exactly as the screen states it. Refusals are 200 `{"ok": false, "code": "invalid|confirm_required", "reason"}`.

## Record (the web app's Record tab)

- `GET /api/record/summary?range=today|week|all` → `{ok, range, start_ms, now_ms, net, trades, wins, you {net, trades,
  wins}, agent {net, trades, wins}, api {usd, wakes, sessions}, by_rule [{rule, trades, wins, net}], by_agent
  [{agent_id, name, market, market_label, archived, runs, trades, wins, net, api_usd, net_after_api}]}`. Read only; open
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

Perps (`adapters/http/perp_routes.py`): `POST /api/perps/approve`, `POST /api/perps/adopt` and `POST /api/perps/mode`
answer `{"ok": false, "code": "busy", "reason"}` (HTTP 200, nothing sent or changed) when another perps approval is
still sending and holds the service lock for more than 2 s (`PerpConfig.lock_wait_s`); the request never waits for the
whole send (up to 30 s, `send_deadline_s`). Try again once the other order is answered. A perps quote (`GET
/api/perps/view`, `quote.age_ms`) is the age of the book's last update, not of the newest message.

- `POST /api/trading/prepare` body `{"strategy", "ticker", "side": "UP|DOWN", "stake", "price_mode": "limit|market",
  "max_price"? (limit only), "take_profit", "stop"?, "stop_index_below"?, "stop_index_above"?, "window_end"?,
  "fair_up"?}` → `{"ok": true, "token", "ticket"}`. The token is single-use and expires after 10 s.
- `POST /api/trading/prepare_exit` body `{"strategy", "bracket_id"}` → a `kind: "exit"` ticket: sell everything held
  at the current bid or better.
- `POST /api/trading/approve` body `{"strategy", "token", "decision": "yes|no", "ticket": <the ticket as shown>}` →
  `{"ok": true, "bracket"}` (or `declined`). Any change to a bound field refuses with `altered`. The Play slide sends
  the ticket with the slide's own side, amount, order type and max price in those fields, and only after checking the
  prepared ticket against what the slide showed (market, window, side, amount, order type, max price or a market buy's
  worst case and count, the BTC stop; for a sell the position, count and floor): a ticket that differs is shown on the
  confirm sheet with what changed, never approved from the slide.
- `POST /api/trading/take_profit` body `{"strategy", "bracket_id", "price"}` (the auto-sell; 0.99 = ride) and
  `POST /api/trading/stop` body `{"strategy", "bracket_id", "level" | null}` (set, move or remove the BTC stop; no order
  is sent for it). The tap is the Yes.
- `POST /api/trading/heartbeat` body `{"visible": true|false}` → `{"present", "last_seen", "timeout_s"}` (every
  service). The web app sends one (web/app.js) every 30 s while Play or Perps is on screen and the page is visible,
  and when either comes into view; the screens send none of their own.
- `POST /api/trading/kill` → `{"killed": true}` (persisted, shared: no new entries in any mode on any tab; open
  positions keep their exits); `POST /api/trading/resume` → `{"killed"}` (still `true` while the Coach pause is on).
- `POST /api/trading/mode` body `{"strategy", "mode": "sandbox|shadow|prod", "confirm"?}`. `prod` needs `confirm`
  exactly `"I understand this uses real money"`, trading enabled and a production key; a switch reconciles first.
- `GET /api/trading/status?strategy=` → `{"mode", "env", "label", "modes", "enabled", "strategy", "venue",
  "configured", "locked", "reasons", "killed", "present", "ready", "limits", "exposure", "open": [<bracket>],
  "external", "stream", "recent"}`.

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
{pin, pin_again}, `POST /api/access/signout` (every phone), `POST /api/access/clear`, `POST /api/access/pc-signout`
(every PC browser, this one too: a new PC secret; the answer clears this browser's PC cookie) → `{ok, reason}`.
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
- `GET /api/telegram/status` returns `bot_username` (2026-11-01): the bot's @username without the `@`, from
  `BTCFLOW_TELEGRAM_BOT_USERNAME`, else from Telegram's `getMe` once the bot has started; `null` when no token is set
  or the name is not known yet (the Agent tab then says "your bot").
- `GET /api/telegram/status` and `POST /api/telegram/alerts` know the alert kind `data` (default on; the recorders'
  freshness), next to the others.
