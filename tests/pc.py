"""Test helpers for "who is asking" (adapters/http/access.py): the PC is a paired browser, never a network position, and
`from_pc` fails closed without an actor.

- `as_actor(app, actor)`: an ASGI wrapper for route tests WITHOUT the access middleware; it records the actor the
  middleware would ("pc", "phone:<8>", "agent-api") so the routes see it. Test-only, outside the app.
- `paired(client, pc=None)`: give a TestClient the PC cookie, as `ops pair-pc` + /pair would. `pc` is the app's
  PcCredentials or its data folder (the runtime keeps the secret there); None: the one install_access put on the app.
"""

from __future__ import annotations

from pathlib import Path

from btc_signal_engine.adapters.pc_credentials import PC_COOKIE, PcCredentials
from btc_signal_engine.application.actor import current_actor


class AsActor:
    def __init__(self, app, actor: str) -> None:
        self.app = app
        self.actor = actor

    def __getattr__(self, name):  # TestClient(app).app.state / .routes and the like reach the real app
        return getattr(self.app, name)

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            scope["state"] = {**scope.get("state", {}), "actor": self.actor}
            token = current_actor.set(self.actor)
            try:
                await self.app(scope, receive, send)
            finally:
                current_actor.reset(token)
            return
        await self.app(scope, receive, send)


def as_actor(app, actor: str = "pc") -> AsActor:
    return AsActor(app, actor)


def as_pc(app) -> AsActor:
    return AsActor(app, "pc")


def paired(client, pc: PcCredentials | Path | str | None = None):
    """`pc` None: the credential install_access put on the app (app.state.pc_credentials)."""
    if pc is None:
        creds = client.app.state.pc_credentials
    else:
        creds = pc if isinstance(pc, PcCredentials) else PcCredentials(Path(pc))
    client.cookies.set(PC_COOKIE, creds.issue())
    return client
