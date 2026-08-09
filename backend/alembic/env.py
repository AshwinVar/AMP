"""Alembic environment for AMP.

Reads DATABASE_URL from the environment rather than alembic.ini so a
migration can never be pointed at the wrong database by a checked-in
string — the same reason database.py uses os.environ["DATABASE_URL"]
without a default.

Imports models so `alembic revision --autogenerate` can diff the live
schema against Base.metadata. Importing models must NOT import main:
main runs the whole boot sequence (create_all, ~30 boot migrations, event
subscribers, the simulator), and running that during a migration would be
both slow and circular.
"""
import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Alembic runs from backend/, but be explicit so `alembic -c backend/alembic.ini`
# from the repo root works too.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import Base  # noqa: E402
import models  # noqa: E402,F401  (registers every table on Base.metadata)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is not set. Alembic will not guess a database — "
        "export the same DATABASE_URL the application uses."
    )

# NOTE: the URL is deliberately NOT written back into `config`.
#
# Alembic's Config is backed by configparser, and configparser treats % as
# interpolation syntax. `config.set_main_option("sqlalchemy.url", DATABASE_URL)`
# therefore raises
#
#     ValueError: invalid interpolation syntax in '...' at position N
#
# for ANY url-encoded character — and url-encoding is the normal case, not an
# exotic one: a Postgres password containing '@' arrives as %40, '/' as %2F,
# ':' as %3A. Railway, RDS and Supabase all generate passwords from character
# sets that include them.
#
# That crash killed the release command, and it did so AFTER migrate.run() had
# created the schema but BEFORE it stamped alembic_version — leaving a database
# that no later deploy could migrate, while the app still booted because main.py
# builds its own schema with create_all. A working product with dead migrations
# is the worst version of this failure, because nothing surfaces it.
#
# Escaping (% -> %%) would also work, but it relies on writing an escaped value
# and reading an un-escaped one back through the same interpolation pass. Not
# putting a connection string through a templating engine at all is simpler and
# cannot drift. Both consumers below take DATABASE_URL directly.
#
# alembic.ini keeps `sqlalchemy.url =` blank, which test_migrate.py asserts.

target_metadata = Base.metadata


def _include_object(obj, name, type_, reflected, compare_to):
    """Keep autogenerate focused on the application's own schema.

    alembic_version is Alembic's bookkeeping table and must never appear in a
    migration. Indexes created by the boot-time _ensure_index() helpers in
    main.py exist in the database but not in models.py metadata; autogenerate
    would otherwise propose dropping them on the first run, which would undo
    the deliberate index programme those calls represent.
    """
    if type_ == "table" and name == "alembic_version":
        return False
    if type_ == "index" and reflected and compare_to is None:
        return False
    return True


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of executing it.

    `alembic upgrade head --sql` produces a reviewable script — the safe way
    to see exactly what will run against production before it runs.
    """
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=_include_object,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # The section is still read so any other sqlalchemy.* tuning in alembic.ini
    # is honoured, but the URL is injected here rather than templated through
    # configparser — see the note above DATABASE_URL. `alembic.ini` leaves
    # sqlalchemy.url blank, so there is nothing to override.
    section = dict(config.get_section(config.config_ini_section, {}) or {})
    section["sqlalchemy.url"] = DATABASE_URL
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=_include_object,
            compare_type=True,
            # SQLite cannot ALTER most things in place; batch mode rewrites the
            # table instead. Harmless on PostgreSQL, essential for running the
            # same migration against a SQLite test database.
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
