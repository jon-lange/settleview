"""Ports: the only way application use cases reach the outside world (KTD20).

Adapters implement these protocols; `bootstrap` wires them in. Tests use the
in-memory fakes under `tests/fakes/`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from btc_signal_engine.domain.models.events import BookSecond, IndexSecond, MarketEvent, Trade


class Clock(Protocol):
    def now_ms(self) -> int: ...


class MarketFeed(Protocol):
    """Live, normalized market events: trades as they happen; BookSecond and the settlement
    IndexSecond rows once per completed second (the engine consumes Trade and BookSecond)."""

    def events(self) -> AsyncIterator[MarketEvent | IndexSecond]: ...


class TradeHistory(Protocol):
    """Historical trades by ID, newest first, for backfill and gap repair."""

    async def trades_before(self, trade_id: int | None, limit: int = 1000) -> list[Trade]: ...


class KalshiGateway(Protocol):
    async def markets(self, *, status: str, cursor: str | None = None, limit: int = 200) -> tuple[list[dict], str | None]: ...

    async def market(self, ticker: str) -> dict: ...

    async def orderbook_quote(self, ticker: str) -> dict:
        """{ticker, yes_bid, yes_ask, no_bid, no_ask} from the live order book (not the lagging /markets list)."""
        ...


class MarketStore(Protocol):
    """Raw market history (trades, per-second aggregates, the settlement index)."""

    def add_trades(self, trades: Iterable[Trade]) -> None: ...

    def add_book_seconds(self, rows: Iterable[BookSecond]) -> None: ...

    def add_index_seconds(self, rows: Iterable[IndexSecond]) -> None: ...

    def index_range(self, start: int, end: int) -> list[IndexSecond]: ...

    def last_index_before(self, ts: int) -> IndexSecond | None: ...

    def trades_range(self, start: int, end: int) -> list[Trade]: ...

    def book_range(self, start: int, end: int) -> list[BookSecond]: ...

    def max_trade_id(self) -> int | None: ...

    def min_trade_id(self) -> int | None: ...


class Repository(Protocol):
    """Generic row store for engine records (snapshots, decisions, picks, outcomes, ...).

    Kept deliberately narrow: application code passes plain dicts keyed by column name
    and the SQLite adapter maps them to tables.
    """

    def insert(self, table: str, rows: Sequence[dict[str, Any]], *, ignore_conflicts: bool = True) -> list[int]: ...

    def update(self, table: str, key: dict[str, Any], values: dict[str, Any]) -> int: ...

    def select(
        self,
        table: str,
        where: dict[str, Any] | None = None,
        *,
        order_by: Sequence[str] = (),
        limit: int | None = None,
        filters: Sequence[tuple[str, str, Any]] = (),
    ) -> list[dict[str, Any]]: ...


class TradingStream(Protocol):
    """A live push feed for one trading environment (adapters/kalshi/stream.py; shadow.ShadowStream).

    It delivers `order`, `fill`, `quote`, `health` and `resync` events to the callback it was built with; the
    application only steers which markets it watches and reads its health. REST polling stays as the backup."""

    env: str

    def subscribe_tickers(self, tickers: Iterable[str]) -> None: ...

    def health(self) -> dict:
        """{"connected": bool, "last_msg_ms": int | None, "stale": bool}"""
        ...


# -- the agent's model (Agent mode U6, KTD7) ----------------------------------------------------------------------------
class LlmError(Exception):
    """A model call or wake that could not run. `message` is shown to the user (never contains the key)."""


class LlmInvalidKey(LlmError):
    """The API key was rejected (401): the session ends."""


class LlmUnavailable(LlmError):
    """Overloaded, rate-limited, a server error, a timeout, no connection or the agent runtime failing, after the
    runtime's own retries: the session pauses."""


class LlmBadRequest(LlmError):
    """The request itself was rejected (a programming error); the message says why."""


class LlmMisconfigured(LlmError):
    """The agent runtime is not set up safely (a credential source other than the app's key, tools other than the
    app's, an inherited API base URL, a missing or too old runtime): the wake is aborted and the session pauses."""


class ApiKeyCheck(Protocol):
    """The launch-time key check (R16)."""

    async def check_key(self) -> None:
        """A cheap call proving the key works; raises LlmInvalidKey or LlmUnavailable."""
        ...


@dataclass(frozen=True)
class TurnUsage:
    """One model turn as it finished: `usage` {input_tokens, output_tokens, cache_creation_input_tokens,
    cache_read_input_tokens}, its stop_reason (end_turn, tool_use, max_tokens, refusal, ...), its summarized thinking
    and text, the API's message id. `unstreamed` marks usage the runtime reported only in its final total (no turn was
    seen for it): it is settled like a turn but is not counted as one."""

    usage: dict[str, int]
    stop_reason: str | None = None
    thinking: str = ""
    text: str = ""
    message_id: str | None = None
    unstreamed: bool = False


@dataclass(frozen=True)
class WakeResult:
    """How a wake's model loop ended.

    `status`: "done" (the model ended its turn), "turn_cap" (max_turns reached), or the reason the gate returned when it
    stopped the wake before the next turn (e.g. "spend_cap", "session_stopped"), or "spend_cap" when the runtime's own
    budget backstop stopped it. `text` = the last turn's text, `thinking` = each turn's summarized thinking,
    `tool_calls` = every tool call the model made ({id, name, input}, in order; a call the runtime refused before it
    reached the executor is here but was never executed), `usage` = the runtime's cumulative usage, `cost_usd` = the
    runtime's own cost estimate (journalled next to ours, never used for the cap), `detail` = runtime facts for the
    journal (no secrets)."""

    status: str
    stop_reason: str | None = None
    text: str | None = None
    thinking: tuple[str, ...] = ()
    tool_calls: tuple[dict, ...] = ()
    usage: dict[str, int] | None = None
    cost_usd: float | None = None
    turns: int = 0
    detail: dict[str, Any] | None = None


class AgentHarness(Protocol):
    """The agent's model loop for one stateless wake (KTD6): one process / connection per wake, nothing carried over.

    - `system_prompt` (the system prompt, the user's prompt, the tool note) replaces the runtime's own prompt;
      `wake_prompt` is the one user message, sent verbatim (no file or command expansion).
    - `tool_specs` [{name, description, input_schema, kind}] are the only tools offered; `execute(name, args)` ->
      (result text, is_error) runs one. The harness never cancels an `execute` it started: a wake that is cancelled or
      fails waits for it to finish before it returns or raises.
    - `on_delta(kind, text)` gets each streamed piece: "thinking", "text", "tool_use" (the tool's name, when its block
      starts, before it runs). `on_turn(TurnUsage)` gets each finished turn.
    - `gate(context_tokens, new_chars)` runs after each tool call, before the next model turn: `context_tokens` = the
      last turn's input and output tokens, `new_chars` = the tool-result characters added since; it returns None to go
      on or a reason to stop the wake before the next model request (the wake's status is then that reason).
    - `max_budget_usd` is the runtime's own post-hoc backstop, never the cap; `max_tokens` bounds each turn's output.
    Raises only `LlmError` subclasses (and CancelledError)."""

    async def run_wake(self, system_prompt: str, wake_prompt: str, tool_specs: list[dict],
                       execute: Callable[[str, dict], Awaitable[tuple[str, bool]]], *, session_id: str, model: str,
                       effort: str, max_turns: int, max_tokens: int, max_budget_usd: float,
                       on_delta: Callable[[str, str], None] | None = None,
                       on_turn: Callable[[TurnUsage], None] | None = None,
                       gate: Callable[[int, int], str | None] | None = None) -> WakeResult: ...


class RecorderData(Protocol):
    """The research recorders' output, read-only (adapters/sqlite/research.py). Both raise `FileNotFoundError` when
    the research database does not exist (said plainly, never treated as "fresh")."""

    def newest_ms(self) -> dict[str, int | None]:
        """{recorder name: the newest row's receive / fetch time in epoch ms, None when it has written none}."""
        ...

    def coverage(self, start_ms: int, end_ms: int, span_ms: int) -> dict:
        """Over the windows of `span_ms` starting in [start_ms, end_ms): {windows, recorded (with at least one quote
        inside the window), settled (a pm_windows row), missing_starts [window starts without a quote]}."""
        ...
