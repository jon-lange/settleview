"""A local fake of the Anthropic Messages API for the agent harness's integration test. It never forwards anything.

`FakeApi(script)` serves POST /v1/messages on 127.0.0.1 (a free port) from a background thread: the n-th model request
(counted by the assistant messages in its body) gets the n-th scripted turn, streamed as server-sent events the way the
API does (message_start, thinking / text / tool_use blocks with their deltas, message_delta with the stop reason and
output tokens, message_stop). Every request is kept (`requests`: path, headers, JSON body); any other path answers 404
and is kept too. Turns: [("thinking", text) | ("text", text) | ("tool_use", name, input), ...]."""

from __future__ import annotations

import json
import socket
import threading
import time
import uuid

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

USAGE = {"input_tokens": 1200, "cache_creation_input_tokens": 3000, "cache_read_input_tokens": 5000, "output_tokens": 1}
OUT_TOKENS = 250


def _sse(ev: str, data: dict) -> bytes:
    return f"event: {ev}\ndata: {json.dumps(data)}\n\n".encode()


class FakeApi:
    def __init__(self, script: list[list[tuple]]) -> None:
        self.script = script
        self.requests: list[dict] = []
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self.url = ""

    def _keep(self, request: Request, body) -> None:
        self.requests.append({"path": request.url.path, "headers": dict(request.headers), "body": body})

    async def _messages(self, request: Request):
        body = json.loads(await request.body())
        self._keep(request, body)
        n = sum(1 for m in body.get("messages", []) if m.get("role") == "assistant")
        blocks = self.script[min(n, len(self.script) - 1)]
        stop = "tool_use" if any(b[0] == "tool_use" for b in blocks) else "end_turn"
        return StreamingResponse(self._stream(body, blocks, stop), media_type="text/event-stream",
                                 headers={"request-id": "req_fake"})

    async def _stream(self, body: dict, blocks: list[tuple], stop: str):
        yield _sse("message_start", {"type": "message_start", "message": {
            "id": "msg_" + uuid.uuid4().hex[:20], "type": "message", "role": "assistant", "model": body.get("model"),
            "content": [], "stop_reason": None, "stop_sequence": None, "usage": USAGE}})
        for i, b in enumerate(blocks):
            if b[0] == "thinking":
                yield _sse("content_block_start", {"type": "content_block_start", "index": i,
                                                   "content_block": {"type": "thinking", "thinking": "",
                                                                     "signature": ""}})
                for w in b[1].split(" "):
                    yield _sse("content_block_delta", {"type": "content_block_delta", "index": i,
                                                       "delta": {"type": "thinking_delta", "thinking": w + " "}})
                yield _sse("content_block_delta", {"type": "content_block_delta", "index": i,
                                                   "delta": {"type": "signature_delta", "signature": "c2ln"}})
            elif b[0] == "text":
                yield _sse("content_block_start", {"type": "content_block_start", "index": i,
                                                   "content_block": {"type": "text", "text": ""}})
                for w in b[1].split(" "):
                    yield _sse("content_block_delta", {"type": "content_block_delta", "index": i,
                                                       "delta": {"type": "text_delta", "text": w + " "}})
            else:
                yield _sse("content_block_start", {"type": "content_block_start", "index": i, "content_block": {
                    "type": "tool_use", "id": "toolu_" + uuid.uuid4().hex[:20], "name": b[1], "input": {}}})
                js = json.dumps(b[2])
                for k in range(0, len(js), 8):
                    yield _sse("content_block_delta", {"type": "content_block_delta", "index": i,
                                                       "delta": {"type": "input_json_delta",
                                                                 "partial_json": js[k:k + 8]}})
            yield _sse("content_block_stop", {"type": "content_block_stop", "index": i})
        yield _sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                                     "usage": {"output_tokens": OUT_TOKENS}})
        yield _sse("message_stop", {"type": "message_stop"})

    async def _other(self, request: Request):
        raw = await request.body()
        self._keep(request, raw[:2000].decode("utf-8", "replace"))
        return JSONResponse({"type": "error", "error": {"type": "not_found_error", "message": "fake"}},
                            status_code=404)

    def messages(self) -> list[dict]:
        return [r for r in self.requests if r["path"] == "/v1/messages"]

    def __enter__(self) -> FakeApi:
        app = Starlette(routes=[Route("/v1/messages", self._messages, methods=["POST"]),
                                Route("/{p:path}", self._other, methods=["GET", "POST", "HEAD", "PUT", "DELETE"])])
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.url = f"http://127.0.0.1:{sock.getsockname()[1]}"
        self._server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off"))
        self._thread = threading.Thread(target=self._server.run, kwargs={"sockets": [sock]}, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 10
        while not self._server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("the fake API did not start")
            time.sleep(0.02)
        return self

    def __exit__(self, *exc) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)
