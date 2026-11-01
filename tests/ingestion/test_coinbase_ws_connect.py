"""The Coinbase feed opens its WebSocket without permessage-deflate: inflating big level2 frames stalled the loop."""

from __future__ import annotations

import asyncio

import websockets

from btc_signal_engine.adapters.coinbase.stream import WS_URL, raw_ws_transport


class _FakeWs:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, _msg):
        pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


def test_coinbase_socket_is_opened_with_compression_off(monkeypatch):
    calls = []

    def fake_connect(url, **kwargs):
        calls.append((url, kwargs))
        return _FakeWs()

    monkeypatch.setattr(websockets, "connect", fake_connect)
    asyncio.run(raw_ws_transport(clock=None)(lambda _u: None))
    assert calls and calls[0][0] == WS_URL
    assert "compression" in calls[0][1] and calls[0][1]["compression"] is None
