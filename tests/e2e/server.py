"""The app the Agent panel's browser tests drive: an in-process uvicorn server on a free 127.0.0.1 port.

Built like tests/agent/test_routes.py's World (and the U9 preview): a real AgentSessions / AgentTools / TradingService on
the in-memory shadow exchange (no network), the real AgentRunner over a scripted FakeHarness (no SDK, no model, no
key), a fresh temp SQLite, and the real routes the page calls (agent, trading, play, coach; perps switched off, as in
an app without BTCFLOW_PERPS_ENABLED), the real web/ files and the real access middleware with its CSP. Every part
reads one shared test clock, so wakes can be spaced past the events engine's gaps.

The runner's loop is not started (`start_task` only records it): a test runs `tick()` on the server's loop with
`server.tick()`, so each wake happens exactly when the test says.

Phone clients: the access middleware decides "the PC's place or a phone" from the TCP peer address (no PC listener
here), and every browser connection comes from 127.0.0.1; the PC is that place plus the paired browser's cookie.
`PeerShim` (test-only, outside the app) rewrites the peer address of a request that carries `X-E2E-Peer: <ip>`, which
is how a phone on the LAN reaches the real app; the app itself is unchanged."""

from __future__ import annotations

import asyncio
import itertools
import socket
import threading
from pathlib import Path
from typing import Any
from collections.abc import Callable

import uvicorn
from fastapi import FastAPI

from btc_signal_engine.adapters.http.access import AccessGuard, PinStore, install_access
from btc_signal_engine.adapters.http.agent_feed import AgUiFeed, FleetFeeds, ObservedTools
from btc_signal_engine.adapters.http.agent_routes import register_agent_routes, wake_hook, wake_start_hook
from btc_signal_engine.adapters.http.app import WEB_DIR, RevalidatingStaticFiles
from btc_signal_engine.adapters.http.coach_routes import register_coach_routes
from btc_signal_engine.adapters.http.copilot_routes import register_copilot_routes
from btc_signal_engine.adapters.http.perp_routes import register_perp_routes
from btc_signal_engine.adapters.http.play_routes import register_play_routes
from btc_signal_engine.adapters.http.push import FeedHub, PushHub, TickStreams
from btc_signal_engine.adapters.http.record_routes import register_record_routes
from btc_signal_engine.adapters.http.trading_routes import register_trading_routes
from btc_signal_engine.application.agent_prompts import AgentPrompts
from btc_signal_engine.application.agent_proposals import AgentProposals
from btc_signal_engine.application.agent_fleet import AgentFleet
from btc_signal_engine.application.agent_runner import AgentRunner
from btc_signal_engine.application.agent_sessions import BRACKETS, position_view
from btc_signal_engine.application.coach import CoachService
from btc_signal_engine.application.copilot import Copilot
from btc_signal_engine.application.your_record import YourRecord
from btc_signal_engine.application.perp_view import PerpView
from btc_signal_engine.application.play_limits import PlayLimits
from tests.agent.fake_harness import FakeHarness
from tests.e2e.stubs import Stubs
from tests.agent.test_runner import Clock, _yield
from tests.agent.test_tools import SID, Rig
from tests.trading.helpers import NOW

REPO = Path(__file__).resolve().parents[2]


def trimmed(n: int) -> dict:
    return {"type": "CUSTOM", "name": "feed_trimmed", "value": {"dropped": n}}

DEFAULT_PROMPT = (REPO / "docs" / "agent" / "default-system-prompt.md").read_text(encoding="utf-8")
PEER_HEADER = b"x-e2e-peer"
PIN = "482916"


class ShadowMarket:
    """A trading service stand-in for a second market (mode, kill switch, presence): enough for an agent session to
    launch on it and show; it never trades in these tests."""
    mode = "shadow"

    def killed(self) -> bool:
        return False

    def present(self, now: int) -> bool:
        return False


class PeerShim:
    """Test-only ASGI wrapper: a request with `X-E2E-Peer: <ip>` arrives from that address (see the module doc)."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            peer = dict(scope.get("headers") or []).get(PEER_HEADER)
            if peer:
                scope = {**scope, "client": (peer.decode("ascii"), 50_000)}
        await self.app(scope, receive, send)


class AgentApp:
    """The app (see the module doc). Built on the server thread: Rig's setup runs its own asyncio.run, which must not
    happen on the test thread while the browser's sync Playwright loop is running there."""

    def __init__(self, store, port: int, script: list) -> None:
        self.store = store
        self.clock = clock = Clock(NOW)
        now = clock.now_ms
        self.r = r = Rig(store)
        ids = itertools.count(1)  # the first session is Rig's SID; later ones (a fleet) get their own ids
        r.ag._id = lambda: (lambda n: SID if n == 1 else f"sess{n:04d}e2e")(next(ids))
        self.prompts = AgentPrompts(store, DEFAULT_PROMPT)
        self.prompts.seed(now())
        self.hub = FeedHub(trimmed=trimmed)
        # as the app (bootstrap): the all-sessions feed and one feed per session whose stream is open
        self.feed = FleetFeeds(AgUiFeed(self.hub, self.state, decisions_fn=r.tools.decisions, now_fn=now),
                               lambda sid: AgUiFeed(FeedHub(trimmed=trimmed), lambda: self.session_state(sid),
                                                    decisions_fn=r.tools.decisions, now_fn=now),
                               live_fn=lambda sid: r.ag.get(sid) is not None)
        r.ag._notify = self.feed.session_changed
        self.llm = FakeHarness(*script)
        self.proposals = AgentProposals(r.ag, r.play, notify_fn=self.feed.custom)
        self.runner = self.make_runner()
        # the fleet hands the first in-app launch this runner (the tests drive it with `tick`); later launches get
        # their own (never ticked here: the second agent of a test only shows)
        made = [self.runner]
        self.fleet = AgentFleet(r.ag, r.play, lambda: made.pop() if made else self.make_runner(), clock=clock,
                                tools=r.tools)
        r.tools._events_fn = self.fleet.events
        self.started: list[str] = []
        self.limits = PlayLimits(store)
        app = self.app = FastAPI()
        register_coach_routes(app, CoachService(store, store, (60, 300)), now)
        register_trading_routes(app, {"you": r.svc}, now)
        register_perp_routes(app, PerpView(None, settings={"band": 0.01, "amber": 0.2, "red": 0.1}), PushHub(), now)
        self.play_streams = TickStreams()  # Play's live view (GET /api/play/stream), closed with the server
        register_play_routes(app, r.play, now, limits=self.limits, streams=self.play_streams)
        self.copilot = Copilot(r.play, r.ag.rule_book, YourRecord(store, {"you": r.svc.modes}))  # Ask Settleview
        register_copilot_routes(app, self.copilot, now)
        register_record_routes(app, store, now)  # the Record tab (the daily summary's numbers)
        register_agent_routes(app, sessions=r.ag, tools=r.tools, prompts=self.prompts, feed=self.feed, now_fn=now,
                              llm=self.llm, fleet=self.fleet,
                              start_task=lambda name, factory: self.started.append(name), limits=self.limits,
                              daily_fn=lambda _now: {"per_day": self.limits.current().per_day, "used": {}},
                              proposals=self.proposals, session_state_fn=self.session_state)
        # No PC listener here (a native layout): a loopback browser is the PC once it holds the PC cookie, which the
        # fixtures give each PC context (tests/e2e/conftest.py `pair_context`); test_pairing.py pairs for real.
        self.pc = install_access(app, AccessGuard(PinStore(store), session_hours=12), api_host="127.0.0.1",
                                 port=port, lan_ip_fn=lambda: None)
        self.stubs = Stubs(now)  # presets / Telegram routes by their contract, while the app has none (stubs.py)
        self.stubs.install(app)
        app.mount("/", RevalidatingStaticFiles(directory=WEB_DIR, html=True), name="web")

    def add_polymarket(self) -> None:
        """A Polymarket 15m market in shadow (test-only: this app has one real trading service, Kalshi's), so a
        second agent can run beside the Kalshi one."""
        self.r.ag.services["you_poly"] = ShadowMarket()

    def run_agent(self, **kw) -> dict:
        """Start an agent straight on the registry (no runner: it only shows): {market, name?, rules?, ...}."""
        spec = {"kind": "in_app", "market": "kalshi_15m", "budget": 20.0, "per_trade": 1.0,
                "ends_ts": self.clock.now_ms() + 3_600_000, "spend_cap": 5.0, "model": "claude-haiku-5-5",
                "level": "auto", **kw}
        out = self.r.ag.launch(spec, self.clock.now_ms())
        assert out["ok"], out
        return out

    def make_runner(self) -> AgentRunner:
        box: dict = {}
        box["r"] = AgentRunner(self.r.ag, ObservedTools(self.r.tools, self.feed), self.r.play, self.llm,
                               prompts_fn=self.prompts.prompts_fn(lambda: self.r.ag.get(box["r"].session_id)),
                               clock=self.clock, notify_fn=wake_hook(self.r.ag, self.feed), live_fn=self.feed.live,
                               start_fn=wake_start_hook(self.r.ag, self.feed), sleep=_yield,
                               proposals=self.proposals)
        return box["r"]

    def session_state(self, sid: str) -> dict:
        """One session's panel state (its own stream and status), as the real app builds it (bootstrap
        `_agent_session_state`)."""
        now = self.clock.now_ms()
        positions = [position_view(b) for b in self.store.select(BRACKETS, {"session_id": sid}, order_by=["created_ts"])]
        return {"session": self.r.ag.view(sid, now), "positions": positions, "prompts": self.prompts.versions(),
                "wakes": self.fleet.metrics(sid, now)}

    def state(self) -> dict:
        """The all-sessions feed's state, as the real app builds it (bootstrap `_agent_state`)."""
        view = self.r.ag.latest_view(self.clock.now_ms())
        positions = []
        if view is not None:
            positions = [position_view(b)
                         for b in self.store.select(BRACKETS, {"session_id": view["id"]}, order_by=["created_ts"])]
        return {"session": view, "positions": positions, "prompts": self.prompts.versions()}


class LiveServer:
    """uvicorn serving an AgentApp on its own thread and event loop; `run` / `call` reach into that loop."""

    def __init__(self, store, script: list) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self._store, self._script = store, script
        self.world: AgentApp | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self._server: uvicorn.Server | None = None
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._main, name="e2e-uvicorn", daemon=True)

    def _main(self) -> None:
        try:
            self.world = AgentApp(self._store, self.port, self._script)
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            config = uvicorn.Config(PeerShim(self.world.app), log_level="warning", lifespan="off",
                                    timeout_graceful_shutdown=1)
            self._server = uvicorn.Server(config)
            self.loop.create_task(self._serve())
        except BaseException as e:  # reported on the test thread
            self._error = e
            self._ready.set()
            return
        self.loop.run_forever()

    async def _serve(self) -> None:
        task = asyncio.ensure_future(self._server.serve(sockets=[self.sock]))
        while not self._server.started and not task.done():
            await asyncio.sleep(0.01)
        self._ready.set()
        try:
            await task
        finally:
            self.loop.stop()

    def start(self) -> LiveServer:
        self._thread.start()
        if not self._ready.wait(20):
            raise RuntimeError("the e2e server did not start within 20 s")
        if self._error is not None:
            raise self._error
        return self

    def stop(self) -> None:
        if self._server is not None and self.loop is not None and self.loop.is_running():
            async def close():
                await self.world.feed.close()
                await self.world.play_streams.close()
                self._server.should_exit = True
            asyncio.run_coroutine_threadsafe(close(), self.loop).result(5)
        self._thread.join(10)
        self.sock.close()

    # -- reaching into the server's loop ------------------------------------------------------------------------------
    def run(self, coro_fn: Callable[[], Any], timeout: float = 10.0) -> Any:
        """Await `coro_fn()` on the server's loop; return its result."""
        return asyncio.run_coroutine_threadsafe(coro_fn(), self.loop).result(timeout)

    def call(self, fn: Callable[[], Any], timeout: float = 10.0) -> Any:
        """Run `fn()` on the server's loop (where the app's state lives); return its result."""
        async def go():
            return fn()
        return self.run(go, timeout)

    def tick(self, advance_ms: int = 10_000, timeout: float = 10.0) -> bool:
        """Move the shared clock on (past the events engine's gaps), then one runner tick (it may wake the model)."""
        return self.tick_async(advance_ms).result(timeout)

    def tick_async(self, advance_ms: int = 10_000):
        """As `tick`, without waiting: returns the concurrent future (for a wake the test holds at a gate)."""
        async def step():
            self.world.clock.t += advance_ms
            return await self.world.runner.tick()
        return asyncio.run_coroutine_threadsafe(step(), self.loop)

    def push(self, type: str, **value) -> None:
        """Push one event into the running session's events engine (what the runner would see)."""
        self.call(lambda: self.world.runner.events.push(self.world.clock.now_ms(), type, value=value or None))
