"""Shared fixtures for the eval tests: Inspect runs in a temporary log directory and working directory (Inspect loads
a `.env` from the working directory or its parents; the tests never give it one), and a writable copy of the
datasets for tests that break an item on purpose. No test calls a real model: every model is `mockllm`."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
from inspect_ai import Task
from inspect_ai import eval as inspect_eval
from inspect_ai.log import EvalLog

from evals import DATASETS
from evals import dataset as ds


@pytest.fixture
def run_eval(tmp_path, monkeypatch) -> Callable[..., EvalLog]:
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("INSPECT_LOG_DIR", str(tmp_path / "logs"))

    def run(task: Task, **kw) -> EvalLog:
        (log,) = inspect_eval(task, log_dir=str(tmp_path / "logs"), display="none", **kw)
        return log
    return run


@pytest.fixture
def datasets_copy(tmp_path, monkeypatch) -> Path:
    d = tmp_path / "datasets"
    shutil.copytree(DATASETS, d)
    monkeypatch.setattr(ds, "DATASETS", d)
    return d


def item(sample_id: str, sets: tuple[str, ...] = ("golden", "adversarial")) -> dict:
    return next(s for s in ds.load(list(sets)) if s.id == sample_id).metadata
