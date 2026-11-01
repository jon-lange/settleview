"""The python-telegram-bot adapter (adapters/telegram/bot.py) on a fake Bot API transport (the library's own
BaseRequest hook; no network, never the real Telegram): updates reach the remote through the library's handlers,
unpaired senders get nothing, stale messages are dropped, taps are answered, an unreachable Telegram is retried with
backoff without blocking the loop, a refused token turns the bot off, and the token never reaches a log line."""

from __future__ import annotations

import asyncio
import json
import logging

from telegram import Update
from telegram.error import NetworkError

from btc_signal_engine.adapters.telegram.bot import REDACTED, TelegramBot
from btc_signal_engine.application.notifier import RemoteControl
from tests.telegram.fakes import BOT_USER, OTHER, USER, Clock, FakeActions, FakeBotApi, live_sessions
from tests.trading.helpers import NOW

TOKEN = "123456:fake-token-for-tests"


def rig(store, **kw):
    api, poll = FakeBotApi(), FakeBotApi()
    kw.setdefault("sessions", live_sessions(store))
    control = RemoteControl(store, now_fn=Clock(), configured=True, actions=FakeActions(), **kw)
    bot = TelegramBot(TOKEN, control, request=api, get_updates_request=poll, pump_s=0.01, backoff_s=0.01,
                      backoff_max_s=0.02)
    return bot, control, api, poll


def text_update(uid: int, text: str, *, date_s: int = NOW // 1000, update_id: int = 1, chat_type="private"):
    return {"update_id": update_id, "message": {
        "message_id": update_id, "date": date_s, "text": text,
        "chat": {"id": uid, "type": chat_type}, "from": {"id": uid, "is_bot": False, "first_name": "Alex",
                                                         "username": "alex_example"},
        "entities": [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        if text.startswith("/") else []}}


def tap_update(uid: int, data: str, update_id: int = 9):
    return {"update_id": update_id, "callback_query": {
        "id": f"cb{update_id}", "chat_instance": "ci", "data": data,
        "from": {"id": uid, "is_bot": False, "first_name": "Alex"},
        "message": {"message_id": 77, "date": NOW // 1000, "chat": {"id": uid, "type": "private"},
                    "text": "Pick a preset.", "from": BOT_USER}}}


async def process(bot: TelegramBot, data: dict) -> None:
    await bot.app.process_update(Update.de_json(data, bot.app.bot))
    await asyncio.sleep(0.05)  # concurrent updates run as tasks


def sends(api: FakeBotApi) -> list[str]:
    return [p["text"] for n, p in api.calls if n == "sendMessage"]


def test_updates_reach_the_remote_only_from_the_paired_user(store):
    bot, control, api, poll = rig(store)

    async def main():
        await bot.start()
        try:
            assert control.running and control.messenger is bot
            assert control.bot_username == "settleview_test_bot"  # learned from getMe (no setting needed)
            code = control.new_code(NOW)["code"]
            await process(bot, text_update(OTHER, "/status", update_id=1))
            assert sends(api) == []  # unpaired: nothing
            await process(bot, text_update(USER, f"/start {code}", update_id=2))
            assert control.paired()["user_id"] == USER and control.paired()["user_label"] == "@alex_example"
            await process(bot, text_update(OTHER, "/stop", update_id=3))
            await process(bot, text_update(USER, "/stop", update_id=4, date_s=NOW // 1000 - 61))  # stale
            assert control.actions.calls == []
            await process(bot, text_update(USER, "/stop", update_id=5))
            assert control.actions.calls == [("stop", "sess0001abcd")]
            assert sends(api) == ["Paired. Proposals and trades will arrive here.", "Stopped Kalshi 15m agent."]
            await process(bot, tap_update(USER, "c:unknown"))
            answers = [p for n, p in api.calls if n == "answerCallbackQuery"]
            assert answers and answers[-1]["callback_query_id"] == "cb9"
            await process(bot, tap_update(OTHER, "c:unknown", update_id=10))
            assert len([n for n, _ in api.calls if n == "answerCallbackQuery"]) == 1  # the stranger: nothing
        finally:
            await bot.stop()
        assert control.running is False and control.messenger is None and bot.app is None
    asyncio.run(main())


def test_buttons_are_sent_as_an_inline_keyboard(store):
    bot, control, api, poll = rig(store)

    async def main():
        await bot.start()
        try:
            mid = await bot.send(USER, "Buy Up $1.00", [[("Approve", "a:p1:n"), ("Skip", "s:p1:n")]])
            assert isinstance(mid, int)
            (n, p), = [c for c in api.calls if c[0] == "sendMessage"]
            markup = p["reply_markup"]
            markup = json.loads(markup) if isinstance(markup, str) else markup
            assert [[b["callback_data"] for b in row] for row in markup["inline_keyboard"]] == [["a:p1:n", "s:p1:n"]]
            await bot.edit(USER, mid, "Approved · Buy Up $1.00")
            assert api.methods()[-1] == "editMessageText"
        finally:
            await bot.stop()
    asyncio.run(main())


def test_messages_go_as_html_with_the_silent_flag_and_pins_are_silent(store):
    bot, control, api, poll = rig(store)
    text = "📊 <b>−$0.10</b> · 23:00–00:00\n<blockquote expandable>Budget $0.10 / $20</blockquote>"

    async def main():
        await bot.start()
        try:
            mid = await bot.send(USER, text, silent=True)
            loud = await bot.send(USER, "🔵 <b>Bought</b>")
            assert await bot.edit(USER, mid, "<b>x</b>", [[("Status", "v:st")]]) is True
            assert await bot.edit_buttons(USER, mid, [[("Yes, stop", "v:y:n")]]) is True
            assert await bot.pin(USER, mid) is True
            assert await bot.unpin(USER, mid) is True
            return mid, loud
        finally:
            await bot.stop()
    mid, loud = asyncio.run(main())
    s1, s2 = [p for n, p in api.calls if n == "sendMessage"]
    # the library passes the expandable quote through untouched, with parse mode HTML
    assert s1["text"] == text and s1["parse_mode"] == "HTML" and s1["disable_notification"] is True
    assert s2["parse_mode"] == "HTML" and s2.get("disable_notification") in (None, False)
    (e,) = [p for n, p in api.calls if n == "editMessageText"]
    assert e["parse_mode"] == "HTML" and int(e["message_id"]) == mid
    (b,) = [p for n, p in api.calls if n == "editMessageReplyMarkup"]
    markup = json.loads(b["reply_markup"]) if isinstance(b["reply_markup"], str) else b["reply_markup"]
    assert markup["inline_keyboard"][0][0]["callback_data"] == "v:y:n"
    (pin,) = [p for n, p in api.calls if n == "pinChatMessage"]
    assert int(pin["message_id"]) == mid and pin["disable_notification"] is True
    (unpin,) = [p for n, p in api.calls if n == "unpinChatMessage"]
    assert int(unpin["message_id"]) == mid


def test_a_refused_pin_or_edit_answers_false_and_never_raises(store):
    bot, control, api, poll = rig(store)
    real = api.do_request

    async def refuse(url, method, request_data=None, **kw):
        name = url.rsplit("/", 1)[-1]
        if name in ("pinChatMessage", "unpinChatMessage", "editMessageText", "editMessageReplyMarkup"):
            api.calls.append((name, {}))
            return 400, json.dumps({"ok": False, "error_code": 400,
                                    "description": "Bad Request: not enough rights"}).encode()
        return await real(url, method, request_data, **kw)
    api.do_request = refuse

    async def main():
        await bot.start()
        try:
            assert await bot.pin(USER, 5) is False
            assert await bot.unpin(USER, 5) is False
            assert await bot.edit(USER, 5, "x") is False
            assert await bot.edit_buttons(USER, 5, None) is False
        finally:
            await bot.stop()
    asyncio.run(main())


def test_long_polling_delivers_updates(store):
    bot, control, api, poll = rig(store)

    async def main():
        code = control.new_code(NOW)["code"]
        poll.updates = [text_update(USER, f"/start {code}", update_id=1)]
        await bot.start()
        try:
            for _ in range(100):
                if control.paired():
                    break
                await asyncio.sleep(0.02)
            assert control.paired()["user_id"] == USER
            assert "getUpdates" in poll.methods()
            # long polling only: no webhook is ever set
            assert "setWebhook" not in api.methods() + poll.methods()
        finally:
            await bot.stop()
    asyncio.run(main())


def test_telegram_unreachable_is_retried_with_backoff_and_never_blocks_the_loop(store, caplog):
    bot, control, api, poll = rig(store)
    api.fail = NetworkError
    caplog.set_level("WARNING", logger="btc_signal_engine.adapters.telegram.bot")

    async def main():
        task = asyncio.create_task(bot.run())
        ticks = 0
        for _ in range(20):  # the loop keeps running while the bot retries
            await asyncio.sleep(0.01)
            ticks += 1
        assert not task.done() and control.running is False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return ticks
    assert asyncio.run(main()) == 20
    assert sum(1 for c in api.calls if c[0] == "getMe") >= 2  # retried
    assert any("unreachable" in r.getMessage() for r in caplog.records)


def test_a_refused_token_turns_the_bot_off_without_retrying(store, caplog):
    bot, control, api, poll = rig(store)

    async def refuse(url, method, request_data=None, **kw):
        api.calls.append((url.rsplit("/", 1)[-1], {}))
        return 401, json.dumps({"ok": False, "error_code": 401, "description": "Unauthorized"}).encode()
    api.do_request = refuse

    async def main():
        task = asyncio.create_task(bot.run())
        await asyncio.sleep(0.2)
        assert not task.done() and control.running is False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    asyncio.run(main())
    assert [n for n, _ in api.calls].count("getMe") == 1
    assert any("refused the bot token" in r.getMessage() for r in caplog.records)
    assert not any(TOKEN in r.getMessage() for r in caplog.records)


def test_the_token_is_masked_in_httpx_log_lines(store, caplog):
    rig(store)
    caplog.set_level("INFO", logger="httpx")
    logging.getLogger("httpx").info('HTTP Request: POST %s "HTTP/1.1 200 OK"',
                                    f"https://api.telegram.org/bot{TOKEN}/getMe")
    (r,) = [r for r in caplog.records if r.name == "httpx"]
    # The bot's own redactor, or the process-wide log mask when main() installed it (bootstrap/logs.py)
    assert TOKEN not in r.getMessage() and (REDACTED in r.getMessage() or "****" in r.getMessage())


def test_the_token_is_masked_in_the_librarys_child_loggers_tracebacks_and_this_modules_own_lines(store, caplog):
    rig(store)
    caplog.set_level("DEBUG")
    url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
    try:
        raise RuntimeError(f"connect failed for {url}")
    except RuntimeError:
        logging.getLogger("telegram.ext.Updater").exception("polling failed")  # a child logger (main: unmasked)
    logging.getLogger("btc_signal_engine.adapters.telegram.bot").debug("telegram: pin failed (%s)", url)
    rows = [r for r in caplog.records if r.name in ("telegram.ext.Updater", "btc_signal_engine.adapters.telegram.bot")]
    assert len(rows) == 2
    for r in rows:
        shown = logging.Formatter("%(message)s").format(r)
        assert TOKEN not in shown and REDACTED in shown, shown


def test_a_bot_without_a_token_cannot_be_built(store):

    import pytest
    with pytest.raises(ValueError):
        TelegramBot("", RemoteControl(store, now_fn=Clock()))


def test_the_command_menu_is_set_once_at_start(store):
    bot, control, api, poll = rig(store)

    async def main():
        await bot.start()
        try:
            await asyncio.sleep(0.05)
        finally:
            await bot.stop()
    asyncio.run(main())
    menus = [p for n, p in api.calls if n == "setMyCommands"]
    assert len(menus) == 1
    cmds = menus[0]["commands"]
    cmds = json.loads(cmds) if isinstance(cmds, str) else cmds
    assert [c["command"] for c in cmds] == ["status", "lights", "stop", "pause", "note", "launch", "mute", "unmute"]
    assert all(0 < len(c["description"]) <= 30 for c in cmds)


def test_a_failed_command_menu_never_stops_the_bot(store, caplog):
    bot, control, api, poll = rig(store)
    real = api.do_request

    async def no_menu(url, method, request_data=None, **kw):
        if url.endswith("/setMyCommands"):
            api.calls.append(("setMyCommands", {}))
            return 400, json.dumps({"ok": False, "error_code": 400, "description": "Bad Request: nope"}).encode()
        return await real(url, method, request_data, **kw)
    api.do_request = no_menu

    async def main():
        await bot.start()
        try:
            assert control.running
        finally:
            await bot.stop()
    caplog.set_level("WARNING", logger="btc_signal_engine.adapters.telegram.bot")
    asyncio.run(main())
    assert any("command menu could not be set" in r.getMessage() for r in caplog.records)
