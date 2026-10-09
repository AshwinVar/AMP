"""what an hour of production COST in electricity

Revision ID: 0016_production_energy
Revises: 0015_part_specs
Create Date: 2026-10-09

WHY
---
AMP had nowhere to put a kilowatt-hour. `grep -n "kwh\\|energy" models.py` matched
nothing: every money, count, duration and weight had a home and energy had none.
So the plant board's power card was hardcoded to "no source" -- not because the
machines cannot measure it, but because AMP could not have stored it if they did.

They can. An injection moulding controller on a customer's floor turned out to
keep a kWh figure for every hour of the last year alongside the shot count for
that hour -- 25,133 kWh against 1,316,137 shots, 0.0191 kWh a shot. It had been
counting for fourteen months and nothing had ever read it.

ON production_records, NOT A TABLE OF ITS OWN. Energy is measured over the same
window as the output it powered: one machine, one hour. Splitting them into two
tables would mean joining on a timestamp range to answer "what did this hour
cost", which is the question. Keeping them on one row also means the existing
idempotency key (tenant_code, source_record_id) protects the energy figure from
a re-import exactly as it protects the counts.

NULLABLE, AND THAT IS THE POINT. NULL means "this record's source did not report
energy", which is true of every row written before today and of every machine
without a meter. It is NOT zero. A zero would say the machine ran that hour and
drew no power, and the plant board would draw it as a bar -- the same lie the
power card exists to avoid. Readers must treat NULL as unmeasured and say so.

FLOAT, for the reason 0015 gives for part_weight_g: this is a physical reading
with a fractional part that matters (3.8 kWh, not 4), not money and not a
duration. 0010's ban on Float covers figures that are summed and compared for
equality; these are summed and ROUNDED at the edge, in the read-model.

Additive and reversible. Nothing existing changes, and every existing row keeps
working with NULL. Guarded, because boot's create_all may have made the column
first on a development database.
"""
import sqlalchemy as sa
from alembic import op

revision = "0016_production_energy"
down_revision = "0015_part_specs"
branch_labels = None
depends_on = None

TABLE = "production_records"
COLUMN = "energy_kwh"


def _inspector():
    return sa.inspect(op.get_bind())


def upgrade():
    inspector = _inspector()
    if TABLE not in inspector.get_table_names():
        return
    columns = {c["name"] for c in inspector.get_columns(TABLE)}
    if COLUMN not in columns:
        # No server_default: a default of 0 would make every historical row
        # claim it consumed nothing, which is a measurement nobody took.
        op.add_column(TABLE, sa.Column(COLUMN, sa.Float(), nullable=True))


def downgrade():
    inspector = _inspector()
    if TABLE not in inspector.get_table_names():
        return
    columns = {c["name"] for c in inspector.get_columns(TABLE)}
    if COLUMN in columns:
        op.drop_column(TABLE, COLUMN)
