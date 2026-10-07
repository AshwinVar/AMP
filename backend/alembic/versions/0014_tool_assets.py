"""tooling that falls due on a count, not a calendar

Revision ID: 0014_tool_assets
Revises: 0013_gateway_credentials
Create Date: 2026-10-06

WHY
---
A plant's tooling carries obligations that arrive on a counter: a mould serviced
every 10,000 shots, a die measured every 10,000 parts, a punch retired at its
rated life. AMP already holds the count -- `production_records` has carried
per-machine output since the beginning -- and does nothing with it, because the
count belongs to the MACHINE and the obligation belongs to the TOOL. Nobody on a
shop floor is counting to ten thousand, so in practice tooling is serviced when
somebody remembers, which is a maintenance regime only in name.

`tool_assets` is the missing subject. It is deliberately NOT a column on
`machines`: tooling outlives the machine it is fitted to and moves between
machines as capacity allows, and a mould's wear history tracked on the press
would reset every time it was moved -- which is exactly when that history is
worth having.

WHAT THIS ADDS
--------------
  tool_assets       tenant_code, tool_no (unique per tenant), name, tool_type,
                    machine_id, cavities, parts_total, parts_at_last_service,
                    service_interval_cycles, life_limit_cycles, status,
                    last_service_at, created_at

PARTS ARE STORED; CYCLES ARE DERIVED. A multi-cavity mould makes `cavities`
parts per shot, so cycles = parts / cavities. Storing cycles directly would mean
dividing each batch as it arrives and dropping the remainder -- on a 4-cavity
mould that silently loses up to 3 parts per batch, forever, and the error only
grows. Keeping the part totals makes the arithmetic exact at every point, and the
division happens once, on read.

THE THRESHOLDS ARE NULLABLE AND THAT IS THE POINT. A tool with no interval set is
NOT watched. It is not "due by default" and not "overdue at some sensible
figure": guessing 10,000 because it is a common number would put AMP's arithmetic
behind a limit the customer never agreed to, and the first time it was wrong it
would be wrong in writing, on a work card, in front of a toolmaker.

NOTHING EXISTING CHANGES. This migration adds one table and alters none. No
backfill, no column added to a populated table, no behaviour altered for any
tenant that never creates a tool. Additive and reversible.

Guarded, because boot's create_all may have made the table first.
"""
import sqlalchemy as sa
from alembic import op

revision = "0014_tool_assets"
down_revision = "0013_gateway_credentials"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def upgrade():
    inspector = _inspector()

    if "tool_assets" not in inspector.get_table_names():
        op.create_table(
            "tool_assets",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_code", sa.String(), nullable=False,
                      server_default="DEFAULT"),
            sa.Column("tool_no", sa.String(), nullable=False),
            sa.Column("name", sa.String(), nullable=False),
            sa.Column("tool_type", sa.String(), server_default="Mould"),
            # Null = in the tool room. A tool off the machine accrues nothing,
            # so the interlock engine skips it rather than ageing it in storage.
            sa.Column("machine_id", sa.Integer(),
                      sa.ForeignKey("machines.id"), nullable=True),
            # Defaults to 1 so a single-cavity tool needs no thought, and so the
            # cycles derivation can never divide by zero or by NULL.
            sa.Column("cavities", sa.Integer(), server_default="1"),
            sa.Column("parts_total", sa.Integer(), server_default="0"),
            sa.Column("parts_at_last_service", sa.Integer(), server_default="0"),
            # NULL means "not watched" -- see the header. Never defaulted.
            sa.Column("service_interval_cycles", sa.Integer(), nullable=True),
            sa.Column("life_limit_cycles", sa.Integer(), nullable=True),
            sa.Column("status", sa.String(), server_default="Active"),
            sa.Column("last_service_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(),
                      server_default=sa.func.now()),
            sa.UniqueConstraint("tenant_code", "tool_no",
                                name="uq_tool_assets_tenant_tool_no"),
        )

    existing = {ix["name"] for ix in _inspector().get_indexes("tool_assets")}
    # THE ID INDEX IS NOT OPTIONAL HERE. The primary key is already indexed and
    # this one is redundant -- but every other table in models.py declares
    # `index=True` on its id, ToolAsset included, and the drift gate compares
    # the migration against the MODEL. Omitting it is exactly what turned this
    # branch red: DIFF ('add_index', Index('ix_tool_assets_id', ...)).
    if "ix_tool_assets_id" not in existing:
        op.create_index("ix_tool_assets_id", "tool_assets", ["id"])
    if "ix_tool_assets_tenant_code" not in existing:
        op.create_index("ix_tool_assets_tenant_code", "tool_assets", ["tenant_code"])
    # The interlock engine's only access path: every live tool on one machine.
    if "ix_tool_assets_machine_status" not in existing:
        op.create_index("ix_tool_assets_machine_status", "tool_assets",
                        ["machine_id", "status"])


def downgrade():
    inspector = _inspector()
    if "tool_assets" not in inspector.get_table_names():
        return
    existing = {ix["name"] for ix in inspector.get_indexes("tool_assets")}
    for name in ("ix_tool_assets_machine_status", "ix_tool_assets_tenant_code",
                 "ix_tool_assets_id"):
        if name in existing:
            op.drop_index(name, table_name="tool_assets")
    op.drop_table("tool_assets")
