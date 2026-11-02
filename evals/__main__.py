"""python -m evals <inspect command ...> | report [LOG]

    eval evals/tasks.py@pr ...   the Inspect CLI (eval, view, log, score, ...), with the log directory evals/logs
                                 and the checkout on PYTHONPATH (tasks.py imports `evals` and the tests' fakes)
    report [LOG]                 the run summary of a log (default: the newest): Adjudication, PASS / DEGRADED / FAIL

Inspect loads a `.env` file from the working directory or any parent into the environment. This repo never reads a
`.env` (keys come from the environment or a secret file), so the CLI refuses to start while one would be found.
"""

from __future__ import annotations

import importlib.util
import os
import sys

from evals import REPO, budget


def dotenv_refusal() -> str | None:
    from dotenv import find_dotenv  # python-dotenv: Inspect's own lookup, so the guard sees what Inspect would load
    found = find_dotenv(usecwd=True)
    if found:
        return (f"refusing to run Inspect: it would load {found} into the environment. Run from a checkout without a "
                ".env (secrets are files in ./secrets: docs/operations.md, \"Docker: secrets as files\").")
    return None


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if args else 2
    why = dotenv_refusal()
    if why:
        print(why, file=sys.stderr)
        return 2
    os.environ.setdefault("INSPECT_LOG_DIR", str(budget.log_dir()))
    # Inspect loads evals/tasks.py as a file; `evals` and `tests` (the simulated world's fakes) import from the checkout
    os.environ["PYTHONPATH"] = os.pathsep.join(x for x in (str(REPO), os.environ.get("PYTHONPATH", "")) if x)
    if args[0] == "report":
        from inspect_ai.log import list_eval_logs

        from evals.report import summary
        if len(args) > 1:
            path = args[1]
        else:
            logs = list_eval_logs(os.environ["INSPECT_LOG_DIR"])
            if not logs:
                print(f"no logs in {os.environ['INSPECT_LOG_DIR']}", file=sys.stderr)
                return 2
            path = logs[0].name
        md, verdict = summary(path)
        print(md)
        return 1 if verdict["status"] == "FAIL" else 0
    # Inspect from this interpreter's own environment (`python -m inspect_ai`), not whichever `inspect` script is first
    # on PATH: an environment that is not activated (CI's .venv) has its scripts off PATH.
    if importlib.util.find_spec("inspect_ai") is None:
        print("the Inspect CLI is not installed (the `evals` extra)", file=sys.stderr)
        return 2
    os.execv(sys.executable, [sys.executable, "-m", "inspect_ai", *args])  # noqa: S606 - argv passed through unchanged
    return 0  # not reached


if __name__ == "__main__":
    sys.exit(main())
