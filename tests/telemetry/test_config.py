"""OpenTelemetry configuration (bootstrap/telemetry.plan / setup): inert without an endpoint, standard OTEL_* only, the
headers file, and every bad value an error naming the variable (never its value); nothing falls back silently."""

from __future__ import annotations

import logging

import pytest

from btc_signal_engine.bootstrap import recorders, telemetry
from btc_signal_engine.bootstrap.config import Settings
from btc_signal_engine.bootstrap.logs import EX_CONFIG
from btc_signal_engine.bootstrap.telemetry import TelemetryConfigError, plan

EP = {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://openobserve:5080/api/default"}


def test_no_endpoint_means_nothing_is_installed():
    assert not plan({}).enabled
    root = logging.getLogger()
    before = list(root.handlers)
    tel = telemetry.setup("settleview-app", environ={})
    assert not tel.enabled and tel._providers == [] and tel._undo == []
    assert root.handlers == before
    tel.bind_runtime(object())  # a no-op, whatever it is handed
    tel.instrument_app(object())
    tel.shutdown()


def test_an_endpoint_turns_every_signal_on_and_the_sdk_switch_turns_them_off():
    assert plan(EP).exporters == {"traces": "otlp", "metrics": "otlp", "logs": "otlp"}
    assert not plan({**EP, "OTEL_SDK_DISABLED": "true"}).enabled
    p = plan({"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://x/v1/traces"})
    assert p.exporters == {"traces": "otlp", "metrics": "none", "logs": "none"}
    p = plan({**EP, "OTEL_LOGS_EXPORTER": "none", "OTEL_METRICS_EXPORTER": "console"})
    assert p.exporters == {"traces": "otlp", "metrics": "console", "logs": "none"}


@pytest.mark.parametrize("env, var", [
    ({**EP, "OTEL_TRACES_EXPORTER": "zipkin"}, "OTEL_TRACES_EXPORTER"),
    ({**EP, "OTEL_METRICS_EXPORTER": "prometheus"}, "OTEL_METRICS_EXPORTER"),
    ({**EP, "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc"}, "OTEL_EXPORTER_OTLP_PROTOCOL"),
    ({**EP, "OTEL_EXPORTER_OTLP_LOGS_PROTOCOL": "http/json"}, "OTEL_EXPORTER_OTLP_LOGS_PROTOCOL"),
    ({**EP, "OTEL_TRACES_SAMPLER_ARG": "2"}, "OTEL_TRACES_SAMPLER_ARG"),
    ({**EP, "OTEL_TRACES_SAMPLER_ARG": "lots"}, "OTEL_TRACES_SAMPLER_ARG"),
])
def test_a_bad_value_is_an_error_naming_the_variable(env, var):
    with pytest.raises(TelemetryConfigError, match=var):
        plan(env)


def test_the_headers_file(tmp_path):
    f = tmp_path / "otlp_headers"
    f.write_text("Authorization=Basic%20YWRtaW46c2VjcmV0LXZhbHVl\nstream-name=default\n", encoding="utf-8")
    p = plan({**EP, "OTEL_EXPORTER_OTLP_HEADERS_FILE": str(f)})
    assert p.headers == {"authorization": "Basic YWRtaW46c2VjcmV0LXZhbHVl", "stream-name": "default"}
    with pytest.raises(TelemetryConfigError, match="both OTEL_EXPORTER_OTLP_HEADERS and"):
        plan({**EP, "OTEL_EXPORTER_OTLP_HEADERS_FILE": str(f), "OTEL_EXPORTER_OTLP_HEADERS": "a=b"})
    with pytest.raises(TelemetryConfigError, match="OTEL_EXPORTER_OTLP_HEADERS_FILE: the file cannot be read") as e:
        plan({**EP, "OTEL_EXPORTER_OTLP_HEADERS_FILE": str(tmp_path / "missing")})
    assert "missing" not in str(e.value)
    bad = tmp_path / "bad"
    bad.write_text("Authorization Basic xyz", encoding="utf-8")
    with pytest.raises(TelemetryConfigError, match="key=value") as e:
        plan({**EP, "OTEL_EXPORTER_OTLP_HEADERS_FILE": str(bad)})
    assert "xyz" not in str(e.value)
    # no endpoint: the file is never read (compose mounts it whether or not telemetry is on)
    assert plan({"OTEL_EXPORTER_OTLP_HEADERS_FILE": str(tmp_path / "missing")}).headers is None


def test_secret_values_reads_every_secret_setting():
    s = Settings(_env_file=None, telegram_bot_token="123456:ABCdefGHIjklMNOpqrSTUvwxYZ0123456789",
                 anthropic_api_key="sk-ant-x" + "y" * 20, polymarket_key_id="kid-123456",
                 database_url="postgresql://u@h/d", database_password="pw-123456")
    vals = telemetry.secret_values(s)
    assert {"123456:ABCdefGHIjklMNOpqrSTUvwxYZ0123456789", "sk-ant-x" + "y" * 20, "kid-123456",
            "pw-123456"} <= set(vals)


def test_a_recorder_with_a_bad_otel_setting_exits_78_before_any_network(monkeypatch, tmp_path):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:9")
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "zipkin")
    s = Settings(_env_file=None, db_path=tmp_path / "x.db")
    assert recorders.main(["pm-settle"], settings=s) == EX_CONFIG


def test_service_version_names_the_commit(monkeypatch):
    monkeypatch.setenv("SETTLEVIEW_GIT_SHA", "0123456789abcdef0123456789abcdef01234567")
    assert telemetry.service_version().endswith("+0123456789ab")
    monkeypatch.setenv("SETTLEVIEW_GIT_SHA", "unknown")
    assert "+" not in telemetry.service_version()


def test_make_secrets_writes_the_openobserve_password_and_an_empty_otlp_header(tmp_path, capsys):
    """The services' header is the ingest-only token (deploy/openobserve/ingest_token.py,
    tests/deploy/test_ingest_token.py), never derived from the root password: make_secrets.py leaves it empty."""
    import importlib.util
    import re
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "make_secrets", Path(__file__).resolve().parents[2] / "deploy" / "postgres" / "make_secrets.py")
    ms = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ms)
    assert ms.main(["make_secrets", str(tmp_path)]) == 0
    pw = (tmp_path / "openobserve.env").read_text(encoding="ascii").strip().removeprefix("ZO_ROOT_USER_PASSWORD=")
    assert re.search(r"[A-Z]", pw) and re.search(r"[a-z]", pw) and re.search(r"\d", pw) and re.search(r"[^\w]", pw)
    assert plan({**EP, "OTEL_EXPORTER_OTLP_HEADERS_FILE": str(tmp_path / "otlp_headers")}).headers == {}
    first = {p.name: p.read_text() for p in tmp_path.iterdir()}
    assert ms.main(["make_secrets", str(tmp_path)]) == 0  # a second run keeps every file
    assert {p.name: p.read_text() for p in tmp_path.iterdir()} == first
    out = capsys.readouterr().out
    assert pw not in out  # file names only, never a value
