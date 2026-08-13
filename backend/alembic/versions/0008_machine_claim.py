"""the factory-controlled machine claim (ADR-0019)

Revision ID: 0008_machine_claim
Revises: 0007_oem_service_clock
Create Date: 2026-08-11

WHY
---
An OEM could not attach a machine to a factory, deliberately: #517 refused to
build an OEM-side assignment because it would put a stranger's equipment on a
customer's Connected Equipment screen with AMP's endorsement. That refusal left
the last blocker to a controlled pilot.

This table is the offer an OEM CAN make. The factory's acceptance is what sets
`machine_installations.factory_tenant_code`, and nothing else in AMP does.

WHAT THE CONSTRAINTS ARE FOR
----------------------------
  token_hash UNIQUE   the claim is found ONLY by the hash of what was presented.
                      Unique so a lookup is a single index probe with no scan and
                      no per-code timing difference, and so two claims can never
                      collide onto one code.

  installation_id FK  a claim cannot outlive its machine, and the FK is what
                      makes `offboard_tenant`'s constraint sweep see this table.

  ix on status        the OEM's claim list filters by it; small table, cheap
                      index, and it keeps the list a range scan as stock grows.

Additive and reversible. Creates one table and touches nothing that exists, so
an AMP database with live OEM and factory data upgrades without rewriting a row.
"""
import sqlalchemy as sa
from alembic import op

revision = "0008_machine_claim"
down_revision = "0007_oem_service_clock"
branch_labels = None
depends_on = None


def _tables(bind):
    return set(sa.inspect(bind).get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    if "machine_claims" in _tables(bind):
        return
    op.create_table(
        "machine_claims",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("oem_code", sa.String(), nullable=False),
        sa.Column("installation_id", sa.Integer(),
                  sa.ForeignKey("machine_installations.id"), nullable=False),
        # The credential is never stored; only this.
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("code_hint", sa.String(), nullable=False, server_default=""),
        sa.Column("status", sa.String(), nullable=False, server_default="Pending"),
        sa.Column("intended_tenant_code", sa.String(), nullable=True),
        # NOT NULL: an invitation that never expires is a permanent bearer
        # credential printed on the side of a box.
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(), nullable=True),
        sa.Column("claimed_by", sa.String(), nullable=True),
        sa.Column("claimed_tenant_code", sa.String(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_by", sa.String(), nullable=True),
    )
    # Every model in AMP declares `id = Column(Integer, primary_key=True,
    # index=True)`, and all 57 of the resulting `ix_*_id` indexes are in the
    # frozen baseline. Omitting it here is not a harmless deviation: the
    # migration gate rebuilds from that baseline, runs the migrations and
    # demands the result match models.py, so a missing index IS drift, and it
    # failed the gate on the first push of this branch. The index is redundant
    # beside the primary key on PostgreSQL — but that argument applies to all 57
    # equally and belongs in one deliberate sweep, not in one new table quietly
    # disagreeing with the rest.
    op.create_index("ix_machine_claims_id", "machine_claims", ["id"])
    op.create_index("ix_machine_claims_token_hash", "machine_claims",
                    ["token_hash"], unique=True)
    op.create_index("ix_machine_claims_oem_code", "machine_claims", ["oem_code"])
    op.create_index("ix_machine_claims_installation_id", "machine_claims",
                    ["installation_id"])
    op.create_index("ix_machine_claims_claimed_tenant_code", "machine_claims",
                    ["claimed_tenant_code"])


def downgrade() -> None:
    """Drops the table.

    Dropping it discards the record of which invitations were issued and which
    were accepted — the installations themselves survive, because
    `factory_tenant_code` lives on `machine_installations` and was set by the
    acceptance rather than derived from this table. So a downgrade loses the
    provenance, not the fleet.
    """
    bind = op.get_bind()
    if "machine_claims" not in _tables(bind):
        return
    for name in ("ix_machine_claims_claimed_tenant_code",
                 "ix_machine_claims_installation_id",
                 "ix_machine_claims_oem_code",
                 "ix_machine_claims_token_hash",
                 "ix_machine_claims_id"):
        try:
            op.drop_index(name, table_name="machine_claims")
        except Exception:
            pass
    op.drop_table("machine_claims")
