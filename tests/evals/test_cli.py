"""`python -m evals`: the Inspect CLI behind the .env guard (Inspect loads a .env into the environment; this repo never
reads one), the log directory and PYTHONPATH it sets, and `report` on a log."""

from __future__ import annotations

import os

from evals import REPO
from evals import __main__ as cli
from evals import tasks


def test_the_cli_refuses_while_inspect_would_load_a_dotenv(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("", encoding="utf-8")  # an empty planted file: its presence is the point
    monkeypatch.setattr(os, "execv", lambda *a: (_ for _ in ()).throw(AssertionError("Inspect must not start")))
    assert cli.main(["eval", "evals/tasks.py@pr"]) == 2
    assert "refusing to run Inspect" in capsys.readouterr().err


def test_the_cli_passes_through_to_inspect_with_the_log_dir_and_the_checkout_on_the_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("INSPECT_LOG_DIR", raising=False)
    monkeypatch.setenv("PYTHONPATH", "")  # restored after the test (the CLI sets both for the Inspect it starts)
    seen = {}
    monkeypatch.setattr(os, "execv", lambda exe, argv: seen.update(argv=argv[1:]))
    cli.main(["eval", "evals/tasks.py@pr", "--limit", "1"])
    assert seen["argv"] == ["eval", "evals/tasks.py@pr", "--limit", "1"]
    assert os.environ["INSPECT_LOG_DIR"].endswith("evals/logs")
    assert str(REPO) in os.environ["PYTHONPATH"].split(os.pathsep)


def test_report_prints_the_adjudication_first_and_exits_by_status(run_eval, capsys, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "")
    log = run_eval(tasks.pr())
    assert cli.main(["report", log.location]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Adjudication: confirmed") and "**Status: PASS**" in out
