"""OpenTelemetry for every service (the app, the recorders): traces, metrics and logs over OTLP/HTTP.

Default-on in code, inert unless configured: with no OTLP endpoint (OTEL_EXPORTER_OTLP_ENDPOINT, or a per-signal
OTEL_EXPORTER_OTLP_{TRACES,METRICS,LOGS}_ENDPOINT) or with OTEL_SDK_DISABLED=true, `setup` installs nothing, patches
nothing and the process logs to stderr as before. Configuration is the standard OTEL_* environment only:
  OTEL_SERVICE_NAME / OTEL_RESOURCE_ATTRIBUTES      who is reporting (default service.name: the caller's)
  OTEL_EXPORTER_OTLP_ENDPOINT (+ per-signal forms)  where to (http/protobuf; OTEL_EXPORTER_OTLP_PROTOCOL must be that)
  OTEL_EXPORTER_OTLP_HEADERS                        headers (none needed for the compose collector)
  OTEL_{TRACES,METRICS,LOGS}_EXPORTER               otlp (default), console or none, per signal
  OTEL_TRACES_SAMPLER / _ARG                        unset: the Settleview sampler below (_ARG = its background ratio)
  OTEL_METRIC_EXPORT_INTERVAL, OTEL_BSP_*, OTEL_BLRP_*, OTEL_EXPORTER_OTLP_TIMEOUT, OTEL_PYTHON_*_EXCLUDED_URLS, ...
An unknown exporter or protocol, or the `otel` extra not installed while an endpoint is set, is an error: nothing falls
back silently.

The services export to the OpenTelemetry Collector (compose service `otel-collector`, deploy/otel-collector/), which
redacts secrets and personal data from every span, log and metric (its standard redaction and transform processors)
and alone holds the backend's credential. This module does no redaction of its own; what it never emits by design:
- traces: FastAPI requests, httpx calls, SQLAlchemy statements (no parameter values), agent wakes (GenAI spans) and
  their tool calls; never headers (capture is off), bodies, prompts, model output or tool arguments;
- metrics: request and client durations, event-loop lag, loop heartbeats and restarts, the writer's backlog and
  batches, the database pools, each venue stream's state and flaps, recorder freshness, the agent wake queue, wakes,
  tokens and cost, order gate decisions, process memory and CPU;
- logs: every record the root logger handles (stderr stays on and stays the source of truth), with trace ids.

Sampling (the default sampler, `SettleviewSampler`): a span with a parent follows it. A root server span (an HTTP
request) is kept when it changes something (POST, PUT, PATCH, DELETE); GET polls (the Play screen asks every 2 s) and
the heartbeat POST are kept at the background ratio, and /api/health and every `/stream` (SSE) are never traced. A root
client HTTP span (a background poll: Kalshi perps every 2 s, Coinbase, Polymarket) is kept at the background ratio
(OTEL_TRACES_SAMPLER_ARG, default 0.01); a database statement outside a request or wake (the writer's flush every
250 ms, the loops' reads) is never a trace of its own (its time is in the writer and pool metrics); a call to
api.telegram.org is never traced (the bot token is in its URL; the collector would redact it anyway). Internal roots
(agent wakes) are always kept. Metrics count everything regardless.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

SIGNALS = ("traces", "metrics", "logs")
EXPORTERS = frozenset({"otlp", "console", "none"})
PROTOCOL = "http/protobuf"
DEFAULT_BACKGROUND_RATIO = 0.01
NEVER_TRACED_HOSTS = frozenset({"api.telegram.org"})
NEVER_TRACED_PATHS = ("/api/health",)
BACKGROUND_POSTS = frozenset({"/api/trading/heartbeat"})
CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
HTTP_CLIENT_KEYS = ("url.full", "http.url", "http.request.method", "http.method")
HEALTH_TTL_S = 5.0  # one /api/health snapshot serves every gauge of one collection
COVERAGE_TTL_S = 600.0  # the 24 h recorder coverage (288 indexed lookups) is read at most this often
SHUTDOWN_S = 5.0  # a stop never waits longer than this for the last export (the backend may be down)
LAG_BUCKETS_MS = (1, 2.5, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000)
WAIT_BUCKETS_MS = (10, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000, 60000, 120000)


class TelemetryConfigError(ValueError):
    """A bad OTEL_* setting: names the variable, never a value."""


def _env(environ: Mapping[str, str], name: str) -> str:
    return (environ.get(name) or "").strip()


def _truthy(v: str) -> bool:
    return v.lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Plan:
    """Which signals export where (from the environment); `enabled` False = install nothing."""

    exporters: Mapping[str, str]  # signal -> otlp | console | none
    background_ratio: float
    custom_sampler: bool  # OTEL_TRACES_SAMPLER unset: SettleviewSampler

    @property
    def enabled(self) -> bool:
        return any(v != "none" for v in self.exporters.values())


def plan(environ: Mapping[str, str]) -> Plan:
    """Read the OTEL_* environment; raises TelemetryConfigError on a bad value."""
    off = {s: "none" for s in SIGNALS}
    if _truthy(_env(environ, "OTEL_SDK_DISABLED")):
        return Plan(off, DEFAULT_BACKGROUND_RATIO, True)
    generic = _env(environ, "OTEL_EXPORTER_OTLP_ENDPOINT")
    exporters: dict[str, str] = {}
    for sig in SIGNALS:
        var = f"OTEL_{sig.upper()}_EXPORTER"
        name = _env(environ, var).lower() or "otlp"
        if name not in EXPORTERS:
            raise TelemetryConfigError(f"{var} must be one of {', '.join(sorted(EXPORTERS))} (this build exports "
                                       "OTLP over HTTP only)")
        if name == "otlp" and not (generic or _env(environ, f"OTEL_EXPORTER_OTLP_{sig.upper()}_ENDPOINT")):
            name = "none"  # no endpoint for this signal: it stays off (the documented default)
        exporters[sig] = name
    for var in ("OTEL_EXPORTER_OTLP_PROTOCOL", *(f"OTEL_EXPORTER_OTLP_{s.upper()}_PROTOCOL" for s in SIGNALS)):
        v = _env(environ, var)
        if v and v != PROTOCOL:
            raise TelemetryConfigError(f"{var} must be {PROTOCOL} (the only OTLP exporter installed)")
    sampler = _env(environ, "OTEL_TRACES_SAMPLER")
    ratio = DEFAULT_BACKGROUND_RATIO
    if not sampler and _env(environ, "OTEL_TRACES_SAMPLER_ARG"):
        try:
            ratio = float(_env(environ, "OTEL_TRACES_SAMPLER_ARG"))
        except ValueError:
            raise TelemetryConfigError("OTEL_TRACES_SAMPLER_ARG must be a number from 0 to 1") from None
        if not 0.0 <= ratio <= 1.0:
            raise TelemetryConfigError("OTEL_TRACES_SAMPLER_ARG must be a number from 0 to 1")
    return Plan(exporters, ratio, not sampler)


def secret_values(settings: Any) -> list[str]:
    """Every secret (and key id) the settings hold, for the log mask (bootstrap/logs.py mask_secrets). Never logged."""
    out: list[str] = []
    for name in ("telegram_bot_token", "anthropic_api_key", "polymarket_secret_key", "database_password",
                 "research_database_password", "polymarket_key_id", "kalshi_key_id", "trading_demo_key_id",
                 "trading_prod_key_id"):
        v = getattr(settings, name, None)
        if v is None:
            continue
        v = v.get_secret_value() if hasattr(v, "get_secret_value") else str(v)
        if v:
            out.append(v)
    return out


def service_version() -> str:
    """The package version, plus the image's git commit when known (SETTLEVIEW_GIT_SHA, set by the Dockerfile)."""
    import importlib.metadata
    try:
        v = importlib.metadata.version("settleview")
    except importlib.metadata.PackageNotFoundError:
        v = "unknown"
    sha = (os.environ.get("SETTLEVIEW_GIT_SHA") or "").strip()
    return f"{v}+{sha[:12]}" if sha and sha != "unknown" else v


# -- sampling ---------------------------------------------------------------------------------------------------------
def _sampler_class():
    from opentelemetry.sdk.trace.sampling import Decision, Sampler, SamplingResult, TraceIdRatioBased
    from opentelemetry.trace import SpanKind, get_current_span

    class SettleviewSampler(Sampler):
        """See the module docstring ("Sampling")."""

        def __init__(self, background_ratio: float = DEFAULT_BACKGROUND_RATIO) -> None:
            self._ratio = float(background_ratio)
            self._background = TraceIdRatioBased(self._ratio)

        def should_sample(self, parent_context, trace_id, name, kind=None, attributes=None, links=None,
                          trace_state=None) -> SamplingResult:
            attrs = attributes or {}
            if kind == SpanKind.CLIENT and _host(attrs) in NEVER_TRACED_HOSTS:
                return SamplingResult(Decision.DROP)
            parent = get_current_span(parent_context).get_span_context()
            if parent.is_valid:
                keep = parent.trace_flags.sampled
                return SamplingResult(Decision.RECORD_AND_SAMPLE if keep else Decision.DROP, attrs,
                                      parent.trace_state)
            if kind == SpanKind.SERVER:
                method, path = _request(name, attrs)
                if path.startswith(NEVER_TRACED_PATHS) or path.endswith("/stream"):
                    return SamplingResult(Decision.DROP)
                if method in CHANGING_METHODS and path not in BACKGROUND_POSTS:
                    return SamplingResult(Decision.RECORD_AND_SAMPLE, attrs)
                return self._background.should_sample(parent_context, trace_id, name, kind, attrs, links)
            if kind == SpanKind.CLIENT:
                # Only an HTTP call is a background trace of its own: httpx names the URL and method at start. A
                # database statement (its attributes come after the start) or a connect outside a request: dropped,
                # the writer and pool metrics have it.
                if not any(k in attrs for k in HTTP_CLIENT_KEYS):
                    return SamplingResult(Decision.DROP)
                return self._background.should_sample(parent_context, trace_id, name, kind, attrs, links)
            return SamplingResult(Decision.RECORD_AND_SAMPLE, attrs)

        def get_description(self) -> str:
            return f"SettleviewSampler{{background={self._ratio}}}"

    return SettleviewSampler


def _host(attrs: Mapping[str, Any]) -> str:
    host = attrs.get("server.address") or attrs.get("net.peer.name") or ""
    if not host:
        url = str(attrs.get("url.full") or attrs.get("http.url") or "")
        host = url.split("://", 1)[-1].split("/", 1)[0].rsplit("@", 1)[-1].split(":", 1)[0]
    return str(host).lower()


def _request(name: str, attrs: Mapping[str, Any]) -> tuple[str, str]:
    method = str(attrs.get("http.request.method") or attrs.get("http.method") or name.split(" ", 1)[0]).upper()
    path = str(attrs.get("http.route") or attrs.get("url.path") or attrs.get("http.target")
               or (name.split(" ", 1)[1] if " " in name else ""))
    return method, path.split("?", 1)[0]


# -- the handle -------------------------------------------------------------------------------------------------------
@dataclass
class Telemetry:
    enabled: bool
    service: str
    exporters: Mapping[str, str] = field(default_factory=dict)
    _providers: list[Any] = field(default_factory=list)
    _undo: list[Callable[[], None]] = field(default_factory=list)
    _health: tuple[float, dict | None] = (0.0, None)
    _fastapi: dict | None = None  # FastAPI instrumentation options (None: not instrumenting)

    def instrument_app(self, app: Any) -> None:
        """Trace and measure this FastAPI app's requests (no per-message send / receive spans; /api/health excluded
        unless OTEL_PYTHON_FASTAPI_EXCLUDED_URLS says otherwise)."""
        if self._fastapi is None:
            return
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        FastAPIInstrumentor.instrument_app(app, **self._fastapi)
        self._undo.append(lambda: FastAPIInstrumentor.uninstrument_app(app))

    # -- the app's gauges (bootstrap/app.py Runtime) ------------------------------------------------------------------
    def bind_runtime(self, rt: Any) -> None:
        """The app's own metrics, read from the Runtime: observable gauges (read at each export, off the loop) plus
        the event-loop lag, wake-queue wait, stream flap and order-gate hooks."""
        if not self.enabled or "metrics" not in self.exporters or self.exporters["metrics"] == "none":
            return
        from opentelemetry import metrics
        from opentelemetry.metrics import Observation
        m = metrics.get_meter("settleview.app")

        def health() -> dict:
            at, snap = self._health
            if snap is None or time.monotonic() - at > HEALTH_TTL_S:
                snap = rt.health_parts()
                self._health = (time.monotonic(), snap)
            return snap

        def gauge(name: str, unit: str, desc: str, read: Callable[[], Iterable[tuple[float, dict]]]) -> None:
            def cb(_options):
                try:
                    return [Observation(v, a) for v, a in read() if v is not None]
                except Exception:  # a gauge never breaks the export
                    log.debug("telemetry gauge %s failed", name, exc_info=True)
                    return []
            m.create_observable_gauge(name, [cb], unit=unit, description=desc)

        lag = m.create_histogram("settleview.event_loop.lag", unit="ms",
                                 description="How long the event loop took to answer the stall watch's probe.",
                                 explicit_bucket_boundaries_advisory=LAG_BUCKETS_MS)
        if rt.stall_watch is not None:
            rt.stall_watch.on_lag = lambda ms: lag.record(ms)
        gauge("settleview.event_loop.stalls", "{stall}", "Event-loop stalls over 500 ms in the last hour.",
              lambda: [(health()["stalls_last_hour"], {})])
        gauge("settleview.loop.heartbeat_age", "s", "Seconds since each supervised loop's last heartbeat.",
              lambda: [(b["last_beat_age_s"], {"loop": n, "critical": b["critical"]})
                       for n, b in health()["loops"].items()])
        gauge("settleview.loop.restarts", "{restart}", "Restarts of each supervised loop since the app started.",
              lambda: [(b["restarts"], {"loop": n}) for n, b in health()["loops"].items()])
        gauge("settleview.writer.pending", "{write}", "Writes not yet committed (new plus those in failed batches).",
              lambda: [(rt.writer.pending, {})])
        gauge("settleview.writer.failed_batches", "{batch}", "Failed writer batches waiting for a retry.",
              lambda: [(rt.writer.failed_batches, {})])
        gauge("settleview.writer.outage", "s", "Seconds the database has not taken writes (0 while it does).",
              lambda: [(rt.writer.outage_s or 0.0, {})])

        def pools():
            for role, eng in (("write", getattr(rt.store, "_write", None)), ("read", getattr(rt.store, "_read", None))):
                pool = getattr(eng, "pool", None)
                if pool is None or (role == "read" and eng is rt.store._write):
                    continue
                yield role, pool
        gauge("settleview.db.pool.checked_out", "{connection}", "Database connections in use, per pool.",
              lambda: [(p.checkedout(), {"pool": r}) for r, p in pools() if hasattr(p, "checkedout")])
        gauge("settleview.db.pool.overflow", "{connection}", "Connections beyond the pool size (max_overflow).",
              lambda: [(max(0, p.overflow()), {"pool": r}) for r, p in pools() if hasattr(p, "overflow")])
        gauge("settleview.db.pool.size", "{connection}", "Each pool's configured size.",
              lambda: [(p.size(), {"pool": r}) for r, p in pools() if hasattr(p, "size")])

        def streams(key: str):
            return [(int(bool(v[key])) if key != "last_msg_age_ms" else v[key],
                     {"stream": n, "venue": v["venue"], "env": v["env"]}) for n, v in health()["streams"].items()]
        gauge("settleview.stream.connected", "1", "1 while each venue stream is connected.",
              lambda: streams("connected"))
        gauge("settleview.stream.stale", "1", "1 while each venue stream is stale (no fresh message).",
              lambda: streams("stale"))
        gauge("settleview.stream.last_message_age", "ms", "Age of each venue stream's last message.",
              lambda: streams("last_msg_age_ms"))
        gauge("settleview.recorder.age", "s", "Age of each research recorder's newest row (as /api/health reads it).",
              lambda: [(r["age_s"], {"recorder": n}) for n, r in health()["recorders"].get("recorders", {}).items()])
        gauge("settleview.recorder.silent", "1", "1 while a recorder's newest row is older than its limit.",
              lambda: [(int(r["state"] != "ok"), {"recorder": n})
                       for n, r in health()["recorders"].get("recorders", {}).items()])

        # The SLIs the OpenObserve alerts read (deploy/openobserve/alerts/, docs/operations.md "Alerting").
        def feed():
            return rt.feed.snapshot()
        gauge("settleview.feed.stale", "1", "1 while the Coinbase trade feed (the BTC index) is not live.",
              lambda: [(int(feed()["status"] != "live"), {"feed": "coinbase"})])
        gauge("settleview.feed.lag", "ms", "The Coinbase feed's trade-to-receipt delay.",
              lambda: [(feed()["lag_ms"], {"feed": "coinbase"})])

        def perps(key: str):
            p = health()["perps"]
            st = p.get("stream") if p.get("enabled") else None
            return [] if st is None else [(int(bool(st[key])), {"env": st["env"]})]
        gauge("settleview.perps.book_live", "1", "1 while the Kalshi perps order book is live (perps on only).",
              lambda: perps("book_live"))
        gauge("settleview.perps.connected", "1", "1 while the Kalshi perps stream is connected (perps on only).",
              lambda: perps("connected"))

        coverage_ttl = {"at": 0.0, "v": None}

        def coverage():
            if time.monotonic() - coverage_ttl["at"] > COVERAGE_TTL_S:
                coverage_ttl["v"] = rt.recorder_watch.rolling_coverage(int(time.time() * 1000))
                coverage_ttl["at"] = time.monotonic()
            return coverage_ttl["v"]
        gauge("settleview.recorder.coverage", "1", "Share of the last 24 h's 5-minute Polymarket windows with a "
              "recorded quote.", lambda: [] if coverage() is None
              else [(coverage()["recorded"] / max(1, coverage()["windows"]), {"recorder": "pm-quotes"})])
        gauge("settleview.recorder.longest_gap", "min", "The longest run of unrecorded 5-minute windows in the last "
              "24 h.", lambda: [] if coverage() is None
              else [(coverage()["longest_gap_min"], {"recorder": "pm-quotes"})])

        flaps = m.create_counter("settleview.stream.flaps", unit="{transition}",
                                 description="Trading-stream up/down transitions (each writes an audit row; "
                                             "Polymarket's only after its hysteresis).")
        decisions = m.create_counter("settleview.order_gate.decisions", unit="{decision}",
                                     description="Order gate decisions by strategy, stage and result code.")
        for strategy, svc in list(getattr(rt, "trading_services", {}).items()):
            svc.audit_observers.append(_flap_observer(flaps, strategy))
            svc.gate = ObservedGate(svc.gate, decisions, strategy)

        fleet = getattr(rt, "agent_fleet", None)
        if fleet is not None:
            wait = m.create_histogram("settleview.agent.wake_queue.wait", unit="ms",
                                      description="How long each agent wake waited for a free slot.",
                                      explicit_bucket_boundaries_advisory=WAIT_BUCKETS_MS)
            fleet.scheduler.on_take = lambda prio, ms: wait.record(ms, {"priority": prio})

            def queue():
                return fleet.scheduler.summary(fleet.now())
            gauge("settleview.agent.wake_queue.depth", "{wake}", "Agent wakes waiting for a slot.",
                  lambda: [(queue()["queued"], {})])
            gauge("settleview.agent.wakes.running", "{wake}", "Agent wakes running now (at most the cap).",
                  lambda: [(queue()["running"], {})])
            gauge("settleview.agent.wake_queue.oldest_wait", "ms", "The oldest queued wake's wait so far.",
                  lambda: [(queue()["oldest_wait_ms"], {})])

        sessions = getattr(rt, "agent_sessions", None)
        if sessions is not None:
            def spend():
                out = []
                for sess in sessions.live():
                    cap = (sess.get("limits") or {}).get("spend_cap")
                    if cap:
                        used = float((sess.get("usage") or {}).get("api_usd") or 0.0)
                        out.append((used / float(cap), {"budget": "session",
                                                        "session": str(sess.get("name") or sess["id"])[:40]}))
                day = sessions.global_limits().get("api_day")
                if day:
                    used = sessions.global_usage(int(time.time() * 1000))["api_day"]
                    out.append((used / float(day), {"budget": "day", "session": "all"}))
                return out
            gauge("settleview.agent.spend_ratio", "1", "Estimated agent API spend as a share of its budget (each "
                  "live session's cap; the global daily cap).", spend)

    def shutdown(self) -> None:
        """Flush and stop every provider (bounded: SHUTDOWN_S), then undo the instrumentation."""
        for undo in reversed(self._undo):
            try:
                undo()
            except Exception:
                log.debug("telemetry: undo failed", exc_info=True)
        self._undo.clear()
        providers, self._providers = self._providers, []

        def stop() -> None:
            for p in providers:
                try:
                    p.shutdown()
                except Exception:
                    log.debug("telemetry: provider shutdown failed", exc_info=True)
        t = threading.Thread(target=stop, name="otel-shutdown", daemon=True)
        t.start()
        t.join(SHUTDOWN_S)

    def force_flush(self, timeout_millis: int = 5000) -> None:
        for p in self._providers:
            try:
                p.force_flush(timeout_millis)
            except Exception:
                log.debug("telemetry: flush failed", exc_info=True)


def _flap_observer(counter, strategy: str) -> Callable[[dict], None]:
    def observe(row: dict) -> None:
        ev = row.get("event")
        if ev in ("stream_up", "stream_down"):
            counter.add(1, {"stream": f"trading_{row.get('env')}", "direction": ev.removeprefix("stream_"),
                            "strategy": strategy})
    return observe


class ObservedGate:
    """The trading service's OrderGate, counting each decision (strategy, stage prepare / approve, ok, code)."""

    def __init__(self, gate: Any, counter: Any, strategy: str) -> None:
        self._gate, self._counter, self._strategy = gate, counter, strategy

    def check(self, ticket: dict, now: int, quote: dict | None, **kwargs):
        r = self._gate.check(ticket, now, quote, **kwargs)
        try:
            self._counter.add(1, {"strategy": self._strategy, "stage": "approve" if "exit_order" in kwargs
                                  else "prepare", "kind": str(ticket.get("kind") or "entry"),
                                  "result": "ok" if r.ok else "refused", "code": r.code})
        except Exception:
            log.debug("order gate counter failed", exc_info=True)
        return r

    def __getattr__(self, name: str):
        return getattr(self._gate, name)


# -- setup ------------------------------------------------------------------------------------------------------------
def _resource(service: str, environ: Mapping[str, str]):
    from opentelemetry.sdk.resources import Resource
    attrs: dict[str, Any] = {"service.version": service_version(), "service.instance.id": socket.gethostname(),
                             "process.pid": os.getpid()}
    sha = (os.environ.get("SETTLEVIEW_GIT_SHA") or "").strip()
    if sha and sha != "unknown":
        attrs["vcs.ref.head.revision"] = sha
    named = _env(environ, "OTEL_SERVICE_NAME") or "service.name=" in _env(environ, "OTEL_RESOURCE_ATTRIBUTES")
    if not named:
        attrs["service.name"] = service
    return Resource.create(attrs)


def setup(service: str, *, environ: Mapping[str, str] | None = None, span_exporter: Any = None, metric_exporter: Any = None, log_exporter: Any = None,
          instrument: bool = True) -> Telemetry:
    """Install OpenTelemetry for this process when configured (see the module docstring), else nothing.

    `service` is the default service.name (OTEL_SERVICE_NAME wins). The exporters are for tests: given ones replace
    the OTLP exporters of their signal and export synchronously."""
    env = os.environ if environ is None else environ
    p = plan(env)
    given = {"traces": span_exporter, "metrics": metric_exporter, "logs": log_exporter}
    exporters = {s: ("given" if given[s] is not None else p.exporters[s]) for s in SIGNALS}
    if all(v == "none" for v in exporters.values()):
        return Telemetry(False, service)
    try:
        from opentelemetry import _logs, metrics, trace
        from opentelemetry.sdk._logs import LoggerProvider
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, SimpleLogRecordProcessor
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
    except ImportError:
        raise TelemetryConfigError("an OTLP endpoint is set but the OpenTelemetry SDK is not installed: install the "
                                   "`otel` extra (pip install -e \".[otel]\")") from None
    resource = _resource(service, env)
    tel = Telemetry(True, service, dict(exporters))

    if exporters["traces"] != "none":
        sampler = _sampler_class()(p.background_ratio) if p.custom_sampler else None
        tp = TracerProvider(resource=resource, sampler=sampler, shutdown_on_exit=False)
        if exporters["traces"] == "given":
            tp.add_span_processor(SimpleSpanProcessor(span_exporter))
        elif exporters["traces"] == "console":
            from opentelemetry.sdk.trace.export import ConsoleSpanExporter
            tp.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
        else:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            tp.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(tp)
        tel._providers.append(tp)

    if exporters["metrics"] != "none":
        if exporters["metrics"] == "given":
            reader = PeriodicExportingMetricReader(metric_exporter, export_interval_millis=3_600_000)
        elif exporters["metrics"] == "console":
            from opentelemetry.sdk.metrics.export import ConsoleMetricExporter
            reader = PeriodicExportingMetricReader(ConsoleMetricExporter())
        else:
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
            reader = PeriodicExportingMetricReader(OTLPMetricExporter())
        mp = MeterProvider(resource=resource, metric_readers=[reader], shutdown_on_exit=False)
        metrics.set_meter_provider(mp)
        tel._providers.append(mp)
        _process_gauges(metrics.get_meter("settleview.process"))

    if exporters["logs"] != "none":
        lp = LoggerProvider(resource=resource, shutdown_on_exit=False)
        if exporters["logs"] == "given":
            lp.add_log_record_processor(SimpleLogRecordProcessor(log_exporter))
        elif exporters["logs"] == "console":
            from opentelemetry.sdk._logs.export import ConsoleLogRecordExporter
            lp.add_log_record_processor(SimpleLogRecordProcessor(ConsoleLogRecordExporter()))
        else:
            from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
            lp.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
        _logs.set_logger_provider(lp)
        tel._providers.append(lp)

    if instrument:
        # The stable HTTP and database conventions (http.route, url.full, db.query.text) instead of the old ones, whose
        # server metrics carry the raw path (one series per session id). An explicit setting is kept.
        os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "http,database")
        _instrument(tel, env)
    log.info("telemetry: OpenTelemetry on for %s (traces %s, metrics %s, logs %s)", service, exporters["traces"],
             exporters["metrics"], exporters["logs"])
    return tel


def _process_gauges(meter) -> None:
    """process.memory.usage (current resident bytes: /proc on Linux, else the platform's own reading) and
    process.cpu.time (user / system seconds)."""
    from opentelemetry.metrics import Observation

    def memory(_options):
        b = current_rss()
        return [Observation(b)] if b is not None else []

    def cpu(_options):
        t = os.times()
        return [Observation(t.user, {"cpu.mode": "user"}), Observation(t.system, {"cpu.mode": "system"})]
    meter.create_observable_gauge("process.memory.usage", [memory], unit="By",
                                  description="The process's current resident memory.")
    meter.create_observable_counter("process.cpu.time", [cpu], unit="s", description="CPU time used by the process.")
    started = time.monotonic()
    meter.create_observable_gauge("process.uptime", [lambda _o: [Observation(time.monotonic() - started)]], unit="s",
                                  description="The time the process has been running.")


def current_rss() -> int | None:
    """Current resident set size in bytes from /proc/self/statm (Linux, every container); None elsewhere."""
    try:
        with open("/proc/self/statm", encoding="ascii") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError, AttributeError):
        return None


def _engine_tracer() -> Callable[[Any], None]:
    """A hook that traces one SQLAlchemy engine's statements (no parameter values, no SQL comments)."""
    from opentelemetry.instrumentation.sqlalchemy.engine import EngineTracer
    from opentelemetry.instrumentation.sqlalchemy.version import __version__
    from opentelemetry.metrics import get_meter
    from opentelemetry.trace import get_tracer
    name = "opentelemetry.instrumentation.sqlalchemy"
    tracer = get_tracer(name, __version__)
    usage = get_meter(name, __version__).create_up_down_counter(
        "db.client.connections.usage", unit="connections",
        description="The number of connections that are currently in state described by the state attribute.")

    def hook(engine) -> None:
        EngineTracer(tracer, engine, usage, enable_commenter=False)
    return hook


def _instrument(tel: Telemetry, env: Mapping[str, str]) -> None:
    """The library instrumentations: FastAPI (options only: Telemetry.instrument_app), httpx, SQLAlchemy (no SQL
    comments), asyncio only when one of its OTEL_PYTHON_ASYNCIO_* variables names something to trace, logging (the
    OTel handler on the root logger, when logs export)."""
    tracing_or_metrics = any(tel.exporters[s] != "none" for s in ("traces", "metrics"))
    if tracing_or_metrics:
        # FastAPI: per app (Telemetry.instrument_app), since the app module imported FastAPI before setup ran.
        excluded = None if (_env(env, "OTEL_PYTHON_FASTAPI_EXCLUDED_URLS") or _env(env, "OTEL_PYTHON_EXCLUDED_URLS")) \
            else "api/health"
        tel._fastapi = {"exclude_spans": ["receive", "send"], "excluded_urls": excluded}

        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        hi = HTTPXClientInstrumentor()
        hi.instrument()
        tel._undo.append(hi.uninstrument)

        # SQLAlchemy: each engine the adapters make (adapters/sqlite/db.py ENGINE_HOOKS), traced by the
        # instrumentation's own EngineTracer. Not SQLAlchemyInstrumentor().instrument(): it patches create_engine,
        # which db.py imported before setup, and it imports sqlalchemy.ext.asyncio, which needs greenlet (absent in
        # the image). Its declared sqlalchemy < 2.1 is not checked here; tests/telemetry check the spans on 2.1.
        from btc_signal_engine.adapters.sqlite import db as dbmod
        hook = _engine_tracer()
        dbmod.ENGINE_HOOKS.append(hook)
        tel._undo.append(lambda: dbmod.ENGINE_HOOKS.remove(hook) if hook in dbmod.ENGINE_HOOKS else None)

        if any(_env(env, v) for v in ("OTEL_PYTHON_ASYNCIO_COROUTINE_NAMES_TO_TRACE",
                                      "OTEL_PYTHON_ASYNCIO_TO_THREAD_FUNCTION_NAMES_TO_TRACE",
                                      "OTEL_PYTHON_ASYNCIO_FUTURE_TRACE_ENABLED")):
            from opentelemetry.instrumentation.asyncio import AsyncioInstrumentor
            ai = AsyncioInstrumentor()
            ai.instrument()
            tel._undo.append(ai.uninstrument)

    if tel.exporters["logs"] != "none":
        from opentelemetry.instrumentation.logging import LoggingInstrumentor
        li = LoggingInstrumentor()
        li.instrument(enable_log_auto_instrumentation=True, log_code_attributes=False)
        tel._undo.append(li.uninstrument)

