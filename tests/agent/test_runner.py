"""Agent runner (U6): the wake loop against a scripted fake harness (no subprocess, no SDK), on a real AgentSessions /
AgentTools / TradingService over the shadow exchange (no network), with an injected clock."""

from __future__ import annotations

import asyncio
import json

import httpx2
import pytest

import anthropic
from btc_signal_engine.adapters.anthropic.llm import AnthropicLlm
from btc_signal_engine.application.agent_runner import AgentRunner, RunnerConfig, worst_case_usd
from btc_signal_engine.application.agent_tools import AgentTools
from btc_signal_engine.application.ports import LlmBadRequest, LlmInvalidKey, LlmMisconfigured, LlmUnavailable
from tests.agent.fake_harness import FakeHarness, text, thinking, tool_use, turn
from tests.agent.test_tools import SID, Rig, decision, entry
from tests.trading.helpers import CLOSE, NOW


class Clock:
    def __init__(self, t: int = NOW) -> None:
        self.t = t

    def now_ms(self) -> int:
        return self.t


async def _yield(_s):
    await asyncio.sleep(0)


def setup(store, *script, model="claude-haiku-5-5", spend_cap=5.0, config=None, live=None):
    r = Rig(store).launch(model=model, spend_cap=spend_cap)
    holder = {}
    tools = AgentTools(r.ag, r.play, events_fn=lambda sid: holder["runner"].events)
    llm = FakeHarness(*script)
    rows = []
    runner = AgentRunner(r.ag, tools, r.play, llm, prompts_fn=lambda: ("You trade carefully.", "", {"system": 1}),
                         clock=Clock(), config=config, notify_fn=rows.append, live_fn=live, sleep=_yield)
    holder["runner"] = runner
    assert runner.attach(SID)
    return r, runner, llm, rows


def note(runner, text_="go"):
    runner.events.push(runner.now(), "human_note", value={"text": text_})


def wakes(r):
    return [j["data"] for j in r.ag.journal_rows(SID) if j["kind"] == "wake"]


def _tag(prompt: str, name: str) -> str:
    return prompt[prompt.index(f"<{name}>") + len(name) + 2:prompt.index(f"</{name}>")]


def test_observe_then_place_entry_sends_one_order_and_journals_the_wake(store):
    first = turn(thinking("Check the book first."), tool_use("t1", "observe", {}))
    r, runner, llm, rows = setup(store, first, turn(tool_use("t2", "place_entry", entry())),
                                 turn(text("Bought UP at 58c: BTC $45 over the strike.")))

    async def main():
        note(runner)
        assert await runner.tick()
    asyncio.run(main())

    assert len(r.buys()) == 1 and len(store.select("trading_brackets")) == 1
    w = wakes(r)
    assert len(w) == 1 and rows and rows[0]["data"] == w[0]
    w = w[0]
    assert w["status"] == "done" and w["trigger"][0]["type"] == "human_note"
    assert w["decision"].startswith("Bought UP") and w["thinking"] == ["Check the book first."]
    assert [(x["name"], x["ok"]) for x in w["tools"]] == [("observe", True), ("place_entry", True)]
    assert w["tools"][1]["args"]["request_id"] == "r1" and w["turns"] == 3
    assert w["est_usd"] > 0 and w["tokens"]["input"] == 6_000
    assert w["runtime"]["turns"] == 3 and w["runtime"]["cost_usd"] == 0.0  # the runtime's own report, journalled
    usage = r.ag.latest()["usage"]
    assert usage["wakes"] == 1 and usage["api_usd"] == pytest.approx(w["est_usd"])
    # the wake message: memory (envelope + facts as data), events, an inline observe, in that order, as one text
    q = llm.requests[0]
    assert q.prompt.startswith("<memory>") and q.prompt.index("<events>") < q.prompt.index("<observe>")
    memory = json.loads(_tag(q.prompt, "memory"))
    assert memory["session"]["budget"] == 20.0 and memory["facts"] and memory["trades"] == []
    # what woke it, and each allowed market's clock (NOW is 10 min before CLOSE; Kalshi entries close 2 min before)
    assert memory["wake"] == {"decision_points": [], "position_events": [], "other": ["human_note"]}
    (mk,) = memory["markets"]
    assert mk["market"] == "kalshi_15m" and mk["seconds_to_close"] == 600 and mk["seconds_to_entry_cutoff"] == 480
    assert q.options["session_id"] == SID and q.options["model"] == "claude-haiku-5-5"
    assert q.options["max_turns"] == 6 and q.options["max_tokens"] == 16_000
    assert q.options["max_budget_usd"] == pytest.approx(5.0)  # the runtime's backstop: what is left of the cap
    # the model got each tool's result back as JSON text
    (name, out, err), = q.results[0]
    assert name == "observe" and json.loads(out)["ok"] is True and err is False


def test_a_kalshi_15m_only_session_reads_and_wakes_for_that_market_only(store):
    r, runner, llm, _ = setup(store, turn(text("Waiting for the window.")))
    asked = []
    real = r.play.now_view
    r.play.now_view = lambda now, venue, *, span, **kw: asked.append((venue, span)) or real(now, venue, span=span, **kw)
    runner.events.push(runner.now(), "price_move", venue="polymarket", span="15m", value={"mid_up": 0.9})

    async def main():
        for _ in range(40):
            runner.clock.t += 1_000
            assert await runner.tick()
    asyncio.run(main())
    assert set(asked) == {("kalshi", "15m")}
    assert all(e["venue"] in (None, "kalshi") for w in wakes(r) for e in w["trigger"])
    assert list(runner.events.schedule(runner.now())) == [("kalshi", "15m")]


def test_system_and_tools_are_byte_identical_across_wakes(store):
    r, runner, llm, _ = setup(store, turn(text("Nothing to do.")), turn(text("Still nothing.")))
    clock = runner.clock

    async def main():
        note(runner)
        await runner.tick()
        clock.t += 10_000
        note(runner, "again")
        await runner.tick()
    asyncio.run(main())

    assert len(llm.requests) == 2

    def prefix(q):
        return json.dumps([q.system, q.tools], separators=(",", ":"))
    assert prefix(llm.requests[0]) == prefix(llm.requests[1])
    q = llm.requests[0]
    assert q.system.startswith("You trade carefully.") and "How this app talks to you" in q.system
    assert {t["name"]: t["kind"] for t in q.tools}["place_entry"] == "write"


def test_spend_cap_reached_mid_wake_finishes_the_tool_then_ends_the_session(store):
    big = {"input_tokens": 20_000, "output_tokens": 20_000, "cache_creation_input_tokens": 0,
           "cache_read_input_tokens": 0}  # $0.08 + $0.40 on Opus
    r, runner, llm, _ = setup(store, turn(tool_use("t1", "place_entry", entry()), usage=big),
                              model="claude-opus-5-5", spend_cap=0.80, config=RunnerConfig(max_tokens=20_000))

    async def main():
        note(runner)
        return await runner.tick()
    assert asyncio.run(main()) is False

    assert len(r.buys()) == 1 and len(llm.requests[0].turns) == 1  # the order went; no second turn
    row = store.select("agent_sessions")[0]
    assert (row["state"], row["end_reason"]) == ("ended", "spend_cap")
    w = wakes(r)[0]
    assert w["status"] == "spend_cap" and w["tools"][0]["ok"] is True
    assert w["spend"]["spent"] + w["spend"]["next_turn_worst_case"] > 0.80
    assert row["usage"]["api_usd"] == pytest.approx(0.48)
    b = store.select("trading_brackets")[0]
    assert b["state"] == "open"  # positions are handed back, not sold


def test_stop_during_a_model_call_sends_nothing_and_keeps_the_reservation(store):
    started, never = asyncio.Event, asyncio.Event
    box = {}

    async def hang(req):
        box["started"].set()
        await box["never"].wait()
    r, runner, llm, _ = setup(store, hang)

    async def main():
        box["started"], box["never"] = started(), never()
        note(runner)
        task = asyncio.create_task(runner.run(SID))
        await box["started"].wait()
        out = await runner.stop(runner.now())
        assert out["ok"] and task.done()
    asyncio.run(main())

    assert r.buys() == []
    w = wakes(r)[0]
    assert w["status"] == "cancelled" and w["turns"] == 0 and w["reserved_lost_usd"] > 0
    row = store.select("agent_sessions")[0]
    assert (row["state"], row["end_reason"]) == ("ended", "stopped")
    assert row["usage"]["api_usd"] == pytest.approx(w["reserved_lost_usd"])


def test_stop_during_place_entry_lets_the_order_finish_and_records_it_once(store):
    r, runner, llm, _ = setup(store, turn(tool_use("t1", "place_entry", entry())))
    sim = r.ex.sim
    real = sim.handle_async_request
    box = {}

    async def slow(request):
        if request.method == "POST":
            box["posting"].set()
            await box["release"].wait()
        return await real(request)
    sim.handle_async_request = slow

    async def main():
        box["posting"], box["release"] = asyncio.Event(), asyncio.Event()
        note(runner)
        task = asyncio.create_task(runner.run(SID))
        await box["posting"].wait()
        stopping = asyncio.create_task(runner.stop(runner.now()))
        for _ in range(5):
            await asyncio.sleep(0)
        assert not stopping.done()  # waits for the order in flight
        box["release"].set()
        out = await stopping
        assert out["ok"] and task.done()
    asyncio.run(main())

    assert len(r.buys()) == 1 and len(store.select("trading_brackets")) == 1
    tool_rows = [j for j in r.ag.journal_rows(SID) if j["kind"] == "tool"]
    assert [j["data"]["name"] for j in tool_rows] == ["place_entry"] and tool_rows[0]["data"]["ok"] is True
    w = wakes(r)[0]
    assert w["status"] == "cancelled" and [(x["name"], x["ok"]) for x in w["tools"]] == [("place_entry", True)]
    assert store.select("agent_sessions")[0]["end_reason"] == "stopped"


def test_a_write_dispatched_after_stop_is_refused_while_the_one_in_flight_finishes_once(store):
    # The SDK runs tool calls in its own tasks, so the CLI can still dispatch one while Stop unwinds the wake.
    r, runner, llm, _ = setup(store, turn(tool_use("t1", "place_entry", entry())))
    sim = r.ex.sim
    real = sim.handle_async_request
    box = {}

    async def slow(request):
        if request.method == "POST":
            box["posting"].set()
            await box["release"].wait()
        return await real(request)
    sim.handle_async_request = slow

    async def main():
        box["posting"], box["release"] = asyncio.Event(), asyncio.Event()
        note(runner)
        task = asyncio.create_task(runner.run(SID))
        await box["posting"].wait()
        stopping = asyncio.create_task(runner.stop(runner.now()))
        await asyncio.sleep(0)
        assert r.ag.latest()["state"] == "running" and not stopping.done()  # still unwinding
        late, is_error = await asyncio.wait_for(  # refused at once, never queued behind the order
            llm.execute("place_entry", entry(request_id="r2", side="DOWN", max_price=0.47)), 2)
        assert is_error and json.loads(late)["code"] == "session_stopped"
        box["release"].set()
        out = await stopping
        assert out["ok"] and task.done()
    asyncio.run(main())

    assert len(r.buys()) == 1 and len(store.select("trading_brackets")) == 1
    tool_rows = [j for j in r.ag.journal_rows(SID) if j["kind"] == "tool"]
    assert [(j["data"]["name"], j["data"]["ok"]) for j in tool_rows] == [("place_entry", True)]
    w = wakes(r)[0]
    assert [(x["name"], x["ok"], x["code"]) for x in w["tools"]] == [("place_entry", True, None),
                                                                     ("place_entry", False, "session_stopped")]
    assert store.select("agent_sessions")[0]["end_reason"] == "stopped"


def test_the_order_gate_refuses_a_new_agent_order_once_a_stop_is_asked_for(store):
    r = Rig(store).launch()
    r.ag.begin_stop(r.ag.latest()["id"])
    out = r.call("place_entry", entry())
    assert out["ok"] is False and out["code"] == "session_stopped" and r.buys() == []
    r.ag.stop(r.ag.latest()["id"], NOW)
    assert not bool(r.ag._stopping)


def test_an_unexpected_wake_error_is_journalled_pauses_the_session_and_does_not_block_later_wakes(store, caplog):
    r, runner, llm, _ = setup(store, ValueError("bad json from the runtime"))

    async def main():
        note(runner)
        await runner.tick()  # never raises out of the loop
    with caplog.at_level("ERROR"):
        asyncio.run(main())

    assert "agent wake failed" in caplog.text and "ValueError" in caplog.text  # the traceback is logged
    row = store.select("agent_sessions")[0]
    assert row["state"] == "paused" and row["pause_code"] == "other"
    assert row["pause_reason"] == "The agent wake failed: ValueError: bad json from the runtime"
    w = wakes(r)[0]
    assert w["status"] == "error" and w["error"] == "ValueError: bad json from the runtime"
    assert w["reserved_lost_usd"] > 0
    assert r.buys() == []
    # the events engine is not left "running": once resumed, the session wakes again
    assert r.ag.resume(r.ag.latest()["id"], NOW)["ok"]
    runner.clock.t = NOW + 60_000
    note(runner)
    runner.clock.t = NOW + 75_000
    assert runner.events.should_wake(runner.now(), budget_exhausted=False)


@pytest.mark.parametrize("error, state, reason, code", [
    (LlmUnavailable("Overloaded (after retries)."), "paused", "The model is unavailable: Overloaded",
     "model_unavailable"),
    (LlmInvalidKey("401 invalid x-api-key"), "ended", "invalid_key", None),
    (LlmMisconfigured("the runtime's credential source is 'none'"), "paused", "agent runtime misconfigured: the "
                                                                                "runtime's credential source",
     "runtime_misconfigured"),
    (LlmBadRequest("prompt is too long"), "paused", "The model rejected the app's request", "bad_request"),
])
def test_model_errors_pause_or_end_the_session(store, error, state, reason, code):
    r, runner, llm, _ = setup(store, error)

    async def main():
        note(runner)
        await runner.tick()
    asyncio.run(main())

    row = store.select("agent_sessions")[0]
    assert row["state"] == state
    assert (row["pause_reason"] or "").startswith(reason) if state == "paused" else row["end_reason"] == reason
    assert row["pause_code"] == code and r.ag.latest_view(NOW)["pause_code"] == code
    w = wakes(r)[0]
    assert w["status"] in ("unavailable", "invalid_key", "misconfigured", "bad_request") and w["reserved_lost_usd"] > 0
    assert r.buys() == []


def test_a_call_the_runtime_never_ran_is_journalled_as_not_run(store):
    class Refusing(FakeHarness):  # the runtime refused the call's input before it reached the registry
        async def run_wake(self, system, prompt, specs, execute, **kw):
            async def refused(name, args):
                return '{"ok":false,"code":"invalid"}', True
            return await super().run_wake(system, prompt, specs, refused, **kw)
    r, runner, _, _ = setup(store)
    llm = runner.harness = Refusing(turn(tool_use("t1", "place_entry", {"market": "nowhere"})), turn(text("Hm.")))

    async def main():
        note(runner)
        await runner.tick()
    asyncio.run(main())

    assert len(llm.requests) == 1 and r.buys() == []
    w = wakes(r)[0]
    assert [(x["name"], x["ok"], x["code"]) for x in w["tools"]] == [("place_entry", False, "not_run")]
    assert w["segments"] == [{"kind": "tool", "index": 0}, {"kind": "text", "text": "Hm."}]


def test_urgent_event_during_turn_two_arrives_as_new_events_with_its_tool_result(store):
    r, runner, llm, _ = setup(store)

    def close_it(req):  # while the model thinks, the take profit fills and the position closes
        bid = store.select("trading_brackets")[0]["id"]
        store.update("trading_brackets", {"id": bid}, {"state": "closed", "exit_reason": "tp"})
        return turn(tool_use("t2", "observe", {}))
    llm.script = [turn(tool_use("t1", "place_entry", entry())), close_it, turn(text("Closed at the take profit."))]

    async def main():
        note(runner)
        await runner.tick()
    asyncio.run(main())

    q = llm.requests[0]
    assert len(q.turns) == 3
    assert "<new_events>" not in q.results[0][0][1]  # the agent's own fill is not news to it
    out = q.results[1][0][1]
    news = json.loads(out[out.index("<new_events>") + 12:out.index("</new_events>")])
    assert [e["type"] for e in news] == ["position_closed"]  # its own fill was absorbed, not re-announced
    assert json.loads(out[:out.index("\n<new_events>")])["ok"] is True  # the result itself is still JSON
    assert wakes(r)[0]["new_events"] == news
    assert runner.events.pending() == []  # delivered once, not again on the next wake


def test_live_deltas_reach_the_hook_in_order_with_the_wake_id(store):
    seen = []
    deltas = (("thinking", "Look"), ("thinking", "ing."), ("text", "Noth"), ("text", "ing."))
    r, runner, llm, _ = setup(store, turn(thinking("Looking."), text("Nothing."), deltas=deltas),
                              live=lambda sid, wake, kind, t: seen.append((sid, wake, kind, t)))

    async def main():
        note(runner)
        await runner.tick()
    asyncio.run(main())

    assert seen == [(SID, 1, k, t) for k, t in deltas]
    w = wakes(r)[0]
    assert w["wake_id"] == 1 and w["thinking"] == ["Looking."] and w["decision"] == "Nothing."


def test_a_wake_without_record_decision_gets_an_unrecorded_row(store):
    r, runner, llm, _ = setup(store, turn(text("Nothing to do.")))

    async def main():
        note(runner)
        await runner.tick()
    asyncio.run(main())

    assert len(llm.requests[0].turns) == 1  # no re-prompt
    assert "record_decision exactly once" in llm.requests[0].system
    d = runner.tools.decisions(SID)
    assert len(d) == 1
    d = d[0]
    assert d["action"] == "UNRECORDED" and d["pass_reason"] is None and d["source"] == "runner"
    assert d["note"] == "the agent did not record a decision" and d["wake_id"] == 1 and d["wake_status"] == "done"
    assert d["market"] is None and d["thesis"] is None
    kinds = [j["kind"] for j in r.ag.journal_rows(SID)]
    assert kinds.index("decision") < kinds.index("wake")  # the decision is in before the wake row is announced


def test_recorded_decision_carries_the_wake_id_and_shows_compactly_in_the_next_memory(store):
    rec = decision(thesis="Market is the probability. " * 20)
    r, runner, llm, _ = setup(store, turn(tool_use("t1", "record_decision", rec)), turn(text("Passed.")),
                              turn(text("Still nothing.")))
    clock = runner.clock

    async def main():
        note(runner)
        await runner.tick()
        clock.t += 10_000
        note(runner, "again")
        await runner.tick()
    asyncio.run(main())

    ds = runner.tools.decisions(SID)
    assert [(d["action"], d["wake_id"], d["source"]) for d in ds] == [("NO_TRADE", 1, "agent"),
                                                                       ("UNRECORDED", 2, "runner")]
    memory = json.loads(_tag(llm.requests[1].prompt, "memory"))
    last = memory["recent_decisions"]
    assert len(last) == 1
    x = last[0]
    assert (x["wake_id"], x["market"], x["action"], x["pass_reason"], x["p_estimate"], x["market_p"]) == (
        1, "kalshi_15m", "NO_TRADE", "NO_VALIDATED_EDGE", 0.56, 0.58)
    assert len(x["thesis"]) <= 201 and "side" not in x  # nulls dropped, long text cut
    assert x["window_end"] == CLOSE


def test_worst_case_prices_come_from_one_table_and_unknown_models_are_errors():
    assert worst_case_usd("claude-opus-5-5", 1_000_000, 0) == pytest.approx(5.00)
    assert worst_case_usd("claude-haiku-5-5", 10_000, 1_000_000) == pytest.approx(0.00125 + 0.50)
    assert worst_case_usd("claude-haiku-5-5", 200_000, 0) == pytest.approx(0.125)  # long-prompt rate
    with pytest.raises(ValueError):
        worst_case_usd("claude-made-up", 1, 1)


# -- the launch key check, with a fake SDK client (no network) --------------------------------------------------------
class _Models:
    def __init__(self, error=None):
        self.error, self.calls = error, []

    async def list(self, **kw):
        self.calls.append(kw)
        if self.error is not None:
            raise self.error


class _Sdk:
    def __init__(self, error=None):
        self.models = _Models(error)


def test_key_check_lists_one_model_and_maps_errors():
    sdk = _Sdk()
    asyncio.run(AnthropicLlm("unused", client=sdk).check_key())
    assert sdk.models.calls == [{"limit": 1}]
    req = httpx2.Request("GET", "https://api.anthropic.com/v1/models")

    def status(cls, code, msg):
        return cls(msg, response=httpx2.Response(code, request=req), body={"error": {"message": msg}})
    for err, want in ((status(anthropic.AuthenticationError, 401, "invalid x-api-key"), LlmInvalidKey),
                      (status(anthropic.BadRequestError, 400, "bad"), LlmBadRequest),
                      (status(anthropic.RateLimitError, 429, "slow down"), LlmUnavailable),
                      (status(anthropic.InternalServerError, 529, "Overloaded"), LlmUnavailable),
                      (anthropic.APITimeoutError(request=req), LlmUnavailable)):
        with pytest.raises(want):
            asyncio.run(AnthropicLlm("unused", client=_Sdk(err)).check_key())
    with pytest.raises(ValueError):
        AnthropicLlm("  ")
    assert not hasattr(AnthropicLlm, "turn")  # the model loop is the harness's


def test_the_wake_row_keeps_text_and_tool_calls_in_the_order_written(store):
    t1 = turn(text("First I look."), tool_use("t1", "observe", {}),
              deltas=(("text", "First "), ("text", "I look."), ("tool_use", "observe")))
    t2 = turn(text("Then I pass."), tool_use("t2", "observe", {}), text("Done."),
              deltas=(("text", "Then I pass."), ("tool_use", "observe"), ("text", "Done.")))
    r, runner, llm, _ = setup(store, t1, t2, turn(text("Nothing more.")))  # the last turn streams one text delta

    async def main():
        note(runner)
        await runner.tick()
    asyncio.run(main())

    w = wakes(r)[0]
    assert w["segments"] == [{"kind": "text", "text": "First I look."}, {"kind": "tool", "index": 0},
                             {"kind": "text", "text": "Then I pass."}, {"kind": "tool", "index": 1},
                             {"kind": "text", "text": "Done."}, {"kind": "text", "text": "Nothing more."}]
    assert [x["name"] for x in w["tools"]] == ["observe", "observe"]


def test_without_streamed_deltas_the_segments_come_from_each_turn_and_each_call(store):
    class Quiet(FakeHarness):  # no deltas at all (a runtime without partial messages)
        async def run_wake(self, system, prompt, specs, execute, **kw):
            return await super().run_wake(system, prompt, specs, execute, **{**kw, "on_delta": None})
    r, runner, _, _ = setup(store)
    runner.harness = Quiet(turn(text("Looking."), tool_use("t1", "observe", {})), turn(text("Pass.")))

    async def main():
        note(runner)
        await runner.tick()
    asyncio.run(main())

    assert wakes(r)[0]["segments"] == [{"kind": "text", "text": "Looking."}, {"kind": "tool", "index": 0},
                                       {"kind": "text", "text": "Pass."}]


def test_start_fn_hears_each_wake_with_its_trigger_before_the_row(store):
    r, runner, llm, rows = setup(store, turn(text("Ok.")))
    started = []
    runner._start = lambda row: started.append((row, len(rows)))

    async def main():
        note(runner)
        await runner.tick()
    asyncio.run(main())

    (row, rows_then), = started
    assert rows_then == 0 and row["kind"] == "wake_start" and row["session_id"] == SID and row["ts"] == NOW
    assert row["data"]["wake_id"] == 1 and [e["type"] for e in row["data"]["trigger"]] == ["human_note"]


def test_the_key_check_client_never_uses_an_inherited_base_url(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:9/elsewhere")
    llm = AnthropicLlm("sk-ant-test-not-a-key")
    assert str(llm._client.base_url).rstrip("/") == "https://api.anthropic.com"


def test_each_wake_journals_its_exact_input_and_fingerprint_beside_the_row(store):
    """Evals' replay set (docs/plans/2026-10-31-1900-agent-evals-plan.md): the user message as sent (memory + events +
    observe), its hash on the row, and the fingerprint of everything else the model saw."""
    r, runner, llm, rows = setup(store, turn(text("Nothing to do.")))
    runner._scrub = lambda t: t.replace("You trade", "x")  # the bootstrap wires the app's secret scrubber
    runner._build = lambda: {"git_sha": "abc123"}

    async def main():
        note(runner, "watch the book")
        assert await runner.tick()
    asyncio.run(main())

    (w,) = wakes(r)
    q = llm.requests[0]
    (wi,) = r.ag.wake_inputs(SID)
    assert wi["message"] == q.prompt and wi["wake_id"] == w["wake_id"]  # verbatim: memory, events and observe
    for tag in ("memory", "events", "observe"):
        assert f"<{tag}>" in wi["message"]
    assert "watch the book" in _tag(wi["message"], "events") and '"balances"' in _tag(wi["message"], "observe")
    journal = [j for j in r.ag.journal_rows(SID) if j["kind"] == "wake"][0]
    assert wi["journal_id"] == journal["id"]
    import hashlib
    assert w["input"] == {"sha256": hashlib.sha256(q.prompt.encode()).hexdigest(), "chars": len(q.prompt),
                          "scrubbed": False, "stored": True} == {**w["input"], "sha256": wi["message_sha256"]}
    assert "message" not in w and "<observe>" not in json.dumps(w)  # the big payload stays off the journal row
    fp = w["fingerprint"]
    assert fp == wi["fingerprint"]
    assert fp["system_prompt_sha256"] == hashlib.sha256(q.system.encode()).hexdigest()
    assert fp["tool_specs_sha256"] == hashlib.sha256(
        json.dumps(q.tools, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert (fp["user_layer_versions"], fp["model"], fp["effort"], fp["max_tokens"], fp["max_turns"]) == (
        {"system": 1}, "claude-haiku-5-5", "medium", 16_000, 6)
    assert fp["git_sha"] == "abc123" and fp["cli_version"] and fp["sdk"]
    assert "observe.balances" in wi["sensitive"]["drop"]  # kept (part of the input), marked for the export


def test_a_scrubbed_wake_input_is_stored_scrubbed_and_never_raw(store):
    r, runner, llm, _ = setup(store, turn(text("ok")))
    runner._scrub = lambda t: t.replace("human_note", "<redacted>")

    async def main():
        note(runner)
        assert await runner.tick()
    asyncio.run(main())
    (w,) = wakes(r)
    (wi,) = r.ag.wake_inputs(SID)
    assert "human_note" in llm.requests[0].prompt and "human_note" not in wi["message"]
    assert w["input"]["scrubbed"] is True and w["fingerprint"]["git_sha"] == "unknown"


def test_a_failing_scrubber_stores_no_input_and_the_wake_still_journals(store, caplog):
    r, runner, llm, _ = setup(store, turn(text("ok")))

    def boom(_t):
        raise RuntimeError("scrubber broke")
    runner._scrub = boom

    async def main():
        note(runner)
        assert await runner.tick()
    asyncio.run(main())
    (w,) = wakes(r)
    assert w["status"] == "done" and w["input"]["stored"] is False and r.ag.wake_inputs(SID) == []
