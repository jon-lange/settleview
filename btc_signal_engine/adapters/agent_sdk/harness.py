"""SdkHarness: the `AgentHarness` port on the Claude Agent SDK (claude-agent-sdk 0.2.168; Agent mode, KTD6-KTD8, KTD10).

One wake = one `ClaudeSDKClient` = one Claude Code CLI process, one query, then it exits (stateless wakes, KTD6). The
CLI runs sandboxed from any Claude Code install on the machine:
- the system prompt is a file (`<data>/agent-cli/prompts/<session>.md`, rewritten only when it changes) that replaces
  Claude Code's own prompt; the wake message is sent verbatim (`verbatim_prompts`: no `@file` expansion and no slash
  commands; the agent's and the user's notes are in it);
- no built-in tools (`tools=[]` plus every built-in named in `disallowed_tools`), only the app's tools through an
  in-process MCP server ("settleview": the model sees `mcp__settleview__<name>`), pre-approved, everything else denied
  (`dontAsk`, no permission prompts); no settings, skills, CLAUDE.md, auto memory, transcripts or other MCP servers;
  an empty working directory and a private config directory per wake under the app's data directory
  (`agent-cli/config/<session>-<random>`, removed when the wake ends: up to BTCFLOW_AGENT_WAKE_CONCURRENCY CLI
  processes never share one);
- the environment, built from an allowlist (`PASS_ENV`: PATH, HOME / USERPROFILE, temp dirs, locale, the OS's own
  system variables): the app's key as ANTHROPIC_API_KEY (KTD10) and our own CLAUDE_* settings; every other inherited
  variable is blanked (the SDK forwards the parent environment and can blank but not remove), so proxies,
  NODE_OPTIONS, venue keys and any other secret never reach the CLI unless named in `pass_env`
  (BTCFLOW_AGENT_ENV_PASS); every credential source that outranks the key is blanked even when absent; an
  ANTHROPIC_BASE_URL inherited from the app's environment refuses the wake (only tests pass one, explicitly);
- at most `concurrency` CLI processes run at once (the fleet's wake cap, BTCFLOW_AGENT_WAKE_CONCURRENCY: a wake
  started outside the fleet's queue waits here too), each wake first waits a small random `jitter_s` so wakes due at
  the same window boundary do not spawn in the same instant;
- the CLI's `system/init` report is checked before anything runs: the key source must be ANTHROPIC_API_KEY, the tools
  exactly ours, and the CLI new enough for verbatim prompts; anything else aborts the wake (LlmMisconfigured).

Spend (KTD8): our caller's price table stays the cap's authority. Each turn's usage is read from the raw stream
(message_start / message_delta) and handed to `on_turn` as the turn ends; after every tool call, whatever its outcome
(PostToolUse, PostToolUseFailure, and a PreToolUse deny of a call that is not ours: every hook matches every tool),
`gate` is asked before the next model request and stops the run there (`continue: false`) when the next turn does not
fit or the session stopped. `max_budget_usd` is
only the CLI's own post-hoc backstop. Usage the CLI reports in its final total but that no streamed turn carried is
handed over as one `unstreamed` TurnUsage, so nothing it counted goes unsettled.

Tools in flight: each call runs as its own task and the handler awaits it shielded, so the CLI cancelling the handler
(an interrupt, a disconnect) never cancels it; the wake waits for every one before it returns or raises. A cancelled
wake (the user's Stop) interrupts the CLI (2 s at most), disconnects, then waits for the tools in flight.

Errors: a 401 / authentication_failed -> LlmInvalidKey; 400 / 404 / 413 / invalid_request -> LlmBadRequest; any other
API failure, a CLI crash or a run with no result -> LlmUnavailable; no CLI -> LlmMisconfigured.

Telemetry (adapters/telemetry.py): each wake is one OpenTelemetry GenAI `invoke_agent` span (model, token usage, the
CLI's cost, status) with an `execute_tool` child span per tool call (name and ok / error); never a prompt, model output,
tool argument or result. The CLI's own telemetry stays off (every OTEL_* variable is blanked for it).
"""

from __future__ import annotations

import asyncio
import collections
import logging
import os
import random
import re
import shutil
import uuid
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, ClaudeSDKError, CLINotFoundError,
                              HookMatcher, ResultError, ResultMessage, SdkMcpTool, StreamEvent, SystemMessage,
                              ToolAnnotations, ToolUseBlock, create_sdk_mcp_server)
from claude_agent_sdk import __version__ as SDK_VERSION

from btc_signal_engine.adapters import telemetry
from btc_signal_engine.application.ports import (LlmBadRequest, LlmError, LlmInvalidKey, LlmMisconfigured,
                                                 LlmUnavailable, TurnUsage, WakeResult)

log = logging.getLogger(__name__)

SERVER = "settleview"
PREFIX = f"mcp__{SERVER}__"
# Belt and braces with tools=[]: a bare name removes the built-in from the model's context.
DISALLOWED = ("Bash", "PowerShell", "Read", "Write", "Edit", "NotebookEdit", "Glob", "Grep", "WebFetch", "WebSearch",
              "Agent", "Task", "Skill", "Monitor", "TodoWrite", "ToolSearch", "AskUserQuestion")
THINKING = {"type": "adaptive", "display": "summarized"}
MIN_CLI = (2, 1, 248)  # the first CLI that honours verbatim prompts (the SDK's VERBATIM_PROMPTS_MINIMUM)
RESULT_CHARS = 200_000  # above the largest observe / get_journal result: never persisted to a file the model can't read
INTERRUPT_S = 2.0
USAGE_KEYS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")

# Credential sources that outrank ANTHROPIC_API_KEY, or replace it, blanked even when absent.
BLANK = ("ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
         "CLAUDE_CODE_USE_ANTHROPIC_AWS", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_PROFILE")
# The inherited variables the CLI keeps (it needs them to start and find its home, temp dir and locale); every other
# inherited one is blanked. Upper-case names (Windows' environment is case-insensitive); LC_* by prefix.
PASS_ENV = frozenset({"PATH", "PATHEXT", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA",
                      "TMP", "TEMP", "TMPDIR", "LANG", "LANGUAGE", "TZ", "TERM", "SYSTEMROOT", "SYSTEMDRIVE",
                      "WINDIR", "COMSPEC", "SHELL", "USER", "USERNAME", "LOGNAME", "PROGRAMDATA", "PROGRAMFILES",
                      "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS"})
PASS_PREFIXES = ("LC_",)
# Never passable, even when named in pass_env: credentials and endpoints the agent must not get from the parent.
NEVER_PASS_PREFIXES = ("ANTHROPIC_", "CLAUDE_", "BTCFLOW_")
JITTER_S = 0.25  # the most a wake waits before it spawns (spreads wakes due at one window boundary)


def env_pass(names: str | list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    """BTCFLOW_AGENT_ENV_PASS: extra inherited variables the CLI may keep (a proxy, a CA bundle), comma-separated.
    A credential / endpoint name (ANTHROPIC_*, CLAUDE_*, BTCFLOW_*) is an error, never quietly dropped."""
    items = [x.strip() for x in (names.split(",") if isinstance(names, str) else names or ()) if x and x.strip()]
    bad = [x for x in items if x.upper().startswith(NEVER_PASS_PREFIXES) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*",
                                                                                             x)]
    if bad:
        raise ValueError(f"BTCFLOW_AGENT_ENV_PASS may not name {bad[0]!r} (credentials and endpoints never pass; "
                         "names are environment variable names)")
    return tuple(x.upper() for x in items)


def child_env(api_key: str, *, config_dir: Path, max_tokens: int, client_app: str, inherited: Mapping[str, str],
              base_url: str | None = None, pass_env: tuple[str, ...] = ()) -> dict[str, str]:
    """The CLI's environment overrides (merged by the SDK over the app's environment): every inherited variable not
    on the allowlist (PASS_ENV, LC_*, `pass_env`) blanked, then our own."""
    if (inherited.get("ANTHROPIC_BASE_URL") or "").strip():
        raise LlmMisconfigured("ANTHROPIC_BASE_URL is set in the app's environment; the agent talks only to the "
                               "Anthropic API. Remove it and restart the app.")
    keep = PASS_ENV | {x.upper() for x in pass_env}
    env = {k: "" for k in BLANK}
    for k in inherited:
        u = k.upper()
        if u not in keep and not u.startswith(PASS_PREFIXES):
            env[k] = ""
    env.update({

        "ANTHROPIC_API_KEY": api_key,
        "CLAUDE_CONFIG_DIR": str(config_dir),
        "CLAUDE_CODE_ENTRYPOINT": "sdk-py",
        "CLAUDE_CODE_SDK_READS_SESSION_STATE": "1",  # the SDK's own run-end signal (set here so a blank can't hide it)
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
        "ENABLE_TOOL_SEARCH": "false",
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(int(max_tokens)),
        "CLAUDE_CODE_MAX_RETRIES": "2",
        "API_TIMEOUT_MS": "60000",
        "CLAUDE_CODE_PROMPT_CACHE_TTL": "5m",
        "CLAUDE_AGENT_SDK_CLIENT_APP": client_app,
        "CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK": "1",
    })
    if base_url:  # tests only: a local fake API
        env["ANTHROPIC_BASE_URL"] = base_url
    return env


def _ints(u: Mapping | None) -> dict[str, int]:
    u = u or {}
    return {k: int(u.get(k) or 0) for k in USAGE_KEYS}


def _version(v: Any) -> tuple[int, ...] | None:
    m = re.match(r"(\d+)\.(\d+)\.(\d+)", str(v or ""))
    return tuple(int(x) for x in m.groups()) if m else None


def _short(text: Any, n: int = 300) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= n else t[:n] + "…"


def _mapped(status: int | None, kind: str | None, text: str) -> LlmError:
    if status == 401 or kind == "authentication_failed":
        return LlmInvalidKey(f"The Anthropic API key was rejected: {text}")
    if status in (400, 404, 413) or kind == "invalid_request":
        return LlmBadRequest(f"The model rejected the request: {text}")
    return LlmUnavailable(f"The Anthropic API answered {status or kind or 'an error'}: {text}")


class SdkHarness:
    def __init__(self, api_key: str, data_dir: Path | str, *, cli_path: Path | str | None = None,
                 client_app: str = "settleview", base_url: str | None = None,
                 environ: Mapping[str, str] | None = None, concurrency: int | None = None,
                 jitter_s: float = JITTER_S, pass_env: str | list[str] | tuple[str, ...] | None = None,
                 rand: Callable[[], float] = random.random,
                 sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep) -> None:
        """`data_dir` holds `agent-cli/config` (each wake's private config directory under it), `agent-cli/prompts`
        and the empty `agent-cwd`. `cli_path` None = the SDK's bundled CLI, else `claude` on PATH. `base_url` is for
        tests (a local fake API) and never comes from settings. `environ` replaces os.environ when building the child
        env (tests). `concurrency`: the most CLI processes at once (None: unbounded; the app passes the fleet's wake
        cap). `jitter_s`: the most a wake waits at random before it spawns. `pass_env`: extra inherited variables the
        CLI keeps (BTCFLOW_AGENT_ENV_PASS)."""
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("an Anthropic API key is required")
        if concurrency is not None and (isinstance(concurrency, bool) or not isinstance(concurrency, int)
                                        or concurrency < 1):
            raise ValueError(f"the CLI concurrency must be a whole number of at least 1 (got {concurrency!r})")
        if not 0 <= float(jitter_s) <= 5:
            raise ValueError(f"the wake jitter must be 0 to 5 seconds (got {jitter_s!r})")
        self.concurrency = concurrency
        self._slots = asyncio.Semaphore(concurrency) if concurrency is not None else None
        self.jitter_s = float(jitter_s)
        self._rand, self._sleep = rand, sleep
        self.pass_env = env_pass(pass_env)
        self.running = 0  # CLI processes running now (telemetry and tests)
        self._key = api_key
        root = Path(data_dir)
        self.config_dir = root / "agent-cli" / "config"
        self.prompt_dir = root / "agent-cli" / "prompts"
        self.cwd = root / "agent-cwd"
        self.cli_path = str(cli_path) if cli_path else None
        self.client_app = client_app
        self._base_url = base_url
        self._environ = environ

    def prompt_file(self, session_id: str, text: str) -> Path:
        """The session's system prompt file, rewritten (atomically) only when its text changed, so the cached prefix
        stays byte-stable across wakes."""
        self.prompt_dir.mkdir(parents=True, exist_ok=True)
        path = self.prompt_dir / (re.sub(r"[^A-Za-z0-9_-]", "_", session_id) + ".md")
        data = text.encode("utf-8")
        if not path.exists() or path.read_bytes() != data:
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, path)
        return path

    def wake_config_dir(self, session_id: str) -> Path:
        """A fresh private config directory for one wake (never shared by two CLI processes)."""
        name = re.sub(r"[^A-Za-z0-9_-]", "_", session_id)[:40] + "-" + uuid.uuid4().hex[:12]
        path = self.config_dir / name
        path.mkdir(parents=True, exist_ok=False)
        return path

    def options(self, wake: _Wake, *, system_file: Path, model: str, effort: str, max_turns: int, max_tokens: int,
                max_budget_usd: float, config_dir: Path | None = None) -> ClaudeAgentOptions:
        self.cwd.mkdir(parents=True, exist_ok=True)
        if config_dir is None:
            self.config_dir.mkdir(parents=True, exist_ok=True)
        env = child_env(self._key, config_dir=config_dir or self.config_dir, max_tokens=max_tokens,
                        client_app=self.client_app, inherited=os.environ if self._environ is None else self._environ,
                        base_url=self._base_url, pass_env=self.pass_env)
        return ClaudeAgentOptions(
            cli_path=self.cli_path,
            model=model,
            system_prompt={"type": "file", "path": str(system_file)},
            tools=[],
            disallowed_tools=list(DISALLOWED),
            allowed_tools=[PREFIX + "*"],
            permission_mode="dontAsk",
            # never --bare: it silently turns SDK hooks off (the spend gate)
            extra_args={"no-session-persistence": None, "permission-prompts": "none"},
            setting_sources=[],
            strict_mcp_config=True,
            skills=[],
            mcp_servers={SERVER: create_sdk_mcp_server(SERVER, tools=wake.sdk_tools())},
            cwd=str(self.cwd),
            verbatim_prompts=True,
            max_turns=max_turns,
            max_budget_usd=max(0.0, max_budget_usd),
            thinking=THINKING,
            effort=effort,
            include_partial_messages=True,
            # every hook matches every tool (matcher None): a call that is not ours is denied in PreToolUse and
            # still passes the gate; a failed call passes it in PostToolUseFailure
            hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[wake.pre_tool])],
                   "PostToolUse": [HookMatcher(matcher=None, hooks=[wake.post_tool])],
                   "PostToolUseFailure": [HookMatcher(matcher=None, hooks=[wake.post_tool_failure])]},
            stderr=wake.stderr,
            env=env,
        )

    async def run_wake(self, system_prompt: str, wake_prompt: str, tool_specs: list[dict],
                       execute: Callable[[str, dict], Awaitable[tuple[str, bool]]], *, session_id: str, model: str,
                       effort: str, max_turns: int, max_tokens: int, max_budget_usd: float,
                       on_delta: Callable[[str, str], None] | None = None,
                       on_turn: Callable[[TurnUsage], None] | None = None,
                       gate: Callable[[int, int], str | None] | None = None) -> WakeResult:
        if self.jitter_s > 0:
            await self._sleep(self.jitter_s * self._rand())
        if self._slots is None:
            return await self._run_wake(system_prompt, wake_prompt, tool_specs, execute, session_id=session_id,
                                        model=model, effort=effort, max_turns=max_turns, max_tokens=max_tokens,
                                        max_budget_usd=max_budget_usd, on_delta=on_delta, on_turn=on_turn, gate=gate)
        async with self._slots:
            return await self._run_wake(system_prompt, wake_prompt, tool_specs, execute, session_id=session_id,
                                        model=model, effort=effort, max_turns=max_turns, max_tokens=max_tokens,
                                        max_budget_usd=max_budget_usd, on_delta=on_delta, on_turn=on_turn, gate=gate)

    async def _run_wake(self, system_prompt: str, wake_prompt: str, tool_specs: list[dict],
                        execute: Callable[[str, dict], Awaitable[tuple[str, bool]]], *, session_id: str, model: str,
                        effort: str, max_turns: int, max_tokens: int, max_budget_usd: float,
                        on_delta: Callable[[str, str], None] | None,
                        on_turn: Callable[[TurnUsage], None] | None,
                        gate: Callable[[int, int], str | None] | None) -> WakeResult:
        span = telemetry.WakeSpan(model=model, session_id=session_id, max_tokens=max_tokens, max_turns=max_turns,
                                  effort=effort)
        wake = _Wake(tool_specs, execute, on_delta=on_delta, on_turn=on_turn, gate=gate, span=span)
        config_dir: Path | None = None
        self.running += 1
        try:
            try:
                config_dir = self.wake_config_dir(session_id)
                opts = self.options(wake, system_file=self.prompt_file(session_id, system_prompt), model=model,
                                    effort=effort, max_turns=max_turns, max_tokens=max_tokens,
                                    max_budget_usd=max_budget_usd, config_dir=config_dir)
                result = await wake.run(opts, wake_prompt)
            except (LlmError, asyncio.CancelledError):
                raise
            except CLINotFoundError as e:
                raise LlmMisconfigured(f"the Claude Code CLI was not found ({_short(e, 200)}); set "
                                       f"BTCFLOW_AGENT_CLI_PATH") from None
            except ResultError as e:
                raise _mapped(e.api_error_status, None, _short(e.result or "; ".join(e.errors) or e)) from None
            except ClaudeSDKError as e:
                raise LlmUnavailable(f"The agent runtime failed: {_short(e)}") from None
            except OSError as e:
                raise LlmUnavailable(f"The agent runtime could not start: {_short(e)}") from None
            finally:
                await wake.drain()
                self.running -= 1
                if config_dir is not None:
                    shutil.rmtree(config_dir, ignore_errors=True)
        except BaseException as e:
            span.fail(e)
            raise
        span.finish(status=result.status, usage=result.usage, cost_usd=result.cost_usd, turns=result.turns,
                    stop_reason=result.stop_reason, models=result.detail.get("models") or (),
                    tool_calls=len(result.tool_calls), detail=result.detail)
        return result


class _Wake:
    """One wake's state: its tools, the turn being streamed, the gate's verdict, the tool calls in flight."""

    def __init__(self, tool_specs: list[dict], execute: Callable[[str, dict], Awaitable[tuple[str, bool]]], *,
                 on_delta: Callable[[str, str], None] | None, on_turn: Callable[[TurnUsage], None] | None,
                 gate: Callable[[int, int], str | None] | None, span: telemetry.WakeSpan | None = None) -> None:
        self.specs = list(tool_specs)
        self.span = span  # the wake's telemetry span: each tool call is a child span
        self.expected = {PREFIX + t["name"] for t in self.specs}
        self._execute = execute
        self._on_delta = on_delta
        self._on_turn = on_turn
        self._gate = gate
        self.verified = False  # the init report passed its checks
        self.inflight: list[asyncio.Future] = []
        self.turns: list[TurnUsage] = []
        self.cur: dict | None = None
        self.context_tokens = 0
        self.new_chars = 0
        self.stopped: str | None = None  # the gate's reason
        self.hooks = {"pre": 0, "post": 0, "failure": 0, "denied": 0}
        self.tool_calls: list[dict] = []
        self.api_error: str | None = None
        self.cli_version: str | None = None
        self.stderr_tail: collections.deque[str] = collections.deque(maxlen=20)

    # -- the tools ----------------------------------------------------------------------------------------------------
    def sdk_tools(self) -> list[SdkMcpTool]:
        out = []
        for t in self.specs:
            read = t.get("kind") == "read"
            out.append(SdkMcpTool(t["name"], t["description"], t["input_schema"], self._handler(t["name"]),
                                  ToolAnnotations(readOnlyHint=read, destructiveHint=not read,
                                                  maxResultSizeChars=RESULT_CHARS)))
        return out

    def _handler(self, name: str) -> Callable[[dict], Awaitable[dict]]:
        async def handler(args: dict) -> dict:
            if not self.verified:
                return {"content": [{"type": "text", "text": '{"ok":false,"code":"not_ready"}'}], "is_error": True}
            if self.span is None:
                text, is_error = await self._call(name, args)
            else:
                with self.span.tool(name) as ts:
                    text, is_error = await self._call(name, args)
                    ts.result(bool(is_error))
            self.new_chars += len(text)
            return {"content": [{"type": "text", "text": text}], "is_error": bool(is_error)}
        return handler

    async def _call(self, name: str, args: dict | None) -> tuple[str, bool]:
        task = asyncio.ensure_future(self._execute(name, dict(args or {})))  # inherits the tool span's context
        self.inflight.append(task)
        return await asyncio.shield(task)  # the CLI cancelling the handler never cancels the call

    async def drain(self) -> None:
        """Wait for every tool call started (an order in flight finishes and is recorded once)."""
        while self.inflight:
            pending, self.inflight = self.inflight, []
            for r in await asyncio.gather(*pending, return_exceptions=True):
                if isinstance(r, BaseException):
                    log.error("agent tool call failed", exc_info=r)

    # -- hooks --------------------------------------------------------------------------------------------------------
    async def pre_tool(self, inp: dict, tool_use_id: str | None, ctx: Any) -> dict:
        self.hooks["pre"] += 1
        if not self.verified or inp.get("tool_name") not in self.expected:
            # denied: no PostToolUse follows, so the gate is asked here (the next model request is still a turn)
            self.hooks["denied"] += 1
            out: dict = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                "permissionDecisionReason": "not one of this app's tools"}}
            return {**out, **self._gated()}
        return {}

    async def post_tool(self, inp: dict, tool_use_id: str | None, ctx: Any) -> dict:
        """After a tool call, before the next model request: the gate decides whether there is one."""
        self.hooks["post"] += 1
        return self._gated()

    async def post_tool_failure(self, inp: dict, tool_use_id: str | None, ctx: Any) -> dict:
        """A tool call that failed (an error result, a refused input, an interrupt): the same gate."""
        self.hooks["failure"] += 1
        return self._gated()

    def _gated(self) -> dict:
        if self.stopped is None and self._gate is not None:
            self.stopped = self._gate(self.context_tokens, self.new_chars)
        if self.stopped is not None:
            return {"continue_": False, "stopReason": self.stopped}
        return {}


    def stderr(self, line: str) -> None:
        self.stderr_tail.append(line)
        log.debug("agent cli: %s", line)

    # -- the stream ---------------------------------------------------------------------------------------------------
    def _delta(self, kind: str, text: str) -> None:
        if self._on_delta is not None:
            self._on_delta(kind, text)

    def _stream(self, e: dict) -> None:
        t = e.get("type")
        if t == "message_start":
            m = e.get("message") or {}
            self.cur = {"id": m.get("id"), "usage": _ints(m.get("usage")), "thinking": [], "text": [], "stop": None}
            self.new_chars = 0
            return
        cur = self.cur
        if cur is None:
            return
        if t == "content_block_start":
            b = e.get("content_block") or {}
            if b.get("type") == "tool_use":
                name = str(b.get("name") or "")
                self._delta("tool_use", name[len(PREFIX):] if name.startswith(PREFIX) else name)
        elif t == "content_block_delta":
            d = e.get("delta") or {}
            if d.get("type") == "thinking_delta":
                cur["thinking"].append(d.get("thinking") or "")
                self._delta("thinking", d.get("thinking") or "")
            elif d.get("type") == "text_delta":
                cur["text"].append(d.get("text") or "")
                self._delta("text", d.get("text") or "")
        elif t == "message_delta":
            for k, v in (e.get("usage") or {}).items():
                if k in USAGE_KEYS and v is not None:
                    cur["usage"][k] = int(v)
            cur["stop"] = (e.get("delta") or {}).get("stop_reason") or cur["stop"]
        elif t == "message_stop":
            self._turn(cur)
            self.cur = None

    def _turn(self, cur: dict) -> None:
        u = cur["usage"]
        turn = TurnUsage(usage=u, stop_reason=cur["stop"], thinking="".join(cur["thinking"]),
                         text="".join(cur["text"]), message_id=cur["id"])
        self.turns.append(turn)
        self.context_tokens = sum(u.values())  # the next turn re-reads all of it, plus this turn's output
        if self._on_turn is not None:
            self._on_turn(turn)

    def _check_init(self, d: dict) -> None:
        src = d.get("apiKeySource")
        if src != "ANTHROPIC_API_KEY":
            raise LlmMisconfigured(f"the runtime's credential source is {src!r}, not the app's API key")
        tools = set(d.get("tools") or [])
        if tools != self.expected:
            odd = sorted(tools ^ self.expected)
            raise LlmMisconfigured(f"the runtime's tools differ from the app's: {', '.join(odd[:8])}")
        self.cli_version = str(d.get("claude_code_version") or "")
        v = _version(self.cli_version)
        if v is None or v < MIN_CLI:
            raise LlmMisconfigured(f"Claude Code {self.cli_version or '(unknown version)'} is older than "
                                   f"{'.'.join(map(str, MIN_CLI))} (verbatim prompts)")
        self.verified = True

    # -- one run ------------------------------------------------------------------------------------------------------
    async def run(self, opts: ClaudeAgentOptions, prompt: str) -> WakeResult:
        result: ResultMessage | None = None
        async with ClaudeSDKClient(options=opts) as client:
            try:
                await client.query(prompt)
                async for m in client.receive_response():
                    if isinstance(m, SystemMessage):
                        if m.subtype == "init":
                            self._check_init(m.data)
                    elif not self.verified and not isinstance(m, ResultMessage):
                        raise LlmMisconfigured("the runtime started without reporting its set-up")
                    elif isinstance(m, StreamEvent):
                        if m.parent_tool_use_id is None:
                            self._stream(m.event)
                    elif isinstance(m, AssistantMessage):
                        if m.error:
                            self.api_error = m.error
                        for b in m.content:
                            if isinstance(b, ToolUseBlock):
                                name = b.name[len(PREFIX):] if b.name.startswith(PREFIX) else b.name
                                self.tool_calls.append({"id": b.id, "name": name, "input": dict(b.input or {})})
                    elif isinstance(m, ResultMessage):
                        result = m
            except asyncio.CancelledError:
                try:
                    await asyncio.wait_for(client.interrupt(), INTERRUPT_S)
                except Exception:
                    log.warning("agent runtime interrupt failed; disconnecting", exc_info=True)
                raise
        if result is None:
            raise LlmUnavailable("The agent runtime ended without a result."
                                 + (f" {_short(self.stderr_tail[-1], 200)}" if self.stderr_tail else ""))
        return self._result(result)

    def _result(self, r: ResultMessage) -> WakeResult:
        models = sorted((r.model_usage or {}).keys())
        detail = {"runtime": f"claude-agent-sdk {SDK_VERSION}", "cli_version": self.cli_version,
                  "subtype": r.subtype, "terminal_reason": r.terminal_reason, "num_turns": r.num_turns,
                  "api_error_status": r.api_error_status, "duration_ms": r.duration_ms,
                  "duration_api_ms": r.duration_api_ms, "models": models,
                  "permission_denials": len(r.permission_denials or []), "hooks": dict(self.hooks),
                  "errors": [_short(x, 200) for x in (r.errors or [])[:3]]}
        if self.stopped is not None:
            status = self.stopped
        elif r.subtype == "error_max_turns":
            status = "turn_cap"
        elif r.subtype == "error_max_budget_usd":
            status = "spend_cap"
        elif self.turns and self.turns[-1].stop_reason == "refusal":
            status = "done"  # the model declined; the caller reads the refusal from the last turn
        elif self.api_error or r.is_error:
            if r.terminal_reason in ("aborted_streaming", "aborted_tools") and not r.api_error_status:
                raise LlmUnavailable(f"The agent runtime aborted the wake ({r.terminal_reason}).")
            raise _mapped(r.api_error_status, self.api_error, _short(r.result or "; ".join(r.errors or []) or
                                                                      r.subtype))
        else:
            status = "done"
        self._reconcile(r.usage)
        last = self.turns[-1] if self.turns else None
        return WakeResult(status=status, stop_reason=last.stop_reason if last else None,
                          text=(last.text.strip() or None) if last else None,
                          thinking=tuple(t.thinking.strip() for t in self.turns if t.thinking.strip()),
                          tool_calls=tuple(self.tool_calls), usage=_ints(r.usage) if r.usage else None,
                          cost_usd=r.total_cost_usd, turns=len(self.turns), detail=detail)

    def _reconcile(self, usage: Mapping | None) -> None:
        """Usage in the runtime's total that no streamed turn carried is settled too (never less than it counted)."""
        if not usage:
            return
        total = _ints(usage)
        seen = {k: sum(t.usage.get(k, 0) for t in self.turns) for k in USAGE_KEYS}
        extra = {k: max(0, total[k] - seen[k]) for k in USAGE_KEYS}
        if any(extra.values()) and self._on_turn is not None:
            log.warning("agent runtime reported usage no streamed turn carried: %s", extra)
            self._on_turn(TurnUsage(usage=extra, unstreamed=True))
