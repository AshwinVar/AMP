"""the OEM ownership dimension: organisations, users, catalogue, installations, sharing

Revision ID: 0006_oem_foundation
Revises: 0005_approval_gate
Create Date: 2026-08-09

WHY
---
A machine manufacturer sells connected equipment into many customer factories.
AMP's every existing question is "what belongs to this tenant"; the OEM question
is "what belongs to this manufacturer, across tenants, to the extent each of
those tenants has agreed". That is a second ownership dimension (ADR-0017).

WHAT THIS ADDS
--------------
Five NEW tables. No existing table is altered, so every existing factory query
is byte-identical and an AMP database with no OEM rows behaves exactly as it did:

  oem_organizations           the manufacturer, and its white-label branding
  oem_users                   OEM principals — a SEPARATE table from users, so a
                              factory admin's user surface cannot mint or promote
                              one, and an OEM admin cannot mint a factory user
  machine_models              the OEM's catalogue; telemetry_profile keeps
                              machine-specific signal vocabulary out of AMP core
  machine_installations       ONE PHYSICAL MACHINE, with the durable identity
  oem_data_sharing_policies   what a FACTORY has agreed to share, default-deny

TWO CONSTRAINTS THAT ARE THE WHOLE DESIGN
-----------------------------------------
`uq_installation_serial` is UNIQUE (oem_code, serial_number) and NOT globally
unique. A global serial namespace lets one manufacturer enumerate another's
fleet by probing for collisions; per-OEM uniqueness makes a guessed serial
belong to nobody.

`machine_installations.factory_tenant_code` is deliberately NOT called
`tenant_code`. Three mechanisms in this codebase key off that literal attribute
name, and one of them destroys data: offboard_tenant.purge_tenant_data sweeps
every mapper carrying the attribute and hard-deletes rows matching the departing
tenant. Under the obvious name, offboarding one customer would have erased the
OEM's own record of machines it built and still owns. Offboarding now unlinks
(offboard_tenant._unlink_oem_installations); it never deletes.
"""
import sqlalchemy as sa
from alembic import op

revision = "0006_oem_foundation"
down_revision = "0005_approval_gate"
branch_labels = None
depends_on = None

_TABLES = ["oem_data_sharing_policies", "machine_installations", "machine_models",
           "oem_users", "oem_organizations"]


def _existing(bind):
    return set(sa.inspect(bind).get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    present = _existing(bind)

    if "oem_organizations" not in present:
        op.create_table(
            "oem_organizations",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("oem_code", sa.String(), nullable=False),
            sa.Column("name", sa.String(), nullable=False),
            sa.Column("brand_name", sa.String(), nullable=True),
            sa.Column("brand_color", sa.String(), nullable=True),
            sa.Column("brand_logo_url", sa.String(), nullable=True),
            sa.Column("support_email", sa.String(), nullable=True),
            sa.Column("support_phone", sa.String(), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False,
                      server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_oem_organizations_oem_code", "oem_organizations",
                        ["oem_code"], unique=True)

    if "oem_users" not in present:
        op.create_table(
            "oem_users",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("oem_code", sa.String(), nullable=False),
            sa.Column("username", sa.String(), nullable=False),
            sa.Column("password", sa.String(), nullable=False),
            sa.Column("role", sa.String(), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False,
                      server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_oem_users_oem_code", "oem_users", ["oem_code"])
        op.create_index("ix_oem_users_username", "oem_users", ["username"],
                        unique=True)

    if "machine_models" not in present:
        op.create_table(
            "machine_models",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("oem_code", sa.String(), nullable=False),
            sa.Column("family", sa.String(), nullable=False, server_default=""),
            sa.Column("model_code", sa.String(), nullable=False),
            sa.Column("name", sa.String(), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("rated_capacity", sa.String(), nullable=True),
            sa.Column("rated_capacity_unit", sa.String(), nullable=True),
            sa.Column("service_interval_hours", sa.Integer(), nullable=True),
            sa.Column("warranty_months", sa.Integer(), nullable=True),
            sa.Column("telemetry_profile", sa.Text(), nullable=True),
            sa.Column("documentation_url", sa.String(), nullable=True),
            sa.Column("image_url", sa.String(), nullable=True),
            sa.Column("status", sa.String(), nullable=False, server_default="Active"),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("oem_code", "model_code",
                                name="uq_machine_model_identity"),
        )
        op.create_index("ix_machine_models_oem_code", "machine_models", ["oem_code"])

    if "machine_installations" not in present:
        op.create_table(
            "machine_installations",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("oem_code", sa.String(), nullable=False),
            sa.Column("serial_number", sa.String(), nullable=False),
            sa.Column("model_id", sa.Integer(),
                      sa.ForeignKey("machine_models.id"), nullable=False),
            sa.Column("factory_tenant_code", sa.String(), nullable=True),
            sa.Column("site", sa.String(), nullable=False, server_default=""),
            sa.Column("machine_id", sa.Integer(),
                      sa.ForeignKey("machines.id"), nullable=True),
            sa.Column("status", sa.String(), nullable=False,
                      server_default="Manufactured"),
            sa.Column("installed_at", sa.DateTime(), nullable=True),
            sa.Column("commissioned_at", sa.DateTime(), nullable=True),
            sa.Column("decommissioned_at", sa.DateTime(), nullable=True),
            sa.Column("warranty_start", sa.Date(), nullable=True),
            sa.Column("warranty_end", sa.Date(), nullable=True),
            sa.Column("operating_hours", sa.Float(), nullable=True),
            sa.Column("last_seen_at", sa.DateTime(), nullable=True),
            sa.Column("firmware_version", sa.String(), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            # Per-OEM, NOT global. See the module docstring.
            sa.UniqueConstraint("oem_code", "serial_number",
                                name="uq_installation_serial"),
        )
        op.create_index("ix_machine_installations_oem_code",
                        "machine_installations", ["oem_code"])
        op.create_index("ix_machine_installations_factory_tenant_code",
                        "machine_installations", ["factory_tenant_code"])
        op.create_index("ix_machine_installations_serial_number",
                        "machine_installations", ["serial_number"])

    if "oem_data_sharing_policies" not in present:
        op.create_table(
            "oem_data_sharing_policies",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("oem_code", sa.String(), nullable=False),
            sa.Column("tenant_code", sa.String(), nullable=False),
            # Default-deny: an empty grant string shares nothing.
            sa.Column("grants", sa.String(), nullable=False, server_default=""),
            sa.Column("updated_by", sa.String(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("oem_code", "tenant_code",
                                name="uq_oem_sharing_policy"),
        )
        op.create_index("ix_oem_data_sharing_policies_oem_code",
                        "oem_data_sharing_policies", ["oem_code"])
        op.create_index("ix_oem_data_sharing_policies_tenant_code",
                        "oem_data_sharing_policies", ["tenant_code"])


def downgrade() -> None:
    """Drop the OEM tables, children first. The factory schema is untouched by
    both directions, so a downgrade returns the database to exactly the shape it
    had before the OEM layer existed."""
    bind = op.get_bind()
    present = _existing(bind)
    for table in _TABLES:
        if table in present:
            op.drop_table(table)
