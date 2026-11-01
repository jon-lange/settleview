"""Multi-agent L0: agents are lasting identities, sessions are runs of them.

New table `agents` (id, name, market, created_ts, archived, updated_ts); `agent_sessions.agent_id` and
`agent_journal.agent_id` (with an index for an agent's journal across its runs). On a fresh database 0001 already
creates all of it (it builds from the current table definitions), so each step checks before it acts.

Backfill of the sessions made before this change (and their journal rows): one agent per (market list, name) the
sessions ran with, oldest session's start as its created_ts.
- A one-market session list -> an active agent on that market, named as the session was ("<market> agent" when it
  had no name, e.g. "Kalshi 15m agent"); a name clash with another backfilled agent gets " 2", " 3" ...
- A multi-market list (allowed before 2026-10-31, refused since) -> an ARCHIVED agent "Earlier runs: Kalshi 15m +
  Polymarket 15m" whose market is the keys joined by "," (it keeps its runs and journal; it can never run again,
  since an agent now trades one market).

Revision ID: 0016
Revises: 0015
"""
import json
import uuid

import sqlalchemy as sa
from alembic import op

from btc_signal_engine.adapters.sqlite.tables import REVISION_0016, metadata
from btc_signal_engine.domain.markets import UnknownMarket
from btc_signal_engine.domain.markets import market as market_spec

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

INDEX = "ix_agent_journal_agent"


def _cols(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def _short(key: str) -> str:
    try:
        return market_spec(key).label.replace(" min", "m").replace(" hour", "h")
    except UnknownMarket:  # a key no longer known: shown as it was stored
        return key


def _backfill(bind) -> None:
    rows = bind.execute(sa.text("select id, limits, started_ts from agent_sessions where agent_id is null "
                                "order by started_ts, id")).fetchall()
    if not rows:
        return
    # Booleans as typed parameters: Postgres has no boolean = integer (SQLite stores them as 0 / 1).
    no = sa.text("select name from agents where archived = :no").bindparams(sa.bindparam("no", False, sa.Boolean))
    taken = {r[0] for r in bind.execute(no).fetchall()}
    groups: dict[tuple, dict] = {}
    for sid, limits, started in rows:
        lim = json.loads(limits) if isinstance(limits, str) else (limits or {})
        markets = [lim["market"]] if lim.get("market") else list(lim.get("venues") or [])
        if not markets:
            markets = ["unknown"]
        name = lim.get("name") or (f"{_short(markets[0])} agent" if len(markets) == 1 else
                                   "Earlier runs: " + " + ".join(_short(m) for m in markets))
        key = (tuple(markets), name)
        g = groups.get(key)
        if g is None:
            archived = len(markets) != 1
            final = name
            if not archived:
                n = 2
                while final in taken:
                    final, n = f"{name} {n}", n + 1
                taken.add(final)
            g = groups[key] = {"id": uuid.uuid4().hex, "name": final, "market": ",".join(markets),
                               "created_ts": int(started), "archived": archived, "sessions": []}
        g["sessions"].append(sid)
    for g in groups.values():
        bind.execute(sa.text("insert into agents (id, name, market, created_ts, archived, updated_ts) values "
                             "(:id, :name, :market, :ts, :archived, :ts)").bindparams(
                                 sa.bindparam("archived", type_=sa.Boolean)),
                     {"id": g["id"], "name": g["name"], "market": g["market"], "ts": g["created_ts"],
                      "archived": bool(g["archived"])})
        for sid in g["sessions"]:
            bind.execute(sa.text("update agent_sessions set agent_id = :a where id = :s"), {"a": g["id"], "s": sid})
    bind.execute(sa.text("update agent_journal set agent_id = (select s.agent_id from agent_sessions s where "
                         "s.id = agent_journal.session_id) where agent_id is null"))


def upgrade() -> None:
    bind = op.get_bind()
    metadata.create_all(bind=bind, tables=list(REVISION_0016), checkfirst=True)
    if "agent_id" not in _cols("agent_sessions"):
        with op.batch_alter_table("agent_sessions") as batch:
            batch.add_column(sa.Column("agent_id", sa.String(40)))
    if "agent_id" not in _cols("agent_journal"):
        with op.batch_alter_table("agent_journal") as batch:
            batch.add_column(sa.Column("agent_id", sa.String(40)))
    if INDEX not in {i["name"] for i in sa.inspect(bind).get_indexes("agent_journal")}:
        op.create_index(INDEX, "agent_journal", ["agent_id", "id"])
    _backfill(bind)


def downgrade() -> None:
    bind = op.get_bind()
    if INDEX in {i["name"] for i in sa.inspect(bind).get_indexes("agent_journal")}:
        op.drop_index(INDEX, table_name="agent_journal")
    for table in ("agent_journal", "agent_sessions"):
        if "agent_id" in _cols(table):
            with op.batch_alter_table(table) as batch:
                batch.drop_column("agent_id")
    metadata.drop_all(bind=bind, tables=list(REVISION_0016), checkfirst=True)
