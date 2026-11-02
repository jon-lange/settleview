# HTTP API: agents (multi-agent fleet)

The agent routes exist only with `BTCFLOW_AGENT_ENABLED` on (otherwise 404). Conventions as in `docs/api.md`: integer
UTC milliseconds, dollars, prices 0-1. A refusal is HTTP 200 `{"ok": false, "code", "reason"}`; a malformed body is
422; a PC-only route answers a phone 403 "Only from the PC itself."; an unknown session id in a
`/api/agent/sessions/{id}/...` path is 404 `{"ok": false, "code": "not_found"}`. The code is in
`btc_signal_engine/adapters/http/agent_routes.py` (shapes: `application/agent_fleet_view.py`). How the fleet works:
`docs/agent/multi-agent.md`.

Any number of agent sessions run at once; one agent = one market. Every route below names its session (the
single-session wrappers were removed with the fleet UI, 2026-10-31: see "Removed" at the end).

## The fleet

### `GET /api/agent/sessions?ended=0` (PC and phone)

`ended` (0-50, default 0): also the N most recently ended sessions, after the live ones.

```json
{"ok": true, "server_ms": 0,
 "sessions": [Row, ...],
 "summary": Summary}
```

Live rows first, the ones that need you first among them, then by launch time; ended rows last.

`Row`:

| field | meaning |
|---|---|
| `id`, `name`, `market` (key), `market_label`, `kind` (`in_app` / `external`), `level` (`auto` / `assist`) | who |
| `state` | `running` / `paused` / `ended` |
| `chip` | the list's state word: `Proposal`, `Paused`, `Holding`, `Watching`, `Ended` |
| `line` | the sentence after it: `"Holding Up $1.00 · LATERESID · 5h left"`, `"Watching · model judgment (no rules) · 2h left"` |
| `net` | realized net of its closed trades (gains positive) |
| `open` | `{positions, at_risk}`: open or settling positions and their worst case |
| `trades`, `wins` | closed, filled trades and how many won |
| `budget` | `{total, used, left, pct}` (used = realized loss + worst case at risk) |
| `api` | `{used (estimated $), cap (null for external), pct}` |
| `ends_ts`, `seconds_left`, `started_ts`, `ended_ts`, `end_reason`, `pause_code`, `pause_reason`, `rules` | as named |
| `needs_you` | `[{code, text}]`, codes `proposal`, `paused`, `budget_near` (>= 90%), `api_near` (>= 90%), `overlap` |
| `also_traded_by` | `[{id, name}]` other live agents on the same market |
| `holding` | `[{bracket_id, side, cost, rule}]` its open or settling filled positions, oldest first, with the rule entry that opened each (null when none): Play's "2 agents here · one holds Up $1.00 (CUSHION100)" |
| `wakes` | the wake queue's view (null for external / ended): `{state: idle / queued / running, priority, position, queue_depth, waiting_ms, wakes, coalesced, last_wait_ms, max_wait_ms, avg_wait_ms}` |
| `spark` | cumulative net after each closed trade, oldest first (at most 30 points) |

`Summary` (over the live sessions): `{count, running, paused, net, winning, losing, open: {positions, at_risk},
budget: {used, total}, api: {used, cap}, today: {net, trades, wins} (every agent session's trades closed this local
day), needs_you: [{id, name, reasons: [{code, text}]}], by_market: [{market, market_label, count, net}], best: {id,
name, net} | null, worst: ... | null, queue: {cap, running, queued, urgent_queued, oldest_wait_ms, sessions,
last_tick_ms} | null, global_limits: {limits: {max_loss_day, api_day}, used: {loss_day, api_day}}}`.

### `GET /api/agent/fleet` (PC and phone)

`{ok, summary: Summary, overlaps: [Overlap], server_ms}`. `Overlap` = two agents holding opposite sides of one window
(KD2, a warning, never a block): `{strategy, market, ticker, window_end, sides: {UP: [{session_id, name, bracket_id}],
DOWN: [...]}}`.

### `POST /api/agent/launch` (PC; phone within your per-trade limit)

Body: `{kind, market, name?, budget, per_trade, minutes | ends_ts (exactly one), spend_cap? (in-app), model?
(in-app), sell_on_stop?, rules?, one_bet_per_window?, level ("auto" | "assist"), proposal_ttl_s? (assist), confirm:
"START"}`.

- `market`: ONE market key (`kalshi_15m`, `polymarket_15m`, `polymarket_5m`). A list is refused `one_market`. The
  deprecated `venues` is accepted only as a list of exactly one market (more: `one_market`; both fields: `invalid`).
- `agent_id` / `name`: which agent runs (see "Agents" below); `name` 1-40 characters, unique among unarchived agents
  (`name_taken`; bad: `invalid_name`).
- Another live session never refuses a launch (there is no `session_live` any more). A rule must trade the agent's
  market (`invalid`).

Answer: `{ok, id, agent_id, session: View, also_traded_by: [{id, name}], token? (external, shown once)}`. Other codes as before:
`confirm_required`, `invalid`, `invalid_level`, `unknown_venue`, `rule_retired`, `production_locked`, `unavailable`,
`unknown_model`, `agent_unavailable`, `model_unavailable`, `over_your_limit`, `not_available` (sell on stop),
`assist_unavailable`.

`View` (also `GET /api/agent/sessions/{id}`'s `session`): `{id, name, market, kind, state, limits: {name, market,
venues: [market], budget, per_trade, spend_cap, sell_on_stop, rules, one_bet_per_window, level, proposal_ttl_s},
pinned, model, prompt_versions, usage, pause_reason, pause_code, started_ts, ends_ts, ended_ts, end_reason,
budget_used, budget_parts: {realized, at_risk, pending_settlement}, budget_left, taken_over, also_traded_by,
seconds_left}`.

### `POST /api/agent/stop_all` (PC and phone)

No body, no typed phrase (stopping only lowers risk, R18); the screen asks "Stop all?" first. Stops every live
session (in-app and external; an order in flight finishes and is recorded first).
`{ok, stopped: [{id, name, ok, code, reason}]}`; none live: `no_session`.

### `GET /api/agent/global_limits` (PC and phone), `POST /api/agent/global_limits` (PC only)

KD3, off by default. Body: any of `{max_loss_day, api_day}`, each a dollar amount above $0 or `null` (off); an
unknown key is refused (`invalid`), never dropped. Answer: `{ok, limits: {max_loss_day, api_day}, used: {loss_day,
api_day}}` (`GET` adds `server_ms`). `max_loss_day`: realized net loss plus the worst case of every agent position
opened this local day, across agents. `api_day`: estimated API dollars of every session started this local day or still
live. A reached limit pauses every agent (`pause_code` `global_limit`); an entry that would pass `max_loss_day` is
refused `over_global_limit`.

## Agents (lasting identities) and their journal across runs

An agent (`agents` table) is a lasting identity: `{id, name, market, created_ts, archived}`. Each session is one run
of one agent; every session and journal row carries `agent_id`. An agent runs one session at a time.

Launch picks the agent (body field `agent_id`, else `name`, else neither):
- `agent_id`: run that agent again. Refused: `unknown_agent`, `agent_archived`, `agent_busy` (it is running),
  `market_mismatch` (a `market` other than the agent's), `invalid` (a `name` with it: rename instead). `market` may be
  left out (the agent's own).
- `name` (no `agent_id`): a new agent with that name; `name_taken` when an unarchived agent has it already.
- neither: the market's default agent: the idle unarchived agent named "<market> agent" (else " 2", " 3" ..., the
  lowest number idle); none idle: a new one with the lowest free number.
The launch answer adds `agent_id`; a session's view and row add `agent_id`.

Sessions from before agents existed were backfilled by migration 0016: one agent per (market list, name) they ran
with. A one-market list became an active agent ("Kalshi 15m agent"); a multi-market list (allowed before 2026-10-31)
became an ARCHIVED agent "Earlier runs: Kalshi 15m + Polymarket 15m" (market "kalshi_15m,polymarket_15m"), which keeps
its runs and journal and cannot run again.

| route | answer |
|---|---|
| `GET /api/agent/agents?archived=false` (PC and phone) | `{ok, agents: [{id, name, market, market_label, created_ts, archived, updated_ts, running_session_id, runs (count), last_run: Run or null, net (all runs' closed trades), trades}], server_ms}` |
| `GET /api/agent/agents/{agent_id}/journal?before_id=&limit=100&kinds=` | `{ok, agent_id, rows, next_before_id, runs: [Run], server_ms}`; 404 `not_found` |
| `GET /api/agent/journal/all?before_id=&limit=100&kinds=` | the same over every agent (the fleet's audit); `agent_id` null |
| `POST /api/agent/agents/{agent_id}/rename` `{name}` (PC and phone) | `{ok, agent}`; `invalid_name`, `name_taken`; 404 `not_found` |
| `POST /api/agent/agents/{agent_id}/archive` (PC only; a phone gets 403) | Record's "Retire": `{ok, agent}` (archived: it keeps its runs and journal, leaves the fleet and cannot run again); `agent_busy` while it runs, `agent_archived` when it is retired already; 404 `not_found` |

Paging: `rows` are newest first (by id), each `{id, session_id, agent_id, ts, kind, data}`; `limit` 1-500;
`next_before_id` is the `before_id` for the next (older) page, null at the end. `kinds`: comma-separated, any of
`wake, tool, note, notes, state, decision, rule, rule_status, proposal, proposal_outcome, proposal_scored, overlap`;
an unknown kind is refused (`invalid`), never ignored. `runs` = a summary of each run the page's rows belong to (for
the run dividers), newest first: `Run = {session_id, agent_id, name, run (1 = the agent's first run), state,
started_ts, ended_ts, end_reason, net, trades}`.

No persistent learning memory yet (plan L0's memory part comes later): just the identity and the cross-run journal.

## One session: `/api/agent/sessions/{id}/...`

| route | body | answer |
|---|---|---|
| `GET /api/agent/sessions/{id}` | | `{ok, session: View, row: Row, pending_proposals: [Proposal], positions: [Position], server_ms}` |
| `POST .../stop` (PC and phone) | | `{ok, session}` once every order of the session in flight (its own or a rule's) is answered and recorded (at most 30 s); the others keep running; `no_session` when it is not live |
| `POST .../pause` (PC and phone) | | `{ok, session}`; `not_running` |
| `POST .../resume` (PC; phone types `{"confirm": "RESUME"}`) | `{confirm?}` | `{ok, session}`; `not_paused`, `cannot_resume` (kill switch, mode change, global limit), `confirm_required` |
| `POST .../note` (PC and phone) | `{text}` (1-1000) | `{ok, note: {id, session_id, ts, text, by, state: "queued", delivered_ts}}` |
| `POST .../rename` (PC and phone) | `{name}` | `{ok, session, agent}`: renames the session's agent; `invalid_name`, `name_taken`, `no_session` |
| `GET .../journal?after_id=0&limit=200` (limit 1-500) | | `{ok, session_id, rows: [{id, session_id, ts, kind, data}], more, next_after_id}`, rows oldest first. `after_id` 0 (or omitted): the newest `limit` rows (the tail; `more` false). `after_id` > 0: the OLDEST `limit` rows after it; `more` is true when rows after those remain: page on with `after_id = next_after_id` until `more` is false, so nothing is skipped however many rows came between polls. `next_after_id`: the last row's id (`after_id` when there are none) |
| `GET .../decisions?after_id=0` | | `{ok, session_id, rows, more, next_after_id}`: the decision rows, paged as `journal` (200 a page) |
| `GET .../proposals` | | `{ok, session_id, pending, recent, server_ms}` (see "Assist proposals"); `assist_unavailable` without assist |
| `GET .../stream` | | SSE, AG-UI events of this session only (STATE_SNAPSHOT of `{session, positions, prompts, wakes}`, then deltas and events); `not_available` when this app has no per-session streams |

Session ids in these paths are at most 40 characters (longer is 422). Every route here is open to the PC and a phone
except where a row says otherwise.

Journal kinds now include `overlap` (the KD2 warning, with `text`). The feeds carry CUSTOM `overlap_warning` (value:
the overlap with `text` and `session_id`).

## External agents' tool API (the agent listener, `BTCFLOW_AGENT_PORT`)

An external agent (an MCP client you run) calls the app's tools on the **agent listener** only
(`http://localhost:8002` in `compose.yaml`; 2026-10-31, review fix P0 #1). Its requests are actor `agent-api`: never
the PC or a person, whatever their address or headers. On the PC and phone listeners these paths answer 403
`{"ok": false, "code": "agent_port"}`; on the agent listener every other path answers 404.

| Route | Body / header | Answer |
|---|---|---|
| `GET /api/agent/tools` | | `{ok, tools: [{name, description, input_schema, kind: read / write / session}]}` (no data) |
| `POST /api/agent/call/{tool}` | the tool's arguments (JSON object); `X-Agent-Token: <the token shown once at launch>` | the tool's answer. Every call, reads too, needs the token of a live external session: else 403 `no_agent_session` and nothing is read or prepared (the token is checked before the body is read). The agent actor (`agent:<8>`) is set only after the token checks out. |
| `GET /api/agent/events/wait` | `?timeout_s<=25` | `not_available` (poll `observe` and `get_journal`) |

The `get_journal` tool (`{after_id?, limit? 1-200, default 50}`) answers `{ok, rows: [{id, ts, kind, data}], more,
next_after_id}` and pages as `GET .../journal`: no `after_id` = the newest rows; with one, the oldest rows after it,
and while `more` is true the caller reads on with `after_id = next_after_id`.

Before this change the tool routes were on the PC's own port and reads needed no token; move the agent's base URL to
the agent port. The agent can never answer a proposal, resume, change limits or a mode, or post the presence
heartbeat: those routes need the paired PC browser or a phone's PIN session (`person_required`).

## Status, scorecard and rules

| route | body | answer |
|---|---|---|
| `GET /api/agent/status` (PC and phone) | | `{ok, in_app: {available, reason}, external: {available}, key: {present, valid, checked_ts, reason}, models: [{id, usd_per_mtok: {input, output, cache_read, cache_write}}], venues: {market key: {label, strategy, mode, rules, also_traded_by, next_agent}}, rules: [{id, markets, market_labels, label, description, status: {status, reason, at, by, since}}], allow_production, live_count, prompts: {system, user} (current versions), daily, global_limits: {limits, used}, wake_cap, settle_warnings, server_ms}` |
| `GET /api/agent/scorecard` (PC and phone) | | `{ok, rules, rows, auto, agents, server_ms}` (below) |
| `POST /api/agent/rules/status` (PC only) | `{rule (≤32), status: "retired" \| "active" (≤16), reason? (≤300), confirm? (≤50)}` | `{ok, changed, rule: {status, reason, at, by, since}}` |

- `status`: `in_app.reason` says why an in-app session cannot launch (no key, or the key was invalid at the last
  check); the Agent tab reads `key`, `models`, `venues`, `rules`, `allow_production`, `prompts`, `daily`,
  `global_limits`, `settle_warnings` and `server_ms` (its clock skew). `venues.{key}.mode` is that market's trading
  mode now (null: not in this app); `next_agent` `{agent_id, name}` is what a launch on that market with neither
  `agent_id` nor `name` runs. `daily` is the per-day limit shared with you `{per_day, used: {pool: dollars}}`, or
  `{error}` when it cannot be read (shown, never as zero); null when not wired. `settle_warnings`: positions on an
  agent market still settling well after the close, `[{id, ticker, side, held, minutes, last_read, message, venue,
  venue_label}]`, shown and never closed. `wake_cap`: the in-app wake queue's concurrency (null without a key).
- `scorecard`: `rules` per rule `{rule, label, status, live, trial (since the last reactivation), bar: {trades, of},
  backtest, proposals}`, `rows` per (rule, market) `{rule, market, market_label, status, live, backtest, proposals}`,
  `auto` `{trades, net_at_or_below, loss}` (the automatic retirement bar). A tally (`live`, `trial`) is `{trades,
  wins, win_pct, contracts, avg_price, break_even_pct, net_usd, net_c, max_drawdown_usd, last_trade_ts, pending,
  open}`; `backtest` `{row, n, won_pct, break_even_pct, net_c, note}` or null. `agents`: see "Changed shapes".
- `rules/status`: retiring needs no phrase; reactivating needs `confirm` exactly `REACTIVATE <rule>` (else
  `confirm_required`) and starts a fresh 30-trade trial. An unknown rule or status is `invalid`. Setting the status
  it already has answers `changed: false`. A change is journaled (`rule_status`) into every live session that arms the
  rule and sent on the feeds as CUSTOM `rule_status`.

## Prompts (the in-app agent's system and user prompts)

Two layers, `system` and `user`, each a list of numbered versions; saving, restoring and resetting each add a new
version (history is never rewritten). A session pins the versions it launched with (its view's `prompt_versions`), so
an edit applies to the next run. Reads are open to the PC and a phone; writes are PC only.

| route | body | answer |
|---|---|---|
| `GET /api/agent/prompts` | | `{ok, system: Prompt, user: Prompt, history: {system: [Hist], user: [Hist]}}` |
| `GET /api/agent/prompts/version?layer=system\|user&version=N` | | `{ok, layer, version, text}`; `unknown_version` |
| `POST /api/agent/prompts/save` (PC only) | `{layer, text}` | `{ok, changed, prompt: Prompt, system, user, history}` |
| `POST /api/agent/prompts/restore` (PC only) | `{layer, version (≥0)}` | the same: that version's text saved as a new version |
| `POST /api/agent/prompts/reset` (PC only) | `{layer}` | the same: the default saved as a new version |

`Prompt` = `{layer, version, text, created_ts, is_default}`; the user layer's version 0 is the empty default (no row).
`Hist` = `{version, created_ts, chars, preview (first 160 characters), is_default}`, newest first, at most 50 (the user
layer ends with version 0). Refusals: `too_long` (over 60,000 characters), `empty` (a blank system prompt),
`unknown_version`, `invalid`, `not_seeded` (no system prompt stored yet). Text identical to the current version adds
nothing (`changed: false`). A layer other than `system` or `user` is 422.

## Assist proposals

An assist session never places an armed rule's match itself: it proposes it, and only the user's Approve sends it,
through the slide's prepare -> OrderGate -> approve path (re-priced now; refused `outside_bounds` if the fresh ticket's
limit, worst case, side, market or BTC stop moved past the proposal's). Without assist in this app every route below
answers `assist_unavailable`.

| route | body | answer |
|---|---|---|
| `GET /api/agent/proposals?session_id=` (PC and phone) | | `{ok, session_id, pending: [Proposal], recent: [Proposal] (that session's last 20), server_ms}`; an unknown `session_id` is 404 `not_found` (as `GET /api/agent/sessions/{id}/proposals`); without `session_id`: every live session's pending, `session_id` null, `recent` [] |
| `POST /api/agent/proposals/approve` (people only) | `{id (1-64)}` | the trading approve's answer (`{ok: true, bracket, ...}`) plus `proposal`; or a refusal (with `proposal` when it names one) |
| `POST /api/agent/proposals/skip` (people only) | `{id}` | `{ok, proposal}`; nothing is sent (past its time it is resolved `expired`) |

Approve and skip need the paired PC or a phone's PIN session (403 `person_required`); a request carrying an
`X-Agent-Token` header is refused 403 `agent_cannot_approve`. Refusals: `not_found`, `already_answered` (with
`proposal`), `in_progress`, `expired`, `session_not_running`, `too_late`, `unavailable`, `outside_bounds`, `error`,
or the order gate's own code; a proposal is answered once and never retried.

`Proposal` = `{id, session_id, agent_name, request_id, rule, rule_label, market, market_label, ticker, side, stake,
max_price, btc_stop, ride_to_settlement, contracts, max_cost, ask, cost_at_ask, could_make, could_lose, sentence,
line, reason, inputs, note, record, ttl_s, created_ts, expires_ts, window_start, window_end, state (pending /
approving / approved / skipped / expired / refused), outcome: {state, code, reason, ts, by, bracket_id} | null, would:
{state, result, won, pnl} | null}` (`would`: how a skipped or expired proposal would have done, scored once the
window's result is known). The proposal card (`web/views/proposal.js`) reads `id`, `line`, `reason`, `btc_stop`,
`could_make`, `could_lose`, `max_cost`, `created_ts` and `expires_ts`. A session's stream carries CUSTOM
`proposal_new` and `proposal_resolved`.

## Presets (saved launch settings)

Registered with agent mode on (`adapters/http/telegram_routes.py`, logic in `application/agent_presets.py`). A preset
is a launch body saved under a name, so a session can start from Telegram's `/launch` without typing limits on the
phone. It fixes everything: level, limits, rules, its one market and that market's trading mode when saved (a preset
whose market has changed mode since is refused at `/launch`, `mode_changed`).

| route | body | answer |
|---|---|---|
| `GET /api/agent/presets` (PC and phone) | | `{ok, presets: [{name, launch, modes: {market: mode}, created_ts}]}`, oldest first |
| `POST /api/agent/presets` (PC only) | `{name, launch}` | `{ok, preset, replaced}` |
| `DELETE /api/agent/presets/{name}` (PC only) | | `{ok}`; `not_found`. The name is normalized as a save stores it (spaces trimmed and runs of spaces collapsed), so `" H4  assist "` deletes `"H4 assist"` |

`launch` is the launch body without `confirm` (refused `invalid` if it has one), with `minutes` not `ends_ts`
(`invalid`), `kind: "in_app"` only (`in_app_only`: an external session's token would have to travel over Telegram),
and ONE market (`one_market`). It is checked with launch's own checks as a dry run, so any launch refusal code can come
back (`unknown_model`, `not_available`, `assist_unavailable`, ...). `name`: 1-40 characters after collapsing spaces
(`invalid_name`); saving an existing name replaces it. At most 10 presets (`too_many`). `launch.name`, when present,
is the agent name template: the idle agent of that name runs again, else a new agent with that name (or "<name> 2"
...).

## Changed shapes

- `GET /api/agent/status`: `venues.{key}` gains `rules` (rule ids for that market), `also_traded_by` and
  `next_agent` `{agent_id, name}` (what a launch on that market with neither `agent_id` nor `name` runs: the idle
  default agent, else the name a new one gets; New agent's prefilled name); new `live_count`, `global_limits {limits,
  used}`, `wake_cap`. `session` was removed (read `GET /api/agent/sessions`).
- `GET /api/record/summary` gains `by_agent` (Record's "By agent": per agent across its runs, net after API; see
  `docs/api.md`).
- `GET /api/agent/scorecard`: new `agents`: per session (live first, then the last 100) `{id, name, market,
  market_label, state, level, rules, started_ts, ended_ts, live: tally, api_usd, net_after_api}` (the "By agent"
  ranking).
- `GET /api/agent/proposals` without `session_id`: `pending` is every live session's pending proposals.
- `POST /api/agent/rules/status`: the change is journaled into every live session that arms the rule.
- Telegram: `GET /api/telegram/status` gains `muted: [session ids]`; new `POST /api/telegram/mute {session_id,
  muted: bool}` (PC and phone) -> `{ok, muted}`; `not_found` for an unknown session, `invalid` for a non-boolean.

## Removed (2026-10-31, with the fleet UI)

The single-session wrappers that acted on the most recently launched live session are gone (nothing used them once
the Agents tab named its session on every call): `POST /api/agent/stop`, `POST /api/agent/resume`,
`POST /api/agent/note`, `GET /api/agent/journal` and `GET /api/agent/decisions` (404: use
`/api/agent/sessions/{id}/...`), `GET /api/agent/stream` (404: each run's own stream is
`/api/agent/sessions/{id}/stream`), status's `session` and prompts' `session_versions` (each run's versions are its
view's `prompt_versions`). `GET /api/agent/proposals` without `session_id` stays (every live session's pending
proposals) with `session_id` null and `recent` [].
