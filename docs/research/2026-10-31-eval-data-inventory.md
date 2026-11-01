# Agent eval data inventory (2026-10-31)

Input for the eval plan (`docs/plans/2026-10-31-1900-agent-evals-plan.md`, build step 1, the export tool). It answers
how much real agent history we hold, whether it can be replayed, and what the golden / replay / adversarial / holdout
sets can be made of.

Source: the final SQLite snapshot from the Postgres cut-over, `data/backups/final/btcflow-final.db`, opened read-only.
It covers everything up to 2026-10-31 13:34 CDT. All times below are CDT. Sessions after 13:34 exist only in Postgres
and can be added later through the export tool. They were not read for this inventory.

This file holds counts and journal ids (`agent_journal.id`) only. It has no account ids, order ids, balances, tokens
or prompt text.

## Headline

| | count |
|---|---|
| Agent sessions | 22 (20 in-app, 2 external/MCP smoke tests) |
| Span | 2026-10-30 16:45 to 2026-10-31 12:34 (about 20 hours; 2 calendar days) |
| In-app session time | 1,420 session-minutes (sessions overlap: 2 ran in parallel from 06:44) |
| Journal rows | 5,306 (wake 931, tool 2,378, decision 997, notes 891, rule 63, state 44, rule_status 1, note 1) |
| Wakes (model runs) | 931 (Haiku 5.5: 890, Opus 5.5: 41) |
| Agent decisions | 934 from `record_decision` (in 931 wakes) + 63 rule-entry decision rows |
| Tool calls inside wakes | 2,375 |
| Rule entries (real money, $1 forward tests) | 63 trades, 19 won |
| Assist proposals | 0 |
| Gate refusals of agent orders | 0 |
| Model refusals | 0 |
| Pauses | 0 |
| Agent self-stops | 1 (journal 4098, the 09:51 "mode conflict") |
| User notes to in-app agents / Telegram notes | 0 / 0 (one phone note went to an external session, id 462) |
| API spend on wakes | $5.93 estimated (Opus $3.42 over 41 wakes, Haiku $2.51 over 890 wakes) |

**Replay verdict: exact frozen replay is not possible from the journal as stored.** The wake row keeps the events
block, every tool call with its arguments and result (capped), the decision, the text and tool calls in order, a
summary of the thinking, tokens, cost and prompt versions. It does **not** keep the two largest inputs: the
`<memory>` block and the inline `<observe>` result. It also does not keep the tool specs or the code version. The
memory block can mostly be rebuilt from other rows. The observe can only be rebuilt from the market-data tables
(approximate), by re-running the server code at that time. Details are under "Completeness for replay" below.

## Sessions

| session | kind | model | user prompt | markets | rules armed | level | start, end | min | end reason |
|---|---|---|---|---|---|---|---|---|---|
| 72bc4abe | in_app | Opus | v0 | K15, P15, P5 | none | - | 10-30 16:45, 17:24 | 39 | stopped |
| e5d5c956 | in_app | Opus | v0 | K15 | none | - | 17:24, 17:35 | 11 | stopped |
| a1db0786 | in_app | Opus | v1 | K15 | none | - | 17:35, 17:36 | 1 | stopped |
| b48fd0e1 | in_app | Opus | v2 | K15 | none | - | 17:36, 17:43 | 7 | stopped |
| a6cf0e3b | in_app | Opus | v2 | K15 | none | - | 17:43, 18:19 | 36 | stopped |
| a7f749c7 | in_app | Opus | v3 | K15 | H4, CUSHION100 | - | 18:20, 18:34 | 14 | stopped |
| 5dd586af | in_app | Haiku | v3 | K15 | H4, CUSHION100 | - | 18:34, 19:45 | 72 | stopped |
| d8ee1a1b | external | - | - | K15 | - | - | 19:47, 19:47 | 0 | stopped |
| 1cf39e22 | external | - | - | P15, P5 | - | - | 19:47, 19:58 | 10 | stopped |
| 5ea62f42 | in_app | Haiku | v3 | K15, P15, P5 | H4, CUSHION100, HPANIC, LATERESID | - | 19:58, 20:09 | 11 | stopped |
| ab81e09c | in_app | Haiku | v3 | K15, P15, P5 | H4, CUSHION100, LATERESID | - | 20:09, 20:21 | 13 | stopped |
| 54f31d2d | in_app | Haiku | v3 | K15, P15 | H4, CUSHION100, LATERESID | - | 20:21, 21:03 | 42 | restart |
| 1d7c90ad | in_app | Haiku | v3 | K15 | H4, CUSHION100, LATERESID | assist | 22:08, 22:41 | 33 | restart |
| 97bbbcab | in_app | Haiku | v3 | K15 | H4, CUSHION100, LATERESID | auto | 22:41, 23:30 | 48 | restart |
| 9a18b222 | in_app | Haiku | v3 | K15 | H4, CUSHION100, LATERESID | auto | 23:30, 10-31 02:10 | 160 | stopped |
| f2a3d93e | in_app | Haiku | v3 | K15, P15 | H4, CUSHION100, LATERESID | auto | 10-31 02:10, 03:01 | 51 | restart |
| 86e1d51d | in_app | Haiku | v3 | K15, P15 | H4, CUSHION100, LATERESID | auto | 03:02, 06:36 | 214 | time |
| dd13298f | in_app | Haiku | v3 | K15 | H4, CUSHION100, LATERESID | auto | 06:44, 07:47 | 63 | stopped |
| e9ea5a71 | in_app | Haiku | v3 | P15 | H4, CUSHION100, LATERESID | auto | 06:44, 07:47 | 62 | stopped |
| afc6be03 | in_app | Haiku | v3 | K15 | H4, CUSHION100, LATERESID, HPANIC | auto | 07:47, 12:34 | 287 | stopped |
| 1d698618 | in_app | Haiku | v3 | P15 | H4, CUSHION100, LATERESID, HPANIC | auto | 07:47, 09:51 | 124 | **agent** (self-stop) |
| f39db269 | in_app | Haiku | v4 | P15 | H4, CUSHION100, LATERESID, HPANIC | auto | 10:22, 12:34 | 132 | stopped |

K15 = kalshi_15m, P15 = polymarket_15m, P5 = polymarket_5m. Level "-" means the session started before the level
field existed (it ran as auto). Every session used system prompt v1. All five prompt rows (system v1, user v1-v4) are
still in `agent_prompts`, so each wake's prompt text can be recovered from its `prompt_versions`. "v0" means no user
layer.

Summary of the 20 in-app sessions:

| dimension | split |
|---|---|
| model | Opus 6 sessions (41 wakes), Haiku 14 sessions (890 wakes) |
| level | not set 10, assist 1, auto 9 |
| markets | K15 only 11, P15 only 3, K15+P15 3, all three 3 |
| rules armed | none 5 (all Opus, "model judgment" era), H4+CUSHION100 2, those + LATERESID 9, all four 4 |
| user prompt | v0 2, v1 1, v2 2, v3 14, v4 1 |
| end reason | stopped 14, restart 4, time 1, agent 1 |

The 09:51 self-stop: in session 1d698618, wake 4098 called `end_session` with reason "Mode conflict" (the agent saw
PRODUCTION with real money while it thought the armed rules were described as experimental $1 tests). It failed
closed. The wake row has status `session_stopped`, and state row 4096 records the end. The user then wrote user prompt
v4 (10:22) and relaunched on Polymarket (f39db269).

## Wakes

931 wakes. Status: done 929, session_stopped 1 (4098), cancelled 1 (5305, the user's 12:34 stop arrived mid-wake).
Stop reason: end_turn 929, tool_use 2. Reason: urgent 498, normal 433.

By day: 10-30 207, 10-31 724. By session markets: K15 only 452, P15 only 223, K15+P15 212, all three 44. By prompt
version (user layer): v3 805, v4 99, v2 16, v0 10, v1 1. Haiku with user prompt v3 alone: 791.

### Triggers

One wake often coalesces several events. "Wakes containing" counts every type present. "Primary" assigns each wake
to one type, in this priority order: rule_entry > settled > position_closed > near_stop > near_take_profit >
session_ending > entry_check > window_ready > window_open > agent_alarm > btc_near_strike > window_ending >
call_changed > price_move > advice_* > signal_changed.

| trigger | wakes containing | primary |
|---|---|---|
| agent_alarm | 360 | 319 |
| price_move | 278 | 194 |
| call_changed | 106 | 88 |
| entry_check | 80 | 77 |
| window_ready | 69 | 65 |
| advice_cleared | 57 | 21 |
| signal_changed | 56 | 5 |
| advice_changed | 52 | 18 |
| window_ending | 51 | 5 |
| settled | 48 | 48 |
| rule_entry | 43 | 43 |
| btc_near_strike | 32 | 27 |
| window_open (pre-rules era) | 8 | 8 |
| near_stop | 8 | 8 |
| near_take_profit | 2 | 2 |
| position_closed | 2 | 2 |
| session_ending | 1 | 1 |

Event classes across all coalesced events: normal 815, urgent 597. There are no `note`, `rule_status`, `proposal` or
`order_refused` triggers in this snapshot. 21 wakes got `new_events` mid-wake (urgent events riding on a tool
result).

### Decisions

| action | pass_reason | count |
|---|---|---|
| NO_TRADE | NO_VALIDATED_EDGE | 321 |
| HOLD | NO_MATERIAL_CHANGE | 265 |
| NO_TRADE | NO_MATERIAL_CHANGE | 251 |
| NO_TRADE | INSUFFICIENT_TIME | 63 |
| ENTRY (source rule) | - | 63 |
| HOLD | none | 15 |
| NO_TRADE | STALE_DATA | 7 |
| NO_TRADE | RISK_LIMIT | 6 |
| NO_TRADE | UNSUPPORTED_MODEL | 4 |
| NO_TRADE | INSUFFICIENT_EDGE | 2 |

The agent itself never recorded ENTRY, EXIT, REDUCE or HEDGE. Every ENTRY row is the app's rule entry. Every wake has
a successful `record_decision`, so there are no UNRECORDED rows. 3 wakes recorded two decisions (1421, 2953, 2958).

p_source: experimental 638, unknown 239, market_implied 113, model 7. Decision market: K15 723, P15 251, P5 18,
none 5. Primary trigger by decision: every settled, entry_check, window_ready and window_open wake ended NO_TRADE.
Every rule_entry, near_stop, near_take_profit, btc_near_strike and advice_* wake ended HOLD. agent_alarm wakes ended
259 NO_TRADE and 60 HOLD; price_move wakes 116 NO_TRADE and 78 HOLD.

**p_estimate vs market:** 427 of 934 agent decisions have no p_estimate. 505 have both p_estimate and market_p.
Comparing p_estimate against the closer of market_p and 1 - market_p, the gap is at least 0.05 in 147, at least 0.10
in 56 and at least 0.15 in 24. In 21 decisions the raw gap is at least 0.5 but the aligned gap is under 0.05, for
example 2105 (HOLD DOWN, p_estimate 0.01, market_p 0.9975). Here the agent gave p for its own side while market_p is
the UP price. The convention (is p_estimate P(UP) or P(side)?) has to be pinned in the tool schema or the grader
before "disagreement" can be graded.

### Tool calls (inside wake rows)

| tool | ok | error code | count |
|---|---|---|---|
| record_decision | yes | | 934 |
| update_notes | yes | | 891 |
| set_alarms | yes | | 339 |
| snooze | yes | | 131 |
| record_decision | no | not_run | 63 |
| observe | yes | | 7 |
| record_decision | no | invalid | 3 |
| update_notes | no | not_run | 2 |
| set_alarms | no | not_run | 2 |
| set_alarms | no | invalid | 1 |
| get_scoreboard | yes | | 1 |
| end_session | yes | | 1 |

- `not_run` (67 calls in 55 wakes): the model emitted a second, parallel `record_decision` (or another call) that the
  runtime did not run. It is a recurring Haiku habit across 12 sessions and both days. Good material for a step
  grader ("one record_decision per wake").
- `invalid` (4): 1139, 4287, 5124 (`record_decision` with p_low > p_estimate or p_estimate > p_high), 2724
  (`set_alarms` seconds_left_below alarm without venue/span). In each case the agent recovered within the wake.
- The agent never called `place_entry`, `place_exit`, `set_btc_stop`, `set_take_profit` or `quote_entry`. No
  `rules_own_entries` refusal exists. Only the two external smoke sessions issued writes (15 tool rows, 445-461:
  place_entry, place_exit, set_btc_stop; all ok).

### Turns, tokens, cost per wake

| | Haiku (n=890) p50 / p90 / max | Opus (n=41) p50 / p90 / max |
|---|---|---|
| turns | 2 / 3 / 6 (cap 6) | 2 / 3 / 4 |
| tool calls | 3 / 3 / 7 | 3 / 3 / 4 |
| output tokens | 2,123 / 3,043 / 5,550 | 1,454 / 1,986 / 2,789 |
| cache write tokens | 9,682 / 12,429 / 28,123 | 7,154 / 19,048 / 22,268 |
| cache read tokens | 38,810 / 61,986 / 143,616 | 36,010 / 57,733 / 86,075 |
| uncached input tokens | 4 / 6 / 12 | 4 / 6 / 8 |
| est. cost $ | 0.0027 / 0.0035 / 0.0131 | 0.075 / 0.132 / 0.147 |
| duration s | 9.6 / 13.7 / 25.3 | 14.9 / 21.0 / 27.6 |
| wake row bytes | 6,182 / 7,773 / 14,854 | 5,575 / 7,191 / 10,827 |

Mean Haiku wake: $0.0028. A 300-item replay run at k=3 trials costs about $2.50 on Haiku (before any judge).

## Trades

### Rule entries (all real money: Kalshi prod 37 brackets, Polymarket prod 32 across agent sessions)

| rule | market | trades | won | stop-outs |
|---|---|---|---|---|
| CUSHION100 | kalshi_15m | 4 | 4 | 1 (1579, still net positive) |
| CUSHION100 | polymarket_15m | 1 | 1 | 0 |
| HPANIC | kalshi_15m | 5 | 1 | 0 |
| HPANIC | polymarket_15m | 7 | 2 | 0 |
| HPANIC | polymarket_5m | 3 | 0 | 0 |
| LATERESID | kalshi_15m | 26 | 8 | 0 |
| LATERESID | polymarket_15m | 17 | 3 | 0 |
| **total** | | **63** | **19** | 1 |

Dollar totals are not kept in this doc; per-rule results (n, win % vs break-even, ¢/contract) are in LEARNINGS.md. Every attempt was ok and filled; no rule row
was refused. Exits: settled 60, take profit 2, BTC stop 1. No agent exits. By day: 10-30 10, 10-31 53. H4 never fired.
LATERESID was retired automatically at 12:15 (rule_status 5136: 43 trades, net ≤ $0, the scorecard's bar).

Rule-entry journal ids. Losses (44): 472, 508, 565, 791, 800, 981, 1126, 1653, 1656, 1767, 1770, 2087, 2090, 2317,
2324, 2454, 2457, 2593, 2596, 2829, 2832, 2932, 2935, 3125, 3128, 3255, 3258, 3512, 3515, 3645, 3650, 3928, 3931,
4153, 4270, 4273, 4465, 4468, 4859, 4862, 5054, 5057, 5216, 5219. Wins (19): 177, 248, 592, 1294, 1412, 1503, 1579,
1994, 1997, 2229, 2232, 2393, 2396, 3367, 3370, 3814, 3817, 4721, 4731.

Settled wakes linked to a rule trade: 37 after a loss (e.g. 553, 837, 843, 1005, 1693, 1797, 2118, 2124, 2356, 2362,
2484, 2631), 11 after a win (638, 1330, 1469, 1549, 2056, 2280, 2427, 3444, 3894, 4812, 4815).

### Assist proposals

**None.** The one assist session (1d7c90ad, 33 min, 14 wakes) had no rule match, so there are no proposal,
proposal_outcome or proposal_scored rows and no would-have P&L. The approve / skip / expire / refuse paths have no
real data and must be crafted (simulated environment).

### Gate audit

305 `trading_audit` rows carry an agent session id, all with actor `agent:`: proposed 72, approved 72, sent 72,
filled 69, tp_placed 8, stop_changed 6, cancelled (tp) 3, closed (exit) 3. That covers the 63 rule entries plus the
two external smoke sessions. No refusal codes.

## Completeness for replay

What one wake's model input was (agent_runner `_wake`): system = system prompt + user layer + TOOL_NOTE; the tool
specs; one user message = `<memory>` + `<events>` + `<observe>`.

| input / output | stored? | where | gap |
|---|---|---|---|
| System prompt text | by reference | `prompt_versions` on the wake + `agent_prompts` (system v1, user v1-v4 all present) | TOOL_NOTE and the tool specs live in code; the wake row has no git sha |
| Model, SDK | yes | `model`; `runtime.runtime` (claude-agent-sdk 0.2.168), `cli_version` (930 of 931; the cancelled wake has none) | effort, max_tokens, max_turns are config defaults, not recorded |
| `<events>` | **yes, verbatim** | `trigger` (the coalesced events the message carried; p50 457 B, max 3 KB) | none |
| `<memory>` | **no** | rebuild: session limits (`agent_sessions.limits`), recent 5 decisions (decision rows before ts; text cut to 200 chars), notes (last notes row; full text, p50 873 chars, cap 6,000), API spend and wakes (sum of earlier wake rows), rule status (rule_status rows + tally), alarms / snoozed (replay set_alarms / snooze rows), FACTS (code constant) | `trades` needs the brackets' state *at ts*, but `trading_brackets` holds only the final state (rebuild from `trading_audit`); `markets` (the engine's schedule snapshot) is not stored; budget_used parts need the same time travel |
| `<observe>` | **no** | the largest input (markets, quotes, six lights, cone, BTC series, entry terms, positions with advice, mode, balances). Rebuildable only approximately by re-running observe's server code over `kalshi_quotes` (60,467 rows in the span), `book_1s` / `index_1s` (~71k each), `trades` (296k), `snapshots` (1,188), `decisions` (10,532); Polymarket quotes are in `polymarket_us-final.db` (not opened) | approximate, not recorded |
| Tool calls and args | yes, full | `tools[].args` in the wake row (the separate `tool` rows cut string args at 500 chars and only cover write/session tools) | none |
| Tool results | yes, capped | `tools[].result`, cut at 1,000 chars (`result_chars`) and stored as a string ending in "…" | 8 cut: observe in 218, 1421, 1511, 2953, 2958, 3947, 4876; get_scoreboard in 231. All other results are whole |
| Mid-wake urgent events | yes | `new_events` (21 wakes) | none |
| Text and call order | yes | `segments`, `decision` (final text) | none |
| Thinking | summary only | `thinking` (all 931) | no signatures, so it cannot be fed back as an assistant turn |
| Usage | yes | `tokens`, `est_usd`, `turns`, `duration_ms`, `runtime.usage`, `message_ids` | none |

What this means:
- **Trajectory and step grading of the recorded runs works now.** Calls, args, results, order, the decision and the
  events are complete for 931 wakes. The 8 cut results are reads and do not affect any write.
- **Re-running a new prompt or model on a stored wake (the replay set's purpose) needs the input rebuilt.** Memory:
  feasible with an exporter that time-travels the brackets through `trading_audit`; fidelity is "reconstructed". The
  observe cannot be recorded after the fact; re-rendering it from the market tables gives "approximate" items. Tag
  each item's fidelity and keep paired comparisons on the same rebuilt inputs (both baseline and candidate see the
  same rebuild, so the comparison stays fair even where the rebuild is not exact).
- **Fix for the future (needed before replay is exact):** journal the exact wake input. Either the full user message
  plus a hash of system+tools+git sha on the wake row, or a side table `agent_wake_inputs(wake journal id, message,
  system_hash, tools_hash, git_sha)` with a retention window. The current code does not record it, so the Postgres
  sessions after 13:34 have the same gap unless this lands.

### Sensitive data in the payloads (what an export must strip)

| what | where it occurs (row counts by kind) | treatment |
|---|---|---|
| Account cash / balances | free text: "cash $NN.NN" in wake 126, notes 120, tool 15, decision 3, state 1 (e.g. the self-stop reason, ids 4096/4098); "balance(s)" in wake 32, notes 23; and the `balances` field of every observe result | redact numbers after cash/balance(s)/equity to `<balance>`; drop `observe.balances` |
| Bracket ids (32-hex) | trigger ids / bracket_id, tool results (`bracket.id`), rule / decision `bracket_id`, and free text (notes 33, update_notes args 33) | replace with a salted stable hash (keeps links within the export) |
| request_id | rule rows (`rule:<id>:<market>:<window start>`, harmless) and agent args (may embed ids) | hash when it contains hex |
| Session / agent ids | every row (`session_id`, `agent_id`) | stable pseudonyms (`s01`, `a01`) |
| Device id of a note author | note 462 `by` = `phone:<id>` | replace with `user` |
| Anthropic message ids | `message_ids` (all wakes) | drop |
| Order ids, client order ids, ticket ids/hashes, TP order ids | not in the journal; they are in `trading_orders`, `trading_audit.detail`, `trading_brackets` | never export these tables raw; export only the derived outcome (rule, side, won, net, exit reason) |
| External write token | `agent_sessions.token_hash` | never export |
| Session limits (budget, per-trade, spend cap, budget used) | memory, limits | keep: session settings, not account data |
| Market tickers, strikes, BTC prices | everywhere | keep (public market data) |
| Real emails, API keys, IPs, file paths | none found | the exporter still runs a deny-list scan and fails on a hit |
| Prompt text | `agent_prompts` | system prompt v1 contains one 32-hex string: review it before copying into `evals/prompts/` |

## Interesting cases (journal ids)

| category | real count | example ids |
|---|---|---|
| Self-stop / fail closed on a mode conflict | 1 | 4098 (wake), 4096 (state) |
| Agent asks the user to confirm the production mode | 113 wakes | 479, 484, 489, 494, 501, 506, 515, 520, 526, 536 |
| Wake cut by a user stop (cancelled / session_stopped) | 2 | 5305, 4098 |
| Retirement notice handled (the wake after rule_status 5136) | 1 | 5141 |
| STALE_DATA pass (decision rows) | 7 | 4052, 4123, 5049, 5139, 5151, 5290, 5302 |
| Stale or missing quote mentioned in the answer | 35 wakes | 7, 218, 225, 231, 832, 843, 1330, 1549, 1740, 2050 |
| RISK_LIMIT pass | 6 | 545, 554, 560, 735, 1201, 4094 |
| UNSUPPORTED_MODEL pass | 4 | 87, 92, 144, 4057 |
| INSUFFICIENT_EDGE pass | 2 | 1191, 4872 |
| HOLD without a pass_reason | 15 | 597, 796, 805, 1130, 1299, 1584, 1661, 1775, 2095, 2465 |
| Invalid tool args, then recovered | 4 | 1139, 2724, 4287, 5124 |
| Duplicate parallel record_decision (not_run) | 55 wakes | 281, 383, 431, 610, 837, 1041, 1056, 1119, 1198, 1446 |
| Two successful record_decision in one wake | 3 | 1421, 2953, 2958 |
| Truncated tool result | 8 | 218, 231, 1421, 1511, 2953, 2958, 3947, 4876 |
| Mid-wake urgent events | 21 | 185, 225, 501, 1150, 1421, 1511, 1797, 2330, 2405, 2467 |
| Supervising a rule entry (rule_entry, HOLD) | 43 | 255, 479, 515, 572, 599, 798, 807, 988, 1133, 1301 |
| Near stop / near take profit | 8 / 2 | 218, 616, 1463, 1531, 1592, 1676, 2029, 4195 / 201, 265 |
| Settled after a loss / after a win | 37 / 11 | see Trades |
| Session ending | 1 | 2671 |
| Long wakes (5+ turns / 5+ tool calls) | 3 / 13 | 1651, 3894, 5214 / 1421, 2467, 3015, 3196, 3365, 3530 |
| p_estimate far from market (aligned gap >= 0.15) | 24 | 4576, 1541, 1449, 2344, 5240, 3847, 5066, 3403, 619, 4027 |
| p convention ambiguity (raw gap >= 0.5, aligned < 0.05) | 21 | 2105, 2110, 492, 539, 3570 |
| No-rules era, no trade without a validated edge (Opus) | 41 wakes | 7, 13, 19, 25 |
| Gate refusals, kill switch, pauses, spend cap, global limit | 0 | none: craft |
| Prompt injection in market text, notes or tool results | 0 | none: craft |
| User / Telegram notes to an in-app agent | 0 | none: craft |
| Assist proposals (approve / skip / expire / refuse) | 0 | none: craft |
| Agent-initiated exit or stop change | 0 in-app (15 external tool rows, 445-461, no wakes) | craft; use 445-461 as arg-shape fixtures |

## Proposed dataset composition

| set | target | real (from this snapshot) | crafted | environment |
|---|---|---|---|---|
| Golden | 60 | 40 | 20 | frozen inputs + simulated session store (decision, notes, alarm tools write to a fake session; reads return fixtures) |
| Replay | 300 | 300 | 0 | frozen (recorded tool results; a different call path is graded "diverged") |
| Adversarial | 40 | 0 (8 real frames reused as seeds) | 40 | simulated (shadow exchange, fake feed, the real OrderGate and session gate in-process) |
| Holdout | 100 | 100 | 0 | frozen, same as replay |
| **total** | **500** | **440** | **60** | |

### Golden (60)

| behaviour | real | crafted | source ids / note |
|---|---|---|---|
| Rule entry arrives: supervise, HOLD, never open or exit by itself | 7 | 0 | rule_entry wakes, both markets, all three firing rules |
| Settled: NO_TRADE, tally updated correctly | 5 | 0 | 3 after a loss, 2 after a win |
| Near stop / near TP: HOLD, never loosens the stop | 6 | 2 | all 8+2 real exist; craft one "move the stop down" temptation |
| Correct pass reason: STALE_DATA, RISK_LIMIT, UNSUPPORTED_MODEL, INSUFFICIENT_EDGE | 8 | 0 | ids above |
| Mode conflict: fail closed or ask the user, never trade | 4 | 2 | 4098 + 3 "please confirm" wakes; craft one pause and one ambiguous-mode case |
| Rule retirement: tells the user | 1 | 3 | 5141; craft HPANIC / CUSHION100 bars and a manual retire |
| Exactly one record_decision; recovers from invalid args | 4 | 0 | 1139, 2724, 4287, 5124 (duplicate not_run wakes as negatives) |
| Session ending / stopped mid-wake: no call after end | 2 | 1 | 2671, 5305; craft "stop arrives during a write" |
| No rules: no entry without a validated edge | 3 | 0 | Opus era |
| Kill switch on, session paused / stopping, spend near cap, global limit | 0 | 6 | none in the data |
| Assist: proposal shown, agent cannot approve | 0 | 3 | none in the data |
| User note asks for a trade in rules mode / asks to loosen a stop | 0 | 3 | none in the data |
| **total** | **40** | **20** | 60 |

### Adversarial (40, all crafted; seeds are real frames)

| category | items | seed |
|---|---|---|
| Injection in market / ticker / venue text | 8 | any window_ready frame |
| Injection in a user or Telegram note | 8 | the notes field of a rule_entry frame |
| Injection inside a tool result (observe, scoreboard, journal) | 6 | 218, 231 |
| Conflicting signals (call vs market vs model) | 4 | aligned-gap cases 4576, 1541 |
| Stale or broken feed (Polymarket "book feed broken") | 4 | 5049, 5302 |
| Limit edges: spend cap, budget, per-trade cap, global daily limit | 6 | 545, 1201 |
| Paused / ended / mode changed mid-wake | 4 | 4098, 5305 |

### Replay (300) stratified sampling

Pool: wakes before the holdout cut (802), minus any wake in a 15-minute window used by a golden item. Sample without
replacement, at most 2 wakes per (session, market window) so a few busy windows do not dominate.

| stratum (primary trigger) | pool (all 931) | sample |
|---|---|---|
| agent_alarm | 319 | 70 |
| price_move | 194 | 45 |
| call_changed | 88 | 25 |
| entry_check | 77 | 25 |
| window_ready | 65 | 25 |
| settled | 48 | 30 (oversampled; balance loss/win roughly 2:1) |
| rule_entry | 43 | 30 (oversampled) |
| btc_near_strike | 27 | 15 |
| advice_changed / cleared | 39 | 15 |
| rare (near_stop, near_tp, position_closed, session_ending, window_open/ending, signal_changed) | 31 | 20 (take all available pre-cut) |

Constraints across strata, each checked after sampling: K15 >= 50% and P15 >= 25% of items; both days present
(10-30 >= 25%); decisions NO_TRADE / HOLD close to their pool share (about 64 / 36); every pass_reason present;
prompt v4 >= 15 items (all v4 wakes 10:22-11:00); the Opus / no-rules era capped at 20 items and tagged (different
regime; report it separately). Fix the seed in the pipeline file.

### Holdout (100), split by date

- **Now:** cut at 2026-10-31 11:00 CDT, a 15-minute window boundary. Wakes from 11:00 to 12:34 (129, sessions
  afc6be03 K15 v3 and f39db269 P15 v4) form the holdout pool. Sample 100 with the same strata, dropping any window
  that holds a golden item (5141, 5305). Nothing at or after 11:00 is used for prompt work.
- **Leakage guards:** split on whole market windows, never single wakes (wakes in the same window share near-identical
  inputs); exclude the window that straddles the cut; the notes and recent decisions a holdout wake carries come from
  earlier wakes, which is fine as input but means no pre-cut wake from the same session may be graded as a golden
  "expected" for the same window.
- **Soon:** the 11:00 block has only two sessions, one per market, and confounds market with prompt version. Once the
  Postgres sessions after 13:34 are exported, they become the main holdout (strictly later dates, no overlap) and the
  11:00 block can join it. Never move holdout items into replay after they have been scored.

## Export tool spec (journal -> eval items)

Read-only on the source (SQLite snapshot, later a Postgres read replica or a dump). One run writes:

```
evals/datasets/replay/<snapshot>-<seed>.jsonl     one wake per line
evals/datasets/holdout/<snapshot>-<seed>.jsonl
evals/datasets/golden/<category>-<nn>.yaml          seeded from a wake; expectations added by hand
evals/datasets/adversarial/<category>-<nn>.yaml     crafted from a seed frame
evals/datasets/MANIFEST.yaml                        source snapshot, cut time, seed, counts per stratum, hash salt id
```

Replay / holdout line:

```yaml
item_id: sha256(salt + journal_id)[:12]
source: {snapshot: btcflow-final, journal_id, session: s07, agent: a02, wake_id}
meta: {ts, day, markets, level, model, prompt_versions, rules, sdk, cli, primary_trigger, trigger_types, decision_action, pass_reason, outcome_link}
fidelity: {events: exact, memory: reconstructed, observe: approximate | missing, tool_results: exact | truncated}
input:
  system_ref: prompts/system.md@1
  user_ref: prompts/layers/user.md@3
  tools_ref: <git sha of the agent tools at the wake's time>
  memory: {...rebuilt, ids hashed, balances redacted, provenance per field}
  events: [...trigger verbatim, ids hashed]
  observe: {...re-rendered from market tables, balances dropped} | null
recorded:
  segments, tools: [{name, args, ok, code, result, truncated}], decision (the record_decision args),
  final_text, new_events, status, stop_reason, turns, tokens, est_usd, duration_ms
  thinking_summary            # optional; judge input only, never fed to the agent
outcome: {trades: [{rule, market, side, won, net, exit_reason}]}   # reported, never graded
```

Keep: everything under `recorded`, the trigger, the prompt and model versions, the SDK / CLI versions, rule names, tickers,
strikes, BTC and market prices, session limits.
Redact or hash: the table in "Sensitive data" (balances to `<balance>`, `observe.balances` dropped, bracket ids and hex
request ids hashed with a salt kept outside git, session and agent ids pseudonymised, note authors to `user`,
message_ids dropped). Never read: `trading_orders`, `trading_audit.detail`, `agent_sessions.token_hash`, `.env*`,
`secrets/`.
Checks before writing: a deny-list scan (32-hex not from the hash map, `cash \$?\d`, `sk-`, `@` emails, order-id
shapes) fails the export on a hit; MANIFEST counts per stratum match the sampling plan; the same seed gives the same
files.

## Next steps

1. Record the exact wake input (message + system/tools hash + git sha) on new wakes, so the Postgres-era data replays
   exactly. Without it, every replay item is "reconstructed".
2. Pin the p_estimate convention (P(UP) or P(side)) in the `record_decision` schema text.
3. Build the exporter for the frozen replay/holdout set (memory rebuild with bracket time travel; observe re-render as
   a second phase).
4. Hand-label the 40 real golden seeds; craft the 20 golden and 40 adversarial items in the simulated environment.
