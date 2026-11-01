# OpenTelemetry: what the in-memory tests passed and the real image caught

**Problem.** The OpenTelemetry set-up (bootstrap/telemetry.py) passed every in-memory test on the Windows dev venv,
then a throwaway compose stack exporting to OpenObserve showed the first three things below; the fourth turned up while
writing the tests:

1. The app did not start in the image with an endpoint set: `ModuleNotFoundError: No module named 'greenlet'` from
   `SQLAlchemyInstrumentor().instrument()`.
2. A request's `url.query` arrived as `pin=1234&token=<redacted>`: the first value was kept.
3. Hundreds of one-span traces a minute: every SQL statement of the writer (every 250 ms) and the loops was a root
   trace, although the sampler was meant to drop background database work.
4. With the SQLAlchemy and FastAPI instrumentors' global `instrument()`, the app's own engines and app object would
   not have been instrumented at all (a test building its own FastAPI app got no server spans).

**Cause.**
1. The SQLAlchemy instrumentation (0.66b1) always wraps `sqlalchemy.ext.asyncio.create_async_engine`, which imports
   greenlet in SQLAlchemy 2.1. greenlet is only on dev hosts (playwright pulls it), so tests never saw it.
2. The scrubber's "redact every query value" rule matched `[?&]key=value`; `url.query` has no leading `?`.
3. SQLAlchemy's `EngineTracer` starts its span with no attributes and sets `db.*` after; a sampler sees only the start
   attributes, so a "drop if db.system" rule never fired.
4. Global `instrument()` patches `sqlalchemy.create_engine` and `fastapi.FastAPI` as module attributes; our modules had
   already done `from sqlalchemy import create_engine` / `from fastapi import FastAPI` at import, before set-up.

**Fix.** Engines are traced where the adapters make them: `adapters/sqlite/db.py` hands every engine to its
`ENGINE_HOOKS`, and telemetry registers one that wraps it in the instrumentation's own `EngineTracer` (no asyncio
import). The FastAPI app is instrumented explicitly (`Telemetry.instrument_app`, called by `Runtime.run`). A bare
`url.query` is scrubbed as `?` + value. A root client span is a background trace only when its start attributes name
an HTTP call (`url.full` / `http.request.method`); anything else without a parent is dropped. Tests:
`test_setup_never_imports_sqlalchemy_asyncio`, the `url.query` assertion, the database-root sampler case.

**How to avoid.** Prove telemetry in the image, not only in the venv: start a throwaway stack with the endpoint set
and look at what arrives (docs/operations.md, "Observability"). Prefer instrumenting objects you hold (an engine, an
app) over global patches. A sampler rule may only use attributes the instrumentation sets at span start.
