"""per-tenant learning consent for AMP-native AI (ADR-0020)

Revision ID: 0009_native_ai_consent
Revises: 0008_machine_claim
Create Date: 2026-09-17

WHY
---
AMP-native AI ships models trained on synthetic data only. The first capability
that would learn from a tenant's OWN data - a per-machine telemetry baseline fitted
on request for the anomaly check - must not run unless an Admin of that tenant has
explicitly said yes, and must stop the moment they say no. That decision has to be
stored (so it survives restarts and applies to every instance), auditable (the
AuditLog row is written in the same transaction by amp_ai.consent.set_consent) and
revocable. This table is where it is stored.

WHAT THE CONSTRAINTS ARE FOR
----------------------------
  UNIQUE (tenant_code, capability)  one answer per tenant per capability. Without
                      it two concurrent grants could leave two rows and a reader
                      could pick either; with it the database refuses the second.

  granted NOT NULL, server default false   a row can never mean "unknown". A row
                      inserted by hand without a value is a refusal, not a grant.

  ix on tenant_code   every read filters it explicitly (the table is deliberately
                      outside the ADR-0002 hook, like agent_policies).

  ix_*_id             every AMP table has one; the migration gate compares against
                      models.py, so omitting it is drift (see 0008's note).

Additive and reversible. Creates one table and touches nothing that exists. A
downgrade drops the stored decisions; the AuditLog history of who granted and
revoked what is untouched.
"""
import sqlalchemy as sa
from alembic import op

revision = "0009_native_ai_consent"
down_revision = "0008_machine_claim"
branch_labels = None
depends_on = None

TABLE = "ai_learning_consents"


def _tables(bind):
    return set(sa.inspect(bind).get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    if TABLE in _tables(bind):
        return
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_code", sa.String(), nullable=False),
        sa.Column("capability", sa.String(), nullable=False),
        sa.Column("granted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("granted_by", sa.String(), nullable=True),
        sa.Column("granted_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_by", sa.String(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("tenant_code", "capability", name="uq_ai_learning_consent"),
    )
    op.create_index("ix_ai_learning_consents_id", TABLE, ["id"])
    op.create_index("ix_ai_learning_consents_tenant_code", TABLE, ["tenant_code"])


def downgrade() -> None:
    """Drops the table, and with it every stored consent decision.

    Run it only together with a code rollback. An application that still reads the
    table fails its consent query, so the anomaly check errors instead of learning:
    it fails closed, never open. The AuditLog keeps the record of who granted and
    revoked what.
    """
    bind = op.get_bind()
    if TABLE not in _tables(bind):
        return
    # DROP TABLE removes the table's own indexes on both PostgreSQL and SQLite.
    # No per-index try/except: on PostgreSQL a failed statement aborts the whole
    # migration transaction, so swallowing the error would only move the failure.
    op.drop_table(TABLE)
