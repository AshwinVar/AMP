"""record when a machine was last serviced, instead of assuming it was on time

Revision ID: 0007_oem_service_clock
Revises: 0006_oem_foundation
Create Date: 2026-08-09

WHY
---
Service position was derived as `operating_hours % service_interval_hours`. That
arithmetic silently ASSUMES every service happened exactly on schedule, and the
assumption is not neutral — it makes the `overdue` state unreachable.

Measured against a 2,000 h interval before this migration:

    machine at 2,100 h, NEVER serviced
        modulo form  -> "1,900 h remaining"   (state: ok)
        truth        ->   100 h OVERDUE

An OEM reading the first figure sends nobody. The state existed in the code and
could not occur in the data, which is the same shape as the fabricated OEE this
platform removed in ADR-0014.

WHAT THIS ADDS
--------------
  machine_installations.last_service_hours   the hours reading at the last
                                             completed service. NULL = never
                                             serviced, which is why a machine
                                             that has never been touched is now
                                             correctly overdue rather than
                                             assumed fresh.
  machine_installations.last_service_at      when that service happened.

Both NULLABLE with no backfill, deliberately. Writing 0 would claim every
existing machine was serviced at zero hours; writing `operating_hours` would
claim every machine was serviced just now — one of those invents a service that
never happened, and the other hides every service that is due. NULL says the
only true thing: nobody has recorded a service for this machine yet.
"""
import sqlalchemy as sa
from alembic import op

revision = "0007_oem_service_clock"
down_revision = "0006_oem_foundation"
branch_labels = None
depends_on = None

_COLUMNS = ("last_service_hours", "last_service_at")


def _cols(bind):
    insp = sa.inspect(bind)
    if "machine_installations" not in set(insp.get_table_names()):
        return None
    return {c["name"] for c in insp.get_columns("machine_installations")}


def upgrade() -> None:
    present = _cols(op.get_bind())
    if present is None:
        return
    if "last_service_hours" not in present:
        op.add_column("machine_installations",
                      sa.Column("last_service_hours", sa.Float(), nullable=True))
    if "last_service_at" not in present:
        op.add_column("machine_installations",
                      sa.Column("last_service_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    present = _cols(op.get_bind())
    if present is None:
        return
    for column in _COLUMNS:
        if column in present:
            op.drop_column("machine_installations", column)
