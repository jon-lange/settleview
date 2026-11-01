"""deploy/secrets_from_env.py (P0 #6, B4#6): the one-time move of the venue / API secrets from `.env` into the secret
files compose mounts. Dummy values only."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mover = _load("secrets_from_env", "deploy/secrets_from_env.py")
make = _load("make_secrets", "deploy/postgres/make_secrets.py")

TOKEN = "7712345678:AAHk3xQ9vZ2mW8pL4tR6yU1oI5eN7bC0dFg"
PM = "c2VjcmV0LXBsYW50ZWQtZm9yLXRoZS10ZXN0LW9ubHk="
ENV = (
    "# my settings\n"
    "TZ=America/Chicago\n"
    f"BTCFLOW_TELEGRAM_BOT_TOKEN={TOKEN}\n"
    f'export BTCFLOW_POLYMARKET_SECRET_KEY="{PM}"\n'
    "BTCFLOW_POLYMARKET_KEY_ID=11111111-2222-3333-4444-555555555555 # the key id\n"
    "BTCFLOW_ANTHROPIC_API_KEY=\n"
    "BTCFLOW_TRADING_ENABLED=true\n"
)


def _run(tmp_path, *args) -> tuple[int, str]:
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = mover.main(["--env", str(tmp_path / ".env"), "--secrets", str(tmp_path / "secrets"), *args])
    return code, buf.getvalue()


def test_the_names_match_make_secrets_and_compose():
    assert mover.APP_SECRETS == make.APP_SECRETS
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    for name in mover.APP_SECRETS:
        assert f"file: ./secrets/{name}\n" in compose
        assert f"BTCFLOW_{name.upper()}_FILE: /run/secrets/{name}" in compose


def test_values_move_to_files_and_leave_env_without_printing_them(tmp_path):
    (tmp_path / ".env").write_text(ENV, encoding="utf-8")
    code, out = _run(tmp_path)
    assert code == 0
    s = tmp_path / "secrets"
    assert (s / "telegram_bot_token").read_text(encoding="utf-8") == TOKEN + "\n"
    assert (s / "polymarket_secret_key").read_text(encoding="utf-8") == PM + "\n"  # quotes and `export` handled
    assert (s / "polymarket_key_id").read_text(encoding="utf-8") == "11111111-2222-3333-4444-555555555555\n"
    for empty in ("anthropic_api_key", "kalshi_key_id", "trading_demo_key_id", "trading_prod_key_id"):
        assert (s / empty).read_text(encoding="utf-8") == ""  # not configured, but present for compose
    env = (tmp_path / ".env").read_text(encoding="utf-8")
    assert TOKEN not in env and PM not in env and "55555555" not in env
    assert "TZ=America/Chicago\n" in env and "BTCFLOW_TRADING_ENABLED=true\n" in env and "# my settings" in env
    assert "# BTCFLOW_TELEGRAM_BOT_TOKEN: moved to secrets/telegram_bot_token" in env
    for secret in (TOKEN, PM, "55555555"):
        assert secret not in out
    assert not list(tmp_path.glob(".env.*"))  # no backup copy of the secrets
    # The settings read the moved values back from the files (what compose's *_FILE variables do)
    code2, out2 = _run(tmp_path)  # again: nothing changes
    assert code2 == 0 and "moved" not in out2 and (tmp_path / ".env").read_text(encoding="utf-8") == env


def test_settings_read_the_moved_files(tmp_path, monkeypatch):
    from btc_signal_engine.bootstrap.config import Settings
    (tmp_path / ".env").write_text(ENV, encoding="utf-8")
    _run(tmp_path)
    for name in mover.APP_SECRETS:
        monkeypatch.delenv(f"BTCFLOW_{name.upper()}", raising=False)
        monkeypatch.setenv(f"BTCFLOW_{name.upper()}_FILE", str(tmp_path / "secrets" / name))
    s = Settings(_env_file=None)
    assert s.telegram_bot_token.get_secret_value() == TOKEN
    assert s.polymarket_secret_key.get_secret_value() == PM
    assert s.anthropic_api_key is None and s.kalshi_key_id is None


def test_a_different_value_already_in_a_file_is_a_conflict_left_alone(tmp_path):
    (tmp_path / ".env").write_text(ENV, encoding="utf-8")
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "telegram_bot_token").write_text("0000:other\n", encoding="utf-8")
    code, out = _run(tmp_path)
    assert code == 1 and "CONFLICT BTCFLOW_TELEGRAM_BOT_TOKEN" in out and "0000:other" not in out
    assert TOKEN in (tmp_path / ".env").read_text(encoding="utf-8")  # that line stays
    assert (tmp_path / "secrets" / "telegram_bot_token").read_text(encoding="utf-8") == "0000:other\n"
    assert PM not in (tmp_path / ".env").read_text(encoding="utf-8")  # the others moved


@pytest.mark.parametrize("flag", ["--dry-run", "--keep-env"])
def test_dry_run_and_keep_env_leave_env_as_it_is(tmp_path, flag):
    (tmp_path / ".env").write_text(ENV, encoding="utf-8")
    code, out = _run(tmp_path, flag)
    assert code == 0 and (tmp_path / ".env").read_text(encoding="utf-8") == ENV
    assert (tmp_path / "secrets" / "telegram_bot_token").exists() == (flag == "--keep-env")


def test_make_secrets_creates_the_app_secret_files_empty_and_never_overwrites(tmp_path, capsys):
    root = tmp_path / "secrets"
    root.mkdir()
    (root / "telegram_bot_token").write_text(TOKEN + "\n", encoding="utf-8")
    assert make.main(["make_secrets.py", str(root)]) == 0
    assert (root / "telegram_bot_token").read_text(encoding="utf-8") == TOKEN + "\n"
    assert (root / "anthropic_api_key").read_text(encoding="utf-8") == ""
    assert TOKEN not in capsys.readouterr().out
