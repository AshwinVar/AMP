"""VDA 5050: real vehicles behind the same fleet rows

Revision ID: 0018_vda5050_adapter
Revises: 0017_fleet_ops
Create Date: 2026-11-12

WHY
---
0017's whole design bet was that the simulator IS the adapter contract: a real
AMR integration writes the same row fields and flips the same task lifecycle.
This migration is that bet being collected. Four columns, no new tables:

  fleet_vehicles.adapter       who moves the row — "sim" (fleet_engine) or
                               "vda5050" (the vehicle's own state reports via
                               fleet_vda5050; advance() skips it)
  fleet_vehicles.vda_identity  "{manufacturer}/{serial}", the vehicle's topic
                               identity. UNIQUE installation-wide, not per
                               tenant, because a VDA 5050 topic carries no
                               tenant: this row is what binds a message to a
                               workspace (the GatewayCredential.gateway_id
                               precedent), fail-closed — unregistered
                               identities are dropped, never created.
  transport_tasks.vda_order_id / vda_order_update_id
                               the order on the wire for this assignment.
                               Fresh orderId per assignment (a vehicle rejects
                               a reused orderId at orderUpdateId 0); the
                               update counter increments on each re-publish
                               (the resume verb). Cleared on release/cancel.

Additive and reversible. Guarded, because boot's create_all (or its
_ensure_column patches) may have made the columns first.
"""
import sqlalchemy as sa
from alembic import op

revision = "0018_vda5050_adapter"
down_revision = "0017_fleet_ops"
branch_labels = None
depends_on = None


INDEX = "ix_fleet_vehicles_vda_identity"   # create_all's name for unique+index


def _columns(table):
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def _indexes(table):
    return {i["name"] for i in sa.inspect(op.get_bind()).get_indexes(table)}


def upgrade():
    vehicle_cols = _columns("fleet_vehicles")
    if "adapter" not in vehicle_cols:
        op.add_column("fleet_vehicles",
                      sa.Column("adapter", sa.String(), server_default="sim"))
    if "vda_identity" not in vehicle_cols:
        op.add_column("fleet_vehicles", sa.Column("vda_identity", sa.String()))
    # Uniqueness as a NAMED UNIQUE INDEX, never a table constraint: an index
    # exists independently of the table's DDL, so both this upgrade and the
    # downgrade can manage it by name on PostgreSQL and SQLite alike. (The
    # first cut used create_unique_constraint; its downgrade could not drop
    # the constraint portably — PostgreSQL auto-names it on the create_all
    # path, and SQLite cannot rebuild the table around it.)
    if INDEX not in _indexes("fleet_vehicles"):
        op.create_index(INDEX, "fleet_vehicles", ["vda_identity"], unique=True)

    task_cols = _columns("transport_tasks")
    if "vda_order_id" not in task_cols:
        op.add_column("transport_tasks", sa.Column("vda_order_id", sa.String()))
    if "vda_order_update_id" not in task_cols:
        op.add_column("transport_tasks",
                      sa.Column("vda_order_update_id", sa.Integer(),
                                server_default="0"))


def downgrade():
    task_cols = _columns("transport_tasks")
    if "vda_order_update_id" in task_cols:
        op.drop_column("transport_tasks", "vda_order_update_id")
    if "vda_order_id" in task_cols:
        op.drop_column("transport_tasks", "vda_order_id")

    # The index first, by name — SQLite refuses to drop an indexed column,
    # and PostgreSQL would otherwise leave a dangling index error path.
    if INDEX in _indexes("fleet_vehicles"):
        op.drop_index(INDEX, table_name="fleet_vehicles")
    vehicle_cols = _columns("fleet_vehicles")
    if "vda_identity" in vehicle_cols:
        op.drop_column("fleet_vehicles", "vda_identity")
    if "adapter" in vehicle_cols:
        op.drop_column("fleet_vehicles", "adapter")
