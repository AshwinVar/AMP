"""who changed a part master, when, and what the figure was before

Revision ID: 0017_part_spec_audit
Revises: 0016_production_energy
Create Date: 2026-10-28

WHY
---
A PartSpec turns a shot count into kilograms and rupees for every hour the plant
runs, and a spec is effective-dated: a revision is a NEW row, never an edit, so
`part_specs` already keeps every figure a part ever carried. What it does NOT
keep is WHO changed WHAT, WHEN, and what the figure was BEFORE -- and a price
restates revenue, so when a customer disputes an invoice that is the first
question. `part_spec_audits` is the answer: one row per create or revise,
carrying the acting user, the moment, and the old->new of every field that moved.

WHAT THIS ADDS
--------------
  part_spec_audits   tenant_code, part_code, part_name, actor, changes, created_at

`changes` is a small JSON object, {field: {old, new}}, written once and read
whole -- never filtered or summed in SQL, so it is TEXT, not a column per field.
A migration every time a spec grows a field is exactly what a blob avoids. For a
brand-new part every field records old=null; a revision records only the fields
that actually moved against the spec that was current when it was made.

Additive and reversible: creates one table and alters nothing that exists, so a
database with live factory data upgrades without rewriting a row. The create is
guarded, because boot's create_all may have made the table already -- the same
shape as 0011 and 0015.
"""
import sqlalchemy as sa
from alembic import op

revision = "0017_part_spec_audit"
down_revision = "0016_production_energy"
branch_labels = None
depends_on = None

TABLE = "part_spec_audits"
# Every model declares `id = Column(..., index=True)` and `tenant_code =
# Column(..., index=True)`, and the drift gate demands the migrated schema match
# models.py, so those indexes are created here rather than left to the keys. The
# composite mirrors models.PartSpecAudit.__table_args__ -- the read path is this
# tenant's trail, newest first.
_INDEXES = [
    ("ix_part_spec_audits_id", ["id"]),
    ("ix_part_spec_audits_tenant_code", ["tenant_code"]),
    ("ix_part_spec_audits_tenant_created", ["tenant_code", "created_at"]),
]


def upgrade() -> None:
    bind = op.get_bind()
    if TABLE in set(sa.inspect(bind).get_table_names()):
        return
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_code", sa.String(), nullable=False,
                  server_default="DEFAULT"),
        sa.Column("part_code", sa.String(), nullable=False),
        # Frozen as it read when the change was made (see models.py): a later
        # rename must not rewrite the history of what was changed.
        sa.Column("part_name", sa.String(), nullable=True),
        sa.Column("actor", sa.String(), nullable=True),
        # {field: {"old": <was>, "new": <now>}} as JSON text. NOT NULL: a trail
        # row with no changes is a row that records nothing.
        sa.Column("changes", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )
    for index_name, columns in _INDEXES:
        op.create_index(index_name, TABLE, columns)


def downgrade() -> None:
    """Drops the table, discarding the part master's change history.

    Nothing that existed before 0017 is touched, so the schema returns exactly
    to 0016's shape. Take an export first if the trail matters: it is the only
    record AMP has of who changed a price and what it was before.

    Indexes are dropped only if present, found by inspection rather than by
    catching the error -- on PostgreSQL a failed DROP INDEX aborts the whole
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
