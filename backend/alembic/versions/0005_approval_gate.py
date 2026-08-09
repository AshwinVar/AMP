"""the approval gate needs revocation and expiry

Revision ID: 0005_approval_gate
Revises: 0004_per_tenant_bom
Create Date: 2026-08-09

WHY
---
An adversarial probe against master found four ways an agent action could be
decided when it should not have been. Two of them needed schema:

  * A DELETED user's token still approved a purchase order. auth.get_current_user
    decodes the JWT and performs no database lookup, and there was no
    is_active column to consult even if it did — so access could not be revoked
    at all, only waited out.

  * A 400-day-old proposal was still approved and advanced. Nothing recorded
    when a recommendation stopped being trustworthy.

WHAT THIS ADDS
--------------
  users.is_active            NOT NULL DEFAULT TRUE
  agent_actions.expires_at   NULLABLE

`is_active` defaults TRUE so every existing login keeps working; revocation is
an explicit act, never a side effect of a deploy.

`expires_at` is NULLABLE on purpose. Backfilling it for historic rows would
either expire a queue of legitimate pending proposals on the day of the deploy,
or pick an arbitrary future date for actions whose context is long gone. Instead
approvals.is_expired treats NULL as "fall back to DEFAULT_TTL_DAYS from
created_at" — so an old proposal is stale by age, and a NULL never means "never
expires".
"""
import sqlalchemy as sa
from alembic import op

revision = "0005_approval_gate"
down_revision = "0004_per_tenant_bom"
branch_labels = None
depends_on = None


def _has_column(bind, table, column):
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())

    if "users" in tables and not _has_column(bind, "users", "is_active"):
        # Added with a server default so existing rows become TRUE without a
        # separate UPDATE, then left NOT NULL: a NULL is_active would be
        # ambiguous exactly where ambiguity is most expensive.
        op.add_column("users", sa.Column("is_active", sa.Boolean(), nullable=True,
                                         server_default=sa.true()))
        op.execute("UPDATE users SET is_active = TRUE WHERE is_active IS NULL"
                   if bind.dialect.name != "sqlite" else
                   "UPDATE users SET is_active = 1 WHERE is_active IS NULL")
        with op.batch_alter_table("users") as batch:
            batch.alter_column("is_active", existing_type=sa.Boolean(),
                               nullable=False, server_default=sa.true())

    if "agent_actions" in tables and not _has_column(bind, "agent_actions",
                                                     "expires_at"):
        op.add_column("agent_actions",
                      sa.Column("expires_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    """Drops both columns.

    Dropping is_active silently restores "access cannot be revoked", so a
    downgrade should be accompanied by disabling the accounts another way.
    """
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "agent_actions" in tables and _has_column(bind, "agent_actions", "expires_at"):
        with op.batch_alter_table("agent_actions") as batch:
            batch.drop_column("expires_at")
    if "users" in tables and _has_column(bind, "users", "is_active"):
        with op.batch_alter_table("users") as batch:
            batch.drop_column("is_active")
