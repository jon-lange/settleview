# Contributing to Settleview

Thanks for your interest. Settleview is a small personal project, so please open an issue to discuss anything
bigger than a bug fix before you write a lot of code.

Coding agents (and humans) should also read [AGENTS.md](AGENTS.md). It holds the project's hard rules and the
research honesty rules.

## Development setup

Production runs in Linux containers, and so does all development (tests, quality gates, research scripts, evals),
on any host (Windows, macOS, Linux; any CPU). There is no virtualenv to create and no wrapper script: the dev
container follows the [Dev Containers spec](https://containers.dev/) (`.devcontainer/devcontainer.json`) on top of
plain Docker Compose (`deploy/dev/compose.yaml`). You need [Docker](https://docs.docker.com/get-docker/) and one of:

- VS Code with the Dev Containers extension: "Reopen in Container"; or GitHub Codespaces;
- the official CLI ([`@devcontainers/cli`](https://github.com/devcontainers/cli)), headless:

  ```bash
  devcontainer up --workspace-folder .
  devcontainer exec --workspace-folder . pytest
  ```

- nothing else (Docker Compose alone):

  ```bash
  docker compose -f deploy/dev/compose.yaml run --rm dev pytest        # builds the image the first time
  docker compose -f deploy/dev/compose.yaml run --rm dev bash          # a shell in the container, for anything else
  ```

  On Linux add `--user "$(id -u):$(id -g)"` after `run`, so files written into the checkout are yours.

Install the git hooks once per clone ([pre-commit](https://pre-commit.com); a worktree shares its clone's hooks):

```bash
uv tool install pre-commit==4.7.0   # on the host, once (the version uv.lock pins)
pre-commit install                  # .pre-commit-config.yaml: the pre-commit and pre-push hooks
```

On every commit they run gitleaks on the staged diff, Ruff, ShellCheck, hadolint, actionlint and the import
contracts on the changed files; on every push, gitleaks on the branch's whole history. gitleaks (`.gitleaks.toml`:
its default rules plus `@gmail.com` addresses and Windows user-profile paths) refuses a secret, a private key or a
personal detail; a made-up placeholder carries `gitleaks:allow` on its line. Commit as your GitHub no-reply address
(`git config user.email <id>+<user>@users.noreply.github.com`; GitHub → Settings → Emails, where "Block command line
pushes that expose my email" should be on). Details and machine-local patterns:
[docs/PUBLISHING.md](docs/PUBLISHING.md#secret-and-personal-data-scanning). hadolint is a system binary (in the dev
container): a host without it fails the hadolint hook on a commit that changes a Dockerfile; run
`pre-commit run --all-files` in the dev container first, then `SKIP=hadolint git commit ...` (pre-commit's own
skip).

## Commit messages: Conventional Commits

Every commit (and every pull request title, which becomes the squash commit) follows
[Conventional Commits 1.0.0](https://www.conventionalcommits.org/en/v1.0.0/): `<type>[(scope)][!]: <description>`.
release-please reads them to choose the next version and write `CHANGELOG.md`, so the changelog is not edited by hand:

| Type | Meaning | In the changelog / next version |
|---|---|---|
| `feat` | a user-visible feature | Features; minor (0.x: minor) |
| `fix` | a user-visible bug fix | Bug Fixes; patch |
| `perf` | faster, same behaviour | Performance Improvements; patch |
| `feat!` / `fix!` / `BREAKING CHANGE:` footer | a breaking change | major (0.x: minor) |
| `docs`, `test`, `refactor`, `build`, `ci`, `chore`, `style` | everything else | not listed; no release on their own |

Write the description for a user ("Play shows the fee per contract", not "add fee field"); it is the changelog line.

## Before you open a pull request

All of these must pass (each runs in Linux containers, as CI does):

```bash
pytest                                     # in the dev container: the suite (e2e and the import contracts included)
docker compose -f deploy/dev/compose.yaml run --rm dev-pg pytest -m postgres -rs   # Postgres tests (throwaway 17)
docker compose -f deploy/dev/compose.yaml --profile postgres down                  # ... then remove that Postgres
pre-commit run --all-files                 # in the dev container: the quality gates (gitleaks, Ruff, ShellCheck,
                                           # hadolint, actionlint, import contracts); CI runs it too, then Trivy
```

and the quality gates (Ruff, import contracts, Hadolint, ShellCheck and Trivy, as CI runs them):
[docs/operations.md, "Quality gates"](docs/operations.md#quality-gates-ruff-trivy-hadolint-shellcheck).

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
