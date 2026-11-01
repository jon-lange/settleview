"""Kalshi's WebSocket is opened without permessage-deflate: decompressing large frames stalled the loop."""

from __future__ import annotations

import asyncio

import websockets

from btc_signal_engine.adapters.kalshi.stream import _ws_connect


def test_kalshi_socket_is_opened_with_compression_off(monkeypatch):
    calls = []

    async def fake_connect(url, **kwargs):
        calls.append((url, kwargs))
        return object()

    monkeypatch.setattr(websockets, "connect", fake_connect)
    asyncio.run(_ws_connect("wss://example.invalid/ws", {"h": "v"}))
    assert calls and calls[0][0] == "wss://example.invalid/ws"
    assert "compression" in calls[0][1] and calls[0][1]["compression"] is None
