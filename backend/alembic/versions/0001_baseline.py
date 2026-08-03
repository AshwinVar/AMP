"""baseline: the schema as create_all built it

Revision ID: 0001_baseline
Revises:
Create Date: 2026-08-04

THIS MIGRATION DELIBERATELY DOES NOTHING.

AMP's schema was built for its whole life by Base.metadata.create_all() plus
the idempotent boot-time DDL helpers in main.py (_ensure_column,
_ensure_index, tenancy.ensure_tenant_columns). Production already has all 48
tables. There is no earlier state to migrate from.

So this revision is an empty anchor. Its only job is to give the existing
production database a version to be stamped with, so that every FUTURE schema
change has a parent revision and a recorded history. Writing the whole schema
out as a create-table migration would be worse than useless: on production it
would have to be skipped anyway, and it would immediately rot against
models.py.

HOW THE TRANSITION WORKS (see migrate.py):
  - Existing database, no alembic_version table  -> stamp 0001_baseline.
    Nothing runs. The schema is already correct.
  - Fresh database                               -> create_all builds it,
    then stamp 0001_baseline.
  - Either, afterwards                           -> upgrade head runs only
    the revisions added after this one.

downgrade() intentionally raises. There is nothing below the baseline, and a
silent no-op downgrade would let someone believe they had rolled back to a
state that never existed.
"""
from alembic import op  # noqa: F401
import sqlalchemy as sa  # noqa: F401

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """No-op: production already has this schema (see module docstring)."""
    pass


def downgrade() -> None:
    raise RuntimeError(
        "0001_baseline is the first revision — there is nothing to downgrade to. "
        "To tear the schema down, drop the database."
    )
