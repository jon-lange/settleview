# Settleview: one image for every Python service in compose.yaml (the app, the research recorders). The command picks
# the service: the app by default, `python -m btc_signal_engine.recorders pm-quotes | pm-settle` for a recorder. The
# database is Postgres (BTCFLOW_DATABASE_URL; compose's `postgres` service or a managed server); without that setting
# the app uses the SQLite file at BTCFLOW_DB_PATH, as on a host without Docker.
# Python 3.13: the interpreter pyproject.toml requires and CI tests on. The base is pinned by its multi-arch index digest
# (linux/amd64 + linux/arm64; resolved 2026-10-31, built by Docker Official Images 2026-10-28), so every build starts
# from the same bytes. To refresh it (when Trivy reports a fixed base package, or for a new Python patch):
#   docker buildx imagetools inspect python:3.13.18-slim-trixie     # the "Digest:" line; replace tag and digest here
FROM python:3.13.18-slim-trixie@sha256:76baad6037690d6e8c4397fbcaef17bca6f19eecef822b708fe8fa3417d30bb5

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    BTCFLOW_DB_PATH=/data/btcflow.db BTCFLOW_RESEARCH_DIR=/research BTCFLOW_API_HOST=0.0.0.0 BTCFLOW_API_PORT=8000 \
    BTCFLOW_FILE_LOG=false

WORKDIR /app
# README.md, LICENSE and NOTICE: pyproject.toml names them (readme, license-files), so the build needs them (the
# vendored libraries' licenses come with web/).
COPY pyproject.toml uv.lock README.md LICENSE NOTICE alembic.ini ./
COPY btc_signal_engine ./btc_signal_engine
COPY rulesets ./rulesets
COPY web ./web
# The agent's default system prompt: read at startup when agent mode is on (bootstrap/app.py AGENT_DEFAULT_PROMPT)
COPY docs/agent/default-system-prompt.md ./docs/agent/default-system-prompt.md
# The dependencies come from uv.lock, hash-checked: `uv export --locked` fails if the lock no longer matches
# pyproject.toml, and every wheel or sdist installed must match a sha256 recorded in the lock (`--require-hashes`), so a
# changed or substituted file on the index fails the build. uv is mounted for these steps only (pinned by version and
# multi-arch digest, as deploy/dev/Dockerfile; not in the final image). The project itself is then installed without
# dependencies and without build isolation: its build backend (setuptools, a locked dependency) is the hashed one
# already installed, so nothing unhashed is fetched.
# Editable install with the `postgres` extra (psycopg 3, the Postgres driver) and the `otel` extra (the OpenTelemetry SDK,
# OTLP/HTTP exporter and instrumentations; inert until OTEL_EXPORTER_OTLP_ENDPOINT is set): the code runs from /app,
# where the app finds web/ (adapters/http/app.py: WEB_DIR) and the migrations next to it. A plain install would copy the
# package into site-packages without web/.
# /data and /research exist in the image owned by the app user: a new named volume mounted there copies that owner, so
# the non-root services can write to it on every host (Docker Desktop on Windows / macOS, Linux, Swarm).
# tini (Debian's package): PID 1 on every platform (signals forwarded to Python, zombies reaped), not only where the
# runtime offers an init switch (compose `init: true`; Azure Container Apps has none).
# `apt-get upgrade`: Debian's security fixes published since the base image was built land in this layer. The trade-off:
# this layer depends on the day it was built (the pinned digest alone is reproducible; this is "reproducible-ish"), which
# is wanted here: a rebuild picks up fixes without waiting for a new base image. Docker's layer cache keeps an old
# result until the layer is rebuilt (`docker build --pull --no-cache`, or a new base digest). DL3008 (pin apt package
# versions) is ignored for the same reason: Debian drops a superseded version from its mirrors, so a pin would break the
# build at the next security update instead of taking it; the base digest is the pin.
# hadolint ignore=DL3008
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends tini && rm -rf /var/lib/apt/lists/*
# pip is removed after the install: nothing runs it at run time (the root filesystem is read-only), and its vendored
# copies of msgpack, urllib3 and setuptools are older than the fixed releases (Trivy, 2026-10-31; no pip release vendors
# fixed ones yet). The app's own urllib3 and setuptools are pinned in pyproject.toml.
RUN --mount=from=ghcr.io/astral-sh/uv:0.13.1@sha256:4a1f3666816dafadd20ceb71eac9a7d56d7ef30279f7d64048519ab901ada336,source=/uv,target=/usr/local/bin/uv \
    uv export --no-cache --locked --extra postgres --extra otel --no-emit-project --format requirements.txt \
       -o /tmp/requirements.txt \
    && uv pip install --system --no-cache --require-hashes --no-deps -r /tmp/requirements.txt \
    && uv pip install --system --no-cache --no-deps --no-build-isolation -e . \
    && rm /tmp/requirements.txt && pip uninstall --yes pip \
    && useradd --create-home --uid 10001 app && mkdir -p /data /research && chown app /data /research

# Which commit this image is (after the install, so a new commit does not rebuild the dependency layer):
#   docker build --build-arg GIT_SHA=$(git rev-parse HEAD) -t settleview:local .
# It is the OCI revision label (`docker inspect`), and telemetry's service.version / vcs.ref.head.revision.
ARG GIT_SHA=unknown
LABEL org.opencontainers.image.title="Settleview" \
      org.opencontainers.image.licenses="PolyForm-Noncommercial-1.0.0" \
      org.opencontainers.image.revision="${GIT_SHA}"
ENV SETTLEVIEW_GIT_SHA=${GIT_SHA}

USER app
# Runs with a read-only root filesystem (compose.yaml): nothing here writes outside /data, /tmp and $HOME (tmpfs).
# /data: the app's local folder (logs, pid and stop files; the SQLite file when no database URL is set).
VOLUME ["/data"]
# The PC listener (BTCFLOW_PC_PORT, compose: 8001) and the agent listener (BTCFLOW_AGENT_PORT, compose: 8002) are
# deliberately not EXPOSEd: `docker run -P` would publish them on every interface. Publish them on 127.0.0.1 only
# (compose.yaml). Being the PC needs a paired browser's cookie (`ops pair-pc`), never just the port.
EXPOSE 8000
# Logs go to stderr only (BTCFLOW_FILE_LOG=false), where `docker logs` and cloud log collectors read them.
# The app's healthcheck; each recorder service overrides it with `python -m btc_signal_engine.ops alive <name>`.
# Standard library only (slim images have no curl). From loopback inside the container (the Host 127.0.0.1:port is on
# the allow-list). /api/health needs no session and answers such a caller only {"status": "up"}; loopback is never
# the PC. Any non-2xx answer or no answer is unhealthy.
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('BTCFLOW_API_PORT', '8000'), timeout=8)"]
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "btc_signal_engine"]
