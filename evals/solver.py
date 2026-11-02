"""The Inspect solver: one agent wake per sample, in the item's mode, with the chosen subject.

Subjects:
- `reference`: `mockllm/model` plays the item's own script: a frozen item's recording, a simulated item's reference
  solution. Free and offline; it tests the harness, the items and the scorers, and says nothing about Claude.
- `mutant`: `mockllm/model` plays the sample's mutant (a deliberately broken subject; the `mutants` task).
- `model`: the eval's model (`--model anthropic/claude-haiku-5-5`): a live run, budget enforced by the task.
- `recorded`: no model at all; the exported wake's own trajectory is graded (frozen-transcript scoring).

The trial (trajectory, context, world) is kept in the sample's store (`trial`) for the scorers; the conversation goes
into `state.messages` and the model calls into the transcript, so `inspect view` shows the wake.
"""

from __future__ import annotations

from inspect_ai.model import get_model
from inspect_ai.solver import Generate, Solver, TaskState, solver
from inspect_ai.util import LimitExceededError

from btc_signal_engine.application.agent_runner import PRICES
from evals import frozen, sim
from evals.harness import InspectHarness, replay_turns, scripted_model

SUBJECTS = ("reference", "mutant", "model", "recorded")


def subject_model_id(meta: dict, subject: str) -> str:
    """The model the session is priced as: the live subject's (it must be one the app prices), else the item's."""
    if subject != "model":
        return meta["model"]
    m = get_model()
    if m.name in PRICES:
        return m.name
    if str(m).startswith("mockllm/"):
        return meta["model"]
    raise ValueError(f"the app has no price for {m}; known: {', '.join(PRICES)}")


def script_for(meta: dict, subject: str):
    if subject == "mutant":
        turns = meta["mutant"]["turns"]
    elif meta["mode"] == "frozen":
        rec = meta["frame"]["recorded"]
        turns = replay_turns(rec.get("tools") or [], rec.get("final_text"))
    else:
        turns = meta["reference"]
    return scripted_model(turns, usage=(meta.get("scenario") or {}).get("usage"))


@solver
def wake(subject: str = "model") -> Solver:
    """Run the sample's wake with `subject` (reference | mutant | model | recorded) and keep its trial."""
    if subject not in SUBJECTS:
        raise ValueError(f"unknown subject {subject!r}; subjects: {SUBJECTS}")

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        meta = state.metadata
        state.store.set("subject", subject)
        if subject == "recorded":
            state.store.set("trial", frozen.recorded_trial(meta))
            return state
        model_id = subject_model_id(meta, subject)
        model = get_model() if subject == "model" else script_for(meta, subject)
        limit: LimitExceededError | None = None
        if meta["mode"] == "frozen":
            harness = InspectHarness(model)
            try:
                trial = await frozen.run(meta, harness, model_id=model_id, replay=subject == "reference")
            except LimitExceededError as e:  # Inspect's token / cost limit: the partial wake is kept, DEGRADED
                limit, trial = e, {"item_id": meta["id"], "mode": "frozen", "model": model_id, "status": "limit",
                                   "tools": [], "context": dict(meta.get("context") or {}), "world": {}}
        else:
            trial, harness = await sim.run(meta, model, model_id=model_id)
            limit = getattr(harness, "limit", None)
        if limit is not None:
            trial["limit"] = str(limit)
        state.store.set("trial", trial)
        state.messages = list(harness.messages) or state.messages
        if harness.output is not None:
            state.output = harness.output
        if limit is not None:
            raise limit
        return state

    return solve
