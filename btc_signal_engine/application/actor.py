"""Who is acting (R13, KTD5): "pc", "phone:<first 8 characters of the session nonce>", "agent:<first 8 characters of
the agent session id>" or "system".

The HTTP access middleware sets it for each request it lets through; trading audit rows read it. A context variable
keeps the adapter -> application import direction and spares every service method an `actor` argument. asyncio tasks
and FastAPI's threadpool (sync routes) run with a copy of the request's context, so the value follows the request.
Anything outside a request (the monitor tick, settlement, the supervisor's kill-switch trip) gets the default.

Agent mode: an agent actor is never read from request input. `as_agent` sets it, together with the full session id
the order gate looks the session up by, only in code that has already established the session (the in-app runner,
and the agent tool route after checking the session's token).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

SYSTEM = "system"
AGENT_PREFIX = "agent:"

current_actor: ContextVar[str] = ContextVar("actor", default=SYSTEM)
current_agent_session: ContextVar[str | None] = ContextVar("agent_session", default=None)


def actor() -> str:
    return current_actor.get()


def agent_session() -> str | None:
    """The full id of the agent session acting now, or None when a person or the system acts."""
    return current_agent_session.get()


@contextmanager
def as_agent(session_id: str) -> Iterator[None]:
    """Run the enclosed calls as the agent of `session_id` (actor "agent:<8>")."""
    a = current_actor.set(AGENT_PREFIX + session_id[:8])
    s = current_agent_session.set(session_id)
    try:
        yield
    finally:
        current_agent_session.reset(s)
        current_actor.reset(a)
