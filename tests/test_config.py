import pytest

from btc_signal_engine.bootstrap.config import Settings
from btc_signal_engine.domain.horizons.config import Horizon


def test_defaults_give_the_three_initial_horizons():
    s = Settings(_env_file=None)
    assert s.horizons == (Horizon(60, "1m"), Horizon(300, "5m"), Horizon(900, "15m"))


def test_horizons_env_parses_custom_set(monkeypatch):
    monkeypatch.setenv("BTCFLOW_PREDICTION_HORIZONS", '{"1m":60,"2m":120}')
    s = Settings(_env_file=None)
    assert [h.seconds for h in s.horizons] == [60, 120]


def test_horizon_that_is_not_whole_minutes_is_rejected(monkeypatch):
    monkeypatch.setenv("BTCFLOW_PREDICTION_HORIZONS", '{"90s":90}')
    with pytest.raises(ValueError, match="multiple of 60"):
        Settings(_env_file=None)


def test_unknown_tie_policy_fails(monkeypatch):
    monkeypatch.setenv("BTCFLOW_TIE_POLICY", "unknown")
    with pytest.raises(ValueError):
        Settings(_env_file=None)


def test_more_than_one_worker_is_refused(monkeypatch):
    monkeypatch.setenv("BTCFLOW_WORKERS", "2")
    with pytest.raises(ValueError, match="single writer"):
        Settings(_env_file=None)


def test_polymarket_shadow_latency_defaults_and_bad_values(monkeypatch):
    for k in ("BTCFLOW_MODES_SHADOW_PLACE_LATENCY_MS", "BTCFLOW_MODES_SHADOW_CANCEL_LATENCY_MS"):
        monkeypatch.delenv(k, raising=False)
    s = Settings(_env_file=None)
    assert (s.modes_shadow_place_latency_ms, s.modes_shadow_cancel_latency_ms) == (150, 150)
    monkeypatch.setenv("BTCFLOW_MODES_SHADOW_PLACE_LATENCY_MS", "-5")
    with pytest.raises(ValueError):
        Settings(_env_file=None)


def test_retired_settings_are_gone():
    s = Settings(_env_file=None)
    for name in ("bots_enabled", "research_enabled", "modes_lab", "modes_lab_enabled", "live_test_override",
                 "micro_gate_min_windows", "promotion_min_live", "challenger_search_hour_utc",
                 "trading_max_stake_per_trade", "trading_max_dollars_per_day", "payout_mode"):
        assert not hasattr(s, name), name


def test_perps_settings_default_off_and_refuse_bad_values(monkeypatch):
    s = Settings(_env_file=None)
    assert (s.perps_enabled, s.perps_subaccount, s.perps_protective_band) == (False, 0, 0.005)
    assert (s.perps_warn_amber, s.perps_warn_red, s.perps_order_hard_ceiling) == (0.05, 0.02, 2000.0)
    for key, bad in (("BTCFLOW_PERPS_SUBACCOUNT", "64"), ("BTCFLOW_PERPS_PROTECTIVE_BAND", "0"),
                     ("BTCFLOW_PERPS_WARN_RED", "0.06")):
        monkeypatch.setenv(key, bad)
        with pytest.raises(ValueError):
            Settings(_env_file=None)
        monkeypatch.delenv(key)


def test_agent_settings_default_off_and_key_stays_secret(monkeypatch):
    s = Settings(_env_file=None)
    assert (s.agent_enabled, s.anthropic_api_key) == (False, None)
    monkeypatch.setenv("BTCFLOW_AGENT_ENABLED", "true")
    monkeypatch.setenv("BTCFLOW_ANTHROPIC_API_KEY", "sk-ant-test-0123456789")
    s = Settings(_env_file=None)
    assert s.agent_enabled is True
    assert s.anthropic_api_key.get_secret_value() == "sk-ant-test-0123456789"
    assert "0123456789" not in repr(s) and "0123456789" not in str(s.anthropic_api_key)
    monkeypatch.setenv("BTCFLOW_AGENT_ENABLED", "maybe")
    with pytest.raises(ValueError):
        Settings(_env_file=None)


def test_a_setting_can_come_from_a_file(monkeypatch, tmp_path):
    """BTCFLOW_<NAME>_FILE (Docker / Swarm secrets): the file's content is the value, one trailing newline dropped."""
    f = tmp_path / "token"
    f.write_text("abc:def\n", encoding="utf-8")
    monkeypatch.delenv("BTCFLOW_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("BTCFLOW_TELEGRAM_BOT_TOKEN_FILE", str(f))
    s = Settings(_env_file=None)
    assert s.telegram_bot_token.get_secret_value() == "abc:def"


def test_a_file_setting_and_the_variable_together_are_refused(monkeypatch, tmp_path):
    f = tmp_path / "id"
    f.write_text("from-file", encoding="utf-8")
    monkeypatch.setenv("BTCFLOW_POLYMARKET_KEY_ID", "from-env")
    monkeypatch.setenv("BTCFLOW_POLYMARKET_KEY_ID_FILE", str(f))
    with pytest.raises(ValueError, match="both BTCFLOW_POLYMARKET_KEY_ID and BTCFLOW_POLYMARKET_KEY_ID_FILE") as e:
        Settings(_env_file=None)
    assert "from-file" not in str(e.value) and "from-env" not in str(e.value)  # never a value


def test_an_empty_secret_file_means_not_configured(monkeypatch, tmp_path):
    """compose mounts every secret file, used or not (make_secrets.py writes the unused ones empty)."""
    empty = tmp_path / "telegram_bot_token"
    empty.write_text("\n", encoding="utf-8")
    monkeypatch.delenv("BTCFLOW_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("BTCFLOW_TELEGRAM_BOT_TOKEN_FILE", str(empty))
    assert Settings(_env_file=None).telegram_bot_token is None
    monkeypatch.setenv("BTCFLOW_TELEGRAM_BOT_TOKEN", "from-env")  # before the cut-over: the variable still works
    assert Settings(_env_file=None).telegram_bot_token.get_secret_value() == "from-env"


def test_a_missing_file_or_unknown_name_is_an_error(monkeypatch, tmp_path):
    monkeypatch.setenv("BTCFLOW_ANTHROPIC_API_KEY_FILE", str(tmp_path / "nope"))
    with pytest.raises(ValueError, match="BTCFLOW_ANTHROPIC_API_KEY_FILE: the file cannot be read"):
        Settings(_env_file=None)
    monkeypatch.delenv("BTCFLOW_ANTHROPIC_API_KEY_FILE")
    monkeypatch.setenv("BTCFLOW_NO_SUCH_SETTING_FILE", str(tmp_path))
    with pytest.raises(ValueError, match="names no setting"):
        Settings(_env_file=None)


def test_research_dir_defaults_next_to_the_database(tmp_path):
    assert Settings(_env_file=None, db_path=tmp_path / "d" / "btcflow.db").research_path == tmp_path / "d" / "research"
    assert Settings(_env_file=None, research_dir=tmp_path / "r").research_path == tmp_path / "r"


def test_without_a_database_url_the_app_uses_the_sqlite_file(tmp_path):
    s = Settings(_env_file=None, db_path=tmp_path / "btcflow.db")
    assert s.database == tmp_path / "btcflow.db" and s.data_dir == tmp_path and s.research_database is None


def test_a_database_url_and_its_password_file_make_the_postgres_url(monkeypatch, tmp_path):
    (tmp_path / "url").write_text("postgresql://settleview_app@postgres:5432/settleview\n", encoding="utf-8")
    (tmp_path / "pw").write_text("s3cret\n", encoding="utf-8")
    monkeypatch.setenv("BTCFLOW_DATABASE_URL_FILE", str(tmp_path / "url"))
    monkeypatch.setenv("BTCFLOW_DATABASE_PASSWORD_FILE", str(tmp_path / "pw"))
    url = Settings(_env_file=None).database
    assert url.drivername == "postgresql+psycopg" and url.password == "s3cret" and url.database == "settleview"
    assert "s3cret" not in url.render_as_string(hide_password=True)


@pytest.mark.parametrize("raw, why", [
    ("sqlite:///data/x.db", "must be a postgresql://"),
    ("mysql://u:hunter2@h/d", "must be a postgresql://"),
    ("postgresql://u@/d", "needs a host"),
    ("postgresql://h:5432/d", "needs a user"),
    ("not a url hunter2", "not a database URL"),
])
def test_a_bad_database_url_stops_the_start_without_echoing_it(raw, why):
    with pytest.raises(ValueError, match=why) as e:
        Settings(_env_file=None, database_url=raw)
    assert "hunter2" not in str(e.value) and raw not in str(e.value)


def test_a_password_in_both_places_or_without_a_url_is_refused():
    with pytest.raises(ValueError, match="already holds a password") as e:
        Settings(_env_file=None, database_url="postgresql://u:inline@h/d", database_password="other")
    assert "inline" not in str(e.value) and "other" not in str(e.value)
    with pytest.raises(ValueError, match="PASSWORD is set without"):
        Settings(_env_file=None, database_password="x")


def test_no_setting_name_ends_in_file():
    """BTCFLOW_<NAME>_FILE always means "read <name> from this file": a setting called `x_file` could never be set
    from the environment (BTCFLOW_LOG_FILE once failed every container start of `ops`)."""
    assert [n for n in Settings.model_fields if n.lower().endswith("_file")] == []


def test_the_file_log_switch_and_the_old_name(monkeypatch, tmp_path):
    monkeypatch.setenv("BTCFLOW_FILE_LOG", "false")
    assert Settings(_env_file=None).file_log is False
    monkeypatch.delenv("BTCFLOW_FILE_LOG")
    monkeypatch.setenv("BTCFLOW_LOG_FILE", "false")
    with pytest.raises(ValueError, match="BTCFLOW_LOG_FILE was renamed BTCFLOW_FILE_LOG"):
        Settings(_env_file=None)


@pytest.fixture
def no_port_env(monkeypatch):
    for k in ("BTCFLOW_API_PORT", "BTCFLOW_PC_PORT", "BTCFLOW_PC_HOST", "BTCFLOW_AGENT_PORT", "BTCFLOW_AGENT_HOST",
              "BTCFLOW_PUBLIC_URL"):
        monkeypatch.delenv(k, raising=False)


def test_the_pc_port_is_off_by_default_and_binds_loopback_when_set(no_port_env, monkeypatch):
    s = Settings(_env_file=None)
    assert s.pc_port is None and s.pc_host is None
    monkeypatch.setenv("BTCFLOW_PC_PORT", "8001")
    s = Settings(_env_file=None)
    assert s.pc_port == 8001 and s.pc_bind_host == "127.0.0.1"
    monkeypatch.setenv("BTCFLOW_PC_HOST", "0.0.0.0")
    assert Settings(_env_file=None).pc_bind_host == "0.0.0.0"


def test_a_pc_port_equal_to_the_api_port_or_bad_is_refused(no_port_env, monkeypatch):
    monkeypatch.setenv("BTCFLOW_PC_PORT", "8000")
    with pytest.raises(ValueError, match="BTCFLOW_PC_PORT must differ from BTCFLOW_API_PORT"):
        Settings(_env_file=None)
    monkeypatch.setenv("BTCFLOW_API_PORT", "9000")
    monkeypatch.setenv("BTCFLOW_PC_PORT", "9000")
    with pytest.raises(ValueError, match="must differ"):
        Settings(_env_file=None)
    for bad in ("0", "70000", "eight"):
        monkeypatch.setenv("BTCFLOW_PC_PORT", bad)
        with pytest.raises(ValueError):
            Settings(_env_file=None)


def test_a_pc_host_without_a_pc_port_is_refused(no_port_env, monkeypatch):
    monkeypatch.setenv("BTCFLOW_PC_HOST", "0.0.0.0")
    with pytest.raises(ValueError, match="BTCFLOW_PC_HOST is set without BTCFLOW_PC_PORT"):
        Settings(_env_file=None)


def test_the_agent_port_is_off_by_default_differs_from_the_others_and_binds_loopback(no_port_env, monkeypatch):
    s = Settings(_env_file=None)
    assert s.agent_port is None and s.agent_host is None and s.public_url == ""
    monkeypatch.setenv("BTCFLOW_PC_PORT", "8001")
    monkeypatch.setenv("BTCFLOW_AGENT_PORT", "8002")
    s = Settings(_env_file=None)
    assert s.agent_port == 8002 and s.agent_bind_host == "127.0.0.1"
    for clash in ("8000", "8001"):
        monkeypatch.setenv("BTCFLOW_AGENT_PORT", clash)
        with pytest.raises(ValueError, match="BTCFLOW_AGENT_PORT must differ"):
            Settings(_env_file=None)
    monkeypatch.delenv("BTCFLOW_AGENT_PORT")
    monkeypatch.setenv("BTCFLOW_AGENT_HOST", "0.0.0.0")
    with pytest.raises(ValueError, match="BTCFLOW_AGENT_HOST is set without BTCFLOW_AGENT_PORT"):
        Settings(_env_file=None)


def test_the_public_url_must_be_a_plain_http_address(no_port_env, monkeypatch):
    for good in ("http://192.168.1.10:8000", "https://pc.tailnet.example/"):
        monkeypatch.setenv("BTCFLOW_PUBLIC_URL", good)
        assert Settings(_env_file=None).public_url == good
    for bad in ("192.168.1.10:8000", "ftp://x", "http://x/play", "http://x/?a=1", "http://"):
        monkeypatch.setenv("BTCFLOW_PUBLIC_URL", bad)
        with pytest.raises(ValueError, match="BTCFLOW_PUBLIC_URL"):
            Settings(_env_file=None)
