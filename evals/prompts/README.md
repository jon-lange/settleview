# Agent prompts (planned home)

Decided (user, 2026-10-31): git is the source of truth for the agent prompts, and they will live here. Each prompt
gets a version label and a changelog line. The app loads the deployed version, and in-app edits become drafts that
are exported for review. The files are shaped so that a later push to a prompt registry (Langfuse-style labels such
as `production` and `staging`) is a one-way sync.

**Nothing has moved yet.** The default system prompt is still `docs/agent/default-system-prompt.md`, which the app
loads at start-up (`bootstrap/app.py`, `AGENT_DEFAULT_PROMPT`). The user layers are still rows in the `agent_prompts`
table. Live eval runs (`--live`) use that file plus the app's tool note, so a prompt change is evaluated from git
today.

Before anything is copied here: system prompt v1 in the database contains one 32-hex string. Review it before it is
committed (docs/research/2026-10-31-eval-data-inventory.md, "Sensitive data"). The move is its own change, and it
must pass the evals as a prompt change: baseline against candidate on the same items.
