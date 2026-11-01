from alembic import context
from sqlalchemy import create_engine

from btc_signal_engine.adapters.sqlite.tables import metadata

config = context.config
target_metadata = metadata


def _run(connection) -> None:
    # Batch mode (copy-and-move table rebuilds) only where ALTER is limited: SQLite. Postgres alters in place.
    context.configure(connection=connection, target_metadata=target_metadata,
                      render_as_batch=connection.dialect.name == "sqlite")
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # db.migrate() hands Postgres a connection (schema search_path, advisory lock, one transaction); SQLite and the
    # alembic command line give a URL.
    connection = config.attributes.get("connection")
    if connection is not None:
        _run(connection)
        return
    engine = create_engine(config.get_main_option("sqlalchemy.url"), hide_parameters=True)
    with engine.connect() as connection:
        _run(connection)


run_migrations_online()
