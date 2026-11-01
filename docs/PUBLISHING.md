# Publishing (for the owner)

Since 2026-11-01 the private GitHub repository (`origin`, `jon-lange/settleview`) is the source of truth, and its
`main` is the project's history. Going public later means flipping the repository's visibility on GitHub after the
checklist below; there is no separate public copy to build.

## How a change reaches GitHub

1. Branch from an up-to-date `main` (`git fetch origin && git switch -c <type>/<topic> origin/main`; a worktree works
   the same way).
2. Commit, with a [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/) message (`feat: ...`,
   `fix: ...`; CONTRIBUTING.md): release-please makes the releases and the changelog from them. Every commit's author
   and committer use the GitHub no-reply address: this clone's `user.email` is set to it (`git config user.email`
   prints `<id>+<user>@users.noreply.github.com`). Check it in a new clone or worktree before the first commit.
3. Push the branch and open a pull request, or push `main` directly from the owner's own session. Pushes are the
   owner's call: coding agents and subagents never push (AGENTS.md, hard rule 5); the owner's main session pushes
   only with the owner's approval.
4. gitleaks (below) runs on every commit and push, and again in CI, and refuses anything that must not reach
   GitHub. GitHub itself refuses a push whose commits carry the owner's private email (the account setting below).

**Retired** (2026-11-01): the `public` orphan branch, cherry-picking commits onto it, and squashed "public snapshot"
commits. Don't recreate them.

**The old private history** (before 2026-11-01; it shares no commit with `main`) is kept as a git bundle outside
the repository, on the owner's machine. It is never pushed.

## Secret and personal-data scanning

[gitleaks](https://github.com/gitleaks/gitleaks), run by [pre-commit](https://pre-commit.com)
(`.pre-commit-config.yaml`; pre-commit is a dev dependency, in the dev container already). Install the git hooks once
per clone (a worktree shares its clone's hooks):

```bash
uv tool install pre-commit==4.7.0   # on the host, once (the version uv.lock pins); or any pre-commit 4.x
pre-commit install                  # the pre-commit and pre-push hooks (default_install_hook_types)
```

Then, with the config in `.gitleaks.toml` (gitleaks' default rules, extended):

- **On every commit**: the staged diff (gitleaks' official hook).
- **On every push**, and in `pre-commit run --all-files` (the quality gate, CI's `lint` job included): every commit
  of the branch (`gitleaks git --log-opts=HEAD`), so a commit made without the hooks is caught before it leaves.
- **What it refuses**: gitleaks' default rules (private keys; cloud, GitHub, AI-provider, Slack, Stripe and many
  other tokens) and this project's: any `@gmail.com` / `@googlemail.com` address, and Windows user-profile paths (a
  drive letter's `Users` folder, also as Git Bash and WSL write it, under `/c/` or `/mnt/c/`). Findings are redacted.
- **Placeholders**: a line in docs or tests with a made-up value carries gitleaks' standard `gitleaks:allow` comment;
  the usual placeholder accounts (`C:/Users/you`, `/c/Users/me`, `Someone`, `<name>`) are allowed by the rules'
  allowlists. A reviewed false positive in history goes in `.gitleaksignore` (gitleaks' fingerprint file).
- **Machine-local patterns** (your real name, LAN host names, the deploy checkout's folder name) never go in the
  repository. Put them in an untracked gitleaks config outside it that extends the tracked one, and point gitleaks at
  it with `GITLEAKS_CONFIG` (set in your shell profile; gitleaks reads it before `.gitleaks.toml`):

  ```toml
  # ~/.config/gitleaks/settleview.toml   (never committed)
  [extend]
  path = ".gitleaks.toml"   # the checkout's own config (hooks run at the checkout's top), which extends the defaults

  [[rules]]
  id = "local-names"
  description = "This machine's private names"
  regex = '''(?i)\b(?:your-real-name|your-deploy-folder|your-nas-host)\b'''
  keywords = ["your-real-name", "your-deploy-folder", "your-nas-host"]
  ```

**Commit emails**: GitHub's own account setting, not a hook. Turn on *Settings → Emails → "Keep my email addresses
private"* and *"Block command line pushes that expose my email"*: GitHub then refuses any push whose commits carry the
private address. Commits use the no-reply address (step 2 above).

**Tags**: release tags are made by release-please on GitHub (docs/operations.md, "Releases"); nobody pushes a tag by
hand, and there is no local-only tag to protect.

`git commit --no-verify` / `git push --no-verify` skip the hooks. Don't, except for a reviewed false positive, and say
so in the pull request; CI runs the same scan anyway.

## Before making the repository public

Work through this before flipping the visibility. Nothing here runs automatically.

### 1. Scan for secrets across the full history

A secret that was ever committed stays in history even after it is deleted from the tree. Scan what GitHub has
(`origin/main` and every pushed branch):

```bash
gitleaks git --redact -v --log-opts="--remotes=origin" .
trufflehog git file://. --only-verified=false --branch main
```

- Confirm that `.env`, `key.pem`, `*.pem`, `*.db` and `data/` never appear in that history:
  `git log --remotes=origin --name-only --format= | sort -u | grep -iE '\.pem|(^|/)\.env$|\.db|^data/'` should print
  nothing.
- If anything real turns up, **rotate the key at the venue first** (Kalshi, Polymarket US), then rewrite the history
  on GitHub before the flip.

### 2. Scrub personal details

Search the tracked files and the pushed history and fix what you find:

- **LAN addresses**: `git grep -nE '192\.168\.|10\.[0-9]+\.[0-9]+\.[0-9]+'`. The addresses in `tests/api/test_access.py`
  are made-up test values; check that nothing else is your real network.
- **Personal paths**: `git grep -nE 'C:[\/]+Users'`. The hard-coded paths to your own `.env` files
  and research folder in `scripts/quick/*` and `scripts/perp_trend_backtest.py` were replaced on 2026-10-31
  (`BTCFLOW_ENV_FILE`, required by the Polymarket penny scripts; the repo's own `data/research/`); gitleaks keeps it
  that way. The example user folders in `.env.example` and `docs/operations.md`, and the one in
  `tests/evals/test_export_redaction.py`, are placeholders and fine.
- **Account details in `docs/research/`**: search for balances, cash amounts, order ids, client order ids, fill ids
  and exact live-trade amounts (`git grep -niE 'balance|order[_ ]?id|fill[_ ]?id|penny'`). The penny-test notes in
  `docs/research/LEARNINGS.md` describe behaviour, not accounts; reread them anyway.
- **Commit author email**: done 2026-11-01. Every commit on GitHub uses the no-reply address (the old history with
  the personal address is the bundle outside the repository only), and GitHub's "Block command line pushes that
  expose my email" refuses a push that carries the private one.
- **Agent and plan notes**: `AGENTS.md`, `CLAUDE.md` and `docs/plans/` describe the live setup generically ("a
  separate deploy checkout", "the owner", "a Windows ARM64 dev host"; decided 2026-11-01). Machine-specific notes
  for local agents (the deploy checkout's folder name) live in the git-ignored `CLAUDE.local.md`, never in tracked
  files.
- **Decisions applied 2026-11-01** (keep them true on every later change):
  - No account-level results: no account or session dollar P&L, no personal trade lists, no balances (including the
    small perps test balance). Per-rule research stays: n, win % vs break-even, ¢/contract and the verdict.
  - Recorded fixtures carry fake ids (`tests/fixtures/kalshi_margin_ws/prod_private_capture.json`: order, trade,
    client order and trigger ids replaced consistently; prices, sizes and timestamps are real).
  - No hard-coded bot name: the Telegram bot's username is `BTCFLOW_TELEGRAM_BOT_USERNAME` or Telegram's `getMe`.
  - Tests use made-up people and bots (`@alex_example`, `@example_settleview_bot`).
  - Known exception, reviewed: `evals/datasets/golden/frames/*.jsonl` are redacted real agent wakes (the export's
    deny-list scan); their text includes per-rule $1 tallies the agent wrote. Re-check before adding new frames.

### 3. Check what is tracked

- `git status --ignored`: `.env`, `*.pem`, `data/`, `*.log`, `*.egg-info/` and any `.venv*/` left from before the dev
  container must show as ignored.
- `git ls-files`: nothing unexpected (no databases, logs, key files, screenshots with account data).
- The `Dockerfile` uses Python 3.12, but the project needs 3.13. Fix or remove it and `compose.yaml` before
  publishing.
- **Licence** (decided 2026-11-01): PolyForm Noncommercial 1.0.0 (`LICENSE`, the official text with the line
  `Required Notice: Copyright (c) 2026 Jon Lange`; `pyproject.toml` `license = "PolyForm-Noncommercial-1.0.0"`, the
  SPDX id; the image label says the same). Source-available, not OSI open source: free for noncommercial use;
  commercial use needs a separate licence from the copyright holder. Commercial licences are offered (README, "License").
  Outside contributions need the contributor licence agreement (CONTRIBUTING.md, "Contributor license agreement";
  a checkbox in `.github/pull_request_template.md`): the contributor keeps copyright and grants a perpetual,
  irrevocable relicensing licence, commercial use included.
- Third-party licences: `NOTICE` lists the vendored libraries (Lightweight Charts, Apache-2.0, with its NOTICE;
  Alpine.js's CSP build, MIT), whose licence texts are in `web/vendor/LICENSE-*`. The charts turn off TradingView's attribution
  logo (`attributionLogo: false`), so the README credits link to tradingview.com instead. The opt-in `openobserve`
  compose service runs the unmodified OpenObserve image, which is AGPL-3.0: it is a separate container, credited in
  the README, and none of its code is copied here. The OpenTelemetry packages (the `otel` extra) are Apache-2.0.
- `.env.example` documents only the general settings. Consider adding the trading settings
  (`BTCFLOW_TRADING_ENABLED`, `BTCFLOW_TRADING_PROD_KEY_*`, `BTCFLOW_TRADING_DEMO_KEY_*`,
  `BTCFLOW_POLYMARKET_KEY_ID`, `BTCFLOW_POLYMARKET_SECRET_KEY`, `BTCFLOW_ACCESS_SESSION_HOURS`) as commented-out
  names with no values.

### 4. GitHub settings

- Enable **private vulnerability reporting** (Settings → Code security). `SECURITY.md` points people to it.
- Enable secret scanning and push protection.
- Check that the CI workflow (`.github/workflows/ci.yml`) is green on `main`.

### 5. Flip the visibility

Only after steps 1 to 4: Settings → General → Danger Zone → Change visibility → Public. The history, branches and
pull requests that exist on GitHub become public as they are; the old history's bundle stays offline. Then add the support
links below.

## Later

- Rename the repository folder and Python package from `btc-flow-engine` / `btc_signal_engine` to `settleview` (the
  distribution name in `pyproject.toml` is already `settleview`). Update imports, `.importlinter`, the tests, the
  Dockerfile and the docs together.
- Add screenshots to `docs/images/` (light and dark, phone and desktop) and link them from the README. Take them in
  shadow mode, and make sure no balance, position, order id, LAN address or PIN is visible.

## Support links (at the public flip; user 2026-11-01)

- Add `.github/FUNDING.yml` with only accounts that exist and pay out: `buy_me_a_coffee: <page name>` (account being
  set up by the user), optionally `github: jon-lange` once a GitHub Sponsors profile exists, or `ko_fi:`. One or two
  links, never more.
- README: one short "Support" line near the end (after License), not in the hero: support is for the software.
- Wording rules (real-money trading app): no claims about results next to the support link; no paid tiers, perks,
  signals, rules or alerts; keep the disclaimer and the "no edge claimed; risk stated" framing.
