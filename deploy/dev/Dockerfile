# Settleview's dev container: the Linux environment the tests and the quality gates run in by default, on any host
# (.devcontainer/devcontainer.json, deploy/dev/compose.yaml). Production runs this same Linux base (the root
# Dockerfile), so a developer's CPU or OS never decides which wheels the code is tested against.
#
# The source is NOT copied: deploy/dev/compose.yaml bind-mounts the checkout at /app, so an edit needs no rebuild. The
# image holds the dependencies of uv.lock (every extra: dev, postgres, otel, evals) and Playwright's Chromium for the
# e2e tests; it is rebuilt only when pyproject.toml, uv.lock or this file changes
# (`docker compose -f deploy/dev/compose.yaml build dev`; a no-op from the layer cache otherwise).
#
# Base: the root Dockerfile's image, pinned by the same multi-arch digest (refresh both together).
FROM python:3.14.7-slim-trixie@sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d

# uv, pinned by version (2026-10-14, older than two weeks when adopted) and multi-arch digest.
COPY --from=ghcr.io/astral-sh/uv:0.13.1@sha256:4a1f3666816dafadd20ceb71eac9a7d56d7ef30279f7d64048519ab901ada336 /uv /usr/local/bin/uv

# PLAYWRIGHT_BROWSERS_PATH: Chromium lives in the image, readable by any user the container runs as.
# UV_CACHE_DIR: the dev project's named volume (compose.yaml), world-writable, so a `uv pip install` in the
# container is quick and survives it. HOME: writable whatever uid the container runs as.
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_SYSTEM_PYTHON=1 UV_LINK_MODE=copy UV_CACHE_DIR=/cache/uv \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright SETTLEVIEW_E2E_BROWSER_CHANNEL=chromium HOME=/tmp

WORKDIR /app
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# The dependencies only (the layer is keyed on pyproject.toml and uv.lock, not on the code): every extra, from uv.lock,
# hash-checked (`--locked`: the lock must match pyproject.toml; `--require-hashes`: every file must match its sha256 in
# the lock), as the root Dockerfile and CI install them.
# Then Playwright's Chromium and the system libraries it needs (Playwright's own installer, at the pinned playwright).
# `apt-get upgrade`: Debian's security fixes since the base image (as in the root Dockerfile; DL3008 for the same reason).
# hadolint ignore=DL3008
RUN --mount=type=bind,source=pyproject.toml,target=/tmp/build/pyproject.toml \
    --mount=type=bind,source=uv.lock,target=/tmp/build/uv.lock \
    --mount=type=cache,target=/root/.cache/uv \
    apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends tini \
    && UV_CACHE_DIR=/root/.cache/uv uv export --project /tmp/build --locked --all-extras --no-emit-project \
       --format requirements.txt -o /tmp/requirements.txt \
    && UV_CACHE_DIR=/root/.cache/uv uv pip install --require-hashes --no-deps -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt \
    && python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /cache/uv /cache/pre-commit && chmod 1777 /cache/uv /cache/pre-commit \
    && useradd --create-home --uid 10001 dev

# The docker CLI and its compose plugin (Debian's) for tests/deploy/test_compose.py, which only resolves compose files
# (`docker compose config`): no daemon and no socket are ever given to this container. git for pre-commit (the quality
# gates, .pre-commit-config.yaml); the bind-mounted checkout belongs to the host user, so git trusts /app explicitly.
# shellcheck (Debian trixie's 0.10.0, the shellcheck-py hook's version) on PATH for the actionlint hook, which lints
# each workflow `run:` script with the shellcheck it finds there and silently skips that check without one (the
# GitHub runner has it, so CI caught SC2086 that the dev container's run had passed).
# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends docker-cli docker-compose git shellcheck \
    && rm -rf /var/lib/apt/lists/* \
    && git config --system --add safe.directory /app && git config --system --add safe.directory /git

# pre-commit's hook environments (Ruff, ShellCheck, gitleaks, actionlint, import-linter) live in the dev project's
# cache volume (deploy/dev/compose.yaml), so they are built once, not on every run. hadolint is not among them: its
# hook runs hadolint's image, which needs Docker, and this container has none (compose.yaml: SKIP=hadolint-docker).
ENV PRE_COMMIT_HOME=/cache/pre-commit

# The project itself, editable and without dependencies: its metadata only (the code at /app is the bind mount).
# README.md, LICENSE, NOTICE and web/vendor's licenses: pyproject.toml names them (readme, license-files).
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY web/vendor ./web/vendor
COPY btc_signal_engine/__init__.py ./btc_signal_engine/__init__.py
RUN uv pip install --no-deps -e .

# A non-root default. On Linux the Dev Containers tools change its uid to the host user's (updateRemoteUserUID), and
# the plain compose path takes `--user "$(id -u):$(id -g)"`, so files written into the checkout belong to that user.
USER dev
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "pytest", "-p", "no:cacheprovider"]
