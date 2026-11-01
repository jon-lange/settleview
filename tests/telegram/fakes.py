"""Fakes for the Telegram remote's tests (tests only): a Messenger that records what it would send, agent controls
that record what they were asked, and a python-telegram-bot BaseRequest that answers the Bot API in memory (no
network, never the real Telegram)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from telegram.request import BaseRequest

from btc_signal_engine.application.agent_sessions import AgentSessions
from btc_signal_engine.application.notifier import Inbound, RemoteControl, Tap
from tests.trading.helpers import NOW

USER, CHAT = 4242, 4242  # the paired user (a private chat's id is the user's id)
OTHER = 777


class FakeMessenger:
    """Records sends (with their `silent` flag), edits, button changes, pins and unpins. `fail`: method names
    ("edit", "pin", "unpin", "edit_buttons") that answer False; `boom`: names that raise."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.edits: list[dict] = []
        self.button_edits: list[dict] = []
        self.answers: list[tuple[str, str | None]] = []
        self.pins: list[tuple[int, int]] = []
        self.unpins: list[tuple[int, int]] = []
        self.fail: set[str] = set()
        self.boom: set[str] = set()
        self._id = 100

    def _check(self, name: str) -> bool:
        if name in self.boom:
            raise RuntimeError(f"fake: {name} failed")
        return name not in self.fail

    async def send(self, chat_id, text, buttons=None, *, silent=False):
        self._id += 1
        self.sent.append({"chat": chat_id, "text": text, "buttons": buttons, "id": self._id, "silent": silent})
        return self._id

    async def edit(self, chat_id, message_id, text, buttons=None):
        ok = self._check("edit")
        if ok:
            self.edits.append({"chat": chat_id, "id": message_id, "text": text, "buttons": buttons})
        return ok

    async def edit_buttons(self, chat_id, message_id, buttons):
        ok = self._check("edit_buttons")
        if ok:
            self.button_edits.append({"chat": chat_id, "id": message_id, "buttons": buttons})
        return ok

    async def pin(self, chat_id, message_id):
        ok = self._check("pin")
        if ok:
            self.pins.append((chat_id, message_id))
        return ok

    async def unpin(self, chat_id, message_id):
        ok = self._check("unpin")
        if ok:
            self.unpins.append((chat_id, message_id))
        return ok

    async def answer(self, callback_id, text=None):
        self.answers.append((callback_id, text))

    def texts(self) -> list[str]:
        return [m["text"] for m in self.sent]

    def button(self, label: str, index: int = -1) -> str:
        """The callback data of the button labelled `label` on sent message `index`."""
        for row in self.sent[index]["buttons"] or []:
            for lab, data in row:
                if lab == label:
                    return data
        raise AssertionError(f"no {label!r} button on {self.sent[index]}")


class StubService:
    """What an agent session needs from a trading service, for remote tests that only need live sessions."""

    mode = "shadow"

    def killed(self) -> bool:
        return False

    def present(self, now: int) -> bool:
        return False


def live_sessions(store, n: int = 1, *, now: int = NOW) -> AgentSessions:
    """Real AgentSessions with `n` live external Kalshi 15m sessions ("sess0001abcd", "sess0002abcd", ...), named
    "Kalshi 15m agent", "Kalshi 15m agent 2", ..."""
    ids = iter(f"sess000{i}abcd" for i in range(1, n + 1))
    ag = AgentSessions(store, {"you": StubService()}, id_fn=lambda: next(ids))
    for _ in range(n):
        assert ag.launch({"kind": "external", "market": "kalshi_15m", "budget": 5.0, "per_trade": 1.0,
                          "minutes": 60, "level": "auto"}, now)["ok"]
    return ag


class FakeActions:
    """AgentActions' surface, recording each call (launch answers ok with a session-like view)."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.launch_out: dict | None = None

    async def launch(self, spec, *, from_pc):
        self.calls.append(("launch", dict(spec), from_pc))
        return self.launch_out or {"ok": True, "session": {"limits": {"level": spec.get("level")},
                                                           "seconds_left": int(spec.get("minutes", 0)) * 60}}

    async def stop(self, session_id):
        self.calls.append(("stop", session_id))
        return {"ok": True}

    async def stop_all(self):
        self.calls.append(("stop_all",))
        return {"ok": True, "stopped": []}

    def pause(self, session_id, why):
        self.calls.append(("pause", session_id, why))
        return {"ok": True}

    def note(self, session_id, text):
        self.calls.append(("note", session_id, text))
        return {"ok": True}

    def answered(self, out):
        self.calls.append(("answered",))
        return out

    def validate(self, spec, now):
        return None


class Clock:
    def __init__(self, t: int = NOW) -> None:
        self.t = t

    def __call__(self) -> int:
        return self.t


def message(text: str, *, user: int = USER, chat: int | None = None, private: bool = True, sent_ts: int = NOW,
            label: str | None = "@you") -> Inbound:
    return Inbound(user_id=user, chat_id=chat if chat is not None else user, private=private, user_label=label,
                   text=text, sent_ts=sent_ts)


def tap(data: str, *, user: int = USER, chat: int | None = None, message_id: int | None = None,
        callback_id: str = "cb") -> Tap:
    return Tap(user_id=user, chat_id=chat if chat is not None else user, message_id=message_id,
               callback_id=callback_id, data=data)


async def pair(control: RemoteControl, *, user: int = USER) -> None:
    code = control.new_code(control.now_fn())["code"]
    await control.handle_message(message(f"/start {code}", user=user, sent_ts=control.now_fn()))
    assert control.paired()["user_id"] == user


# -- a fake Bot API transport for the adapter (python-telegram-bot's own BaseRequest hook) --------------------------
BOT_USER = {"id": 999, "is_bot": True, "first_name": "Settleview", "username": "settleview_test_bot"}


class FakeBotApi(BaseRequest):
    """Answers the Bot API methods the bot calls, in memory. `calls` records (method, parameters)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.updates: list[dict] = []
        self._msg = 500
        self.fail: type[Exception] | None = None  # raise this from every call (Telegram unreachable)

    @property
    def read_timeout(self) -> float | None:
        return 1.0

    async def initialize(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    async def do_request(self, url, method, request_data=None, read_timeout=None, write_timeout=None,
                         connect_timeout=None, pool_timeout=None) -> tuple[int, bytes]:
        name = url.rsplit("/", 1)[-1]
        params: dict[str, Any] = dict(request_data.parameters) if request_data is not None else {}
        self.calls.append((name, params))
        if self.fail is not None:
            raise self.fail("fake: Telegram unreachable")
        if name == "getMe":
            result: Any = BOT_USER
        elif name in ("sendMessage", "editMessageText"):
            self._msg += 1
            chat = int(params.get("chat_id"))
            result = {"message_id": int(params.get("message_id") or self._msg), "date": NOW // 1000,
                      "chat": {"id": chat, "type": "private"}, "text": params.get("text"), "from": BOT_USER}
        elif name == "getUpdates":
            if not self.updates:
                await asyncio.sleep(0.02)  # a long poll that found nothing
            result, self.updates = self.updates, []
        else:  # answerCallbackQuery, deleteWebhook, ...
            result = True
        return 200, json.dumps({"ok": True, "result": result}).encode()

    def methods(self) -> list[str]:
        return [n for n, _ in self.calls]
