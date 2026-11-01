"""Loop supervision (KTD7): one failing loop never ends the process.

Every background loop runs under `Supervisor.start`. A loop that raises, or returns (a loop is meant to run forever),
is logged by name with its traceback and restarted after a backoff: 1 s, doubling per failure in the last 10 minutes,
up to 30 s. A critical loop (the `you` monitor, the writer, the web server) that fails 5 times within 10 minutes also
calls `on_trip` (the app turns the kill switch on: no new entries) and logs at ERROR; it keeps being restarted, so open
positions stay monitored. Each loop's last heartbeat (`beat`, or its latest start) and restart count are readable
through `heartbeats()`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)

BASE_DELAY_S = 1.0
MAX_DELAY_S = 30.0
TRIP_FAILURES = 5
WINDOW_S = 600.0


class Supervisor:
    def __init__(self, on_trip: Callable[[str], Awaitable[object]] | None = None) -> None:
        self._on_trip = on_trip
        self._tasks: dict[str, asyncio.Task] = {}
        self._beats: dict[str, float] = {}
        self._restarts: dict[str, int] = {}
        self._critical: dict[str, bool] = {}
        self._stop = asyncio.Event()

    def start(self, name: str, factory: Callable[[], Awaitable[object]], *, critical: bool = False) -> asyncio.Task:
        """Run `factory()` under supervision as task `name`; cancelling the returned task stops it for good."""
        self._critical[name] = critical
        self._restarts[name] = 0
        task = asyncio.create_task(self._supervise(name, factory, critical), name=name)
        self._tasks[name] = task
        task.add_done_callback(lambda t, n=name: self._forget(n, t))
        return task

    def beat(self, name: str) -> None:
        self._beats[name] = time.time()

    def heartbeats(self) -> dict[str, dict]:
        """name -> last heartbeat (epoch seconds), restarts since start, critical; for the health view. Read from
        FastAPI's threadpool while the loop starts and forgets loops, so it works on copies."""
        critical, beats, restarts = dict(self._critical), dict(self._beats), dict(self._restarts)
        return {n: {"last_beat": beats.get(n), "restarts": restarts.get(n, 0), "critical": c}
                for n, c in critical.items()}

    def request_stop(self) -> None:
        """Ask the app to shut down; a loop that returns after this is not restarted."""
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    async def wait_stop(self) -> None:
        await self._stop.wait()

    async def stop(self) -> None:
        """Cancel every supervised task and wait until each has finished."""
        self._stop.set()
        tasks = list(self._tasks.values())
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _forget(self, name: str, task: asyncio.Task) -> None:
        if self._tasks.get(name) is task:  # a stopped nested loop (a feed of a mode no longer used) leaves the view
            del self._tasks[name]
            for d in (self._beats, self._restarts, self._critical):
                d.pop(name, None)

    async def _supervise(self, name: str, factory, critical: bool) -> None:
        failures: list[float] = []
        last_trip = float("-inf")
        while True:
            self.beat(name)
            try:
                await factory()
            except asyncio.CancelledError:
                raise
            except Exception:
                if self._stop.is_set():
                    return
                log.warning("loop %s failed; restarting", name, exc_info=True)
            else:
                if self._stop.is_set():
                    return
                log.warning("loop %s returned; restarting", name)
            now = time.monotonic()
            failures = [t for t in failures if now - t <= WINDOW_S] + [now]
            if critical and sum(t > last_trip for t in failures) >= TRIP_FAILURES:
                last_trip = now
                log.error("critical loop %s failed %d times in %d min: kill switch on (no new entries); still "
                          "restarting it", name, TRIP_FAILURES, WINDOW_S // 60)
                if self._on_trip is not None:
                    try:
                        await self._on_trip(name)
                    except Exception:
                        log.exception("turning the kill switch on after %s failed", name)
            self._restarts[name] = self._restarts.get(name, 0) + 1
            await asyncio.sleep(min(MAX_DELAY_S, BASE_DELAY_S * 2 ** (len(failures) - 1)))
