"""a consent names what it was given for: the hosted provider (ADR-0038)

Revision ID: 0012_consent_scope
Revises: 0011_action_outcomes
Create Date: 2026-09-21

WHY
---
ADR-0037 made a company's data leave AMP for a hosted language model only with
that company's consent. The consent row said WHICH company decided and WHEN,
and nothing about WHAT it decided on. Two providers can be configured
(Anthropic under commercial data terms; Gemini's free tier, whose data may be
used for training), the operator can switch with one variable, and
auto-detection falls through to whichever key is left. An Admin who consented
under one provider's terms would have had their company's questions sent to
the other's the moment the platform's configuration moved -- silently, under a
decision they never made.

WHAT THIS ADDS
--------------
  ai_learning_consents.scope   the hosted provider the consent was given for
                               ("anthropic", "gemini"), written at the moment
                               of the grant. The gate honours the row only
                               while that is the provider configured. NULL for
                               the learning capabilities, which name no third
                               party.

NULLABLE WITH NO BACKFILL, deliberately. A row that exists before this
revision was granted with no provider named; writing today's provider into it
would claim the Admin decided about that one. NULL says the only true thing,
and the gate reads a NULL scope on a scoped capability as "given for nothing
that is configured now", so the company is asked again -- ADR-0017's rule for
a grant whose meaning changed.

Additive and reversible: one nullable column, no row rewritten. The add is
guarded because boot's create_all, or the boot-time repair in main.py, may
have made the column already.
"""
import sqlalchemy as sa
from alembic import op

revision = "0012_consent_scope"
down_revision = "0011_action_outcomes"
branch_labels = None
depends_on = None

TABLE = "ai_learning_consents"
COLUMN = "scope"


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
