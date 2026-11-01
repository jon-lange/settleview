"""Event-loop stall detector: logs what the loop thread was running whenever the asyncio loop stops answering.

Everything (web requests, feeds, the order path) shares one event loop, so any synchronous work on it freezes the whole
app; the Play screen then shows "The server took too long to answer". A watcher thread asks the loop to answer every
`interval_s`; if no answer arrives within `threshold_s`, it samples the loop thread's current stack (sys._current_frames)
and logs it (with the running task's name, if any) once per stall, then logs the stall's total length when the loop
answers again. Diagnostic only: it never touches the loop's work. `stalls_last_hour()` counts the stalls that began
within the last hour (for /api/health).

Every answer's lag also goes into a histogram (KTD14): `lag_view()` gives the counts per bucket since start and the
lags over LAG_REPORT_MS (50 ms) in the last hour, which is how the Perps targets (no lag over 50 ms) are checked
without lowering the 500 ms stall warning, which would flood the log from every other path. `on_lag` (set by
bootstrap/telemetry.py) is also handed every lag in ms, from the watcher thread.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import sys
import threading
import time
import traceback
from collections.abc import Callable

log = logging.getLogger(__name__)

HOUR_S = 3600.0
LAG_REPORT_MS = 50.0
LAG_BUCKETS_MS = ((10.0, "<10"), (25.0, "<25"), (50.0, "<50"), (100.0, "<100"), (250.0, "<250"), (500.0, "<500"))


class StallWatch:
    def __init__(self, loop: asyncio.AbstractEventLoop, *, threshold_s: float = 0.5, interval_s: float = 0.1,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._loop = loop
        self._clock = clock  # seconds; dates each stall for the rolling hour count
        self._stalls: collections.deque[float] = collections.deque()  # start times, oldest first
        self._stalls_lock = threading.Lock()
        self._buckets = {name: 0 for _, name in LAG_BUCKETS_MS} | {">=500": 0}
        self._slow: collections.deque[tuple[float, float]] = collections.deque()  # (clock s, lag ms) over 50 ms
        self._threshold = threshold_s
        self._interval = interval_s
        self._loop_thread = threading.get_ident()  # constructed on the loop thread
        self._answered = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="stall-watch", daemon=True)
        self.on_lag: Callable[[float], None] | None = None

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def stalls_last_hour(self) -> int:
        return self._count(None)

    def lag_view(self) -> dict:
        """{"buckets_ms": answers per lag bucket since start, "over_50ms_last_hour", "max_ms_last_hour"}."""
        now = self._clock()
        with self._stalls_lock:
            while self._slow and self._slow[0][0] < now - HOUR_S:
                self._slow.popleft()
            return {"buckets_ms": dict(self._buckets), "over_50ms_last_hour": len(self._slow),
                    "max_ms_last_hour": round(max((lag for _, lag in self._slow), default=0.0), 1)}

    def _lag(self, ms: float) -> None:
        name = next((n for limit, n in LAG_BUCKETS_MS if ms < limit), ">=500")
        now = self._clock()
        with self._stalls_lock:
            self._buckets[name] += 1
            if ms >= LAG_REPORT_MS:
                self._slow.append((now, ms))
            while self._slow and self._slow[0][0] < now - HOUR_S:
                self._slow.popleft()
        if self.on_lag is not None:
            try:
                self.on_lag(ms)
            except Exception:  # telemetry never stops the watch
                log.debug("stall watch: the lag hook failed", exc_info=True)

    def _count(self, new: float | None) -> int:
        now = self._clock()
        with self._stalls_lock:
            if new is not None:
                self._stalls.append(new)
            while self._stalls and self._stalls[0] < now - HOUR_S:  # pruned on every write too: never unbounded
                self._stalls.popleft()
            return len(self._stalls)

    def _run(self) -> None:
        while not self._stop.is_set():
            self._answered.clear()
            asked = time.monotonic()
            try:
                self._loop.call_soon_threadsafe(self._answered.set)
            except RuntimeError:  # the loop is closed
                return
            if self._answered.wait(self._threshold):
                self._lag((time.monotonic() - asked) * 1000)
                time.sleep(self._interval)
                continue
            self._count(self._clock())
            frames = sys._current_frames()
            names = {t.ident: t.name for t in threading.enumerate()}
            loop_frame = frames.get(self._loop_thread)
            task = asyncio.current_task(self._loop)  # None when the stall is in a plain callback (e.g. a protocol)
            where = f" (task {task.get_name()})" if task is not None else ""
            stack = "".join(traceback.format_stack(loop_frame, limit=25)) if loop_frame is not None else "(no frame)\n"
            parts = [f"--- loop thread{where} ---\n" + stack]
            for ident, frame in frames.items():  # a busy other thread (GIL) or a lock holder is often the real cause
                if ident in (self._loop_thread, threading.get_ident()):
                    continue
                stack = traceback.format_stack(frame, limit=12)
                if any("btc_signal_engine" in line for line in stack):  # skip idle library/pool threads
                    parts.append(f"--- thread {names.get(ident, ident)} ---\n" + "".join(stack))
            log.warning("event loop stalled > %.0f ms; the loop thread is running:\n%s", self._threshold * 1000,
                        "".join(parts))
            self._answered.wait()
            lag_ms = (time.monotonic() - asked) * 1000
            self._lag(lag_ms)
            log.warning("event loop stall ended after %.0f ms", lag_ms)
