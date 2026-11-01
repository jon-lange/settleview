"""`ops migrate` (the compose migrate job): a failure is one ERROR record (it reaches OpenObserve as service
settleview-migrate, where deploy/openobserve/alerts/settleview_migration_failed.json reads it) and exit 1."""

from __future__ import annotations

import logging

from btc_signal_engine.adapters.sqlite import db
from btc_signal_engine.bootstrap import ops


def _env(monkeypatch, tmp_path):
    for k in list(__import__("os").environ):
        if k.startswith(("BTCFLOW_", "OTEL_")):
            monkeypatch.delenv(k)
    monkeypatch.setenv("BTCFLOW_DB_PATH", str(tmp_path / "m.db"))


def test_a_failed_migration_logs_one_error_and_exits_1(monkeypatch, tmp_path, caplog):
    _env(monkeypatch, tmp_path)

    def boom(target):
        raise RuntimeError("lock timeout")
    monkeypatch.setattr(db, "migrate", boom)
    with caplog.at_level(logging.INFO):
        assert ops.migrate_db() == 1
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert [r.getMessage() for r in errors] == ["migration failed"]
    assert errors[0].name == "btc_signal_engine.migrate" and errors[0].exc_info


def test_a_migration_that_works_exits_0(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    assert ops.migrate_db() == 0
    assert (tmp_path / "m.db").exists()
