# Multi-agent: many bounded agent sessions at once

Status: approved direction (user, 2026-10-31: "I want to be able to have multiple agents running … if I wanted at some
point to have like 30 agents running, each with their own thing, I want the infrastructure to facilitate that").
Builds on docs/plans/2026-10-30-1402-feat-agent-mode-mcp-plan.md. Starts after the design pass lands (same screens).

## Product contract

- **One agent = one market** (user, 2026-10-31: "as a human I focus on one market … an agent can do Kalshi 15
  minute. That's what it can do. But then you can just have multiple agents … a Kalshi 15-minute agent, a Polymarket
  15-minute agent, a Polymarket 5-minute agent, etc., each individually running with their own rules, budget,
  monitoring"). A session's `market` is a single market key (kalshi_15m, polymarket_15m, polymarket_5m, later perps);
  multi-market sessions go away. Scaling out = launching more agents.
- Any number of agent sessions run at once. Each is today's bounded session: its own name, level (Assist / Auto),
  one market, rules, model, prompt layer, budget, per-trade cap, API cap, end time, Stop, journal and scorecard line.
- An agent's rules must exist for its market; a market without rules (Polymarket 5m today) runs model-judgment only
  until 5m rules are researched and added — launch says so plainly.
- Nothing about one agent's limits is shared with another unless the user sets a global limit (below).
- Every agent order still goes through prepare -> OrderGate -> approve, stamped with its own `agent:<id>` actor. The
  kill switch stops every agent at once.
- The user sees a list of agents and a combined total everywhere today's single session shows (tab-bar accessory,
  Agent tab, Record, Telegram pinned status, digests, daily summary).
- Target scale: 30 concurrent sessions on one machine without blocking the event loop or Play.

## Key decisions

- **KD1 — Session registry, not a singleton.** `AgentSessions` becomes a registry keyed by session id. "The live
  session" disappears from the API; every call names its session. `launch` no longer refuses `session_live`.
- **KD2 — Market overlap is allowed, shown, and owned.** (Each agent has one market; several agents may share it,
  e.g. two Kalshi 15m agents with different rules.) Two agents may arm the same market (different strategies are
  legitimate). Launch shows "Also traded by: <agent>" and the agent can only see, exit or modify its own positions.
  Opposite-side positions by two agents in one window get a visible warning (not a block) — add capability, not limits.
- **KD3 — Optional global limits, opt-in and shown.** A global max loss per day and a global API spend per day across
  all agents, off by default, set on the PC. When set, the OrderGate checks it before each agent order; reaching it
  pauses every agent with code `global_limit`.
- **KD4 — Rules don't wait for the model.** Rule entries/stops are code (agent_rules) and run on every tick for every
  session, independent of model wakes. Only model wakes are queued.
- **KD5 — Wake scheduler.** One process-wide queue for model wakes with a concurrency cap (default 4 parallel CLI
  runs, a setting; container-ready). Urgent wakes (fills, near-stop, settled, proposals) jump the queue; normal wakes
  coalesce per session (the newest supersedes an unstarted older one). Queue depth and wait time are visible per agent.
- **KD6 — One market-data feed, many readers.** Sessions read the shared Play/market views; no per-session
  subscriptions. Event detection (agent_events) runs once per market and fans out to the sessions that arm it.
- **KD7 — Names.** Each session gets a short name (default "<market> agent", e.g. "Kalshi 15m agent", then
  "Kalshi 15m agent 2"; editable at launch, unique among
  live sessions). Presets carry a name template.
- **KD8 — Telegram stays one bot.** /status lists agents (one line each + total); /stop asks which (or "all");
  /launch from presets as today; the pinned status shows the total plus one line per live agent (collapsed beyond 5).
  Alerts carry the agent name.

## Approved (user, 2026-10-31)

Fleet wireframe approved as drawn (scratchpad fleet-wireframe.html; artifact WXg6aLRKneim3FLzxeT5ZF). The tab is
renamed "Agents". Starting an agent is a slide, like an order.

## Units

- U1 Registry: sessions keyed by id; DB already stores sessions by id — check every query that assumes one live
  session (status, journal, takeover, pause codes, settle, scorecard, proposals, presets, Telegram, feed).
- U2 Gate and actor: OrderGate session step resolves the session from `agent:<id>`; per-session budget/limits; KD3 global
  limits; kill switch pauses all.
- U3 Runner and scheduler: one tick loop over live sessions; rule path per session (KD4); wake scheduler (KD5) with
  coalescing, priorities, cap, metrics; harness spawns per wake as today.
- U4 Events fan-out (KD6).
- U5 Routes: `/api/agent/sessions` (list), `/api/agent/sessions/{id}/…` for status, stop, pause, note, journal, feed;
  launch returns the new id; keep the old routes working against "all" for one release only if trivial, else remove.
- U6 UI — the human focuses on one market; the agents are managed by exception (user, 2026-10-31: "as a human I
  need to focus on one at a time, but if the agents are doing well I would have multiple agents doing multiple
  things"). Wireframe first (3 agents and 30 agents), user approves, then build:
  - Play stays single-focus; one quiet line when agents hold or arm the viewed market ("2 agents here · holding Up $1").
  - Agent tab = fleet: a summary card (total net, winning/losing count, budget used, API) then one row per agent
    (name, market, state chip, net, sparkline); "needs you" rows (proposal, pause, near a limit) sort first; healthy
    ones collapse into "N running normally"; tap a row for today's per-session view. Launch adds a name and an overlap
    notice.
  - Tab-bar accessory shows the count and total ("6 agents · +$3.10"), amber when any agent needs you; Stop all
    with a confirm.
  - Record gains "By agent" next to "By rule" (which agents earn their keep).
  - Telegram: fleet-level digest; per-agent lines only on events; alerts name the agent; mute per agent.
  - Built 2026-10-31 (U6 and U6b, the journal across runs): web/views/agent.js, tests/e2e/test_fleet_ui.py; the
    single-session routes were removed with it (docs/api-agents.md, "Removed").
- U6b Activity is an audit trail of the agent, not of one run (user 2026-10-31: "I look at the activity as an
  audit … I wanted to see what it's been doing"). An agent's All activity spans every run, newest first, with a run
  divider per session ("Run 3 · 22:35–23:41 · ended by restart · +$2.52 · 6 trades"), the same filters, infinite
  scroll by id; a fleet-level "All agents" audit on the Agents tab; search by rule, market and kind. Needs the
  lasting agent id (L0) on every session and journal row.
- U7 Telegram: KD8.
- U8 Scale test: 30 sessions on fake markets with a fake harness — tick latency, event-loop lag, wake queue fairness,
  memory; a budget so Play stays responsive. Document the numbers in LEARNINGS (engineering note).

## Trust (north star, user 2026-10-31)

"As we begin to trust agents to do work, we need to babysit them less … can the agents play the game the same way or
better than a human can? Whether that means it's profitable or not is a different question." The UX earns delegation
with evidence about conduct, shown apart from P&L. Wireframe first, then build (after the fleet):
- T1 Report card per agent (graded on conduct): Discipline (entries only on real rule matches; holds/exits per rule),
  Execution (paid vs the ask shown at the signal; signal-to-fill seconds), Coverage (matches taken vs missed, with
  reasons), Consistency (same inputs → same action). P&L beside it, labelled as the odds, not the grade.
- T2 Side by side with you: when the human trades the same market, agreement/disagreement and who was right after
  settlement.
- T3 Earned autonomy: Assist approvals/skips are votes; after N proposals suggest promotion ("You approved 19 of 20 …
  Promote to Auto?"); a discipline slip demotes to Assist automatically.
- T4 Fewer interruptions as trust grows: alert volume per agent steps down with its track record (new = every trade,
  proven = exceptions + daily).

## Learning loop (user 2026-10-31: "after every action there should be an agent codifying the learnings, so this
thing just gets better over time")

- L0 Agents are lasting identities; sessions are runs (user 2026-10-31: "part of their agent loop should be putting
  their learning somewhere, so the next [run benefits]"). Today `update_notes` and decisions live in the session's
  journal and the next session starts empty. Add a persistent per-agent memory (agent id = name + market): the loop
  writes notes and lesson candidates to it during and at the end of each run; the next run's memory block loads it
  (bounded, newest first, each lesson labelled with its status and n). Clone copies the memory; retire archives it.
- L1 Reviewer agent (cheap model) after each settled trade and at each session end writes structured lesson
  candidates: claim, evidence (trade ids, inputs), n, scope (agent / rule / market), status `candidate`.
- L2 Evidence gate: a candidate affects nothing until enough trades support it; then it is pre-registered and tested
  offline (scripts/experiments.py honesty rules, holdout). Pass → becomes a rule, a retirement or a proposed prompt
  change the user approves; fail → recorded as killed so it is not re-proposed. Never self-edit prompts from single
  outcomes.
- L3 Lessons view (Record → Lessons): candidate → testing → adopted / killed, each with its evidence; Telegram daily
  summary mentions new candidates and verdicts.
- L4 Every verdict is written to docs/research/LEARNINGS.md automatically (the register stays the source of truth).

## Publish prep (last)

Fresh public history (user, 2026-10-31): scrub per docs/PUBLISHING.md, secret scan (tool needs user OK), one squashed
commit on an orphan `public` branch authored with the GitHub no-reply email; the user pushes.

## Verification

- Unit: registry, gate per session and global, overlap ownership, scheduler priority/coalescing/cap, fan-out.
- Integration: two sessions on different markets; two on the same market with opposite sides; kill switch pauses all;
  global limit pauses all; Stop one leaves others running; restart ends all (as today).
- E2E (4 looks): list, launch second agent, accessory total, stop one, stop all.
- Live: two $1 sessions (Kalshi 15m, Polymarket 15m) for an hour, then grow.
