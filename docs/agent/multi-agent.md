# Multi-agent

Plan: `docs/plans/2026-10-31-0010-feat-multi-agent-plan.md`. Routes: `docs/api-agents.md`.

## Sessions

Any number of agent sessions run at once. `AgentSessions` (`application/agent_sessions.py`) is a registry keyed by
session id; there is no "the live session". Every read and write names its session: status, journal, pause, resume,
stop, note, usage, takeover, proposals, scorecard lines. A launch never waits for another session to end. App restart
still ends every live session ("restart"); positions stay.

## Agents are lasting; sessions are runs

An agent is a lasting identity with a name and one market (table `agents`). Each session is one run of one agent,
and every session and journal row carries the agent's id, so an agent's journal covers all of its runs
(`GET /api/agent/agents/{id}/journal`, newest first, with a summary of each run for the run dividers). An agent runs
one session at a time.

A launch names an existing agent (`agent_id`), or creates one (`name`). With neither, it runs the market's idle
default agent ("Kalshi 15m agent", then "Kalshi 15m agent 2" and so on); if none is idle, it creates the next one.
Names are unique among unarchived agents and can be changed. A preset's name is a template: it runs that agent again
when it is idle, and otherwise a new "H4 agent 2".

Sessions from before agents existed were grouped by migration 0016: one agent per (market list, name). Old
multi-market sessions became an archived "Earlier runs: ..." agent, which keeps its history and cannot run again.
There is no persistent learning memory yet; that comes later.

## One agent, one market

A session trades exactly one market (`limits.market`). Asking for two is refused (`one_market`): launch a second
agent instead. The old `venues` field still works as a list of one market, so today's Agent tab and saved presets keep
working. A rule must trade the agent's market.

## Orders and ownership

Every agent order still goes through prepare, then the OrderGate, then approve. The gate finds the session from the
acting `agent:<id>` context and checks that session's own limits: budget, per-trade cap, pinned mode, level.

An agent can see, exit or change only its own positions. `observe` hides other agents' positions; an exit or stop
change on someone else's position is refused (`not_yours`). The user's positions stay visible as "you".

Two agents may trade the same market (KD2). If they hold opposite sides of one window, the app records a warning in
both journals, sends `overlap_warning` on the feeds and Telegram, and lists it under `overlaps` and `needs_you`.
Nothing is blocked. Same-market overlap also depends on the trading service's own `max open positions` setting,
which is per venue and shared by every agent and you.

## Global limits (KD3)

Off by default and set on the PC (`POST /api/agent/global_limits`):

- `max_loss_day`: the most all agents together may lose in a local day (midnight to midnight, as Record). That is realized net loss plus the worst case of
  every agent position opened that day.
- `api_day`: the estimated API spend of every session started that day or still live.

When a limit is set, the gate checks it before each agent entry. An entry that would pass `max_loss_day` is refused
(`over_global_limit`). Once either limit is reached, every running agent pauses with `global_limit`. Resume works only
after you raise or turn off the limit, or after local midnight passes. The runner also stops a model turn that would
not fit `api_day`, counting every other running wake's reserved worst case (`AgentSessions.hold_api`, one ledger for
the fleet; docs/solutions/2026-10-31-agent-stop-and-wake-races.md).


The kill switch pauses every agent (`kill_switch`).

## The fleet loop and the wake queue (KD4, KD5, KD6)

`AgentFleet` (`application/agent_fleet.py`) runs one tick loop for every in-app session. It starts with the first
in-app launch as one supervised task. Each tick:

1. The sessions review themselves: end times, mode changes, the kill switch and the global limits.
2. Each market armed by a live session is read once with `PlayService.now_view`, and every session on that market
   shares the view. Call and light flips are detected once per market through one shared `SignalBoard`; each session
   turns them into its own events.
3. Each session's `AgentRunner.step` runs as its own task. That covers its account diff, its event engine and its
   armed rules, which open or propose entries in code. A session's slow order never delays another session, and a
   step never overlaps the same session's previous one. Rules run every tick, even while that session's model wake is
   running or queued. Every rule entry has one price cap, Auto or Assist alike (`agent_rules.entry_cap`): the ask the
   rule matched (Assist: the ask shown in the proposal) + 2¢, never above the rule's own cap; a match without a usable
   ask sends nothing. A setup light's match carries the same cap.
4. New overlaps are warned once.
5. Each session whose engine says a wake is due is offered to the wake queue.

The wake queue (`WakeScheduler`) is shared by the whole process. At most `BTCFLOW_AGENT_WAKE_CONCURRENCY` model wakes
(Claude Code CLI processes) run at once; the default is 4. Urgent wakes (fills, near the stop, settlements, rule
entries, notes, alarms) start before normal ones, and each class is first come, first served. A session has at most
one queued wake. A newer normal request merges into the waiting one and keeps its place; the events are taken when the
wake starts, so it sees the latest state. An urgent request moves a waiting normal wake into the urgent class. Each
agent's row shows its queue state, its place, how long it has waited, and its wait times so far.

Stop ends one session: its queued wake is dropped, its model call is cancelled, any order in flight finishes and is
recorded, and the other agents keep running. Stop all stops every live session.

## Telegram (KD8)

There is still one bot. Every alert names its agent. `/status` lists the agents with numbers and the total. `/stop`
with one agent stops it at once. With several agents it asks which one, with a button per agent and an All button,
and each needs a second tap to confirm. `/stop <n>`, `/stop <name>` and `/stop all` act at once. `/pause` and `/note`
take `<n>` when more than one agent is running.

The pinned message is the fleet line, followed by one line per agent (at most 5). The hourly digest covers the whole
fleet with one line per agent. The daily summary adds a line per agent. `/mute <n>` turns off that agent's trade
alerts; its pauses, its end and its proposals still arrive.

## The Agents tab (U6, U6b)

The web app's Agents tab is the fleet: a summary card (net today, winning of N, budget, API), the agents that need
you first in amber (a proposal waiting, a pause, 90% of a budget or API cap, an overlap), then one row per agent;
more than eight live agents fold into one row per market and a "N running normally · best · worst" sentence. Agents
that are not running are listed with their last run. Tapping an agent opens its screen for its live run, else its
last; All activity there reads the agent's journal across every run (`GET /api/agent/agents/{id}/journal`) with a
divider per run, and the fleet's audit reads every agent's (`GET /api/agent/journal/all`). New agent picks one
market first; starting needs the typed START (the server requires it) and then a slide. Stop on the bar above the
tabs stops the only agent at once, or asks "Stop all?" on screen when several run. Play shows one line when agents
are on the market you are viewing. Record's "By agent" ranks agents by net after API cost, with Clone and Retire
(`POST /api/agent/agents/{id}/archive`, PC only). Engineering note: the audit's run dividers repeat, so their list
keys include the row (see [docs/solutions/alpine-x-for-duplicate-keys.md](../solutions/alpine-x-for-duplicate-keys.md)).
