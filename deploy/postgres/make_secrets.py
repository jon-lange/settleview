"""Write the secrets compose.yaml reads, once: `python deploy/postgres/make_secrets.py` (any OS, standard library only).
Creates `secrets/` next to compose.yaml with one random password per role; an existing file is kept (the database or
OpenObserve already uses it), so running it again only adds what is missing. Prints file names only, never a value.
`secrets/` is git- and docker-ignored.

  pg_superuser_password   the `postgres` superuser (administration only; no service uses it)
  pg_app_password         settleview_app: the app and the migrate job
  pg_recorder_password    settleview_recorder: the research recorders (the schema research only;
                          deploy/postgres/roles.sh)
  pg_research_password    settleview_research: read-only, for research scripts
  pg_backup_password      settleview_backup: the backup service
  openobserve.env         OpenObserve's root password (ZO_ROOT_USER_PASSWORD=...), the `openobserve` service's env file
                          (profile `observability`; its image has no shell, so it cannot read a secret file itself)
  openobserve_ingest_token
                          OpenObserve's ingest-only organization token, mounted into the otel-collector only (the
                          services send to the collector and hold no backend credential): created EMPTY here. Once
                          OpenObserve runs, deploy/openobserve/ingest_token.py writes it (the collector never holds
                          the root password).
  polymarket_key_id, polymarket_secret_key, anthropic_api_key, telegram_bot_token, kalshi_key_id,
  trading_demo_key_id, trading_prod_key_id
                          the venue / API secrets compose mounts into the app (the Polymarket pair also into
                          rec-pm-quotes), created EMPTY (= not configured): put the value in the ones you use (one
                          line), or move them out of an existing `.env` with deploy/secrets_from_env.py. Never
                          overwritten.

To change a Postgres password later: ALTER ROLE ... PASSWORD in psql first, then write the new value to its file and
restart the services that use it (docs/operations.md, "Postgres"). OpenObserve keeps the root password it first started
with in its own volume: change it in its UI, then delete secrets/openobserve.env and run this again with the new
password in O2_ROOT_PASSWORD (docs/operations.md, "Observability").
"""

from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

NAMES = ("pg_superuser_password", "pg_app_password", "pg_research_password", "pg_backup_password",
         "pg_recorder_password")
# The venue / API secrets (each the file of a BTCFLOW_<NAME>_FILE setting in compose.yaml); the same list as
# deploy/secrets_from_env.py's.
APP_SECRETS = ("polymarket_key_id", "polymarket_secret_key", "anthropic_api_key", "telegram_bot_token", "kalshi_key_id",
               "trading_demo_key_id", "trading_prod_key_id")
O2_ENV = "openobserve.env"
O2_INGEST_TOKEN = "openobserve_ingest_token"  # noqa: S105 - a file name, not a secret


def _write(f: Path, text: str) -> None:
    fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    with os.fdopen(fd, "w", encoding="ascii", newline="\n") as out:
        out.write(text)


def _kept(f: Path) -> bool:
    if f.exists() and f.stat().st_size > 0:
        print(f"kept    {f}")
        return True
    return False


def o2_password() -> str:
    """Random, and always with the upper case letter, lower case letter, digit and symbol OpenObserve requires."""
    return "Sv-" + secrets.token_urlsafe(24) + "-9a"


def main(argv: list[str]) -> int:
    if len(argv) > 2 or (len(argv) == 2 and argv[1].startswith("-")):
        # never take an option (`--help`) for the folder: that wrote a secrets folder named "--help"
        print((__doc__ or "").strip() + "\n\nusage: python deploy/postgres/make_secrets.py [folder]", file=sys.stderr)
        return 0 if len(argv) == 2 and argv[1] in ("-h", "--help") else 2
    root = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parents[2] / "secrets"
    # The folder is owner-only; the files are world-readable so the containers' own users (postgres, the app's uid
    # 10001) can read the bind-mounted files on Linux. Other users on the host cannot reach them through the folder.
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name in NAMES:
        f = root / name
        if not _kept(f):
            _write(f, secrets.token_urlsafe(32) + "\n")  # URL-safe: no quoting needed anywhere
            print(f"created {f}")
    env_file = root / O2_ENV
    if not _kept(env_file):
        _write(env_file, f"ZO_ROOT_USER_PASSWORD={os.environ.get('O2_ROOT_PASSWORD') or o2_password()}\n")
        print(f"created {env_file}")
    for name in (O2_INGEST_TOKEN, *APP_SECRETS):
        f = root / name
        if f.exists():
            print(f"kept    {f}")
        else:
            _write(f, "")
            print(f"created {f} (empty: not configured)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
