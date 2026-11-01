import asyncio
import dataclasses
import json
import logging

import pytest
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError

from btc_signal_engine.adapters.sqlite.writer import BufferedStore
from btc_signal_engine.domain.models.events import Trade


def _trade(i: int) -> Trade:
    return Trade(trade_id=i, ts=1_700_000_000_000 + i, ingest_ts=1_700_000_000_100 + i, price=80_000.0, size=0.01, buy=i % 2 == 0)


def test_same_trade_twice_is_stored_once(store):
    store.add_trades([_trade(1)])
    store.add_trades([_trade(1)])
    assert len(store.select("trades")) == 1


def test_same_live_decision_twice_is_stored_once(store):
    row = {"horizon": 60, "boundary": 1_700_000_040_000, "ruleset_hash": "abc", "run_id": 0, "call": "UP",
           "explanation": "x", "conditions": [], "flags": [], "created_ts": 1}
    store.insert("decisions", [row])
    store.insert("decisions", [row])
    assert len(store.select("decisions")) == 1


def test_buffered_writes_commit_in_batches(store):
    buffered = BufferedStore(store, store._write, flush_interval=0.01)
    for chunk in range(10):
        buffered.add_trades(_trade(chunk * 1000 + i) for i in range(1000))
    assert store.select("trades", limit=1) == []  # nothing committed before a flush
    assert buffered.flush_sync() == 10
    assert len(store.select("trades")) == 10_000
    buffered.close()


def test_cancelled_writer_loses_at_most_the_unflushed_batch(store):
    buffered = BufferedStore(store, store._write, flush_interval=0.01)

    async def scenario():
        task = asyncio.create_task(buffered.run())
        buffered.add_trades([_trade(1), _trade(2)])
        await asyncio.sleep(0.1)  # first batch flushes
        buffered.add_trades([_trade(3)])
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    ids = {r["trade_id"] for r in store.select("trades")}
    assert {1, 2} <= ids
    assert buffered.pending <= 1


def test_read_only_connection_cannot_write(store):
    # SQLite: mode=ro (OperationalError); Postgres: a read-only transaction (InternalError): both DBAPIErrors.
    with pytest.raises(DBAPIError):
        with store._read.begin() as c:
            c.exec_driver_sql("insert into gaps (start_ts, cause, repaired, created_ts) values (1, 'x', false, 1)")


class FlakyEngine:
    """The real write engine, except that the next `fails` transactions raise "database is locked"."""

    def __init__(self, engine, fails: int) -> None:
        self._engine = engine
        self.fails = fails

    def begin(self):
        if self.fails > 0:
            self.fails -= 1
            raise OperationalError("COMMIT", {}, Exception("database is locked"))
        return self._engine.begin()


def test_a_flush_whose_commit_fails_keeps_the_batch_and_the_next_flush_writes_it(store):
    engine = FlakyEngine(store._write, fails=1)
    buffered = BufferedStore(store, engine, flush_interval=0.01)
    buffered.add_trades([_trade(1), _trade(2)])

    async def scenario():
        await buffered.flush()  # locked: nothing written, nothing dropped
        assert store.select("trades") == []
        buffered.add_trades([_trade(3)])
        await buffered.flush()  # the failed batch in its own transaction, then the new rows

    asyncio.run(scenario())
    assert {r["trade_id"] for r in store.select("trades")} == {1, 2, 3}
    buffered.close()


def test_the_writer_loop_backs_off_and_keeps_running_while_writes_fail(store):
    engine = FlakyEngine(store._write, fails=3)
    buffered = BufferedStore(store, engine, flush_interval=0.01)

    async def scenario():
        task = asyncio.create_task(buffered.run())
        buffered.add_trades([_trade(1)])
        for _ in range(300):
            await asyncio.sleep(0.01)
            if store.select("trades"):
                break
        assert not task.done()
        task.cancel()

    asyncio.run(scenario())
    assert [r["trade_id"] for r in store.select("trades")] == [1]
    buffered.close()


# -- outages, bad rows, replay (docs/plans/2026-10-31-1800-review-fix-plan.md P0 #5) ----------------------------------
def _loud(monkeypatch):
    for name in ("btc_signal_engine", "btc_signal_engine.adapters.sqlite.writer"):  # may be detached (dictConfig)
        lg = logging.getLogger(name)
        monkeypatch.setattr(lg, "disabled", False)
        monkeypatch.setattr(lg, "propagate", True)


class FakeEngine:
    """The real write engine, except that while `down` every transaction fails as an unreachable server does
    (OperationalError: connection refused)."""

    def __init__(self, engine) -> None:
        self._engine = engine
        self.down = False
        self.begins = 0

    def begin(self):
        self.begins += 1
        if self.down:
            raise OperationalError("connect", {}, ConnectionRefusedError("connection refused"))
        return self._engine.begin()


class RefusingStore:
    """`store`, except that an insert into `gaps` with cause "bad" raises an IntegrityError inside the transaction
    (which rolls the whole batch back)."""

    def __init__(self, store) -> None:
        self._store = store

    def __getattr__(self, name):
        return getattr(self._store, name)

    def insert(self, table, rows, **kw):
        if table == "gaps" and any(r.get("cause") == "bad" for r in rows):
            raise IntegrityError("INSERT INTO gaps", {}, Exception("violates check constraint"))
        return self._store.insert(table, rows, **kw)


def _gap(i: int, cause: str = "x") -> dict:
    return {"start_ts": i, "cause": cause, "repaired": False, "created_ts": 1}


def test_an_outage_is_retried_indefinitely_and_nothing_is_dead_lettered(store, tmp_path, caplog, monkeypatch):
    _loud(monkeypatch)
    dead = tmp_path / "dead.jsonl"
    engine = FakeEngine(store._write)
    t = [0.0]
    buffered = BufferedStore(store, engine, flush_interval=0.01, dead_letter_path=dead, clock=lambda: t[0],
                             outage_log_every_s=60)
    engine.down = True

    async def scenario():
        for i in range(60):  # far past the old limit of 10 attempts (~33 s)
            buffered.add_trades([_trade(i)])
            buffered.insert("gaps", [_gap(i)])
            assert await buffered.flush() == 0
            t[0] += 5
        assert buffered.outage_s == 300
        assert buffered.pending == 120 and buffered.failed_batches == 1
        engine.down = False
        return await buffered.flush()

    with caplog.at_level(logging.WARNING):
        assert asyncio.run(scenario()) == 120
    assert not dead.exists()
    assert len(store.select("trades")) == 60 and len(store.select("gaps")) == 60
    assert buffered.outage_s is None and buffered.pending == 0 and buffered.dead_lettered == 0
    msgs = [r.getMessage() for r in caplog.records]
    assert sum("is not taking writes" in m for m in msgs) == 1  # once when it starts ...
    assert sum("has not taken writes for" in m for m in msgs) == 4  # ... every 60 s while it lasts (5 s steps) ...
    assert sum("takes writes again" in m for m in msgs) == 1  # ... and once when it ends
    buffered.close()


def test_a_refused_row_is_bisected_out_and_every_good_write_around_it_commits(store, tmp_path, caplog, monkeypatch):
    _loud(monkeypatch)
    dead = tmp_path / "dead.jsonl"
    buffered = BufferedStore(RefusingStore(store), FakeEngine(store._write), flush_interval=0.01,
                             dead_letter_path=dead)
    for i in range(1, 6):
        buffered.add_trades([_trade(i)])
    buffered.insert("gaps", [_gap(99, "bad")])
    for i in range(6, 11):
        buffered.add_trades([_trade(i)])
        buffered.insert("gaps", [_gap(i)])
    with caplog.at_level(logging.ERROR):
        assert asyncio.run(buffered.flush()) == 15
    assert {r["trade_id"] for r in store.select("trades")} == set(range(1, 11))
    assert {r["start_ts"] for r in store.select("gaps")} == set(range(6, 11))
    lines = [json.loads(x) for x in dead.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 1 and lines[0]["reason"] == "data" and "IntegrityError" in lines[0]["error"]
    assert lines[0]["writes"] == [{"method": "insert", "args": ["gaps", [_gap(99, "bad")]], "kwargs": {}}]
    assert any(r.levelno == logging.ERROR and "dead-lettered" in r.getMessage() for r in caplog.records)
    assert buffered.pending == 0 and buffered.dead_lettered == 1
    buffered.close()


def test_a_missing_table_is_a_bad_write_not_an_outage(store, tmp_path):
    dead = tmp_path / "dead.jsonl"
    buffered = BufferedStore(store, store._write, flush_interval=0.01, dead_letter_path=dead)
    buffered.insert("no_such_table", [{"a": 1}])
    buffered.add_trades([_trade(1)])
    assert asyncio.run(buffered.flush()) == 1
    lines = [json.loads(x) for x in dead.read_text(encoding="utf-8").splitlines()]
    assert [x["writes"] for x in lines] == [[{"method": "insert", "args": ["no_such_table", [{"a": 1}]],
                                              "kwargs": {}}]]
    buffered.close()


def test_transient_and_data_errors_are_told_apart():
    from sqlalchemy.exc import DataError, InterfaceError, ProgrammingError
    from sqlalchemy.exc import TimeoutError as PoolTimeout

    from btc_signal_engine.adapters.sqlite.writer import is_transient
    assert is_transient(OperationalError("x", {}, Exception("connection refused")))
    assert is_transient(OperationalError("x", {}, Exception("database is locked")))
    assert is_transient(InterfaceError("x", {}, Exception("connection already closed")))
    assert is_transient(PoolTimeout("QueuePool limit reached"))
    assert is_transient(ConnectionResetError())
    assert not is_transient(IntegrityError("x", {}, Exception("duplicate key")))
    assert not is_transient(DataError("x", {}, Exception("invalid input")))
    assert not is_transient(ProgrammingError("x", {}, Exception("undefined column")))
    assert not is_transient(OperationalError("x", {}, Exception("no such table: t")))
    assert not is_transient(KeyError("no_such_table"))


def test_a_full_queue_sheds_the_oldest_market_data_to_the_dead_letter_file_and_keeps_app_rows(store, tmp_path):
    dead = tmp_path / "dead.jsonl"
    engine = FakeEngine(store._write)
    buffered = BufferedStore(store, engine, flush_interval=0.01, dead_letter_path=dead, max_queued=6)
    engine.down = True
    for i in range(1, 9):
        buffered.add_trades([_trade(i)])
    buffered.insert("gaps", [_gap(1)])
    buffered.update("gaps", {"start_ts": 1}, {"repaired": True})
    asyncio.run(buffered.flush())
    assert buffered.pending == 6
    lines = [json.loads(x) for x in dead.read_text(encoding="utf-8").splitlines()]
    assert [x["reason"] for x in lines] == ["overflow"]
    assert [w["args"][0][0]["trade_id"] for w in lines[0]["writes"]] == [1, 2, 3, 4]  # the oldest market data
    engine.down = False
    asyncio.run(buffered.flush())
    assert {r["trade_id"] for r in store.select("trades")} == {5, 6, 7, 8}
    assert store.select("gaps")[0]["repaired"] is True  # the app's rows are never shed
    buffered.close()


def test_a_shutdown_during_an_outage_dead_letters_the_queue_at_once(store, tmp_path):
    dead = tmp_path / "dead.jsonl"
    engine = FakeEngine(store._write)
    buffered = BufferedStore(store, engine, flush_interval=0.01, dead_letter_path=dead)
    engine.down = True
    for i in range(5000):  # three batches of at most 2000 writes
        buffered.add_trades([_trade(i)])
    asyncio.run(buffered.flush())
    tries = engine.begins
    buffered.close()
    assert engine.begins == tries + 1  # one more try, not one per batch against a dead server
    lines = [json.loads(x) for x in dead.read_text(encoding="utf-8").splitlines()]
    assert {x["reason"] for x in lines} == {"shutdown"}
    assert sum(len(x["writes"]) for x in lines) == 5000


def test_writes_from_other_threads_are_never_lost_while_the_loop_flushes(store):
    import threading
    buffered = BufferedStore(store, store._write, flush_interval=0.001)

    def writer(base: int) -> None:
        for i in range(500):
            buffered.add_trades([_trade(base + i)])

    async def scenario():
        task = asyncio.create_task(buffered.run())
        threads = [threading.Thread(target=writer, args=(k * 10_000,)) for k in range(4)]
        for th in threads:
            th.start()
        while any(th.is_alive() for th in threads):
            await asyncio.sleep(0.001)
        task.cancel()

    asyncio.run(scenario())
    buffered.close()
    assert len(store.select("trades")) == 2000


def test_replay_applies_the_dead_letter_file_once_and_leaves_what_is_still_refused(store, tmp_path):
    from btc_signal_engine.adapters.sqlite.writer import replay_dead_letter
    dead = tmp_path / "dead.jsonl"
    engine = FakeEngine(store._write)
    buffered = BufferedStore(store, engine, flush_interval=0.01, dead_letter_path=dead)
    engine.down = True
    buffered.add_trades([_trade(1), _trade(2)])
    buffered.insert("gaps", [_gap(7)])
    buffered.update("gaps", {"start_ts": 7}, {"repaired": True})
    asyncio.run(buffered.flush())
    buffered.close()  # the database is still down: everything queued goes to the file (reason shutdown)
    with dead.open("a", encoding="utf-8") as f:  # plus a write the database refuses again
        f.write(json.dumps({"ts": 1, "reason": "data", "attempts": 1, "error": "x",
                            "writes": [{"method": "insert", "args": ["no_such_table", [{"a": 1}]], "kwargs": {}}]})
                + "\n")
    out: list[str] = []
    dry = replay_dead_letter(dead, store, store._write, apply=False, say=out.append)
    assert dry.pending == 4 and dry.applied == 0 and store.select("trades") == []
    res = replay_dead_letter(dead, store, store._write, apply=True, say=out.append)
    assert (res.applied, res.refused, res.stopped) == (3, 1, None)
    assert {r["trade_id"] for r in store.select("trades")} == {1, 2}
    assert store.select("gaps")[0]["repaired"] is True
    again = replay_dead_letter(dead, store, store._write, apply=True, say=out.append)
    assert (again.applied, again.skipped, again.refused) == (0, 3, 1)  # idempotent
    assert len(store.select("trades")) == 2


def test_replay_stops_while_the_database_is_unreachable_and_finishes_later(store, tmp_path):
    from btc_signal_engine.adapters.sqlite.writer import replay_dead_letter
    dead = tmp_path / "dead.jsonl"
    dead.write_text(json.dumps({"ts": 1, "reason": "shutdown", "writes": [
        {"method": "add_trades", "args": [[dataclasses.asdict(_trade(1))]], "kwargs": {}}]}) + "\n", encoding="utf-8")
    engine = FakeEngine(store._write)
    engine.down = True
    res = replay_dead_letter(dead, store, engine, apply=True, say=lambda _m: None)
    assert res.stopped and res.applied == 0
    assert replay_dead_letter(dead, store, store._write, apply=True, say=lambda _m: None).applied == 1
    assert [r["trade_id"] for r in store.select("trades")] == [1]


def test_ops_replay_dead_letter_is_a_dry_run_unless_asked(store, tmp_path, monkeypatch, capsys):
    if not hasattr(store._write.url, "database") or store._write.dialect.name != "sqlite":
        pytest.skip("the command reads its database from the settings (SQLite file here)")
    from btc_signal_engine.bootstrap import ops
    dead = tmp_path / "dead.jsonl"
    dead.write_text(json.dumps({"ts": 1, "reason": "data", "writes": [
        {"method": "add_trades", "args": [[dataclasses.asdict(_trade(5))]], "kwargs": {}}]}) + "\n", encoding="utf-8")
    monkeypatch.setenv("BTCFLOW_DB_PATH", store._write.url.database)
    monkeypatch.delenv("BTCFLOW_DATABASE_URL", raising=False)
    assert ops.main(["replay-dead-letter", "--file", str(dead)]) == 0
    assert store.select("trades") == []
    assert ops.main(["replay-dead-letter", "--file", str(dead), "--apply"]) == 0
    assert [r["trade_id"] for r in store.select("trades")] == [5]
    assert "1 writes applied" in capsys.readouterr().out


def test_writes_queued_behind_a_failed_batch_commit_after_it_in_order(store):
    """An UPDATE queued after a failed INSERT of the same row must not commit first (it would match 0 rows and be
    lost): it waits behind the failed batch and runs once that batch commits."""
    engine = FlakyEngine(store._write, fails=2)
    buffered = BufferedStore(store, engine, flush_interval=0.01)
    buffered.insert("gaps", [{"start_ts": 1, "cause": "x", "repaired": False, "created_ts": 1}])

    async def scenario():
        await buffered.flush()  # the insert fails and waits for retry
        buffered.update("gaps", {"start_ts": 1}, {"repaired": True})
        await buffered.flush()  # the insert's retry fails again: the update must wait
        assert store.select("gaps") == []
        await buffered.flush()  # database back: the insert, then the update

    asyncio.run(scenario())
    rows = store.select("gaps")
    assert len(rows) == 1 and rows[0]["repaired"] is True
    assert buffered.pending == 0
    buffered.close()


def test_pending_counts_writes_waiting_in_failed_batches(store):
    engine = FlakyEngine(store._write, fails=100)
    buffered = BufferedStore(store, engine, flush_interval=0.01)
    buffered.add_trades([_trade(1), _trade(2)])
    asyncio.run(buffered.flush())  # fails: two writes' worth of rows wait in one failed batch
    buffered.add_trades([_trade(3)])
    assert buffered.pending == 2
    assert buffered.failed_batches == 1
    engine.fails = 0  # the database recovers before shutdown
    buffered.close()
