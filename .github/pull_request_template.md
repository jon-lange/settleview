## What and why

<!-- What does this change, and why? Link the issue if there is one. -->

## How it was checked

<!-- Tests added or changed; any manual check (shadow mode, phone, light/dark). Say if anything was not checked. -->

## Checklist

- [ ] `python -m pytest` passes.
- [ ] `lint-imports` passes (layer contracts in `.importlinter`).
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
