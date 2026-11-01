# Publishing checklist (for the owner)

Work through this before making the repository public. Nothing here runs automatically.

## 1. Scan for secrets across the full history

A secret that was ever committed stays in history even after it is deleted from the tree.

```bash
gitleaks git --redact -v .                   # or: gitleaks detect --redact -v (older versions)
trufflehog git file://. --only-verified=false
```

- Scan every branch and tag, not just `HEAD`.
- Confirm that `.env`, `key.pem`, `*.pem`, `*.db` and `data/` never appear in history:
  `git log --all --name-only --format= | sort -u | grep -iE '\.pem|(^|/)\.env$|\.db|^data/'` should print nothing.
- If anything real turns up, **rotate the key at the venue first** (Kalshi, Polymarket US), then rewrite history or
  start a fresh history (step 3).

## 2. Scrub personal details

Search the tracked files (and, if you keep history, all of history) and fix what you find:

- **LAN addresses**: `git grep -nE '192\.168\.|10\.[0-9]+\.[0-9]+\.[0-9]+'`. The addresses in `tests/api/test_access.py`
  are made-up test values; check that nothing else is your real network.
- **Personal paths**: `git grep -nE 'C:[\\/]+Users'`. The hard-coded paths to your own `.env` files
  and research folder in `scripts/quick/*` and `scripts/perp_trend_backtest.py` were replaced on 2026-10-31
  (`BTCFLOW_ENV_FILE`, required by the Polymarket penny scripts; the repo's own `data/research/`); re-run the grep to
  keep it that way. The `C:/Users/you/...` examples in `.env.example` and `docs/operations.md`, and
  `C:\Users\Someone\...` in `tests/evals/test_export_redaction.py`, are placeholders and fine.
- **Account details in `docs/research/`**: search for balances, cash amounts, order ids, client order ids, fill ids
  and exact live-trade amounts (`git grep -niE 'balance|order[_ ]?id|fill[_ ]?id|penny'`). The penny-test notes in
  `docs/research/LEARNINGS.md` describe behaviour, not accounts; reread them anyway.
- **Commit author email**: every commit carries your personal email. To hide it, set a GitHub no-reply address
  (`git config user.email <id>+<user>@users.noreply.github.com`) and either start a fresh history or rewrite authors.
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

## 3. Consider a squashed, fresh history

The simplest way to be sure nothing private leaks from old commits:

```bash
git checkout --orphan public
git add -A          # check `git status` first: nothing ignored should be staged
git commit -m "Settleview: initial public snapshot"
```

Push only that branch to the new public repository. Keep the full history in your private copy.

## 4. Check what will be published

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

## 5. GitHub settings after creating the repository

- Enable **private vulnerability reporting** (Settings → Code security). `SECURITY.md` points people to it.
- Enable secret scanning and push protection.
- Check that the CI workflow (`.github/workflows/ci.yml`) passes on the first push, and fix the Linux-specific
  problems it finds.

## 6. Later

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
