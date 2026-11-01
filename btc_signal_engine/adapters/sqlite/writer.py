"""The single batched writer (KTD3).

Live code calls the same store methods it would call synchronously; `BufferedStore` queues the writes and one dedicated
thread commits them in batches every `flush_interval` seconds. Reads pass straight through to the store's read engine.
Queuing is thread-safe: the event loop and the web server's worker threads may both write.

Writes commit in the order they were made, in batches of at most MAX_BATCH writes, each batch in its own transaction.
A batch that fails stays at the head of the queue and everything newer waits behind it (an UPDATE must not commit
before the failed INSERT of its row, or it matches nothing and is lost). What happens next depends on the error:

- **The database is unreachable or busy** (`is_transient`: OperationalError, InterfaceError, a dropped or timed-out
  connection, a pool timeout, an OS-level socket error; SQLite's "database is locked"): the batch is retried
  **indefinitely**, the loop backing off up to MAX_BACKOFF_S. Nothing is dropped. The outage is logged at WARNING when
  it starts, every OUTAGE_LOG_EVERY_S while it lasts and once when it ends, and is visible in `/api/health`
  (`writer_outage_s`) and the `settleview.writer.*` metrics. Memory is bounded: past `max_queued` queued writes the
  oldest market-data writes (Coinbase trades, book and index seconds: re-derivable and idempotent) are moved to the
  dead-letter file (reason `overflow`, replayable); trading and app rows are never shed.
- **The batch itself is bad** (anything else: an IntegrityError, a DataError, a bug in a row): the batch is split in
  halves and each half retried, recursively, so only the bad write(s) end in the dead-letter file (reason `data`) and
  every good write around them commits, in order.

The dead-letter file is one JSON line per dead-lettered batch: {ts, reason, attempts, error, writes: [{method, args,
kwargs}]}. At shutdown (`close`) a database that is still unreachable sends everything queued there too (reason
`shutdown`) instead of retrying batch after batch against a dead server past the stop grace period.
`replay_dead_letter` (`python -m btc_signal_engine.ops replay-dead-letter`) applies the file again, idempotently.

Telemetry: each batch's commit time and size (`settleview.writer.flush.duration`, `settleview.writer.batch.size`, by
result ok / error), failed attempts by kind (`settleview.writer.retries`), dead-lettered writes by reason
(`settleview.writer.dead_lettered`); the backlog, failed batches and outage age are gauges (bootstrap/telemetry.py).
"""

from __future__ import annotations

import asyncio
import collections
import dataclasses
import functools
import hashlib
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, NamedTuple
from collections.abc import Callable

from sqlalchemy import Connection, Engine
from sqlalchemy.exc import DBAPIError, DisconnectionError, InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from btc_signal_engine.adapters import telemetry
from btc_signal_engine.adapters.sqlite.store import SqliteStore
from btc_signal_engine.domain.models.events import BookSecond, IndexSecond, Trade

log = logging.getLogger(__name__)

_WRITE_METHODS = {"add_trades", "add_book_seconds", "add_index_seconds", "insert", "update"}
# Market data: re-derivable (Coinbase backfill), idempotent (insert-or-ignore; index rows are recomputed and
# replaced), never read back by trading. The only writes shed to the dead-letter file when the queue is full.
MARKET_DATA = {"add_trades": Trade, "add_book_seconds": BookSecond, "add_index_seconds": IndexSecond}
MAX_BACKOFF_S = 5.0
MAX_BATCH = 2_000  # writes per transaction: a long outage's backlog commits in many short transactions
MAX_QUEUED = 200_000  # writes held in memory during an outage before market data is shed (hours of it)
OUTAGE_LOG_EVERY_S = 60.0
# SQLite reports a missing table or column as an OperationalError; that is a bad write, not an outage.
_SQLITE_SCHEMA_ERRORS = ("no such table", "no such column", "has no column named")


def is_transient(exc: BaseException) -> bool:
    """True when `exc` says the database could not be reached or was busy (retry the same batch later), False when
    the batch itself is bad (find the bad write)."""
    if isinstance(exc, DBAPIError) and exc.connection_invalidated:
        return True
    if isinstance(exc, OperationalError):
        msg = str(exc.orig if exc.orig is not None else exc).lower()
        return not any(s in msg for s in _SQLITE_SCHEMA_ERRORS)
    return isinstance(exc, (InterfaceError, DisconnectionError, PoolTimeoutError, OSError))


def _err(exc: BaseException) -> str:
    """A short, log-safe description (the engines hide bound parameters: no row values in SQLAlchemy errors)."""
    first = (str(exc).strip().splitlines() or [""])[0]
    return f"{type(exc).__name__}: {first[:300]}"


class _Write(NamedTuple):
    """One queued write: the store call (method name, args, kwargs) next to the callable that applies it."""
    method: str
    args: tuple
    kwargs: dict
    op: Callable[[Connection], Any]


@dataclasses.dataclass
class _Batch:
    writes: list[_Write]
    attempts: int = 0
    error: str = ""


def _jsonable(o: Any) -> Any:
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        return dataclasses.asdict(o)
    return {"__repr__": repr(o)}  # kept for a person to read; replay refuses the write rather than guess the value


class BufferedStore:
    def __init__(self, store: SqliteStore, write_engine: Engine, flush_interval: float = 0.25,
                 dead_letter_path: Path | None = None, *, max_queued: int = MAX_QUEUED,
                 outage_log_every_s: float = OUTAGE_LOG_EVERY_S, clock: Callable[[], float] = time.monotonic) -> None:
        self._store = store
        self._engine = write_engine
        self._interval = flush_interval
        self._dead_letter = dead_letter_path
        self._max_queued = max_queued
        self._outage_log_every_s = outage_log_every_s
        self._clock = clock
        self._pending: list[_Write] = []  # new writes; any thread appends, under _plock
        self._plock = threading.Lock()
        self._queue: collections.deque[_Batch] = collections.deque()  # oldest first; the writer thread's only
        self._lock = asyncio.Lock()  # the loop and an explicit flush (decision loop) never interleave
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="db-writer")
        self._closed = False
        self._outage_since: float | None = None
        self._outage_logged = 0.0
        self._overflow_told = False
        self.dead_lettered = 0  # writes this process sent to the dead-letter file

    def __getattr__(self, name: str):
        attr = getattr(self._store, name)
        if name not in _WRITE_METHODS:
            return attr

        @functools.wraps(attr)
        def enqueue(*args, **kwargs):
            if name in MARKET_DATA:
                args = (list(args[0]),) + args[1:]  # materialize iterators now
            w = _Write(name, args, kwargs, lambda conn: attr(*args, conn=conn, **kwargs))
            with self._plock:
                self._pending.append(w)
            return [] if name == "insert" else None

        return enqueue

    @property
    def pending(self) -> int:
        """Writes not yet committed: the new ones and those queued (in or behind a failed batch)."""
        return len(self._pending) + sum(len(b.writes) for b in list(self._queue))

    @property
    def failed_batches(self) -> int:
        """Batches that failed and wait for a retry (the writes behind them wait too)."""
        return sum(1 for b in list(self._queue) if b.attempts)

    @property
    def outage_s(self) -> float | None:
        """Seconds since the database stopped taking writes, None while it takes them."""
        since = self._outage_since
        return None if since is None else round(self._clock() - since, 1)

    # -- the writer thread ----------------------------------------------------------------------------------------------
    def _apply(self, batch: list[_Write]) -> None:
        t0, result = time.perf_counter(), "error"
        try:
            with self._engine.begin() as conn:
                for w in batch:
                    w.op(conn)
            result = "ok"
        finally:
            attrs = {"result": result}
            telemetry.writer_flush.record(time.perf_counter() - t0, attrs)
            telemetry.writer_batch.record(len(batch), attrs)

    def _take_pending(self) -> None:
        """The new writes join the queue in batches of at most MAX_BATCH (an untried tail batch is topped up)."""
        with self._plock:
            new, self._pending = self._pending, []
        while new:
            tail = self._queue[-1] if self._queue else None
            if tail is not None and not tail.attempts and len(tail.writes) < MAX_BATCH:
                room = MAX_BATCH - len(tail.writes)
                tail.writes.extend(new[:room])
                new = new[room:]
            else:
                self._queue.append(_Batch(new[:MAX_BATCH]))
                new = new[MAX_BATCH:]

    def _drain(self, final: bool = False) -> int:
        """Commit the queue oldest first; returns the writes committed. Stops at a transient failure (the batch waits;
        at shutdown everything queued is dead-lettered instead); a bad batch is bisected down to its bad write(s)."""
        self._take_pending()
        done = 0
        while self._queue:
            head = self._queue[0]
            try:
                self._apply(head.writes)
            except Exception as exc:
                head.attempts += 1
                head.error = _err(exc)
                if is_transient(exc):
                    telemetry.writer_retries.add(1, {"kind": "transient"})
                    if final:
                        self._dead_letter_all("shutdown")
                        return done
                    self._outage(head)
                    self._shed()
                    return done
                telemetry.writer_retries.add(1, {"kind": "data"})
                self._queue.popleft()
                if len(head.writes) == 1:
                    self._dead_letter_batch(head, "data")
                else:
                    mid = len(head.writes) // 2
                    log.warning("writer: a batch of %d writes was refused (%s); retrying it in halves to find the bad "
                                "write(s)", len(head.writes), head.error)
                    self._queue.appendleft(_Batch(head.writes[mid:]))
                    self._queue.appendleft(_Batch(head.writes[:mid]))
                continue
            self._queue.popleft()
            done += len(head.writes)
            self._back()
        return done

    def _outage(self, head: _Batch) -> None:
        now = self._clock()
        if self._outage_since is None:
            self._outage_since = self._outage_logged = now
            log.warning("writer: the database is not taking writes (%s); %d writes queued, retrying until it does",
                        head.error, self.pending)
        elif now - self._outage_logged >= self._outage_log_every_s:
            self._outage_logged = now
            log.warning("writer: the database has not taken writes for %.0f s (%s); %d writes queued, %d tries of the "
                        "oldest batch", now - self._outage_since, head.error, self.pending, head.attempts)

    def _back(self) -> None:
        if self._outage_since is not None:
            log.warning("writer: the database takes writes again after %.0f s; committing the %d queued writes",
                        self._clock() - self._outage_since, self.pending)
            self._outage_since = None
            self._overflow_told = False

    def _shed(self) -> None:
        """Past max_queued: the oldest market-data writes go to the dead-letter file (replayable), never others."""
        excess = self.pending - self._max_queued
        if excess <= 0:
            return
        shed: list[_Write] = []

        def sift(writes: list[_Write]) -> list[_Write]:
            nonlocal excess
            keep = []
            for w in writes:
                if excess > 0 and w.method in MARKET_DATA:
                    shed.append(w)
                    excess -= 1
                else:
                    keep.append(w)
            return keep

        for b in self._queue:
            if excess <= 0:
                break
            b.writes[:] = sift(b.writes)
        if excess > 0:
            with self._plock:
                self._pending = sift(self._pending)
        self._queue = collections.deque(b for b in self._queue if b.writes)
        for i in range(0, len(shed), MAX_BATCH):
            self._dead_letter_batch(_Batch(shed[i:i + MAX_BATCH], 0, "queue full during a database outage"),
                                    "overflow")
        if excess > 0 and not self._overflow_told:
            self._overflow_told = True
            log.error("writer: %d writes queued during the outage and no market data left to shed (trading and app "
                      "rows stay in memory)", self.pending)

    def _dead_letter_all(self, reason: str) -> None:
        while self._queue:
            self._dead_letter_batch(self._queue.popleft(), reason)

    def _dead_letter_batch(self, b: _Batch, reason: str) -> None:
        where = "NOT SAVED: no dead-letter file configured"
        if self._dead_letter is not None:
            try:
                line = json.dumps({"ts": int(time.time() * 1000), "reason": reason, "attempts": b.attempts,
                                   "error": b.error, "writes": [{"method": w.method, "args": w.args,
                                                                 "kwargs": w.kwargs} for w in b.writes]},
                                  default=_jsonable)
                self._dead_letter.parent.mkdir(parents=True, exist_ok=True)
                with self._dead_letter.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
                where = f"saved to {self._dead_letter} (python -m btc_signal_engine.ops replay-dead-letter)"
            except Exception:
                log.exception("writer: writing the dead-letter file failed")
        self.dead_lettered += len(b.writes)
        telemetry.writer_dead_lettered.add(len(b.writes), {"reason": reason})
        log.error("writer: %d writes dead-lettered (%s: %s); %s", len(b.writes), reason, b.error or "-", where)

    # -- the event loop -------------------------------------------------------------------------------------------------
    async def flush(self) -> int:
        """Commit everything queued, oldest first, in the writer thread (module docstring). Never raises; returns the
        number of writes committed."""
        async with self._lock:
            return await asyncio.get_running_loop().run_in_executor(self._executor, self._drain)

    def flush_sync(self) -> int:
        """Last chance (shutdown): everything queued is tried once more; with the database still unreachable it all
        goes to the dead-letter file (reason `shutdown`)."""
        if not self._pending and not self._queue:
            return 0
        return self._executor.submit(self._drain, True).result()

    async def run(self, beat: Callable[[], None] | None = None) -> None:
        """Flush forever, backing off (doubling up to MAX_BACKOFF_S) while the database is not taking writes; `beat`
        (the supervisor's heartbeat) is called after every flush. On cancellation, at most the in-flight batch is
        lost."""
        delay = self._interval
        while not self._closed:
            await asyncio.sleep(delay)
            await self.flush()
            if beat is not None:
                beat()
            delay = min(MAX_BACKOFF_S, delay * 2) if self._outage_since is not None else self._interval

    def close(self) -> None:
        self._closed = True
        self.flush_sync()
        self._executor.shutdown(wait=True)


# -- replay ---------------------------------------------------------------------------------------------------------------
@dataclasses.dataclass
class ReplayResult:
    lines: int = 0  # dead-lettered batches in the file
    skipped: int = 0  # writes an earlier run already applied
    applied: int = 0  # writes this run committed
    refused: int = 0  # writes refused again or not rebuildable (left for a later run or a person)
    pending: int = 0  # writes a dry run would apply
    stopped: str | None = None  # the database was unreachable: the run stopped there


def _rebuild(w: dict) -> tuple[str, tuple, dict]:
    """A dead-letter write back as (method, args, kwargs); ValueError when it cannot be replayed faithfully."""
    method, args, kwargs = w.get("method"), list(w.get("args") or []), dict(w.get("kwargs") or {})
    if method not in _WRITE_METHODS:
        raise ValueError(f"unknown method {method!r}")
    if '"__repr__"' in json.dumps([args, kwargs]):
        raise ValueError("it holds a value that was not saved as data")
    if method in MARKET_DATA:
        if not args or not isinstance(args[0], list) or not all(isinstance(r, dict) for r in args[0]):
            raise ValueError(f"{method} without its rows")
        args[0] = [MARKET_DATA[method](**r) for r in args[0]]
    return method, tuple(args), kwargs


def _what(method: str, args: tuple) -> str:
    return method if method in MARKET_DATA else f"{method} {args[0] if args else '?'}"


def replay_dead_letter(path: Path, store: SqliteStore, engine: Engine, *, apply: bool,
                       say: Callable[[str], None] = print) -> ReplayResult:
    """Apply the dead-letter file `path` through `store`'s own write methods, oldest line first, each line in one
    transaction (a refused line is retried write by write, so only the bad write stays out). Idempotent: each write
    applied is recorded in `<path>.replayed` (by the line's hash and the write's position) and skipped by any later
    run, and inserts ignore rows that already exist. A dry run (`apply` False) only says what it would apply. Stops at
    the first sign of an unreachable database (run it again later). The file itself is never changed."""
    marks_path = path.with_name(path.name + ".replayed")
    done = set(marks_path.read_text(encoding="utf-8").split()) if marks_path.exists() else set()
    res = ReplayResult()
    if not path.exists():
        say(f"no dead-letter file at {path}: nothing to replay")
        return res

    def mark(keys: list[str]) -> None:
        if keys:
            with marks_path.open("a", encoding="utf-8") as m:
                m.write("".join(k + "\n" for k in keys))
            done.update(keys)
            res.applied += len(keys)

    def run(items: list) -> None:
        with engine.begin() as conn:
            for _key, method, args, kwargs in items:
                getattr(store, method)(*args, conn=conn, **kwargs)

    with path.open("r", encoding="utf-8") as f:
        for n, raw in enumerate(f, 1):
            raw = raw.strip()
            if not raw:
                continue
            res.lines += 1
            key = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
            try:
                entry = json.loads(raw)
            except ValueError:
                say(f"line {n}: not JSON; left as it is")
                res.refused += 1
                continue
            todo, kinds = [], collections.Counter()
            for i, w in enumerate(entry.get("writes") or []):
                wkey = f"{key}:{i}"
                if wkey in done:
                    res.skipped += 1
                    continue
                try:
                    method, args, kwargs = _rebuild(w)
                except (TypeError, ValueError) as exc:
                    say(f"line {n} write {i}: cannot be replayed ({exc}); left for a person")
                    res.refused += 1
                    continue
                todo.append((wkey, method, args, kwargs))
                kinds[_what(method, args)] += 1
            if not todo:
                continue
            when = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(entry.get("ts") or 0) / 1000))
            say(f"line {n} ({when}, {entry.get('reason', 'failed')}): "
                + ", ".join(f"{c} x {k}" for k, c in sorted(kinds.items())))
            if not apply:
                res.pending += len(todo)
                continue
            try:
                run(todo)
                mark([t[0] for t in todo])
                continue
            except Exception as exc:
                if is_transient(exc):
                    res.stopped = _err(exc)
                    say(f"STOPPED at line {n}: the database is not reachable ({res.stopped}); run it again later")
                    return res
            for item in todo:  # one by one: everything but the bad write(s)
                try:
                    run([item])
                    mark([item[0]])
                except Exception as one:
                    if is_transient(one):
                        res.stopped = _err(one)
                        say(f"STOPPED at line {n}: the database is not reachable ({res.stopped}); run it again "
                            "later")
                        return res
                    res.refused += 1
                    say(f"line {n}: {_what(item[1], item[2])} refused again ({_err(one)}); left for a person")
    return res
