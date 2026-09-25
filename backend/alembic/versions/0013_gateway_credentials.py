"""a gateway proves which workspace it speaks for, and a record is written once

Revision ID: 0013_gateway_credentials
Revises: 0012_consent_scope
Create Date: 2026-09-25

WHY
---
AMP takes tenant and site from the MQTT topic. `mqtt_identity` is right that the
topic is the only part a BROKER can enforce -- but that enforcement belongs to
the broker, and depends on per-gateway ACLs being configured correctly on it.
Every pilot gateway holds valid broker credentials by definition. So on a broker
whose ACL is wrong, absent, or simply `#`, one customer publishes into another
customer's factory by editing a string in a config file they own. There is no
exploit involved; it is the protocol working as designed.

`gateway_credentials` binds a gateway id to EXACTLY ONE workspace and ONE site.
The gateway signs each message with `secret`; AMP verifies the signature to
learn who is speaking, then compares this row against the topic to learn whether
they may. Re-signing a stolen packet with your own key passes the first check
and fails the second.

The second change is about arithmetic rather than access. A gateway removes a
record from its local queue only once the broker has acknowledged it, which
makes delivery AT-LEAST-ONCE: a publish that times out may or may not have
arrived, and re-sending is the only safe response to not knowing. Today a retry
writes a SECOND production record and a shift's output doubles -- silently, and
in the direction that flatters the customer, which is the worst direction for a
number they will act on. `production_records.source_record_id` plus a unique
constraint makes the retry a no-op instead.

WHAT THIS ADDS
--------------
  gateway_credentials           tenant_code, site, gateway_id (unique), secret,
                                label, is_active, created_at, last_seen_at
  production_records
    .source_record_id           the gateway's own id for the message, NULL for
                                everything not published by a gateway
    uq_production_source_record UNIQUE (tenant_code, source_record_id)

NULLABLE, NO BACKFILL, AND NOTHING BREAKS. Every production record that exists
today was written by a CSV import, the HTTP ingest or a person typing it in;
none has a gateway id, so all of them carry NULL. NULL is distinct from NULL in
both SQLite and PostgreSQL, so the unique constraint does not collapse them into
one -- which is the whole reason it can be added to a populated table at all.

AND NO TENANT LOSES INGEST. A workspace with no credential row keeps the
behaviour it has: unsigned packets are accepted, exactly as before. The rule
only tightens once a credential EXISTS for that tenant, which is a deliberate
act by an operator. A migration that silently required signatures would take
every existing customer's telemetry offline the moment it ran.

Additive and reversible. Every add is guarded, because boot's create_all -- or
the boot-time repair in main.py -- may have made the table or the column first.
"""
import sqlalchemy as sa
from alembic import op

revision = "0013_gateway_credentials"
down_revision = "0012_consent_scope"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def upgrade():
    inspector = _inspector()

    if "gateway_credentials" not in inspector.get_table_names():
        op.create_table(
            "gateway_credentials",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_code", sa.String(), nullable=False),
            # NOT NULL with a server default, for the same reason machines.site
            # is: in PostgreSQL NULL != NULL, so a nullable column cannot be
            # compared for equality reliably, and "" is how a single-plant
            # customer spells "no site".
            sa.Column("site", sa.String(), nullable=False, server_default=""),
            sa.Column("gateway_id", sa.String(length=64), nullable=False),
            sa.Column("secret", sa.String(), nullable=False),
            sa.Column("label", sa.String(), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("last_seen_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("gateway_id", name="uq_gateway_credential_id"),
        )
        # The primary key is already indexed; this one is redundant and exists
        # only because every other table in models.py declares `index=True` on
        # its id. The drift gate compares against the model, so the migration
        # matches the model rather than the model being made an exception.
        op.create_index("ix_gateway_credentials_id", "gateway_credentials", ["id"])
        op.create_index("ix_gateway_credentials_tenant_code", "gateway_credentials",
                        ["tenant_code"])
        op.create_index("ix_gateway_credentials_gateway_id", "gateway_credentials",
                        ["gateway_id"])
        op.create_index("ix_gateway_credentials_created_at", "gateway_credentials",
                        ["created_at"])

    # THE TABLE MAY NOT EXIST. get_columns() RAISES on a missing table rather
    # than returning empty, and `production_records` is created by boot's
    # create_all rather than by any migration -- so a database built from
    # migrations alone does not have it. That is not hypothetical: it is exactly
    # the shape verify_pg_migration.py builds to prove the identity migration
    # survives an old deployment, and it is the shape a long-lived deployment
    # can genuinely be in. Unguarded, this revision took the whole upgrade down
    # with NoSuchTableError.
    inspector = _inspector()
    if "production_records" in inspector.get_table_names():
        columns = {c["name"] for c in inspector.get_columns("production_records")}
        if "source_record_id" not in columns:
            op.add_column("production_records",
                          sa.Column("source_record_id", sa.String(length=64), nullable=True))
            op.create_index("ix_production_records_source_record_id", "production_records",
                            ["source_record_id"])

        existing = {c["name"] for c in _inspector().get_unique_constraints("production_records")}
        if "uq_production_source_record" not in existing:
            # SQLite cannot ALTER TABLE ADD CONSTRAINT, so it needs a batch
            # operation (which rebuilds the table). PostgreSQL takes it
            # directly. Guarded rather than assumed: this project runs both.
            with op.batch_alter_table("production_records") as batch:
                batch.create_unique_constraint("uq_production_source_record",
                                               ["tenant_code", "source_record_id"])


def downgrade():
    inspector = _inspector()

    # Same guard as the upgrade side, for the same reason.
    if "production_records" in inspector.get_table_names():
        existing = {c["name"] for c in inspector.get_unique_constraints("production_records")}
        if "uq_production_source_record" in existing:
            with op.batch_alter_table("production_records") as batch:
                batch.drop_constraint("uq_production_source_record", type_="unique")

        columns = {c["name"] for c in _inspector().get_columns("production_records")}
        if "source_record_id" in columns:
            indexes = {i["name"] for i in _inspector().get_indexes("production_records")}
            if "ix_production_records_source_record_id" in indexes:
                op.drop_index("ix_production_records_source_record_id",
                              table_name="production_records")
            op.drop_column("production_records", "source_record_id")

    if "gateway_credentials" in _inspector().get_table_names():
        # Dropping this table DESTROYS every issued gateway key, and there is no
        # way back: the keys are on plant PCs and AMP has no other copy. A
        # downgrade past this revision therefore means re-issuing credentials
        # for every gateway in the field. Said here rather than discovered.
        op.drop_table("gateway_credentials")
