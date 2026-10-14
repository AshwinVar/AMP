"""the plant's own timezone, so the board reads local time

Revision ID: 0017_tenant_timezone
Revises: 0016_production_energy
Create Date: 2026-10-14

WHY
---
ProductionRecord.created_at is UTC. The plant board bucketed a day and its hours
straight off that UTC timestamp, so a plant in Asia/Kolkata (UTC+5:30) saw a shot
run at 05:37 local land in HOUR 0 -- 12:00 on the loading screen -- and a shot
just after local midnight counted on the wrong day. The board now reads a per-
tenant timezone and buckets in LOCAL time; this is where that name is stored.

NULLABLE, AND NULL MEANS UTC. Every tenant that existed before today, and every
tenant that never sets one, keeps the exact behaviour it had: tenant_zone()
falls back to UTC on a NULL or unknown name, so no board moves and nothing 500s.
It is an IANA name ("Asia/Kolkata"), validated at read time, not here.

Additive and reversible: one nullable column, no row rewritten. The add is
guarded because boot's create_all, or the boot-time repair in main.py
(_ensure_column), may have made the column already.
"""
import sqlalchemy as sa
from alembic import op

revision = "0017_tenant_timezone"
down_revision = "0016_production_energy"
branch_labels = None
depends_on = None

TABLE = "tenant_configs"
COLUMN = "timezone"


def _cols(bind):
    insp = sa.inspect(bind)
    if TABLE not in set(insp.get_table_names()):
        return None
    return {c["name"] for c in insp.get_columns(TABLE)}


def upgrade() -> None:
    present = _cols(op.get_bind())
    if present is None:
        return
    if COLUMN not in present:
        op.add_column(TABLE, sa.Column(COLUMN, sa.String(), nullable=True))


def downgrade() -> None:
    present = _cols(op.get_bind())
    if present is None:
        return
    if COLUMN in present:
        op.drop_column(TABLE, COLUMN)
