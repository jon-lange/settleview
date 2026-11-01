"""Export agent wakes from the journal into eval items (JSONL), redacted and scanned.

    python -m evals.export --source <sqlite file | postgres url> --out evals/datasets/replay/<name>.jsonl \
        [--ids 4098,255,...] [--all] [--since 2026-10-31] [--until ...] [--salt-file <outside git>]

Read-only on the source: a SQLite file opened `file:...?mode=ro`, or Postgres through the read-only research role
(`adapters/sqlite/research.readonly_engine`). Only these are read, by explicit column: `agent_journal` (wake, decision,
notes, rule, rule_status, state, note rows), `agent_sessions` (id, kind, state, limits, model, prompt versions, agent
id, start / end; never `token_hash` or `pinned`), `agent_wake_inputs` when the table exists (the exact message and
fingerprint, from 2026-11-01), and the outcome columns of `trading_brackets` (side, state, fill, cost, proceeds, fees,
exit reason; never order ids, ticket hashes or take-profit order ids). `trading_orders` and `trading_audit` are never
read.

One line per wake (schema `settleview.eval.wake/1`): `item_id`, `source` (journal id, pseudonymous session / agent),
`meta` (time, day, model, markets, level, rules, prompt versions, SDK / CLI, primary trigger, decision), `fidelity`
(events exact; memory exact or reconstructed; observe exact or missing; tool results exact or truncated), `input`
(events verbatim, memory, observe, the message the model saw or its reconstruction, the fingerprint), `recorded`
(tool calls with args and results, decisions, final text, segments, status, turns, tokens, cost), `context` (what the
graders need: rules armed, level, retired rules since the last wake, the live UP mid at the decision) and `outcome`
(derived trade results of brackets the wake touched: never graded, reported).

Redaction (evals/redact.py) runs on every item, then the deny-list scan runs on the finished items; any hit fails the
export and nothing is written (house rule 7). Pseudonyms are stable within one export (a random salt, or
`--salt-file` for several exports that must link).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text

from evals.redact import Redactor, scan

SCHEMA = "settleview.eval.wake/1"
TZ = ZoneInfo("America/Chicago")  # the inventory's day boundary (CDT/CST)
KINDS = ("wake", "decision", "notes", "rule", "rule_status", "state", "note")
FORBIDDEN = ("trading_orders", "trading_audit", "token_hash", "ticket_hash", "tp_order_id", "pinned")
# Every statement the exporter runs (tests assert none of them names a FORBIDDEN table or column).
Q_JOURNAL = ("select id, session_id, ts, kind, data, agent_id from agent_journal "
             "where kind in ('wake', 'decision', 'notes', 'rule', 'rule_status', 'state', 'note') order by id")
Q_SESSIONS = ("select id, kind, state, limits, model, prompt_versions, agent_id, started_ts, ends_ts, ended_ts, "
              "end_reason from agent_sessions order by started_ts")
Q_INPUTS = "select journal_id, message, message_sha256, fingerprint from agent_wake_inputs"
Q_BRACKETS = ("select id, session_id, ticker, side, state, filled, entry_cost, proceeds, tp_fees, exit_fees, "
              "exit_reason, window_end from trading_brackets where session_id is not null")
QUERIES = (Q_JOURNAL, Q_SESSIONS, Q_INPUTS, Q_BRACKETS)
PRIMARY = ("rule_entry", "settled", "position_closed", "near_stop", "near_take_profit", "session_ending",
           "entry_check", "window_ready", "window_open", "agent_alarm", "btc_near_strike", "window_ending",
           "call_changed", "price_move", "advice_changed", "advice_cleared", "human_note", "signal_changed")
DECISION_TEXT = 200
RAW_HEX32 = re.compile(r"(?i)\b[0-9a-f]{32}\b")


class ExportScanError(RuntimeError):
    """The deny-list scan found something the redaction let through; nothing was written."""

    def __init__(self, hits: list) -> None:
        self.hits = hits
        super().__init__(f"export refused: {len(hits)} deny-list hit(s); first: " + "; ".join(map(str, hits[:5])))


def _json(v: Any) -> Any:
    if isinstance(v, (bytes, bytearray)):
        v = v.decode("utf-8")
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


@dataclass
class Source:
    label: str
    sessions: dict[str, dict] = field(default_factory=dict)
    rows: dict[str, list[dict]] = field(default_factory=lambda: defaultdict(list))  # session -> rows by id
    by_id: dict[int, dict] = field(default_factory=dict)
    inputs: dict[int, dict] = field(default_factory=dict)
    brackets: dict[str, dict] = field(default_factory=dict)
    has_inputs_table: bool = False


def open_engine(source: str):
    """A SQLite file (opened mode=ro) or a postgresql:// URL (the read-only research role), read-only either way."""
    from btc_signal_engine.adapters.sqlite.research import readonly_engine
    from btc_signal_engine.adapters.sqlite.urls import postgres_url
    if "://" in str(source):
        return readonly_engine(postgres_url(str(source), setting="--source"))
    return readonly_engine(source)


def load(engine, label: str) -> Source:
    src = Source(label)
    with engine.connect() as c:
        for r in c.execute(text(Q_SESSIONS)).mappings():
            d = dict(r)
            d["limits"] = _json(d["limits"]) or {}
            d["prompt_versions"] = _json(d["prompt_versions"])
            src.sessions[d["id"]] = d
        for r in c.execute(text(Q_JOURNAL)).mappings():
            d = {**dict(r), "data": _json(r["data"])}
            src.rows[d["session_id"]].append(d)
            src.by_id[d["id"]] = d
        for r in c.execute(text(Q_BRACKETS)).mappings():
            src.brackets[r["id"]] = dict(r)
    try:
        with engine.connect() as c:
            for r in c.execute(text(Q_INPUTS)).mappings():
                src.inputs[int(r["journal_id"])] = {"message": r["message"], "sha256": r["message_sha256"],
                                                    "fingerprint": _json(r["fingerprint"])}
            src.has_inputs_table = True
    except Exception:  # noqa: BLE001 - the table exists from migration 0021 on; an older store has none
        src.has_inputs_table = False
    return src


# -- one wake -> one item -------------------------------------------------------------------------------------------
def _tag(message: str, name: str) -> str | None:
    a, b = f"<{name}>", f"</{name}>"
    if a not in message or b not in message:
        return None
    return message[message.index(a) + len(a):message.index(b)]


def _parts(message: str) -> dict[str, Any]:
    return {k: _json(_tag(message, k)) for k in ("memory", "events", "observe")}


def _dumps(x: Any) -> str:
    return json.dumps(x, separators=(",", ":"), default=str, ensure_ascii=False)


def _compact(d: dict) -> dict:
    keys = ("ts", "wake_id", "market", "window_end", "mid_up", "action", "side", "p_estimate", "p_low", "p_high",
            "p_source", "market_p", "edge_cents", "pass_reason", "thesis", "invalidation", "next_check", "note")
    out = {}
    for k in keys:
        v = d.get(k)
        if v is None:
            continue
        out[k] = v[:DECISION_TEXT] + "…" if isinstance(v, str) and len(v) > DECISION_TEXT else v
    return out


def primary_trigger(types: list[str]) -> str | None:
    for p in PRIMARY:
        if p in types:
            return p
    return types[0] if types else None


def _wake_decisions(rows: list[dict], wake: dict) -> list[dict]:
    wid = wake["data"].get("wake_id")
    return [r["data"] for r in rows if r["kind"] == "decision" and r["id"] < wake["id"]
            and r["data"].get("wake_id") == wid and r["data"].get("source") != "rule"]


def _retired_since(rows: list[dict], wake: dict, start_ts: int) -> list[str]:
    prev = max((r["id"] for r in rows if r["kind"] == "wake" and r["id"] < wake["id"]), default=0)
    return sorted({r["data"].get("rule") for r in rows if r["kind"] == "rule_status" and prev < r["id"] < wake["id"]
                   and r["data"].get("status") == "retired" and r["data"].get("rule")})


def _memory(src: Source, s: dict, rows: list[dict], start_ts: int) -> dict:
    lim = s.get("limits") or {}
    before = [r for r in rows if r["ts"] < start_ts]
    notes = next((r["data"].get("text") for r in reversed(before) if r["kind"] == "notes"), None)
    decs = [r["data"] for r in before if r["kind"] == "decision"][-5:]
    return {"provenance": "reconstructed from the journal (pre-0021: the memory block was not stored)",
            "session": {"venues": lim.get("venues"), "budget": lim.get("budget"), "per_trade": lim.get("per_trade"),
                        "spend_cap_usd": lim.get("spend_cap"), "rules": lim.get("rules") or [],
                        "level": lim.get("level"), "one_bet_per_window": lim.get("one_bet_per_window", True)},
            "recent_decisions": [_compact(d) for d in decs], "notes": notes}


def _stale(observe: Any) -> bool | None:
    if not isinstance(observe, dict) or not isinstance(observe.get("markets"), dict):
        return None
    flags = [((m or {}).get("market") or {}).get("fresh") for m in observe["markets"].values()]
    return any(f is False for f in flags) if flags else None


def _outcome(src: Source, rows: list[dict], raw: str) -> list[dict]:
    rule_of = {r["data"].get("bracket_id"): r["data"].get("rule") for r in rows
               if r["kind"] == "rule" and r["data"].get("ok")}
    out = []
    for bid in dict.fromkeys(m.group(0).lower() for m in RAW_HEX32.finditer(raw)):
        b = src.brackets.get(bid)
        if b is None:
            continue
        closed = b.get("state") == "closed"
        net = None
        if closed:
            net = round(float(b.get("proceeds") or 0) - float(b.get("entry_cost") or 0) - float(b.get("tp_fees") or 0)
                        - float(b.get("exit_fees") or 0), 4)
        out.append({"bracket_id": bid, "rule": rule_of.get(bid), "ticker": b.get("ticker"), "side": b.get("side"),
                    "state": b.get("state"), "filled": b.get("filled"), "exit_reason": b.get("exit_reason"),
                    "net": net, "won": None if net is None else net > 0})
    return out


def build_item(src: Source, wake: dict) -> dict:
    """One wake row -> one unredacted item (redact before it leaves this module)."""
    sid = wake["session_id"]
    s = src.sessions.get(sid) or {}
    rows = src.rows[sid]
    d = wake["data"]
    lim = s.get("limits") or {}
    start_ts = int(wake["ts"]) - int(d.get("duration_ms") or 0)
    trigger = d.get("trigger") or []
    types = list(dict.fromkeys(e.get("type") for e in trigger))
    tools = [{k: c.get(k) for k in ("name", "args", "ok", "code", "result")} for c in d.get("tools") or []]
    decs = _wake_decisions(rows, wake)
    ok_args = [c["args"] for c in tools if c["name"] == "record_decision" and c["ok"]]
    final = ok_args[-1] if ok_args else None
    wi = src.inputs.get(wake["id"])
    truncated = any(isinstance(c.get("result"), str) and c["result"].endswith("…") for c in tools)
    if wi is not None:
        parts = _parts(wi["message"])
        memory, observe = parts["memory"], parts["observe"]
        fidelity = {"events": "exact", "memory": "exact", "observe": "exact" if observe is not None else "missing"}
        fp = wi.get("fingerprint") or d.get("fingerprint")
    else:
        memory, observe = _memory(src, s, rows, start_ts), None
        fidelity = {"events": "exact", "memory": "reconstructed", "observe": "missing"}
        fp = d.get("fingerprint")
    fidelity["tool_results"] = "truncated" if truncated else "exact"
    message = "\n\n".join(("<memory>" + _dumps(memory) + "</memory>", "<events>" + _dumps(trigger) + "</events>",
                           "<observe>" + _dumps(observe) + "</observe>"))
    runtime = d.get("runtime") or {}
    dt = datetime.fromtimestamp(int(wake["ts"]) / 1000, TZ)
    live = next((x.get("mid_up") for x in decs if x.get("mid_up") is not None), None)
    raw = json.dumps([trigger, tools, decs], default=str)
    return {
        "schema": SCHEMA,
        "item_id": None,  # set after redaction (a salted hash of the journal id)
        "source": {"snapshot": src.label, "journal_id": wake["id"], "session": sid, "agent": s.get("agent_id"),
                   "wake_id": d.get("wake_id")},
        "meta": {"ts": int(wake["ts"]), "local_time": dt.strftime("%Y-%m-%d %H:%M %Z"), "day": dt.strftime("%Y-%m-%d"),
                 "model": d.get("model"), "markets": lim.get("venues"), "level": lim.get("level"),
                 "rules": lim.get("rules") or [], "prompt_versions": d.get("prompt_versions"),
                 "sdk": runtime.get("runtime"), "cli": runtime.get("cli_version"), "reason": d.get("reason"),
                 "primary_trigger": primary_trigger(types), "trigger_types": types, "status": d.get("status"),
                 "decision_action": (final or {}).get("action"), "pass_reason": (final or {}).get("pass_reason"),
                 "decision_market": (final or {}).get("market"), "session_kind": s.get("kind")},
        "fidelity": fidelity,
        "input": {"events": trigger, "memory": memory, "observe": observe, "message": message,
                  "message_verbatim": wi is not None, "fingerprint": fp},
        "recorded": {"tools": tools, "decision": final, "decisions": decs, "final_text": d.get("decision"),
                     "segments": d.get("segments"), "new_events": d.get("new_events"), "status": d.get("status"),
                     "stop_reason": d.get("stop_reason"), "turns": d.get("turns"), "tokens": d.get("tokens"),
                     "est_usd": d.get("est_usd"), "duration_ms": d.get("duration_ms"), "model": d.get("model"),
                     "thinking_summary": d.get("thinking"), "refusal": d.get("refusal")},
        "context": {"rules_armed": lim.get("rules") or [], "level": lim.get("level"),
                    "retired_rules": _retired_since(rows, wake, start_ts), "live_market_p": live,
                    "stale_data": _stale(observe), "notes_before": (memory or {}).get("notes")
                    if isinstance(memory, dict) else None},
        "outcome": {"trades": _outcome(src, rows, raw)},
    }


def redact_item(item: dict, r: Redactor) -> dict:
    jid = item["source"]["journal_id"]
    out = r.value(item)
    out["source"]["journal_id"] = jid  # the journal id is a row number, not an identifier of anything private
    out["item_id"] = "w-" + r.hid(f"journal:{jid}")[2:]
    return out


def wakes(src: Source, ids: list[int] | None = None, *, since: int | None = None,
          until: int | None = None) -> list[dict]:
    """The wake rows to export: `ids` (any journal id; a non-wake id resolves to its session's next wake row), else
    every wake row in [since, until)."""
    if ids:
        out = []
        for jid in ids:
            row = src.by_id.get(jid)
            if row is None:
                raise KeyError(f"journal id {jid} not found")
            if row["kind"] != "wake":
                row = next((r for r in src.rows[row["session_id"]] if r["kind"] == "wake" and r["id"] > jid), None)
                if row is None:
                    raise KeyError(f"journal id {jid} has no later wake in its session")
            out.append(row)
        return list({r["id"]: r for r in out}.values())
    return [r for rows in src.rows.values() for r in rows if r["kind"] == "wake"
            and (since is None or r["ts"] >= since) and (until is None or r["ts"] < until)]


def raw_ids(src: Source) -> tuple[str, ...]:
    """Every raw session / agent id (and the 8-character prefix the UI quotes): the scan's exact deny values."""
    out: set[str] = set()
    for s in src.sessions.values():
        for v in (s.get("id"), s.get("agent_id")):
            if v:
                out.add(str(v))
                if len(str(v)) >= 16:
                    out.add(str(v)[:8])
    return tuple(sorted(out))


def export_items(src: Source, rows: list[dict], redactor: Redactor, *, extra: tuple[str, ...] = ()) -> list[dict]:
    """Build, redact and scan; raises ExportScanError on any hit (nothing is returned to be written)."""
    for s in src.sessions.values():  # pseudonyms in a stable order, and known before any free text is redacted
        redactor.session(s["id"])
        if s.get("agent_id"):
            redactor.agent(s["agent_id"])
    items = [redact_item(build_item(src, w), redactor) for w in sorted(rows, key=lambda r: r["id"])]
    hits = scan(items, extra=raw_ids(src) + tuple(extra))
    if hits:
        raise ExportScanError(hits)
    return items


def write_jsonl(path: Path, items: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False, sort_keys=True) + "\n")
    os.replace(tmp, path)


def _day_ms(s: str | None) -> int | None:
    if not s:
        return None
    return int(datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=TZ).timestamp() * 1000)


def _salt(path: str | None) -> bytes | None:
    if not path:
        return None
    return Path(path).read_bytes().strip() or None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m evals.export", description=__doc__.split("\n\n")[0])
    ap.add_argument("--source", required=True, help="a SQLite file (read-only) or a Postgres URL (research role)")
    ap.add_argument("--out", required=True, help="the JSONL file to write")
    ap.add_argument("--ids", default="", help="comma-separated journal ids (a non-wake id -> its next wake)")
    ap.add_argument("--all", action="store_true", help="every wake (with --since / --until)")
    ap.add_argument("--since", help="YYYY-MM-DD (America/Chicago)")
    ap.add_argument("--until", help="YYYY-MM-DD, exclusive")
    ap.add_argument("--label", default=None, help="the snapshot's name in each item (default: the file stem)")
    ap.add_argument("--salt-file", default=None, help="a file holding the pseudonym salt (keep it out of git)")
    ap.add_argument("--split", action="store_true",
                    help="--out is a directory: one <journal id>.jsonl per wake (golden frames)")
    a = ap.parse_args(argv)
    ids = [int(x) for x in a.ids.split(",") if x.strip()]
    if not ids and not a.all:
        ap.error("give --ids or --all")
    label = a.label or (Path(a.source).stem if "://" not in a.source else "postgres")
    src = load(open_engine(a.source), label)
    rows = wakes(src, ids or None, since=_day_ms(a.since), until=_day_ms(a.until))
    salt = _salt(a.salt_file)
    r = Redactor(salt) if salt else Redactor()
    try:
        items = export_items(src, rows, r)
    except ExportScanError as e:
        print(str(e), file=sys.stderr)
        for h in e.hits[:50]:
            print(f"  {h}", file=sys.stderr)
        return 2
    out = Path(a.out)
    if a.split:
        for it in items:
            write_jsonl(out / f"{it['source']['journal_id']}.jsonl", [it])
        out = out / "export"
    else:
        write_jsonl(out, items)
    manifest = {"source": label, "items": len(items), "salt_id": r.salt_id, "wake_inputs_table": src.has_inputs_table,
                "fidelity_memory": {k: sum(1 for i in items if i["fidelity"]["memory"] == k)
                                    for k in ("exact", "reconstructed")},
                "scan": "clean"}
    out.with_suffix(".manifest.json").write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    print(f"exported {len(items)} wake(s) to {out} (scan clean; salt {r.salt_id})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
