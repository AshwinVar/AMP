"""machine identity is (tenant, site, name), not name

Revision ID: 0002_machine_site_identity
Revises: 0001_baseline
Create Date: 2026-08-09

WHY
---
MQTT ingest resolved a machine by name alone. Names are not unique — three
customers all run a "CNC-01" — so a packet from one customer's gateway landed
on whichever row the database returned first. Reproduced before this change:
FACTORY_B's breakdown telemetry flipped FACTORY_A's CNC-01 to Breakdown, and
the production/downtime/event rows it wrote landed under "DEFAULT", a tenant
neither customer can see.

The application-level fix is in mqtt_service. This migration is the part that
makes the same mistake IMPOSSIBLE to reintroduce anywhere else: a database
constraint, so a second CNC-01 in one tenant+site is rejected by PostgreSQL
rather than depending on every future query remembering to filter.

WHY site IS NOT NULL WITH AN EMPTY DEFAULT
-------------------------------------------
In PostgreSQL, NULL != NULL, so a UNIQUE constraint over a nullable column does
NOT prevent duplicates — ('ACME', NULL, 'CNC-01') can be inserted any number of
times and the constraint never fires. A nullable `site` would therefore look
like a fix while enforcing nothing. Empty string is the single-plant customer's
site, and it compares equal to itself.

WHAT HAPPENS TO EXISTING DUPLICATES
------------------------------------
A database that already contains two machines with the same name in the same
tenant cannot have this constraint added. There are three options and only one
of them is acceptable:

  * fail the migration       -> a deploy that cannot proceed, and the schema is
                                left half-applied on the next attempt
  * delete the duplicates    -> destroys a customer's machine and every
                                production record hanging off it. Never.
  * rename the duplicates    -> chosen. The row, its id and all its history are
                                untouched; only the display label changes, to
                                "CNC-01#57". It is visible in the UI, the
                                operator can rename it properly, and nothing is
                                lost if they don't.

The rename is logged with the exact rows affected so the deploy output says
what happened rather than leaving it to be discovered.
"""
import logging

import sqlalchemy as sa
from alembic import op

revision = "0002_machine_site_identity"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")

CONSTRAINT = "uq_machine_identity"


def _has_column(bind, table: str, column: str) -> bool:
    """main.py's boot-time _ensure_column may have already added `site` on a
    running deployment. Adding it twice is an error, so this migration checks
    the live schema rather than assuming it is the only thing that writes DDL.
    That dual-path reality is exactly why 0001 is an empty baseline."""
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def _has_constraint(bind, table: str, name: str) -> bool:
    """Same question for the constraint, and it is not hypothetical.

    On a FRESH database migrate.py runs create_all first, and create_all builds
    the constraint straight from models.py — so by the time this migration runs
    it is already there and ADD CONSTRAINT fails with DuplicateObject. On an
    EXISTING database create_all cannot add it, so the migration must. Both
    paths are real and the migration has to be correct on each, which is only
    knowable by asking the database rather than assuming which path ran.
    """
    return any(c["name"] == name
               for c in sa.inspect(bind).get_unique_constraints(table))


def upgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"

    if not _has_column(bind, "machines", "site"):
        # Added nullable, then backfilled, then made NOT NULL. Adding a NOT NULL
        # column with a default in one step is fine on modern PostgreSQL but
        # rewrites the table on older ones and is not supported at all by
        # SQLite's ALTER; three explicit steps behave the same everywhere.
        op.add_column("machines",
                      sa.Column("site", sa.String(), nullable=True,
                                server_default=""))

    op.execute("UPDATE machines SET site = '' WHERE site IS NULL")

    with op.batch_alter_table("machines") as batch:
        batch.alter_column("site", existing_type=sa.String(),
                           nullable=False, server_default="")

    # ---- de-duplicate before constraining -------------------------------
    duplicates = bind.execute(sa.text("""
        SELECT id, tenant_code, name FROM machines
        WHERE id NOT IN (
            SELECT MIN(id) FROM machines GROUP BY tenant_code, site, name
        )
    """)).fetchall()

    if duplicates:
        log.warning(
            "%d machine(s) share a (tenant, site, name) identity and are being "
            "renamed so the identity constraint can be applied. No rows or "
            "history are deleted.", len(duplicates))
        for row in duplicates:
            log.warning("  renaming machine id=%s tenant=%s %r -> %r",
                        row[0], row[1], row[2], f"{row[2]}#{row[0]}")
        bind.execute(sa.text("""
            UPDATE machines SET name = name || '#' || CAST(id AS VARCHAR)
            WHERE id NOT IN (
                SELECT MIN(id) FROM machines GROUP BY tenant_code, site, name
            )
        """))

    if _has_constraint(bind, "machines", CONSTRAINT):
        log.info("%s already present (create_all built it) — nothing to add",
                 CONSTRAINT)
        return

    # SQLite cannot ADD CONSTRAINT; batch mode rewrites the table. On
    # PostgreSQL this is a plain ALTER TABLE ... ADD CONSTRAINT.
    if is_sqlite:
        with op.batch_alter_table("machines") as batch:
            batch.create_unique_constraint(
                CONSTRAINT, ["tenant_code", "site", "name"])
    else:
        op.create_unique_constraint(
            CONSTRAINT, "machines", ["tenant_code", "site", "name"])


def downgrade() -> None:
    """Drops the constraint and the column.

    Machines renamed by upgrade() keep their suffixed names: the original name
    is not recoverable from the schema, and inventing one would be worse than
    leaving a name an operator can see and correct.
    """
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("machines") as batch:
            batch.drop_constraint(CONSTRAINT, type_="unique")
            batch.drop_column("site")
    else:
        op.drop_constraint(CONSTRAINT, "machines", type_="unique")
        op.drop_column("machines", "site")
