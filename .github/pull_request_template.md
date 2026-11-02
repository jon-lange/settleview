## What and why

<!-- What does this change, and why? Link the issue if there is one. -->

## How it was checked

<!-- Tests added or changed; any manual check (shadow mode, phone, light/dark). Say if anything was not checked. -->

## Checklist

- [ ] `pytest` passes in the dev container (`docker compose -f deploy/dev/compose.yaml run --rm dev pytest`, or
      `devcontainer exec --workspace-folder . pytest`).
- [ ] The quality gates pass: `pre-commit run --all-files` in the dev container (gitleaks, Ruff, ShellCheck,
      actionlint, the layer contracts in `.importlinter`; hadolint runs in the host's commit hook and in CI;
      docs/operations.md, "Quality gates"), and CI's lint job and Trivy.
- [ ] The title is a Conventional Commit (`feat: ...`, `fix: ...`; CONTRIBUTING.md): release-please writes the
      changelog from it.
- [ ] Tests added or updated for changed behaviour, and they need no network, keys or local database.
- [ ] No secrets: no `.env`, key files, key ids, key paths, balances, order ids, personal paths or IP addresses.
- [ ] No fake, demo or sample data in `web/` or `btc_signal_engine/`.
- [ ] No silent fallbacks: missing or unreadable values are refused with a clear message.
- [ ] Trading guards (approval, order gate, limits, kill switch, hard ceiling) are not weakened.
- [ ] Research changes follow the honesty rules in `AGENTS.md` and are recorded in `docs/research/LEARNINGS.md`.
- [ ] Docs (`README.md`, `docs/`) updated if behaviour or settings changed.

## Contributor license agreement

- [ ] I agree to the contributor license agreement in [CONTRIBUTING.md](../CONTRIBUTING.md#contributor-license-agreement):
      I keep my copyright, I grant the copyright holder a perpetual, irrevocable, worldwide, royalty-free license to
      use, sublicense and relicense this contribution under any terms (including commercial), and I have the right
      to grant it.
