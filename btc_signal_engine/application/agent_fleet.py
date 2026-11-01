"""The agent fleet: every live in-app session's runner on one tick loop, and one process-wide wake queue
(docs/plans/2026-10-31-0010-feat-multi-agent-plan.md U3/U4; KD4, KD5, KD6).

Tick (every `tick_s`, one supervised task for the whole fleet):
1. the sessions review themselves once for all (end times, mode changes, the kill switch and a reached global limit
   pause every agent: AgentSessions.review); open assist proposals expire once for all;
2. KD6: each market armed by a live session is read ONCE (`PlayService.now_view`) and that view is shared by every
   session on it (the call / light flip detection is shared too: the runners' engines use the fleet's SignalBoard);
   likewise every session's brackets are read in ONE query, in a worker thread, and handed to each runner
   (`AgentRunner.prime`) for its account diff, its rules' holds check and its wake budget: the fleet's database reads
   per tick no longer grow with the number of sessions (review fix plan 2026-10-31 P1: ~200-300 queries/s at 30);
3. KD4: every session's `AgentRunner.step` runs (its account diff, its event engine, its armed rules, which open or
   propose entries in code): concurrently, each as its own task, so one session's slow order send never holds up
   another's tick; a session whose previous step is still running skips this tick (it never overlaps itself). Rules
   run every tick whether or not a model wake is running or queued;
4. new opposite-side overlaps between sessions are warned once (AgentSessions.check_overlaps; KD2: shown, never
   blocked);
5. KD5: each session whose engine says a wake is due is offered to the wake queue.

Wake queue (`WakeScheduler`, pure and clock-injected): at most `cap` model wakes run at once across the fleet (a
setting, default 4); urgent wakes start before normal ones, each class first come first served; a session has at most
one queued wake: a newer normal offer coalesces into the unstarted one (it keeps its place; its events are taken
when it starts, so the newest state wins), an urgent offer upgrades it. A started wake runs as its own task
(`AgentRunner.wake`); the next queued one starts as soon as a slot frees. Per session it reports whether a wake is
queued or running, its place, how long it has waited, and its wait times so far (`metrics`); `summary` the whole
queue. Nothing here blocks the event loop: the model call is the harness's subprocess, awaited in its task.

Stop (`stop`): the session's queued wake is dropped, its runner stops (a write in flight finishes and is recorded,
the model call is cancelled), and the session ends; the other sessions keep running. `stop_all` stops every live
session (in-app and external). A session that ended any other way (end time, the agent's end_session, spend cap) is
dropped from the fleet at its next tick.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from btc_signal_engine.application.agent_runner import AgentRunner
from btc_signal_engine.application.agent_sessions import AgentSessions
from btc_signal_engine.application.ports import Clock
from btc_signal_engine.domain.markets import market as market_spec

log = logging.getLogger(__name__)

URGENT, NORMAL = "urgent", "normal"
PRIORITIES = (URGENT, NORMAL)
DEFAULT_CAP = 4


@dataclass
class _Queued:
    sid: str
    priority: str
    seq: int
    enqueued_ms: int
    offers: int = 1  # offers coalesced into this one (1 = just the first)


@dataclass
class _Stats:
    wakes: int = 0
    coalesced: int = 0
    total_wait_ms: int = 0
    max_wait_ms: int = 0
    last_wait_ms: int | None = None
    started_ms: int | None = None  # the running wake's start


@dataclass
class WakeScheduler:
    """The process-wide model-wake queue (KD5). See the module docstring."""

    cap: int = DEFAULT_CAP
    _queue: dict[str, _Queued] = field(default_factory=dict)
    _running: set[str] = field(default_factory=set)
    _stats: dict[str, _Stats] = field(default_factory=dict)
    _seq: int = 0
    # Told (priority, waited ms) as each wake starts: the composition root's telemetry (never raises into the queue).
    on_take: Callable[[str, int], None] | None = None

    def __post_init__(self) -> None:
        if isinstance(self.cap, bool) or not isinstance(self.cap, int) or self.cap < 1:
            raise ValueError(f"the wake concurrency cap must be a whole number of at least 1 (got {self.cap!r})")

    def offer(self, sid: str, priority: str, now: int) -> str:
        """Ask for a wake of `sid`: "running" (one runs; nothing queued), "queued", "coalesced" (into the unstarted
        one, which keeps its place) or "upgraded" (a queued normal wake became urgent)."""
        if priority not in PRIORITIES:
            raise ValueError(f"unknown wake priority {priority!r}; one of {', '.join(PRIORITIES)}")
        if sid in self._running:
            return "running"
        q = self._queue.get(sid)
        st = self._stats.setdefault(sid, _Stats())
        if q is not None:
            q.offers += 1
            st.coalesced += 1
            if priority == URGENT and q.priority != URGENT:
                q.priority = URGENT
                return "upgraded"
            return "coalesced"
        self._seq += 1
        self._queue[sid] = _Queued(sid, priority, self._seq, now)
        return "queued"

    def _order(self) -> list[_Queued]:
        return sorted(self._queue.values(), key=lambda q: (q.priority != URGENT, q.seq))

    def take(self, now: int) -> list[str]:
        """The sessions whose wake starts now (free slots, urgent first, then first come); marked running."""
        out = []
        for q in self._order():
            if len(self._running) >= self.cap:
                break
            del self._queue[q.sid]
            self._running.add(q.sid)
            st = self._stats.setdefault(q.sid, _Stats())
            wait = max(0, now - q.enqueued_ms)
            st.wakes += 1
            st.total_wait_ms += wait
            st.max_wait_ms = max(st.max_wait_ms, wait)
            st.last_wait_ms = wait
            st.started_ms = now
            out.append(q.sid)
            if self.on_take is not None:
                try:
                    self.on_take(q.priority, wait)
                except Exception:
                    log.debug("wake queue observer failed", exc_info=True)
        return out

    def done(self, sid: str) -> None:
        self._running.discard(sid)
        st = self._stats.get(sid)
        if st is not None:
            st.started_ms = None

    def cancel(self, sid: str) -> None:
        """The session is over: its queued wake goes (a running one is the runner's to cancel)."""
        self._queue.pop(sid, None)

    def forget(self, sid: str) -> None:
        self.cancel(sid)
        self._running.discard(sid)
        self._stats.pop(sid, None)

    def queued(self, sid: str) -> bool:
        return sid in self._queue

    def running(self, sid: str) -> bool:
        return sid in self._running

    def metrics(self, sid: str, now: int) -> dict:
        """One session's view of the queue: {state: idle | queued | running, priority, position (1 = next), waiting_ms,
        queue_depth (wakes queued ahead of it, plus its own), wakes, coalesced, last_wait_ms, max_wait_ms,
        avg_wait_ms}."""
        st = self._stats.get(sid) or _Stats()
        q = self._queue.get(sid)
        order = self._order()
        pos = next((i + 1 for i, x in enumerate(order) if x.sid == sid), None)
        return {"state": "running" if sid in self._running else "queued" if q else "idle",
                "priority": q.priority if q else None, "position": pos,
                "queue_depth": pos or 0, "waiting_ms": max(0, now - q.enqueued_ms) if q else None,
                "wakes": st.wakes, "coalesced": st.coalesced, "last_wait_ms": st.last_wait_ms,
                "max_wait_ms": st.max_wait_ms,
                "avg_wait_ms": round(st.total_wait_ms / st.wakes) if st.wakes else None}

    def summary(self, now: int) -> dict:
        order = self._order()
        return {"cap": self.cap, "running": len(self._running), "queued": len(order),
                "urgent_queued": sum(1 for q in order if q.priority == URGENT),
                "oldest_wait_ms": max((now - q.enqueued_ms for q in order), default=None)}


class AgentFleet:
    def __init__(self, sessions: AgentSessions, play: Any, make_runner: Callable[[], AgentRunner], *, clock: Clock,
                 cap: int = DEFAULT_CAP, tick_s: float = 1.0, proposals: Any | None = None, tools: Any | None = None,
                 sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep) -> None:
        """`make_runner()` -> a fresh, unattached AgentRunner (built with the fleet's SignalBoard); `tools` the
        registry (its per-session memo is dropped when a session leaves); `proposals` expire once per tick."""
        self.sessions, self.play, self._make = sessions, play, make_runner
        self.clock = clock
        self.scheduler = WakeScheduler(cap)
        self.tick_s = tick_s
        self.proposals, self.tools = proposals, tools
        self._sleep = sleep
        self._runners: dict[str, AgentRunner] = {}
        self._steps: dict[str, asyncio.Task] = {}
        self._wakes: dict[str, asyncio.Task] = {}
        self.last_tick_ms: int | None = None  # how long the last tick took (its own work, not the steps it awaits)

    def now(self) -> int:
        return self.clock.now_ms()

    # -- membership ---------------------------------------------------------------------------------------------------
    def attach(self, session_id: str) -> AgentRunner:
        """Give the live in-app session `session_id` its runner (idempotent). Its loop is the fleet's tick."""
        r = self._runners.get(session_id)
        if r is not None:
            return r
        r = self._make()
        if not r.attach(session_id):
            raise ValueError(f"session {session_id[:8]} is not live")
        self._runners[session_id] = r
        return r

    def runner(self, session_id: str) -> AgentRunner | None:
        return self._runners.get(session_id)

    def events(self, session_id: str) -> Any | None:
        """The session's event engine (the tool registry's alarms / snooze), or None."""
        r = self._runners.get(session_id)
        return r.events if r is not None else None

    def _drop(self, sid: str) -> None:
        r = self._runners.pop(sid, None)
        if r is not None:
            r.detach()
        self.scheduler.forget(sid)
        self._steps.pop(sid, None)
        if self.tools is not None and hasattr(self.tools, "forget"):
            self.tools.forget(sid)

    # -- the loop -------------------------------------------------------------------------------------------------------
    async def run(self) -> None:
        """The fleet's loop (one supervised task); a failed tick is logged and the next one runs."""
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("agent fleet tick failed")
            await self._sleep(self.tick_s)

    def _views(self, now: int) -> dict[str, dict]:
        """KD6: one read per market armed by a live in-app session this tick."""
        views = {}
        for sid in self._runners:
            s = self.sessions.get(sid)
            key = self.sessions.market_key(s) if s is not None else None
            if key is not None and key not in views:
                m = market_spec(key)
                views[key] = self.play.now_view(now, m.venue, span=m.span)
        return views

    async def _prime(self, now: int) -> None:
        """One read of every runner's session brackets for this tick, off the event loop; a failure leaves the
        runners to read their own (as before)."""
        sids = [sid for sid in self._runners if sid not in self._steps]
        if not sids:
            return
        try:
            rows = await asyncio.to_thread(self.sessions.brackets_by_session, sids)
        except Exception:
            log.warning("agent fleet: the sessions' brackets could not be read; each session reads its own",
                        exc_info=True)
            return
        for sid in sids:
            r = self._runners.get(sid)
            if r is not None:
                r.prime(now, rows.get(sid, []))

    async def tick(self) -> None:
        t0 = self.now()
        now = t0
        self.sessions.review(now)
        if self.proposals is not None:
            self.proposals.expire(now)
        for sid in [sid for sid in self._runners if self.sessions.get(sid) is None and sid not in self._wakes]:
            self._drop(sid)  # ended elsewhere (end time, end_session, spend cap): no step needed
        views = self._views(now)
        await self._prime(now)
        started = []
        for sid, r in list(self._runners.items()):
            if sid in self._steps:  # the previous step still runs (an order in flight): never overlap itself
                continue
            task = asyncio.ensure_future(self._step(sid, r, now, views))
            self._steps[sid] = task
            started.append(task)
        self.last_tick_ms = self.now() - t0
        if started:
            await asyncio.wait(started, timeout=self.tick_s)
        try:
            self.sessions.check_overlaps(now)
        except Exception:
            log.exception("agent overlap check failed")
        self._pump()

    async def _step(self, sid: str, r: AgentRunner, now: int, views: dict) -> None:
        try:
            alive = await r.step(now, views, expire=False)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("agent session %s step failed", sid[:8])
            alive = self.sessions.get(sid) is not None
        finally:
            self._steps.pop(sid, None)
        if not alive:
            if sid not in self._wakes:
                self._drop(sid)
            return
        if self.scheduler.running(sid):
            return
        if self.scheduler.queued(sid):
            if r.has_urgent():
                self.scheduler.offer(sid, URGENT, now)
            return
        due = r.wake_due(now)
        if due is not None:
            self.scheduler.offer(sid, due, now)
            self._pump()

    def _pump(self) -> None:
        for sid in self.scheduler.take(self.now()):
            r = self._runners.get(sid)
            s = self.sessions.get(sid)
            if r is None or s is None or s["state"] != "running" or self.sessions.stopping(sid):
                self.scheduler.done(sid)  # paused or stopping since it queued: no wake (its events stay pending)
                continue
            self._wakes[sid] = asyncio.ensure_future(self._wake(sid, r))

    async def _wake(self, sid: str, r: AgentRunner) -> None:
        try:
            await r.wake(self.now())
        except asyncio.CancelledError:
            pass  # a Stop: the runner recorded the cancelled wake
        except Exception:
            log.exception("agent session %s wake failed", sid[:8])
        finally:
            self._wakes.pop(sid, None)
            self.scheduler.done(sid)
            if self.sessions.get(sid) is None and sid in self._runners and sid not in self._steps:
                self._drop(sid)
            self._pump()

    # -- stop -----------------------------------------------------------------------------------------------------------
    async def stop(self, session_id: str, now: int, reason: str = "stopped", text: str = "Stopped by you.") -> dict:
        """Stop one session; the others keep running."""
        self.scheduler.cancel(session_id)
        r = self._runners.get(session_id)
        if r is None:
            out = self.sessions.stop(session_id, now, reason, text)
        else:
            out = await r.stop(now, reason, text)
            step = self._steps.get(session_id)
            if step is not None and not step.done():
                try:
                    await step  # a rule order in flight finishes and is recorded
                except Exception:
                    log.warning("agent step ended with an error while stopping", exc_info=True)
            self._drop(session_id)
        return out

    async def stop_all(self, now: int, reason: str = "stopped", text: str = "Stopped by you.") -> list[dict]:
        """Stop every live session (in-app and external), each with its own answer."""
        ids = [s["id"] for s in self.sessions.live()]
        outs = await asyncio.gather(*(self.stop(sid, now, reason, text) for sid in ids), return_exceptions=True)
        result = []
        for sid, out in zip(ids, outs, strict=True):
            if isinstance(out, BaseException):
                log.error("stopping agent session %s failed", sid[:8], exc_info=out)
                out = {"ok": False, "code": "error", "reason": f"{type(out).__name__}: {out}"}
            result.append({"id": sid, **out})
        return result

    # -- reads ------------------------------------------------------------------------------------------------------------
    def metrics(self, session_id: str, now: int) -> dict:
        return self.scheduler.metrics(session_id, now)

    def summary(self, now: int) -> dict:
        return {**self.scheduler.summary(now), "sessions": len(self._runners), "last_tick_ms": self.last_tick_ms}
