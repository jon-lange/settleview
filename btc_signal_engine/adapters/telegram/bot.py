"""The Telegram transport for the remote (application/notifier.py), on python-telegram-bot's Application (async).

Lifecycle (`run`, one supervised background task): build the Application, `initialize` (getMe), `start`, register
the command menu (`set_my_commands` with notifier.BOT_COMMANDS; a failure there is logged, never fatal), then
`updater.start_polling` (long polling; message and callback_query updates only; pending updates are NOT dropped
wholesale: the remote drops each message older than 60 s itself, so a fresh /stop sent just before a restart still
counts). While polling, the remote's `pump` runs about every second (alerts and diffs). Telegram unreachable or any
start failure: logged in one line, everything is shut down, and it retries after a backoff (2 s doubling to 5 min);
the app and the event loop never wait on Telegram. A token Telegram refuses (InvalidToken) is logged once and the bot
stays off until the app restarts with a fixed token. Cancelling the task stops polling and shuts the Application down.

Messages are sent and edited with parse_mode HTML (the remote escapes every dynamic string; <blockquote expandable>
is passed through as text, Bot API 7.4+), `silent` maps to disable_notification, pins are silent. Edits, button
changes, pins and unpins return False on failure (details at debug): the remote decides what to log.

Updates are processed concurrently (`concurrent_updates`), so /stop never waits behind an approval in flight.

The token is read once from the settings' SecretStr by the wiring and handed to the library; it is never logged:
`TokenRedactor` masks it (message, arguments and the formatted traceback) in the records of this module's logger and
of every "telegram*" / "httpx*" / "httpcore*" logger that exists when the bot is built (a logger's filter does not
reach its children's records, so each is named; httpx logs each request URL, which carries the token), and this
module never logs an exception's text unmasked. Records from any other logger (a traceback logged elsewhere) need the
app's handler-level scrubbing filter (bootstrap; not this module).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, InvalidToken, TelegramError
from telegram.ext import Application, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from telegram.request import BaseRequest

from btc_signal_engine.adapters.retry import Backoff
from btc_signal_engine.application.notifier import BOT_COMMANDS, Inbound, Keyboard, RemoteControl, Tap

log = logging.getLogger(__name__)

PUMP_S = 1.0
BACKOFF_S = 2.0
BACKOFF_MAX_S = 300.0
STOP_TIMEOUT_S = 10.0
ALLOWED_UPDATES = ("message", "callback_query")
REDACTED = "<telegram-token>"


class TokenRedactor(logging.Filter):
    """Masks the bot token in a log record (message and arguments rendered first)."""

    def __init__(self, token: str) -> None:
        super().__init__()
        self._token = token

    def mask(self, text: Any) -> str:
        s = str(text)
        return s.replace(self._token, REDACTED) if self._token else s

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        if self._token and self._token in msg:
            record.msg, record.args = msg.replace(self._token, REDACTED), None
        if self._token and record.exc_info and not record.exc_text:
            # the traceback (an exception's text may carry a request URL): formatted now, masked, and cached on the
            # record so the handler's formatter prints this text
            try:
                text = logging.Formatter().formatException(record.exc_info)
            except Exception:
                return True
            if self._token in text:
                record.exc_text = text.replace(self._token, REDACTED)
        elif self._token and record.exc_text and self._token in record.exc_text:
            record.exc_text = record.exc_text.replace(self._token, REDACTED)
        return True


def _markup(buttons: Keyboard | None) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=data) for label, data in row]
                                 for row in buttons])


class TelegramBot:
    """The remote's Messenger over python-telegram-bot. `request` / `get_updates_request`: the library's own
    transport hook (tests pass a fake BaseRequest; the app uses the library's default HTTPXRequest)."""

    def __init__(self, token: str, control: RemoteControl, *, request: BaseRequest | None = None,
                 get_updates_request: BaseRequest | None = None, pump_s: float = PUMP_S,
                 backoff_s: float = BACKOFF_S, backoff_max_s: float = BACKOFF_MAX_S) -> None:
        if not token:
            raise ValueError("a Telegram bot needs a token")
        self._token = token
        self.control = control
        self._request, self._get_updates_request = request, get_updates_request
        self.pump_s, self.backoff_s, self.backoff_max_s = pump_s, backoff_s, backoff_max_s
        self.app: Application | None = None
        self._redactor = TokenRedactor(token)
        for name in self.logger_names():
            logging.getLogger(name).addFilter(self._redactor)

    @staticmethod
    def logger_names() -> list[str]:
        """This module's logger, the library's and httpx's: every one already created (they are made at import)."""
        known = [n for n in logging.root.manager.loggerDict if n.split(".")[0] in ("telegram", "httpx", "httpcore")]
        return sorted({__name__, "httpx", "httpcore", "telegram", "telegram.ext", *known})

    # -- the library --------------------------------------------------------------------------------------------------
    def build(self) -> Application:
        b = Application.builder().token(self._token).concurrent_updates(True).job_queue(None)
        if self._request is not None:
            b = b.request(self._request)
        if self._get_updates_request is not None:
            b = b.get_updates_request(self._get_updates_request)
        app = b.build()
        app.add_handler(MessageHandler(filters.UpdateType.MESSAGE & filters.TEXT, self._on_message))
        app.add_handler(CallbackQueryHandler(self._on_tap))
        app.add_error_handler(self._on_error)
        return app

    async def _on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg, user, chat = update.message, update.effective_user, update.effective_chat
        if msg is None or user is None or chat is None or msg.text is None:
            return
        label = f"@{user.username}" if user.username else (user.first_name or None)
        await self.control.handle_message(Inbound(
            user_id=user.id, chat_id=chat.id, private=chat.type == ChatType.PRIVATE, user_label=label,
            text=msg.text, sent_ts=int(msg.date.timestamp() * 1000)))

    async def _on_tap(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if q is None or q.from_user is None or q.message is None:
            return
        await self.control.handle_tap(Tap(user_id=q.from_user.id, chat_id=q.message.chat.id,
                                          message_id=q.message.message_id, callback_id=q.id, data=q.data or ""))

    async def _on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        err = context.error
        log.warning("telegram: handling an update failed: %s", type(err).__name__,
                    exc_info=(type(err), err, err.__traceback__) if err is not None else None)

    @staticmethod
    def _poll_error(err: TelegramError) -> None:
        log.warning("telegram: polling error (%s); the library retries", type(err).__name__)

    # -- Messenger (every text is HTML: parse mode HTML, never MarkdownV2) -----------------------------------------
    async def send(self, chat_id: int, text: str, buttons: Keyboard | None = None, *,
                   silent: bool = False) -> int | None:
        if self.app is None:
            return None
        try:
            m = await self.app.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=_markup(buttons),
                                                disable_notification=silent)
        except TelegramError as e:
            log.warning("telegram: a message could not be sent (%s)", type(e).__name__)
            return None
        return m.message_id

    async def edit(self, chat_id: int, message_id: int, text: str, buttons: Keyboard | None = None) -> bool:
        """False when it could not be edited (the caller logs; details at debug). "Not modified" counts as done."""
        if self.app is None:
            return False
        try:
            await self.app.bot.edit_message_text(text, chat_id=chat_id, message_id=message_id,
                                                 parse_mode=ParseMode.HTML, reply_markup=_markup(buttons))
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return True
            log.debug("telegram: a message could not be edited (%s)", self._redactor.mask(e))
            return False
        except TelegramError as e:
            log.debug("telegram: a message could not be edited (%s)", type(e).__name__)
            return False
        return True

    async def edit_buttons(self, chat_id: int, message_id: int, buttons: Keyboard | None) -> bool:
        if self.app is None:
            return False
        try:
            await self.app.bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id,
                                                         reply_markup=_markup(buttons))
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return True
            log.debug("telegram: a message's buttons could not be changed (%s)", self._redactor.mask(e))
            return False
        except TelegramError as e:
            log.debug("telegram: a message's buttons could not be changed (%s)", type(e).__name__)
            return False
        return True

    async def pin(self, chat_id: int, message_id: int) -> bool:
        """Pin silently (no notification for the pin)."""
        if self.app is None:
            return False
        try:
            return bool(await self.app.bot.pin_chat_message(chat_id, message_id, disable_notification=True))
        except TelegramError as e:
            log.debug("telegram: a message could not be pinned (%s)", self._redactor.mask(e))
            return False

    async def unpin(self, chat_id: int, message_id: int) -> bool:
        if self.app is None:
            return False
        try:
            return bool(await self.app.bot.unpin_chat_message(chat_id, message_id=message_id))
        except TelegramError as e:
            log.debug("telegram: a message could not be unpinned (%s)", self._redactor.mask(e))

            return False

    async def answer(self, callback_id: str, text: str | None = None) -> None:
        if self.app is None:
            return
        try:
            await self.app.bot.answer_callback_query(callback_id, text=text)
        except TelegramError as e:
            log.info("telegram: a tap could not be answered (%s)", type(e).__name__)

    # -- lifecycle -------------------------------------------------------------------------------------------------
    async def start(self) -> None:
        """Initialize, start and begin long polling (raises when Telegram cannot be reached)."""
        app = self.app = self.build()
        await app.initialize()
        if self.control.bot_username is None and getattr(app.bot, "username", None):  # getMe ran in initialize
            self.control.bot_username = app.bot.username
        await app.start()
        try:  # the command menu (status, lights, stop, pause, note, launch): no BotFather step for the user
            await app.bot.set_my_commands([BotCommand(c, d) for c, d in BOT_COMMANDS])
        except TelegramError as e:  # the menu is a convenience: the commands work without it
            log.warning("telegram: the command menu could not be set (%s)", type(e).__name__)
        await app.updater.start_polling(allowed_updates=list(ALLOWED_UPDATES), drop_pending_updates=False,
                                        error_callback=self._poll_error)
        self.control.messenger = self
        self.control.running = True
        log.info("telegram: bot started (long polling)")

    async def stop(self) -> None:
        app, self.app = self.app, None
        self.control.running = False
        if self.control.messenger is self:
            self.control.messenger = None
        if app is None:
            return
        for step, fn in (("updater", lambda: app.updater.stop() if app.updater.running else None),
                         ("application", lambda: app.stop() if app.running else None),
                         ("shutdown", app.shutdown)):
            try:
                coro: Any = fn()
                if coro is not None:
                    await asyncio.wait_for(coro, STOP_TIMEOUT_S)
            except Exception as e:  # a failed stop step never keeps the others from running
                log.warning("telegram: stopping (%s) failed: %s", step, type(e).__name__)

    async def run(self) -> None:
        """The supervised loop: start (retrying with backoff), pump, and stop when cancelled. Never returns."""
        backoff = Backoff(self.backoff_s, self.backoff_max_s)  # full jitter (adapters/retry.py)
        while True:
            try:
                await self.start()
                backoff.reset()
                while self.app is not None and self.app.updater.running:
                    try:
                        await self.control.pump()
                    except Exception:  # an alert failing never stops the bot (commands keep working)
                        log.warning("telegram: the alert pump failed", exc_info=True)
                    await asyncio.sleep(self.pump_s)
                log.warning("telegram: polling stopped; restarting")
            except asyncio.CancelledError:
                await asyncio.shield(self.stop())
                raise
            except InvalidToken:
                log.error("telegram: Telegram refused the bot token (BTCFLOW_TELEGRAM_BOT_TOKEN); the bot stays off "
                          "until the token is fixed and the app restarted")
                await self.stop()
                await asyncio.Event().wait()  # off for good (cancellation still ends it)
            except Exception as e:
                log.warning("telegram: unreachable or failed (%s); retrying within %.0f s", type(e).__name__,
                            backoff.ceiling)
            await self.stop()
            await asyncio.sleep(backoff.next())
