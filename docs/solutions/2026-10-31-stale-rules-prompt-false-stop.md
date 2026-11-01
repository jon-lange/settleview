# Agent stopped itself over a stale rules prompt

**Problem.** 2026-10-31 09:51 CDT the Polymarket 15m agent (Haiku, Auto, $1 rules) ended its own session (with a small gain),
reason: "Mode conflict: app reports PRODUCTION with real money … while the armed rules were described as
experimental $1 tests, and there is no validated edge for them."

**Cause.** The stored user prompt layer (v3, "RULES MODE") was written when only H4 and CUSHION100 existed and
named Kalshi 15m as their market. The session armed LATERESID and HPANIC on Polymarket 15m. Entries from rules the
prompt did not mention, on a market it did not name, plus the system prompt's "Production needs a validated edge"
rule, read as unauthorized real-money trading, and the agent failed closed. The stop was correct behaviour; the
prompt was the bug.

**Fix.** User layer v4 (user's choice B): lists all four rules with their time windows, says each agent trades only
its own market, and states that this is a Production $1 test of rules with no proven edge, authorized at launch.
The agent's right to end the session was deliberately left unchanged (option A, narrowing when it may stop, was
declined: it weakens a safeguard to fix a missing-information bug).

**How to avoid.** The rules section should be generated from the armed rules (name, window, market, evidence)
instead of hand-written text, so adding or reactivating a rule can never leave the prompt stale. Until then, any
rule added to `agent_rules` needs the user layer updated in the same change. Running sessions keep the prompt
version they launched with (`prompt_versions`); a prompt fix reaches a session only on relaunch.
