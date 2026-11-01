# An append-only table needs its schema taken away too

**Problem.** P3 item 5: make `trading_audit` append-only for the app's role. Revoking `UPDATE`, `DELETE` and
`TRUNCATE` is not enough while that role owns the table (an owner can grant itself everything back, or disable the
trigger), and moving the table to another owner is still not enough while the role owns the schema `app`.

**Cause.** In Postgres the owner of a schema may `DROP` any table in it, whoever owns the table. `settleview_app`
owned `app` (initdb.sh), so it could have dropped the audit outright.

**Fix.** `deploy/postgres/audit.sql` gives the table, its sequence, its trigger function **and the schema** to the
no-login role `settleview_audit`, then grants the app `USAGE, CREATE` on the schema and `SELECT, INSERT` on the table.
The app still owns (and migrates) its other tables. On a new volume the ownership move cannot run at initdb (the
table is made later by the app's migration), so `audit.sql` defines a `SECURITY DEFINER` function the migration calls;
on an existing volume the operator runs `audit.sql` once after the migration.

**How to avoid.** When locking a table away from a role, check every path: table privileges, table ownership, the
trigger function's owner (`CREATE OR REPLACE` by its owner), the sequence (`setval`) and the schema's owner (`DROP`).
`tests/storage/test_audit_postgres.py` tries each one as the app's role.
