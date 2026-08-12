"""Alembic environment.

Builds the database URL from ``app.config.Settings`` (the same source
every other part of the app reads config from) rather than a hardcoded
``sqlalchemy.url`` in ``alembic.ini`` — this avoids duplicating Postgres
credentials in a second place. ``DATABASE_URL`` / ``TEST_DATABASE_URL``
env vars override it, which is how the test suite points migrations at
the ephemeral test database (see ``tests/conftest.py``).

No SQLAlchemy ORM models exist anywhere in this codebase — migrations
are plain SQL via ``op.execute()`` (see ``migrations/versions/``).
SQLAlchemy itself is only present here as Alembic's own dependency, for
its engine/connection abstraction.
"""

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None


def _database_url() -> str:
    """Resolve the Postgres URL, rewritten for SQLAlchemy's psycopg3 dialect."""
    raw = (
        os.environ.get("DATABASE_URL")
        or os.environ.get("TEST_DATABASE_URL")
        or get_settings().database_url
    )
    return raw.replace("postgresql://", "postgresql+psycopg://", 1)


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live DB connection."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Connect to the database and run migrations directly."""
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(
        configuration, prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
