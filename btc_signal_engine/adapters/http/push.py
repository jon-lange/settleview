"""Server-to-browser push for the Perps screen (KTD11): server-sent events, one-way, no new package.

A `PushHub` fans the read model's changed fields out to every open stream. Each stream starts with the full view
(`snapshot()`), then sends `event: view` frames carrying only the fields that changed, merged so that a burst of changes
inside one interval (`min_interval_s`, 100 ms) becomes ONE frame with the newest values. A stream with nothing to say
sends `event: ping` every `heartbeat_s` (15 s), so the browser and any proxy keep it open and the page can tell a quiet
stream from a dead one. `close()` (app shutdown) ends every stream cleanly.

Publishing never waits: it only merges into each stream's pending dict and wakes it, so a caller on the event loop
(the perps monitor, a stream event) pays a dict update per open stream. The route sits under the PIN middleware like
every /api path (access.py treats a streaming response like any other request), and the session cookie is checked
once, when the stream opens.

`FeedHub` (below) is the append-only variant for the Agent panel (KTD12): no merging, a bounded backlog per stream.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import AsyncIterator, Callable
from typing import Any

from fastapi.responses import StreamingResponse


class _Sub:
    def __init__(self) -> None:
        self.pending: dict[str, Any] = {}
        self.wake = asyncio.Event()


def _frame(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'), default=str)}\n\n"


class PushHub:
    def __init__(self, *, min_interval_s: float = 0.1, heartbeat_s: float = 15.0) -> None:
        self.min_interval_s = float(min_interval_s)
        self.heartbeat_s = float(heartbeat_s)
        self._subs: set[_Sub] = set()
        self._closed = False

    @property
    def subscribers(self) -> int:
        return len(self._subs)

    def publish(self, fields: dict[str, Any]) -> None:
        """Merge changed top-level view fields into every open stream (the newest value of a field wins)."""
        if not fields:
            return
        for sub in self._subs:
            sub.pending.update(fields)
            sub.wake.set()

    async def close(self) -> None:
        self._closed = True
        for sub in self._subs:
            sub.wake.set()

    async def stream(self, snapshot: Callable[[], dict[str, Any]]) -> AsyncIterator[str]:
        """The frames of one browser stream: the full view first, then coalesced changes and pings."""
        if self._closed:
            return
        sub = _Sub()
        self._subs.add(sub)
        loop = asyncio.get_running_loop()
        try:
            yield _frame("view", snapshot())
            last = loop.time()
            while not self._closed:
                try:
                    await asyncio.wait_for(sub.wake.wait(), self.heartbeat_s)
                except TimeoutError:
                    yield _frame("ping", {})
                    continue
                if self._closed:
                    break
                gap = last + self.min_interval_s - loop.time()
                if gap > 0:
                    await asyncio.sleep(gap)  # let the burst finish: one frame per interval
                sub.wake.clear()
                fields, sub.pending = sub.pending, {}
                if fields:
                    yield _frame("view", fields)
                    last = loop.time()
        finally:
            self._subs.discard(sub)

    def response(self, snapshot: Callable[[], dict[str, Any]]) -> StreamingResponse:
        return StreamingResponse(self.stream(snapshot), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# -- append-only feed (Agent mode U7, KTD12) ----------------------------------------------------------------------------
class _FeedSub:
    def __init__(self, keep: int) -> None:
        self.queue: deque[str] = deque(maxlen=keep)
        self.dropped = 0
        self.wake = asyncio.Event()


class FeedHub:
    """An append-only SSE fan-out: every published event reaches every open stream as its own frame, in order; nothing
    is merged (unlike `PushHub`, whose newest-value merge would collapse a burst of agent events, KTD12).

    Frames are `data: <json>\n\n` with no `event:` line (the event's own `type` names it, the AG-UI convention).
    Memory is bounded per stream: a client that falls `keep` (200) frames behind loses the oldest ones and is told
    how many with `trimmed(n)`'s event before the rest; it reconnects for a fresh snapshot and reads the journal.
    A quiet stream sends an SSE comment (`: ping`) every `heartbeat_s` (15 s), after `idle()` (e.g. a state refresh),
    then `heartbeat()`'s event when given (comments are invisible to EventSource; the event lets a page tell a quiet
    stream from a dead one).
    `publish` may be called from another thread; it then hops onto the streams' event loop."""

    def __init__(self, *, keep: int = 200, heartbeat_s: float = 15.0,
                 trimmed: Callable[[int], dict] | None = None) -> None:
        self.keep = int(keep)
        self.heartbeat_s = float(heartbeat_s)
        self._trimmed = trimmed
        self._subs: set[_FeedSub] = set()
        self._closed = False
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def subscribers(self) -> int:
        return len(self._subs)

    def publish(self, event: dict) -> None:
        if not self._subs:
            return
        loop = self._loop
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if loop is not None and running is not loop:
            loop.call_soon_threadsafe(self._append, _data(event))
        else:
            self._append(_data(event))

    def _append(self, frame: str) -> None:
        for sub in self._subs:
            if len(sub.queue) == sub.queue.maxlen:
                sub.dropped += 1
            sub.queue.append(frame)
            sub.wake.set()

    async def close(self) -> None:
        self._closed = True
        for sub in self._subs:
            sub.wake.set()

    async def stream(self, first: Callable[[], list[dict]], idle: Callable[[], None] | None = None,
                     heartbeat: Callable[[], dict] | None = None) -> AsyncIterator[str]:
        """One stream: `first()`'s events (the snapshot), then every published event, pings when quiet. `first()` is
        read and the stream joins the hub in one step (no await between), so no event falls between the two."""
        if self._closed:
            return
        self._loop = asyncio.get_running_loop()
        frames = [_data(e) for e in first()]
        sub = _FeedSub(self.keep)
        self._subs.add(sub)
        try:
            for f in frames:
                yield f
            while not self._closed:
                if not sub.queue:
                    sub.wake.clear()
                    try:
                        await asyncio.wait_for(sub.wake.wait(), self.heartbeat_s)
                    except TimeoutError:
                        if idle is not None:
                            idle()
                        if not sub.queue:
                            yield ": ping\n\n"
                        if heartbeat is not None:
                            yield _data(heartbeat())
                        continue
                    if self._closed:
                        break
                if sub.dropped and self._trimmed is not None:
                    n, sub.dropped = sub.dropped, 0
                    yield _data(self._trimmed(n))
                while sub.queue:
                    yield sub.queue.popleft()
        finally:
            self._subs.discard(sub)

    def response(self, first: Callable[[], list[dict]], idle: Callable[[], None] | None = None,
                 heartbeat: Callable[[], dict] | None = None) -> StreamingResponse:
        return StreamingResponse(self.stream(first, idle, heartbeat), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _data(event: dict) -> str:
    return f"data: {json.dumps(event, separators=(',', ':'), default=str)}\n\n"
