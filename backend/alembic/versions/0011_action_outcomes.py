"""closed-loop actions: what changed after an approved action, measured (ADR-0029)

Revision ID: 0011_action_outcomes
Revises: 0010_outcome_contracts
Create Date: 2026-09-20

WHY
---
AMP has proposed, ranked and approved work since ADR-0005 and has never looked
back to see whether any of it helped. Two accepted ADRs carry that as an honest
limit in writing: the Risk Radar cannot say how often a LIKELY risk became a
problem (ADR-0026), and the health score's weights are "judgement, not evidence"
because nothing records what happened next (ADR-0027).

One table fixes the missing half. When a human approves an action, AMP records
the metric that action was meant to move and the reading it had at that moment.
Once the same length of window has elapsed, it measures again and states the two
side by side.

WHAT THIS DOES NOT CLAIM
------------------------
`verdict` describes THE METRIC, not the action. A factory is not a laboratory:
downtime can fall because the order book emptied. Every surface labels this
CORRELATION and says so in words. The schema is deliberately incapable of
holding a causal claim — there is no "caused_by", no confidence and no p-value,
because AMP has no design that could produce one.

WHY BOTH VALUE COLUMNS ARE NULLABLE
-----------------------------------
"No reading" and "a reading of 0" are different claims (ADR-0014). A NULL on
either side makes the verdict NOT MEASURABLE; writing 0.0 in its place would
manufacture an improvement out of an absence.

WHY action_id IS NOT A FOREIGN KEY
----------------------------------
`agent_actions` is created by the application's create_all, not by any
migration, so a database built from migrations alone does not have it when this
revision runs. It is also the right call on its own terms: an outcome is
evidence about a decision, and evidence should outlive the row it describes —
the same reasoning ADR-0021 applied to its snapshot ids.

THE UNIQUE KEY
--------------
  uq_action_outcome_action   (action_id)
      ONE outcome per action. Two rows would let two different answers to "did
      it help?" both be true, and whichever the reader saw first would win.

Additive and reversible: creates one table and alters nothing that exists, so a
database with live factory data upgrades without rewriting a row. The create is
guarded, because boot's create_all may have made the table already.
"""
import sqlalchemy as sa
from alembic import op

revision = "0011_action_outcomes"
down_revision = "0010_outcome_contracts"
branch_labels = None
depends_on = None

TABLE = "action_outcomes"
# Every model declares `id = Column(..., index=True)`, and the CI drift gate
# demands the migrated schema match models.py, so ix_<table>_id is created here
# rather than left to the primary key.
_INDEXES = [
    ("ix_action_outcomes_id", ["id"]),
    ("ix_action_outcomes_tenant_code", ["tenant_code"]),
    ("ix_action_outcomes_action_id", ["action_id"]),
    ("ix_action_outcomes_created_at", ["created_at"]),
    ("ix_action_outcomes_tenant_created", ["tenant_code", "created_at"]),
]


def upgrade() -> None:
    bind = op.get_bind()
    if TABLE in set(sa.inspect(bind).get_table_names()):
        return
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_code", sa.String(), nullable=False),
        # No ForeignKey: agent_actions is created by boot's create_all and by no
        # migration, so it is absent on a migrate-only path (see models.py).
        sa.Column("action_id", sa.Integer(), nullable=False),
        sa.Column("metric", sa.String(length=48), nullable=False),
        sa.Column("scope_kind", sa.String(length=16), nullable=False),
        sa.Column("scope_id", sa.Integer(), nullable=True),
        sa.Column("scope_label", sa.String(), nullable=True),
        sa.Column("window_days", sa.Integer(), nullable=False, server_default="7"),
        # Nullable on purpose: see "WHY BOTH VALUE COLUMNS ARE NULLABLE" above.
        sa.Column("baseline_value", sa.Float(), nullable=True),
        sa.Column("baseline_at", sa.DateTime(), nullable=False),
        sa.Column("measured_value", sa.Float(), nullable=True),
        sa.Column("measured_at", sa.DateTime(), nullable=True),
        sa.Column("verdict", sa.String(length=16), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("action_id", name="uq_action_outcome_action"),
    )
    for index_name, columns in _INDEXES:
        op.create_index(index_name, TABLE, columns)


def downgrade() -> None:
    """Drops the table, discarding every record of what changed after an action.

    Nothing that existed before 0011 is touched, so the schema returns exactly
    to 0010's shape. Take an export first if the measurements matter: they are
    the only evidence AMP has about whether its own recommendations help.

    Indexes are dropped only if present, found by inspection rather than by
    catching the error — on PostgreSQL a failed DROP INDEX aborts the whole
    migration transaction, so try/except would turn a missing index into a
    failed downgrade.
    """
    bind = op.get_bind()
    if TABLE not in set(sa.inspect(bind).get_table_names()):
        return
    existing = {ix["name"] for ix in sa.inspect(bind).get_indexes(TABLE)}
    for index_name, _ in reversed(_INDEXES):
        if index_name in existing:
            op.drop_index(index_name, table_name=TABLE)
    op.drop_table(TABLE)
