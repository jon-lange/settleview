"""The PC listener (BTCFLOW_PC_PORT): a second uvicorn server in the same process on the same app. Only the main server
runs the ASGI lifespan and owns the signals; asking either to exit stops both, and the ports are free afterwards."""

from __future__ import annotations

import asyncio
import contextlib
import socket

import httpx

from btc_signal_engine.bootstrap.app import Runtime
from btc_signal_engine.bootstrap.config import Settings


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _port_is_free(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
        return True


class App:
    """Counts lifespan startups / shutdowns and answers with the port the request arrived on."""

    def __init__(self) -> None:
        self.startups = self.shutdowns = 0

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "lifespan":
            while True:
                msg = await receive()
                if msg["type"] == "lifespan.startup":
                    self.startups += 1
                    await send({"type": "lifespan.startup.complete"})
                else:
                    self.shutdowns += 1
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        body = str(scope["server"][1]).encode()
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-length", b"%d" % len(body))]})
        await send({"type": "http.response.body", "body": body})


def _runtime(tmp_path, monkeypatch, **kw) -> Runtime:
    for k in ("BTCFLOW_API_HOST", "BTCFLOW_API_PORT", "BTCFLOW_PC_PORT", "BTCFLOW_PC_HOST", "BTCFLOW_TRADING_STREAM",
              "BTCFLOW_FORWARDED_ALLOW_IPS", "BTCFLOW_AGENT_PORT", "BTCFLOW_AGENT_HOST"):
        monkeypatch.delenv(k, raising=False)
    return Runtime(Settings(_env_file=None, db_path=tmp_path / "pc.db", trading_stream=False, **kw))


async def _until_started(servers, timeout_s: float = 10.0) -> None:
    async with asyncio.timeout(timeout_s):
        while not all(s.started for s in servers):
            await asyncio.sleep(0.02)


def test_without_a_pc_port_there_is_one_server(tmp_path, monkeypatch):
    rt = _runtime(tmp_path, monkeypatch, api_port=_free_port())
    assert len(rt.make_servers(App())) == 1


def test_two_servers_start_share_one_lifespan_and_stop_together(tmp_path, monkeypatch):
    main_port, pc_port = _free_port(), _free_port()
    rt = _runtime(tmp_path, monkeypatch, api_port=main_port, pc_port=pc_port)
    app = App()
    servers = rt.make_servers(app)
    assert [s.config.port for s in servers] == [main_port, pc_port]
    assert servers[1].config.host == "127.0.0.1" and servers[1].config.lifespan == "off"
    # the client is the TCP peer: uvicorn never rewrites it from X-Forwarded-For (the lockout key, the PC's place)
    assert all(s.config.proxy_headers is False for s in servers)

    async def run() -> bool:
        task = asyncio.ensure_future(rt.serve_all(servers))
        try:
            await _until_started(servers)
            async with httpx.AsyncClient() as c:
                assert (await c.get(f"http://127.0.0.1:{main_port}/")).text == str(main_port)
                assert (await c.get(f"http://127.0.0.1:{pc_port}/")).text == str(pc_port)
            assert app.startups == 1  # the PC listener runs no lifespan of its own
            servers[0].should_exit = True  # what Ctrl+C does (only the main server takes the signal)
            async with asyncio.timeout(10):
                return await task
        finally:
            task.cancel()
            with contextlib.suppress(BaseException):
                await task

    assert asyncio.run(run()) is True
    assert all(s.should_exit for s in servers) and app.startups == 1 and app.shutdowns == 1
    assert _port_is_free(main_port) and _port_is_free(pc_port)


def test_proxy_headers_are_uvicorns_on_the_phone_listener_only_and_only_when_configured(tmp_path, monkeypatch):
    """BTCFLOW_FORWARDED_ALLOW_IPS (the Tailscale sidecar's address) turns on uvicorn's own proxy headers for the main
    (phone) server with exactly that address; the PC and agent listeners never trust a forwarded header."""
    rt = _runtime(tmp_path, monkeypatch, api_port=_free_port(), pc_port=_free_port(), agent_port=_free_port(),
                  forwarded_allow_ips="10.89.250.2")
    main, pc, agent = rt.make_servers(App())
    assert main.config.proxy_headers is True and main.config.forwarded_allow_ips == "10.89.250.2"
    assert pc.config.proxy_headers is False and agent.config.proxy_headers is False


def test_stopping_the_pc_listener_stops_the_main_server_too(tmp_path, monkeypatch):
    main_port, pc_port = _free_port(), _free_port()
    rt = _runtime(tmp_path, monkeypatch, api_port=main_port, pc_port=pc_port)
    servers = rt.make_servers(App())

    async def run() -> bool:
        task = asyncio.ensure_future(rt.serve_all(servers))
        await _until_started(servers)
        servers[1].should_exit = True
        async with asyncio.timeout(10):
            return await task

    assert asyncio.run(run()) is True
    assert servers[0].should_exit and _port_is_free(main_port) and _port_is_free(pc_port)


def test_cancelling_serve_all_stops_both_servers(tmp_path, monkeypatch):
    """The supervisor cancels the server task on shutdown: neither listener may stay open."""
    main_port, pc_port = _free_port(), _free_port()
    rt = _runtime(tmp_path, monkeypatch, api_port=main_port, pc_port=pc_port)
    servers = rt.make_servers(App())

    async def run() -> None:
        task = asyncio.ensure_future(rt.serve_all(servers))
        await _until_started(servers)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert _port_is_free(main_port) and _port_is_free(pc_port)
