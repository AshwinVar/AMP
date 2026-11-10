"""the fleet layer: autonomous movers, movement orders, assistance pleas

Revision ID: 0017_fleet_ops
Revises: 0016_production_energy
Create Date: 2026-11-10

WHY
---
The MES already knows the exact moment material must move — a machine finishes
a batch and the knowledge evaporates into a worker noticing a full bin. Fleet
Ops captures that knowledge as a dispatchable order (transport_tasks), gives it
something to dispatch TO (fleet_vehicles — AMRs, AGVs, tugs, forklifts, real or
simulated behind the same two position floats), and gives the vehicles a way to
ask a human for help when navigation fails (fleet_alerts — the remote-assistance
queue, modelled on how Waymo/Zoox actually run theirs: the vehicle initiates,
the person answers with advice from a closed vocabulary, never a steering
wheel).

THREE TABLES, NOT COLUMNS ON machines/alerts. A vehicle produces nothing, so
every production rollup (OEE, downtime, breakdown counts) must never see it;
a navigation plea is not a production alert, so the notification paths must
never spam it. Separate tables are the isolation.

STATIONS ARE DELIBERATELY ABSENT. Origin/destination are NAMES the engine can
place on the floor (a machine's own name, or a fixed logistics anchor); the
floor map derives from the machine roster live, so there is no stations table
to drift out of date.

Additive and reversible. Nothing existing changes. Guarded, because boot's
create_all may have made the tables first.
"""
import sqlalchemy as sa
from alembic import op

revision = "0017_fleet_ops"
down_revision = "0016_production_energy"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def upgrade():
    inspector = _inspector()
    tables = set(inspector.get_table_names())

    if "fleet_vehicles" not in tables:
        op.create_table(
            "fleet_vehicles",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_code", sa.String(), nullable=False,
                      server_default="DEFAULT"),
            sa.Column("name", sa.String(), nullable=False),
            sa.Column("vehicle_type", sa.String(), server_default="AMR"),
            sa.Column("status", sa.String(), server_default="Idle"),
            sa.Column("x", sa.Float(), server_default="5"),
            sa.Column("y", sa.Float(), server_default="85"),
            sa.Column("battery_pct", sa.Float(), server_default="100"),
            sa.Column("current_task_id", sa.Integer(), nullable=True),
            sa.Column("odometer_m", sa.Float(), server_default="0"),
            sa.Column("deliveries_total", sa.Integer(), server_default="0"),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
            sa.UniqueConstraint("tenant_code", "name",
                                name="uq_fleet_vehicles_tenant_name"),
        )

    if "transport_tasks" not in tables:
        op.create_table(
            "transport_tasks",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_code", sa.String(), nullable=False,
                      server_default="DEFAULT"),
            sa.Column("origin", sa.String(), nullable=False),
            sa.Column("destination", sa.String(), nullable=False),
            sa.Column("payload", sa.String(), server_default=""),
            sa.Column("priority", sa.Integer(), server_default="2"),
            sa.Column("status", sa.String(), server_default="Pending"),
            sa.Column("vehicle_id", sa.Integer(), nullable=True),
            sa.Column("source", sa.String(), server_default="manual"),
            sa.Column("requested_at", sa.DateTime(), server_default=sa.func.now()),
            sa.Column("assigned_at", sa.DateTime(), nullable=True),
            sa.Column("picked_up_at", sa.DateTime(), nullable=True),
            sa.Column("completed_at", sa.DateTime(), nullable=True),
        )

    if "fleet_alerts" not in tables:
        op.create_table(
            "fleet_alerts",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_code", sa.String(), nullable=False,
                      server_default="DEFAULT"),
            sa.Column("vehicle_id", sa.Integer(), nullable=False),
            sa.Column("task_id", sa.Integer(), nullable=True),
            sa.Column("kind", sa.String(), nullable=False),
            sa.Column("severity", sa.String(), server_default="High"),
            sa.Column("message", sa.String(), server_default=""),
            sa.Column("status", sa.String(), server_default="Open"),
            sa.Column("advice", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
            sa.Column("resolved_at", sa.DateTime(), nullable=True),
        )

    # Matches models.py, which declares index=True on id like every other
    # table. The drift gate compares the migration against the model.
    for table in ("fleet_vehicles", "transport_tasks", "fleet_alerts"):
        existing = {ix["name"] for ix in _inspector().get_indexes(table)}
        if f"ix_{table}_id" not in existing:
            op.create_index(f"ix_{table}_id", table, ["id"])
        if f"ix_{table}_tenant_code" not in existing:
            op.create_index(f"ix_{table}_tenant_code", table, ["tenant_code"])


def downgrade():
    for table in ("fleet_alerts", "transport_tasks", "fleet_vehicles"):
        if table not in _inspector().get_table_names():
            continue
        existing = {ix["name"] for ix in _inspector().get_indexes(table)}
        for name in (f"ix_{table}_tenant_code", f"ix_{table}_id"):
            if name in existing:
                op.drop_index(name, table_name=table)
        op.drop_table(table)
