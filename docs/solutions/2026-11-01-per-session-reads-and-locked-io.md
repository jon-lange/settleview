# Per-session reads, I/O under the trading lock, and loop state read from threads

Review fix plan 2026-10-31, P1 "Event loop and database" / "Threads touching loop state" / "Hot paths".

**Problem.** Load grew with the number of agents and with history, mostly on the event loop:
- every fleet tick each session read its own brackets three times (account diff, the rules' "already holding?"
  check, the wake budget): 96 reads per tick at 30 sessions (21 at 5) in `tests/agent/test_fleet_load.py`;
- the agent list asked three queries per agent (91 for 30); the fleet view read the journal once per holding session;
- the Coach tick (twice a second) read 31 minutes of `index_1s` (1,862 rows) each time; the live odds series
  recomputed every point with a linear walk over 30 minutes of rows per point (21.6 ms a call);
- reconciliation and the external-fill check read every order id ever placed (`order_id IS NOT NULL`), every bracket
  of the env, and every other strategy's orders;
- `TradingService.tick` held the trading lock while it read REST quotes, so a slow order book delayed an approve or
  another position's stop;
- sync routes (FastAPI's threadpool: status, Play's view) iterated `TradingService._brackets` and Play's caches with
  Python-level comprehensions while the loop changed them, and `PerpPublisher.notify()` called `asyncio.Event.set()`
  from those threads.

**Cause.** Each piece was written for one session and a short history. Two non-obvious ones:
- `{k: v for k, v in d.items()}` iterates in bytecode, so the GIL can switch to the loop mid-iteration and the dict can
  change size; `list(d.values())` copies in one C call and is safe. A comprehension over that copy is fine.
- `asyncio.Event.set()` from another thread does not reliably wake a waiter (it resolves futures without waking the
  selector); the Perps view then refreshed only on its 1 s timer.

**Fix.**
- The fleet reads every session's brackets in one query per tick in a worker thread and hands each runner its rows
  (`AgentRunner.prime`); a runner drops them before any order of its own (a rule entry, a tool call, an approved
  proposal), and a scan after an action or during a wake always reads. Global limits are cached (only
  `set_global_limits` writes them). `RuleBook.enforce` was already shared and throttled per tick (P0, bc4e4fc).
- `runs(agent_ids=...)` batches the agent list (3 queries); `FleetView.rows` reads all holding sessions' rule links
  at once. Index `ix_trading_brackets_session` (migration 0022).
- `IndexTail` (application/index_tail.py) serves the Coach and Play from a cache of the newest 50 minutes, re-reading
  the last 10 s each call and everything once a minute. `minute_sigma` and the closing-minute lookup bisect instead of
  walking (same numbers, checked against the old walk); the live series keeps points 15 s old and rebuilds every 60 s.
- Reconciliation asks only about the resting orders' ids and the held markets; the external-fill check about the
  fills' order ids; `known_order_ids_fn(ids)` takes those ids.
- `tick` prefetches the REST quotes it will need concurrently before taking the lock; under the lock each bracket uses
  its prefetched quote (a failed read = no bid this tick, as before).
- Thread reads go through `_watched()` (a snapshot copy); `PerpPublisher.notify` hops onto its loop with
  `call_soon_threadsafe` from a thread.
- Record "all" keeps a `RuleLinks`: the journal's rule / decision rows read once, then only newer ids (re-reading the
  last 200, so an insert committed after a higher id was read is not missed). Index `ix_agent_journal_kind`.

Measured (SQLite, dev container): fleet tick at 30 sessions 96 -> 6 reads, 11.0 -> 2.1 ms median; agent list 91 -> 3;
Coach series 21.6 -> 2.2 ms; Coach tick 1,862 -> 10 index rows; Record "all" with 20,000 journal rows 15.2 -> 6.8 ms.

**Deferred.** Under the lock still: take-profit polls and the external positions read (they decide money-moving state
transitions; moving them needs a re-check design), and reconcile of a not-ready env. The notifier's per-second reads
moved to a worker thread; its other steps and the fleet's per-session journal writes stay on the loop.

**How to avoid.** Count queries per tick in a test with many sessions, not one. Never iterate loop-owned dicts from a
sync route without a C-level copy; never set an asyncio primitive from a thread. A scan "to know which ids are ours"
should ask about the ids being judged.
