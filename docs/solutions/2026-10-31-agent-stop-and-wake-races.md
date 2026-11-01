# Agent stop, pause and reservation races

Review finding P0 #4 (docs/plans/2026-10-31-1800-review-fix-plan.md), verified against the code and fixed on
`fix/p0-agent-races`. Tests: `tests/agent/test_races.py`, `tests/telegram/test_races.py`.

## 1. Stop lost a rule entry in flight

**Problem.** A Stop while a rule's IoC was on its way could end the session before the order was answered. In the
fleet the session ended first and the fill, the "rule" journal row and the decision row landed on an ended session
(the end summary said "No positions left open"). In a session run on its own (`AgentRunner.run`) the cancelled loop
dropped the "rule" row entirely, so `RuleBook` (which joins "rule" rows to brackets) never counted the trade and
auto-retire undercounted.

**Cause.** Only the agent's own write was tracked (`_inflight`); a rule entry ran inside whoever called
`rules_tick` (the fleet's step, the loop, the side task during a wake), and its journaling ran *after* the shielded
`tools.call` in that caller, so cancelling the caller skipped it. `AgentFleet.stop` awaited the step only after
`runner.stop` had ended the session.

**Fix.** Every order runs as its own tracked task (`AgentRunner._track`): the rule entry's whole body (send, "rule"
row, decision row, rule_entry event) is one task, awaited shielded. `stop` cancels the loop and the wake, then waits
for every tracked task (`stop_drain_s`, 30 s), re-reads the brackets, and only then ends the session.

**How to avoid.** Anything that must be recorded after an awaited order belongs in the same task as the order, not
in its caller. Shielding the inner call is not enough when the caller does the journaling.

## 2. A queued wake ran after Pause

**Problem.** The fleet checks a session's state when it takes a wake off the queue, but the wake task starts on a
later loop iteration; a Pause (or Stop, or end) in between still ran a model turn, and an ended session raised
`TypeError` (`s["model"]` on None) outside the wake's try.

**Fix.** `wake()` re-checks first (not running: `{status: "not_running"}`, no journal row, events kept for the next
wake) and again after reading the wake message; `_execute` refuses a write while paused. The gate between turns
already stopped later turns.

## 3. A tool call's re-baseline swallowed other positions' news

**Problem.** After every tool call and every rule entry the runner re-read *all* the session's brackets with
`push=False`, so another position's fill, stop-out or settlement that happened during the call was recorded as seen
and never told to the agent (and `_closed_seen` never set, so no auto-retire check).

**Fix.** `_scan(push=True, quiet={touched bracket})`: only the bracket the action touched is re-baselined silently.

## 4. Concurrent wakes overshot the global daily API limit

**Problem.** Each wake checked `api_day - used` and reserved its worst case locally; up to the wake cap (4) wakes
could each reserve the same last dollars.

**Fix.** A fleet-wide ledger in `AgentSessions` (`hold_api`, `global_api_left(exclude=)`): a turn's reservation is
held there (the runner's `_reserved` setter) and every other session's check subtracts it. Check and hold happen with
no await between, which is atomic on one event loop. A wake that does not fit because of the others' holds is
treated as the limit reached (every agent pauses), the same conservative rule as before.

## Related, smaller

- The spend gate ran only on PostToolUse: a failed call (PostToolUseFailure) or one denied in PreToolUse went to the
  next model turn unchecked. All three hooks now gate, and every hook matches every tool (`matcher=None`).
  Unverified against the real CLI here (no CLI on this ARM64 box): whether an MCP `is_error` result fires
  PostToolUse or PostToolUseFailure; both are covered.
- A write tool that raised left `_inflight` set; now try/finally, recorded as `error`, told to the model.
- Telegram: the "own stop" flag was a global counter, so another agent's pause during a `/stop` was hidden; it is a
  set of the session ids the command acts on.
