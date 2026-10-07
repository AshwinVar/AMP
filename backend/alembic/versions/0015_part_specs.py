"""what a moulded part IS, so a shot count can become kilograms and rupees

Revision ID: 0015_part_specs
Revises: 0014_tool_assets
Create Date: 2026-10-07

WHY
---
A machine counts shots. Every figure a plant actually wants is a conversion away
from that number, and none of the conversions exist in any controller:

    parts       = shots x ACTIVE cavities
    kg consumed = parts x part_weight_g / 1000
    revenue     = parts x price_per_piece
    ideal rate  = 3600 / ideal_cycle_time_s x active cavities

Without them a shot counter is a number nobody can act on. `part_specs` is the
process-planning and commercial data a person enters once per part.

ACTIVE CAVITIES IS SEPARATE FROM CAVITIES, DELIBERATELY. A 56-cavity mould run
with 4 cavities blocked makes 52 parts a shot. The tool still HAS 56. Collapsing
the two overstates output by 8% permanently, and the error is invisible because
both figures look plausible.

EFFECTIVE-DATED, because price changes, cavities get blocked and cycle times are
re-optimised. Overwriting a spec would retrospectively restate every month
already reported -- last year's revenue would move because this year's price
did. UNIQUE (tenant_code, part_code, effective_from) lets a part carry a history
and the reader pick the row in force when the parts were made.

WHAT THIS ADDS
--------------
  part_specs             tenant_code, part_code, part_name, material,
                         part_weight_g, cavities, active_cavities,
                         ideal_cycle_time_s, price_per_piece, effective_from
  tool_assets.part_code  which part the fitted tool makes -- NULLABLE, so every
                         existing tool keeps working and simply cannot be priced

FLOATS HERE, ON PURPOSE. 0010 bans Float for money and durations because they
are summed and compared. These are not: part_weight_g (0.33 g) and
price_per_piece (0.09) are PER-UNIT specifications entered by a person, and
rounding them to integers would destroy the precision the figures depend on --
0.33 g becomes 0 g, and a 330 kg month becomes nothing. The sums they feed are
rounded at the edge, in the read-model, not stored.

Additive and reversible. Nothing existing changes. Guarded, because boot's
create_all may have made the table or column first.
"""
import sqlalchemy as sa
from alembic import op

revision = "0015_part_specs"
down_revision = "0014_tool_assets"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def upgrade():
    inspector = _inspector()

    if "part_specs" not in inspector.get_table_names():
        op.create_table(
            "part_specs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_code", sa.String(), nullable=False,
                      server_default="DEFAULT"),
            sa.Column("part_code", sa.String(), nullable=False),
            sa.Column("part_name", sa.String(), nullable=False),
            sa.Column("material", sa.String(), nullable=False),
            sa.Column("part_weight_g", sa.Float(), nullable=False),
            sa.Column("cavities", sa.Integer(), server_default="1"),
            sa.Column("active_cavities", sa.Integer(), server_default="1"),
            sa.Column("ideal_cycle_time_s", sa.Float(), nullable=False),
            sa.Column("price_per_piece", sa.Float(), server_default="0"),
            # NOT NULL: a spec with no start date cannot be placed in a history,
            # and "in force since forever" is a claim nobody made.
            sa.Column("effective_from", sa.Date(), nullable=False),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
            sa.UniqueConstraint("tenant_code", "part_code", "effective_from",
                                name="uq_part_specs_tenant_part_from"),
        )

    existing = {ix["name"] for ix in _inspector().get_indexes("part_specs")}
    # Matches models.py, which declares index=True on id like every other table.
    # The drift gate compares the migration against the model.
    if "ix_part_specs_id" not in existing:
        op.create_index("ix_part_specs_id", "part_specs", ["id"])
    if "ix_part_specs_tenant_code" not in existing:
        op.create_index("ix_part_specs_tenant_code", "part_specs", ["tenant_code"])
    # The read-model's only lookup: this part, as of this date.
    if "ix_part_specs_lookup" not in existing:
        op.create_index("ix_part_specs_lookup", "part_specs",
                        ["tenant_code", "part_code", "effective_from"])

    if "tool_assets" in _inspector().get_table_names():
        cols = {c["name"] for c in _inspector().get_columns("tool_assets")}
        if "part_code" not in cols:
            # Nullable with no backfill: every tool that exists today keeps
            # working and simply cannot be priced until someone says what it
            # makes. A default would assert a part nobody chose.
            op.add_column("tool_assets",
                          sa.Column("part_code", sa.String(), nullable=True))


def downgrade():
    inspector = _inspector()
    if "tool_assets" in inspector.get_table_names():
        cols = {c["name"] for c in inspector.get_columns("tool_assets")}
        if "part_code" in cols:
            op.drop_column("tool_assets", "part_code")

    if "part_specs" not in _inspector().get_table_names():
        return
    existing = {ix["name"] for ix in _inspector().get_indexes("part_specs")}
    for name in ("ix_part_specs_lookup", "ix_part_specs_tenant_code", "ix_part_specs_id"):
        if name in existing:
            op.drop_index(name, table_name="part_specs")
    op.drop_table("part_specs")
