"""One way to read and write a stored setting (strategy_settings, trading_settings, coach_settings).

A setting is one row: its key columns (strategy_settings: strategy + key; the others: key), a JSON `value` and
`updated_ts`. A write is one upsert (`Repository.insert(..., ignore_conflicts=False)`: insert, or replace the value
and time of the row with that key), never an update-then-insert pair that two writers could interleave (review fix
plan 2026-10-31, P2 "DRY": seven copies of that pair).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from btc_signal_engine.application.ports import Repository

STRATEGY_SETTINGS = "strategy_settings"


def get_setting(repo: Repository, key: Mapping[str, Any], default: Any = None, *,
                table: str = STRATEGY_SETTINGS) -> Any:
    """The stored value for this key, or `default` when there is no row."""
    rows = repo.select(table, dict(key), limit=1)
    return rows[0]["value"] if rows else default


def put_setting(repo: Repository, key: Mapping[str, Any], value: Any, now: int, *,
                table: str = STRATEGY_SETTINGS) -> None:
    """Store `value` under this key (one upsert)."""
    repo.insert(table, [{**key, "value": value, "updated_ts": now}], ignore_conflicts=False)
