"""One OpenTelemetry pipeline for the whole tests/telemetry package (the SDK's global providers can be set once per
process): bootstrap/telemetry.setup with in-memory exporters in place of the OTLP ones, so everything the tests see went
through the same scrubbing the real exporters get. Header capture is switched on (as a careless operator might) to prove
the scrubber still removes credentials, and the background sampling ratio is 0 so the tests are deterministic."""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from typing import Any

import pytest

pytest.importorskip("opentelemetry.sdk", reason="the `otel` extra is not installed")

from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter  # noqa: E402
from opentelemetry.sdk.metrics.export import MetricExporter, MetricExportResult  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402

from btc_signal_engine.bootstrap import telemetry  # noqa: E402

# Secrets planted by the tests: none may ever reach an exporter.
TG_TOKEN = "7123456789:AAHfakeTelegramTokenValue_abcdefghijk12"
ANTHROPIC_KEY = "sk-ant-api03-FAKEfakeFAKEfake0123456789abcdefABCDEF"
DB_PASSWORD = "Wc3n_x9QpL2vR7tY8uZa1bKd"  # gitleaks:allow - planted fake
BEARER = "Bearer eyJhbGciOiJIUzI1NiJ9.ZmFrZQ.c2lnbmF0dXJlVmFsdWU"
COOKIE = "settleview_session=s3ss10nV4lu3Abc"
QUERY_SECRET = "q5ecr3tQueryValue"
SIGNATURE = "kH3s9+Qm2Lx7Vb1Nc8Zr5Tw0Py4Jd6Ga/Fe3Ui9Oq2Ws7Ex1Rc5Tv8Yb2Nm4Kl6Hj0Gf3Dd=="
PLANTED = (TG_TOKEN, ANTHROPIC_KEY, DB_PASSWORD, BEARER.split()[1], COOKIE.split("=")[1], QUERY_SECRET, SIGNATURE)
# Shapes of a credential, whatever its value.
TOKEN_SHAPES = (re.compile(r"\d{5,}:[A-Za-z0-9_-]{30,}"), re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}"),
                re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{8,}"))

ENV = {"OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_CLIENT_REQUEST": "Authorization,Cookie,X-Api-Key,Content-Type",
       "OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_REQUEST": "Authorization,Cookie,X-Api-Key,Content-Type",
       "OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_CLIENT_RESPONSE": "Set-Cookie,Content-Type"}


class CaptureMetrics(MetricExporter):
    def __init__(self) -> None:
        super().__init__()
        self.batches: list[Any] = []

    def export(self, metrics_data, timeout_millis: float = 10_000, **kwargs) -> MetricExportResult:
        self.batches.append(metrics_data)
        return MetricExportResult.SUCCESS

    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        return True

    def shutdown(self, timeout_millis: float = 30_000, **kwargs) -> None:
        pass

    def points(self) -> Iterator[tuple[str, Any]]:
        for data in self.batches:
            for rm in data.resource_metrics:
                for sm in rm.scope_metrics:
                    for m in sm.metrics:
                        for p in m.data.data_points:
                            yield m.name, p


class Pipeline:
    def __init__(self, tel, spans: InMemorySpanExporter, logs: InMemoryLogRecordExporter, metrics: CaptureMetrics):
        self.tel, self.span_ex, self.log_ex, self.metric_ex = tel, spans, logs, metrics

    def spans(self):
        return list(self.span_ex.get_finished_spans())

    def logs(self):
        return [r.log_record for r in self.log_ex.get_finished_logs()]

    def collect(self) -> list[tuple[str, Any]]:
        self.metric_ex.batches.clear()
        self.tel.force_flush()
        return list(self.metric_ex.points())

    def clear(self) -> None:
        self.span_ex.clear()
        self.log_ex.clear()
        self.metric_ex.batches.clear()

    def strings(self) -> Iterator[str]:
        """Every string an exporter received: span names, attributes, events, links, statuses, resources, log
        bodies and attributes, metric names and point attributes."""
        def walk(v):
            if isinstance(v, str):
                yield v
            elif isinstance(v, bytes):
                yield v.decode("utf-8", "replace")
            elif isinstance(v, dict) or hasattr(v, "items"):
                for k, x in v.items():
                    yield str(k)
                    yield from walk(x)
            elif isinstance(v, (list, tuple)):
                for x in v:
                    yield from walk(x)
        for s in self.spans():
            yield s.name
            yield from walk(dict(s.attributes or {}))
            yield from walk(dict(s.resource.attributes))
            yield s.status.description or ""
            for e in s.events:
                yield e.name
                yield from walk(dict(e.attributes or {}))
            for lk in s.links:
                yield from walk(dict(lk.attributes or {}))
        for r in self.logs():
            yield from walk(r.body)
            yield from walk(dict(r.attributes or {}))
        for name, p in self.metric_ex.points():
            yield name
            yield from walk(dict(p.attributes or {}))

    def assert_clean(self, extra: tuple[str, ...] = ()) -> None:
        for text in self.strings():
            for secret in (*PLANTED, *extra):
                assert secret not in text, f"a planted secret reached an exporter: {text[:200]!r}"
            for shape in TOKEN_SHAPES:
                assert not shape.search(text), f"a token-shaped value reached an exporter: {text[:200]!r}"


@pytest.fixture(scope="package")
def otel() -> Iterator[Pipeline]:
    saved = {k: os.environ.get(k) for k in ENV}
    os.environ.update(ENV)
    spans, logs, metrics = InMemorySpanExporter(), InMemoryLogRecordExporter(), CaptureMetrics()
    tel = telemetry.setup("settleview-test", environ={"OTEL_TRACES_SAMPLER_ARG": "0"},
                          secrets=[DB_PASSWORD, ANTHROPIC_KEY, TG_TOKEN],
                          span_exporter=spans, metric_exporter=metrics, log_exporter=logs)
    assert tel.enabled
    try:
        yield Pipeline(tel, spans, logs, metrics)
    finally:
        tel.shutdown()
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@pytest.fixture
def pipe(otel: Pipeline) -> Iterator[Pipeline]:
    otel.clear()
    yield otel
    otel.assert_clean()  # every test: nothing secret left the process
