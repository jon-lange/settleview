"""All runtime configuration comes from environment variables (prefix BTCFLOW_) or `.env`.

Any setting can instead be read from a file named by `BTCFLOW_<NAME>_FILE` (the Docker / Swarm secrets convention:
secrets are mounted as files under /run/secrets), so a container needs no `.env`. The file's content is the value
(one trailing newline is dropped). A file that is empty (or only whitespace) means the setting is not configured,
like an empty variable: compose mounts every secret file whether or not that feature is used. Setting
`BTCFLOW_<NAME>` while `BTCFLOW_<NAME>_FILE` names a non-empty file, a `_FILE` that names no setting, or a `_FILE` that
cannot be read is an error naming the variable (never its value); nothing falls back.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from btc_signal_engine.adapters.sqlite.urls import DbTarget, PgTimeouts, postgres_url
from btc_signal_engine.domain.horizons.config import Horizon, parse_horizons
from btc_signal_engine.domain.markets import market

PREFIX = "BTCFLOW_"
FILE_SUFFIX = "_FILE"
# Renamed settings whose old names ended in _FILE (they read as "<NAME>_FILE" and failed): the error names the new one.
RENAMED = {"BTCFLOW_LOG_FILE": "BTCFLOW_FILE_LOG"}
# A Telegram bot username without its @ (Telegram: 5-32 characters, letters, digits and _, ending in "bot").
BOT_USERNAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{1,28}[Bb][Oo][Tt]")


class FileValueSource(PydanticBaseSettingsSource):
    """`BTCFLOW_<NAME>_FILE` -> the setting `<name>` read from that file (environment variables only)."""

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:  # pragma: no cover
        return None, field_name, False  # unused: __call__ reads every field at once

    def __call__(self) -> dict[str, Any]:
        fields = set(self.settings_cls.model_fields)
        env = {k.upper(): v for k, v in os.environ.items()}
        out: dict[str, Any] = {}
        for key, path in env.items():
            if not (key.startswith(PREFIX) and key.endswith(FILE_SUFFIX)) or not path:
                continue
            name = key[len(PREFIX):-len(FILE_SUFFIX)].lower()
            if key in RENAMED:
                raise ValueError(f"{key} was renamed {RENAMED[key]}: set that instead")
            if name not in fields:
                raise ValueError(f"{key} names no setting (BTCFLOW_{name.upper()} does not exist)")
            try:
                text = Path(path).read_text(encoding="utf-8")
            except OSError as exc:
                raise ValueError(f"{key}: the file cannot be read ({type(exc).__name__})") from None
            if not text.strip():
                continue  # an empty secret file: not configured (as an empty variable)
            if env.get(PREFIX + name.upper()):
                raise ValueError(f"both BTCFLOW_{name.upper()} and {key} are set: use one (docs/operations.md, "
                                 "\"Secrets as files\")")
            out[name] = text.removesuffix(chr(10)).removesuffix(chr(13))
        return out


class Settings(BaseSettings):
    # hide_input_in_errors: a validation error never echoes the settings (they hold keys, tokens and passwords).
    model_config = SettingsConfigDict(env_prefix="BTCFLOW_", env_file=".env", extra="ignore", env_ignore_empty=True,
                                      hide_input_in_errors=True)

    db_path: Path = Path("data/btcflow.db")
    # Postgres (docs/plans/2026-10-31-0900-feat-postgres-plan.md): when set, the app's data lives there (schema `app`,
    # the recorders' in `research`) instead of the SQLite file at db_path, whose folder still holds logs, the pid and
    # stop files. postgresql://user@host:port/database (BTCFLOW_DATABASE_URL); the password in its own setting,
    # normally a secret file (BTCFLOW_DATABASE_PASSWORD_FILE). Never logged.
    database_url: SecretStr | None = None
    database_password: SecretStr | None = None
    # Research scripts' read-only connection (the settleview_research role), same form (BTCFLOW_RESEARCH_DATABASE_URL,
    # BTCFLOW_RESEARCH_DATABASE_PASSWORD). Unset: they read the SQLite files read-only.
    research_database_url: SecretStr | None = None
    research_database_password: SecretStr | None = None
    # Postgres server-side limits for every connection (adapters/sqlite/urls.py PgTimeouts; docs/operations.md
    # "Postgres: timeouts"), milliseconds, 0 = none: a lock, a stuck statement or an abandoned transaction fails within
    # seconds instead of freezing a stop or a stream. Migrations, the SQLite move, the trade archive and research
    # scripts use the long statement timeout, with the same short lock timeout. BTCFLOW_DB_STATEMENT_TIMEOUT_MS,
    # BTCFLOW_DB_LOCK_TIMEOUT_MS, BTCFLOW_DB_IDLE_IN_TRANSACTION_TIMEOUT_MS, BTCFLOW_DB_LONG_STATEMENT_TIMEOUT_MS; and
    # the wait for a free pooled connection, BTCFLOW_DB_POOL_TIMEOUT_S. A migration that hits the lock timeout (the
    # backup's pg_dump holds a lock on every table for its ~30 s) retries with backoff for up to
    # BTCFLOW_DB_MIGRATION_LOCK_WAIT_S seconds (db.migrate) before the start fails.
    db_statement_timeout_ms: int = Field(5_000, ge=0)
    db_lock_timeout_ms: int = Field(2_000, ge=0)
    db_idle_in_transaction_timeout_ms: int = Field(30_000, ge=0)
    db_long_statement_timeout_ms: int = Field(600_000, ge=0)
    db_pool_timeout_s: float = Field(5.0, gt=0)
    db_migration_lock_wait_s: float = Field(120.0, ge=0, le=3_600)
    # Where the research recorders write (polymarket_us.db) and the app reads their freshness (read-only). None = a
    # `research` folder next to the database (BTCFLOW_RESEARCH_DIR).
    research_dir: Path | None = None
    # The app's own rotating log file under `<db dir>/logs` (BTCFLOW_FILE_LOG). Containers turn it off and log to
    # stderr only, where the platform's log collector reads it. (No setting name may end in `_file`: BTCFLOW_X_FILE
    # always means "read X from this file".)
    file_log: bool = True
    # The `httpx` / `httpcore` loggers' level (BTCFLOW_HTTP_CLIENT_LOG_LEVEL). WARNING: at INFO httpx logs one line per
    # request (bootstrap/logs.py); request counts and latencies are OpenTelemetry metrics instead.
    http_client_log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "WARNING"
    prediction_horizons: dict[str, int] = {"1m": 60, "5m": 300, "15m": 900}
    tie_policy: Literal["kalshi", "push"] = "kalshi"
    backfill_days: int = 30
    # Coinbase trade rows older than this move from the database to `<db>-archive.db` in the daily maintenance job
    # (bootstrap/maintenance.py; nothing is deleted). BTCFLOW_TRADES_RETENTION_DAYS.
    trades_retention_days: int = Field(60, ge=1)
    decision_lag_ms: int = 2000

    kalshi_base_url: str = "https://api.elections.kalshi.com/trade-api/v2"
    kalshi_series: str = market("kalshi_15m").series
    kalshi_fee_rate: float = 0.07
    # Read-only account link (R29): API key id + path to the RSA private key file (Play's Kalshi balance). Never stored
    # in the DB.
    kalshi_key_id: str | None = None
    kalshi_private_key_path: Path | None = None

    # Human-confirmed trading. Separate keys per environment; never the read-only kalshi_key_*.
    trading_enabled: bool = False  # master switch for PRODUCTION only
    trading_demo_key_id: str | None = None  # BTCFLOW_TRADING_DEMO_KEY_ID
    trading_demo_key_path: Path | None = None  # BTCFLOW_TRADING_DEMO_KEY_PATH (Ed25519 PEM ok)
    trading_prod_key_id: str | None = None  # BTCFLOW_TRADING_PROD_KEY_ID
    trading_prod_key_path: Path | None = None  # BTCFLOW_TRADING_PROD_KEY_PATH
    # Polymarket US API (Ed25519): key id (UUID) and the Base64 secret. Never logged or returned by any route.
    polymarket_key_id: str | None = None  # BTCFLOW_POLYMARKET_KEY_ID
    polymarket_secret_key: SecretStr | None = None  # BTCFLOW_POLYMARKET_SECRET_KEY
    trading_max_open_positions: int = 1
    # The phone Play screens' limits are NOT here: they are the user's own setting in the app DB (GET / POST
    # /api/play/limits, default no limits; application/play_limits.py), plus the venue's cash in production.
    # Every trading gateway's ceiling on one buy's notional (count x limit, $): a bug guard so a wrong count or price
    # can never send an absurd order, not a trading limit (BTCFLOW_TRADING_ORDER_HARD_CEILING).
    trading_order_hard_ceiling: float = Field(10_000.0, gt=0)
    # Kalshi BTC perpetuals (the Perps tab; docs/plans/2026-10-29-2356-feat-kalshi-perps-tab-plan.md). Off until the user
    # turns it on. They trade from ONE perps subaccount, the primary account 0 by default: Kalshi refuses numbered
    # subaccount creation below its "advanced" API tier (2026-10-30), so the whole perps balance backs a position. The
    # perps keys are the trading keys above (demo -> perp_demo, prod -> perp_prod).
    perps_enabled: bool = False  # BTCFLOW_PERPS_ENABLED
    # Agent mode (docs/plans/2026-10-30-1402-feat-agent-mode-mcp-plan.md): off until the user turns it on. The user's own
    # Anthropic API key; never logged, returned by a route or shown in the app (the app says only whether it works).
    agent_enabled: bool = False  # BTCFLOW_AGENT_ENABLED
    anthropic_api_key: SecretStr | None = None  # BTCFLOW_ANTHROPIC_API_KEY
    # The Claude Code CLI the agent's model loop runs (one process per wake, Claude Agent SDK). None = the SDK's bundled
    # CLI, else `claude` on PATH; set it where the SDK bundles none (e.g. Windows ARM64) or to pin a vetted build.
    agent_cli_path: Path | None = None  # BTCFLOW_AGENT_CLI_PATH
    # Agent sessions may run on venues in production (real money) only when this is on. Off by default; the user turns it
    # on for a reviewed live run (BTCFLOW_AGENT_ALLOW_PRODUCTION).
    agent_allow_production: bool = False
    # Multi-agent (docs/plans/2026-10-31-0010-feat-multi-agent-plan.md KD5): at most this many model wakes (Claude Code
    # CLI processes) run at once across every agent session; the rest wait in one queue, urgent first
    # (BTCFLOW_AGENT_WAKE_CONCURRENCY).
    agent_wake_concurrency: int = Field(4, ge=1, le=32)
    # The agent CLI's environment is an allowlist (PATH, HOME, temp dirs, locale, OS basics; harness.PASS_ENV): every
    # other inherited variable is blanked for it. Extra variable NAMES it may keep (a proxy, a CA bundle),
    # comma-separated; ANTHROPIC_* / CLAUDE_* / BTCFLOW_* are refused (BTCFLOW_AGENT_ENV_PASS).
    agent_env_pass: str = ""

    # Telegram remote (U11): the bot's token from @BotFather. None = the bot is off (not an error). Never logged or
    # returned; only the adapter reads it (adapters/telegram/bot.py, long polling, no inbound port).
    telegram_bot_token: SecretStr | None = None  # BTCFLOW_TELEGRAM_BOT_TOKEN
    # The bot's @username, shown in the pairing instruction ("Send /start CODE to @name"). Optional: unset, the app
    # learns it from Telegram (getMe) when the bot starts, and until then the UI says "your bot". A leading @ is
    # allowed; anything that is not a Telegram bot username is an error, never ignored.
    telegram_bot_username: str | None = None  # BTCFLOW_TELEGRAM_BOT_USERNAME
    perps_subaccount: int = Field(0, ge=0, le=63)  # BTCFLOW_PERPS_SUBACCOUNT; validated against Kalshi at startup
    # Market-style entries/exits: an IOC this far through the touch (KTD4; the user may change it per ticket).
    perps_protective_band: float = Field(0.005, gt=0, lt=0.1)  # BTCFLOW_PERPS_PROTECTIVE_BAND
    # Distance-to-liquidation warning bands (fraction of the mark; display only).
    perps_warn_amber: float = Field(0.05, gt=0, lt=1)  # BTCFLOW_PERPS_WARN_AMBER
    perps_warn_red: float = Field(0.02, gt=0, lt=1)  # BTCFLOW_PERPS_WARN_RED
    # The margin client's ceiling on one entry's notional (count x price, $): a bug guard sized for leveraged notional,
    # not a trading limit (Kalshi's own BTC notional risk limit on this account is $500).
    perps_order_hard_ceiling: float = Field(2_000.0, gt=0)  # BTCFLOW_PERPS_ORDER_HARD_CEILING
    trading_stop_slippage: float = 0.05
    trading_approval_ttl_s: int = 10
    trading_presence_timeout_s: int = 180
    # Live feed for trading: Kalshi WebSocket (shadow: real-book polling) as the primary source of order/fill
    # updates and stop-monitor prices; REST polling always runs as the backup. The same switch runs the production
    # order-book stream behind the Coach's live Kalshi prices (fallback: the REST order book). False = polling only.
    trading_stream: bool = True  # BTCFLOW_TRADING_STREAM
    # The Polymarket shadow exchange's simulated latency: an order goes live / a cancel takes effect this long after it
    # is sent (the names are kept from the retired strategy lab, so existing .env files keep working).
    modes_shadow_place_latency_ms: int = Field(150, ge=0)
    modes_shadow_cancel_latency_ms: int = Field(150, ge=0)

    max_active_per_horizon: int = 5

    api_host: str = "127.0.0.1"
    api_port: int = 8000
    # Phone access (adapters/http/access.py): set BTCFLOW_API_HOST=0.0.0.0 to listen on the home LAN. Other devices are
    # refused until a PIN is set on the PC (the Phone access card; stored hashed in the database, never here), then
    # must sign in with it. No HTTPS: home LAN / VPN only, never port-forward it to the internet.
    access_session_hours: float = Field(12.0, gt=0, le=24 * 30)  # BTCFLOW_ACCESS_SESSION_HOURS
    # Extra Host names the dashboard answers to, comma-separated (a VPN DNS name, e.g. "pc.tailnet.example"; the port
    # is added unless given). 127.0.0.1, localhost, [::1] and this machine's own addresses are always allowed; any
    # other Host gets 400, which stops DNS rebinding (BTCFLOW_ALLOWED_HOSTS).
    allowed_hosts: str = ""
    # Without a PC listener (a native install): extra client IPs that may pair as this PC on the main port,
    # comma-separated (BTCFLOW_LOCAL_CLIENTS). Default: loopback only. Being the PC always needs the paired browser's
    # cookie too (`ops pair-pc`, adapters/http/access.py). A non-loopback entry logs a WARNING at startup.
    local_clients: str = ""
    # The PC listener (BTCFLOW_PC_PORT; unset = none): a second listener in the same process, same app, and the only
    # place where a browser can pair as this PC and its PC cookie counts (the PC = this listener AND the cookie, never
    # the port alone). For Docker: compose publishes it on the host's 127.0.0.1 only. Must differ from api_port. It
    # binds BTCFLOW_PC_HOST (default 127.0.0.1; compose sets 0.0.0.0 inside the container so the 127.0.0.1 port
    # publish reaches it). Never expose it in the cloud.
    pc_port: int | None = Field(None, ge=1, le=65535)
    pc_host: str | None = None
    # The agent listener (BTCFLOW_AGENT_PORT; unset = none): external agents' tool API (/api/agent/tools,
    # /api/agent/call/{tool}) and nothing else; its requests are never the PC or a person, and every call needs a live
    # external session's token. It binds BTCFLOW_AGENT_HOST (default 127.0.0.1; compose: 0.0.0.0 inside the container).
    agent_port: int | None = Field(None, ge=1, le=65535)
    agent_host: str | None = None
    # The address phones open (BTCFLOW_PUBLIC_URL, e.g. http://192.168.1.10:8000): shown on the Phone access card and
    # added to the allowed Hosts. In a container the app cannot see the host's LAN address, so set it there.
    public_url: str = ""
    workers: int = 1

    _horizons: tuple[Horizon, ...] = ()

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings, dotenv_settings,
                                   file_secret_settings):
        return init_settings, env_settings, FileValueSource(settings_cls), dotenv_settings, file_secret_settings

    @model_validator(mode="after")
    def _validate(self) -> Settings:
        if self.workers != 1:
            raise ValueError("workers must be 1: the app has a single writer task and in-memory trading state (KTD3)")
        if self.perps_warn_red >= self.perps_warn_amber:
            raise ValueError("perps_warn_red must be below perps_warn_amber (red is the nearer band)")
        if self.pc_port is not None and self.pc_port == self.api_port:
            raise ValueError(f"BTCFLOW_PC_PORT must differ from BTCFLOW_API_PORT (both are {self.api_port})")
        if self.pc_host is not None and self.pc_port is None:
            raise ValueError("BTCFLOW_PC_HOST is set without BTCFLOW_PC_PORT")
        if self.agent_port is not None and self.agent_port in (self.api_port, self.pc_port):
            raise ValueError(f"BTCFLOW_AGENT_PORT must differ from BTCFLOW_API_PORT and BTCFLOW_PC_PORT "
                             f"(it is {self.agent_port})")
        if self.agent_host is not None and self.agent_port is None:
            raise ValueError("BTCFLOW_AGENT_HOST is set without BTCFLOW_AGENT_PORT")
        if self.public_url.strip():
            parts = urlsplit(self.public_url.strip())
            if (parts.scheme not in ("http", "https") or not parts.hostname or parts.path not in ("", "/")
                    or parts.query or parts.fragment):
                raise ValueError("BTCFLOW_PUBLIC_URL must be an http(s) address with no path, e.g. "
                                 "http://192.168.1.10:8000")
        if self.telegram_bot_username is not None:
            name = self.telegram_bot_username.strip().removeprefix("@")
            if not name:
                object.__setattr__(self, "telegram_bot_username", None)  # empty = not set, like an empty token
            elif not BOT_USERNAME.fullmatch(name):
                raise ValueError("BTCFLOW_TELEGRAM_BOT_USERNAME must be a Telegram bot username (5-32 letters, digits "
                                 "or _, starting with a letter and ending in 'bot'), e.g. @my_settleview_bot")
            else:
                object.__setattr__(self, "telegram_bot_username", name)
        object.__setattr__(self, "_horizons", parse_horizons(self.prediction_horizons))
        for name in ("database", "research_database"):  # parse now: a bad URL stops the start, naming the setting
            self._url(name)
        return self

    def _url(self, name: str):
        url, pw = getattr(self, f"{name}_url"), getattr(self, f"{name}_password")
        if url is None or not url.get_secret_value().strip():
            if pw is not None:
                raise ValueError(f"BTCFLOW_{name.upper()}_PASSWORD is set without BTCFLOW_{name.upper()}_URL")
            return None
        return postgres_url(url.get_secret_value(), pw.get_secret_value() if pw is not None else None,
                            setting=f"BTCFLOW_{name.upper()}_URL")

    @property
    def database(self) -> DbTarget:
        """Where the app's data lives: the Postgres URL (BTCFLOW_DATABASE_URL), else the SQLite file (BTCFLOW_DB_PATH)."""
        url = self._url("database")
        return url if url is not None else Path(self.db_path)

    @property
    def pg_timeouts(self) -> PgTimeouts:
        """The Postgres connection limits (db.pg_engine)."""
        return PgTimeouts(statement_ms=self.db_statement_timeout_ms, lock_ms=self.db_lock_timeout_ms,
                          idle_in_transaction_ms=self.db_idle_in_transaction_timeout_ms,
                          migration_statement_ms=self.db_long_statement_timeout_ms,
                          migration_lock_wait_s=self.db_migration_lock_wait_s, pool_timeout_s=self.db_pool_timeout_s)

    @property
    def research_database(self) -> DbTarget | None:
        """The research scripts' read-only Postgres connection, or None (they read SQLite files)."""
        return self._url("research_database")

    @property
    def data_dir(self) -> Path:
        """The app's local folder (logs, pid and stop files, SQLite's backups): the folder of BTCFLOW_DB_PATH."""
        return Path(self.db_path).parent

    @property
    def research_path(self) -> Path:
        """The research data folder (BTCFLOW_RESEARCH_DIR, else `<db dir>/research`)."""
        return self.research_dir if self.research_dir is not None else Path(self.db_path).parent / "research"

    @property
    def pc_bind_host(self) -> str:
        """The PC listener's bind address: BTCFLOW_PC_HOST, else loopback."""
        return self.pc_host if self.pc_host is not None else "127.0.0.1"

    @property
    def agent_bind_host(self) -> str:
        """The agent listener's bind address: BTCFLOW_AGENT_HOST, else loopback."""
        return self.agent_host if self.agent_host is not None else "127.0.0.1"

    @property
    def horizons(self) -> tuple[Horizon, ...]:
        return self._horizons
