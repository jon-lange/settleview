# SQLite to Postgres: what SQLite let through that Postgres refuses

**Problem.** Moving the app's database to Postgres (docs/plans/2026-10-31-0900-feat-postgres-plan.md) with "the same
SQLAlchemy code" is not enough: SQLite accepted data and relied-on behaviour that Postgres does not give, and none of
it shows up in a SQLite-only test run.

**Causes** (each found while building G1 / G6, 2026-10-31):
- **JSON columns have NUMERIC affinity in SQLite.** A column declared `JSON` stores the text `10` or `10.0` as the
  integer 10 (checked: `typeof()` says `integer`). SQLAlchemy reads it back as 10, but a copy that reads the raw value
  gets an int, not JSON text, and a checksum that compares raw text with Postgres's says the rows differ.
- **Postgres's `json` / `jsonb` refuse `NaN` and `Infinity`**, which Python's `json.dumps` writes and SQLite stores
  as text. Features and data-quality rows can hold them.
- **VARCHAR(n) lengths were never enforced by SQLite.** On Postgres a longer reason or code would fail the whole
  write batch (trading rows included).
- **Booleans were 0 / 1.** Postgres has no `boolean = integer`; raw SQL like `archived = 0` (migration 0016) and
  rows built with ints fail there.
- **Row order.** An unordered `SELECT` on SQLite comes back in rowid order (insertion order), and code and tests came
  to rely on it. Postgres gives no order, and an updated row moves to the end of the table.
- **`INSERT OR REPLACE` is not an upsert.** It deletes the old row and inserts the new one, so every column not given
  goes back to its default; `ON CONFLICT DO UPDATE` keeps them unless told otherwise.

**Fix.** JSON stays text on Postgres (`tables.JsonText`, json.dumps/loads: NaN-safe and the same round trip);
Postgres VARCHAR has no length (`tables._pg_string`); the store turns 0 / 1 into bools for Boolean columns and gives
every unordered `select` a default order (id, else creation time, else key) on both dialects; the upsert sets every
non-key column from `excluded` (defaults included), which is what `OR REPLACE` left. The move tool writes SQLite's
JSON numbers back as JSON text and normalises values (bool = int, 1 = 1.0) before hashing.

**How to avoid.** Run the whole suite on both databases (CI's `postgres` job). For any new JSON column that a query
will read inside, use JSONB in its own migration and keep NaN out of it. Never write raw SQL with integer booleans;
use typed bind parameters. Ask for an order when the order matters.
