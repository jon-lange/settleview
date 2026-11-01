# Contributing to Settleview

Thanks for your interest. Settleview is a small personal project, so please open an issue to discuss anything
bigger than a bug fix before you write a lot of code.

Coding agents (and humans) should also read [AGENTS.md](AGENTS.md). It holds the project's hard rules and the
research honesty rules.

## Development setup

Production runs in Linux containers, and so does all development (tests, quality gates, research scripts, evals),
on any host (Windows, macOS, Linux; any CPU). There is no virtualenv to create. You need
[Docker](https://docs.docker.com/get-docker/) and any Python 3 to start the dev container (`scripts/dev.py` uses the
standard library only), or just [uv](https://docs.astral.sh/uv/):

```bash
python scripts/dev.py test                          # builds the dev container the first time, then runs the suite
uv run --no-project python scripts/dev.py test      # the same with uv's Python (no project environment is created)
python scripts/dev.py shell                         # a shell in the container, for anything else
```

Install the pre-push hook once per clone:

```bash
git config core.hooksPath .githooks
```

It runs `scripts/prepush_scan.py` (standard library only; any Python 3.8+ or uv) and refuses a push whose commits
carry an author or committer email other than a GitHub no-reply address, add a personal path (a Windows user
profile), a `@gmail.com` address, a private key or a token, or that pushes a tag (release tags need
`SETTLEVIEW_ALLOW_TAGS=1`). Set your no-reply address first:
`git config user.email <id>+<user>@users.noreply.github.com` (GitHub → Settings → Emails). Details, the local
patterns file and the opt-in Trivy scan: [docs/PUBLISHING.md](docs/PUBLISHING.md#the-pre-push-scan).

## Before you open a pull request

All of these must pass (each runs in Linux containers, as CI does):

```bash
python scripts/dev.py test             # the test suite (e2e and the import contracts in .importlinter included)
python scripts/dev.py postgres-tests   # the Postgres tests, on a throwaway Postgres 17
python scripts/dev.py quality          # Ruff, import contracts, Hadolint, ShellCheck and Trivy, as CI runs them
```

- Keep the layers: `domain/` is pure (no I/O), `application/` talks to the outside only through ports,
  `adapters/` do the I/O, and `bootstrap/` wires everything. Only `adapters/` may import the venue SDKs.
- Add or update tests for behaviour you change. Tests must not need network access, API keys or a local database.
  Build data in the test (fixtures, fakes, the shadow exchanges).
- Venue calls go through the official SDKs (`kalshi_python_async` under our guarded Kalshi backend, `polymarket_us`
  for every Polymarket US call). Don't hand-roll HTTP or signing.
- Don't add dependencies without discussing them first. Pin exact versions in `pyproject.toml`.

## Project rules

**No fake data in the app.** Nothing in `web/` or `btc_signal_engine/` may show demo, sample or placeholder data. If
the real data isn't there, the UI says so and why. Tests may build their own data.

**No silent fallbacks.** When a setting, response or stored value is missing or unreadable, refuse with a clear
message. Never quietly substitute a default, an older value or a guess, especially anywhere near orders, limits or
the kill switch. A stated default (shown to the user as "default") is fine; a hidden one is not.

**Never weaken a trading guard to make something work.** Human approval, the order gate, limits, the kill switch,
the gateway's hard ceiling and the venue allow-lists exist on purpose.

**Secrets never enter the repo.** Don't commit `.env`, key files (`*.pem`), key ids, key paths, account numbers,
balances, order ids or personal paths and addresses. Use `.env.example` for documentation.

## Research contributions

Trading ideas are tested with `scripts/experiments.py` and recorded in
[`docs/research/LEARNINGS.md`](docs/research/LEARNINGS.md). The full honesty rules are in
[AGENTS.md](AGENTS.md#honesty-rules-for-research-how-to-not-fool-yourself). In short:

- Write the exact rule and the pass/fail bar **before** looking at results.
- Don't change the harness's scoring, splits or fee code to make a rule look better.
- Train on the first part of the data and judge once on the holdout. The holdout is not a tuning set.
- A rule survives only if it clears the multiple-testing correction, is positive in both halves, and has enough
  holdout trades. Even then it is a **candidate**, not a strategy.
- Always report win rate next to the break-even rate.
- Record every result, including negative ones, in `LEARNINGS.md`.

## Commit style

- One logical change per commit.
- The subject line says what changed and why it matters, in plain words (for example: "Polymarket feed health judges
  only markets needed now"). Add a body when the reason isn't obvious from the subject.
- Mention any test or live check you ran that isn't part of the test suite.
- Never commit secrets, local data (`data/`), logs or virtual environments.

## Contributor license agreement

Settleview is licensed under the [PolyForm Noncommercial License 1.0.0](LICENSE), and commercial licenses are
offered separately. So outside contributions need this short agreement:

- **You keep the copyright** in your contribution.
- You grant the copyright holder (Jon Lange) a **perpetual, irrevocable, worldwide, royalty-free license to use,
  copy, change, sublicense and relicense your contribution under any terms**, including commercial ones.
- You confirm that you **have the right to grant this** (it is your own work, or you are allowed to submit it, for
  example by your employer).

You agree by ticking the "Contributor license agreement" box in the pull request template. Without it the pull
request can't be merged; open an issue instead if you prefer. This is a plain-language summary, not legal advice.

## Reporting bugs and security issues

Use the issue templates for bugs and feature requests. Report security problems privately as described in
[SECURITY.md](SECURITY.md). Everyone taking part is expected to follow the [Code of Conduct](CODE_OF_CONDUCT.md).
