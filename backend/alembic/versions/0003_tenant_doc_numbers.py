"""business document numbers are unique per tenant, not per platform

The revision id is kept SHORT on purpose: alembic_version.version_num is
VARCHAR(32), and a longer id lets the migration apply all of its DDL and then
fail on the stamp — leaving a database whose schema has changed but whose
recorded version has not, which no later deploy can migrate. Measured against
PostgreSQL 18 with the original 35-character id:

    INFO  Running upgrade 0002_machine_site_identity -> 0003_...
    psycopg2.errors.StringDataRightTruncation: value too long for type
    character varying(32)
    [SQL: UPDATE alembic_version SET version_num='0003_tenant_scoped_document_numbers'...]

Same failure class as #499. test_migrate.py now asserts the length.

Revision ID: 0003_tenant_doc_numbers
Revises: 0002_machine_site_identity
Create Date: 2026-08-09

WHY
---
Nineteen business codes carried a plain ``unique=True``, which is a UNIQUE
constraint over the whole table and therefore over every customer at once.
Measured on master, seeding two tenants and asking each to create the same code:

    20 of 20 business codes cannot be reused by a second tenant

WO-001, INV-001, PO-001, SUP-001 — the numbers every manufacturer actually uses
— were first-come-first-served across the entire platform. Two consequences,
and the second is worse than the first:

  * A customer cannot use their own numbering. Onboarding customer #2 means
    telling them their part codes are taken.
  * The generated numbers collide during ORDINARY work. The counters read
    ``db.query(Model).count()``, which the ADR-0002 scoping hook scopes to the
    current tenant, so two customers independently generate MIS-5001 — and the
    global constraint then rejects the second one. Reproduced: FACTORY_B's very
    first material issue slip raised
    ``IntegrityError: UNIQUE constraint failed: material_issue_slips.slip_no``
    and reached the client as a 500.

WHAT CHANGES
------------
Each of the nineteen becomes ``UNIQUE (tenant_code, <code>)``. This is the
pattern the GMATS tables in this same schema already use (``GmatsInvoice.
invoice_no`` is ``nullable=False`` with no global unique, and its counter
filters by tenant explicitly) — so this migration makes the rest of the schema
agree with the part of it that was already right.

WHAT STAYS GLOBAL, AND WHY
--------------------------
  * ``User.username`` — the login identity. It is resolved BEFORE any tenant is
    known, so it cannot be scoped by one without making login ambiguous.
  * ``TenantConfig.tenant_code``, ``AgentPolicy.tenant_code``,
    ``CompanyTenant.company_code`` — these ARE the tenant registry. One row per
    tenant is the point.

Per-site was considered and rejected for all nineteen: a document number is a
commercial and legal artefact of the COMPANY (an invoice number must be unique
to the entity that issued it, not to the plant that printed it), and no customer
has asked for per-site sequences. Machine identity is the one thing that is
genuinely per-site, and ADR-0011 already handles it.

WHY THIS MIGRATION CANNOT FAIL ON EXISTING DATA
-----------------------------------------------
Unlike 0002, this constraint is strictly WEAKER than the one it replaces.
Every row that satisfied "unique across the platform" also satisfies "unique
within its tenant", so there is nothing to de-duplicate and no row to rename.
The only risk is the DDL itself, which is why the drop is driven by reflection
rather than by guessed constraint names.
"""
import logging

import sqlalchemy as sa
from alembic import op

revision = "0003_tenant_doc_numbers"
down_revision = "0002_machine_site_identity"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")

# (table, code column). Constraint names match models.py.
TARGETS = [
    ("work_orders", "work_order_no"),
    ("production_plans", "plan_no"),
    ("inventory_items", "item_code"),
    ("quality_inspections", "inspection_no"),
    ("customer_orders", "order_no"),
    ("suppliers", "supplier_code"),
    ("purchase_orders", "po_no"),
    ("compliance_documents", "document_no"),
    ("maintenance_tasks", "task_no"),
    ("production_schedules", "schedule_no"),
    ("cost_records", "cost_no"),
    ("operator_job_executions", "execution_no"),
    ("report_requests", "report_no"),
    ("remnants", "tag_no"),
    ("material_issue_slips", "slip_no"),
    ("goods_receipt_notes", "grn_no"),
    ("cycle_counts", "count_no"),
    ("industrial_devices", "device_code"),
    ("plc_signal_mappings", "mapping_code"),
]


def _name(table, col):
    return f"uq_{table}_tenant_{col}"


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing_tables = set(insp.get_table_names())
    is_sqlite = bind.dialect.name == "sqlite"

    converted, skipped = 0, []
    for table, col in TARGETS:
        if table not in existing_tables:
            # A table this deployment has never created (a model added later than
            # the database). Nothing to constrain; say so rather than crashing a
            # deploy over a table that is not there.
            skipped.append(f"{table} (absent)")
            continue

        target = _name(table, col)
        uniques = insp.get_unique_constraints(table)
        if any(u["name"] == target for u in uniques):
            skipped.append(f"{table} (already converted)")
            continue

        if is_sqlite:
            # SQLite writes a column-level UNIQUE into the CREATE TABLE and has
            # no DROP CONSTRAINT, so the only way to remove it is to rebuild the
            # table. batch_alter_table does exactly that, and driving it from
            # models.py metadata means the rebuilt table matches the model
            # instead of a hand-written approximation of it.
            from database import Base
            model_table = Base.metadata.tables[table]
            with op.batch_alter_table(table, copy_from=model_table,
                                      recreate="always"):
                pass
        else:
            # PostgreSQL names the constraint itself (work_orders_work_order_no_key).
            # Reflect it rather than guessing: the name depends on the server
            # version and on whether the column was ever renamed.
            for u in uniques:
                if u["column_names"] == [col]:
                    op.drop_constraint(u["name"], table, type_="unique")
                    break
            else:
                # Some deployments carry it as a unique INDEX instead (which is
                # what CREATE UNIQUE INDEX or an older create_all produces).
                for ix in insp.get_indexes(table):
                    if ix.get("unique") and ix["column_names"] == [col]:
                        op.drop_index(ix["name"], table_name=table)
                        break
            op.create_unique_constraint(target, table, ["tenant_code", col])
        converted += 1

    log.info("tenant-scoped %d document-number constraints", converted)
    for s in skipped:
        log.info("  skipped %s", s)


def downgrade() -> None:
    """Restore platform-wide uniqueness.

    This can legitimately FAIL, and that is correct rather than a defect: once
    two tenants each hold a WO-001 — which is the entire point of the upgrade —
    there is no way to restore a global constraint without renaming or deleting
    one of them, and this migration will not silently pick a customer to
    damage. The error names the collision so an operator can decide.
    """
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing_tables = set(insp.get_table_names())
    is_sqlite = bind.dialect.name == "sqlite"

    for table, col in TARGETS:
        if table not in existing_tables:
            continue
        dupes = bind.execute(sa.text(
            f"SELECT {col} FROM {table} GROUP BY {col} HAVING count(*) > 1"
        )).fetchall()
        if dupes:
            raise RuntimeError(
                f"cannot restore platform-wide uniqueness on {table}.{col}: "
                f"{len(dupes)} value(s) are held by more than one tenant "
                f"(e.g. {dupes[0][0]!r}). Resolve them deliberately first.")
        if is_sqlite:
            continue    # the rebuild path is not reversible in place
        op.drop_constraint(_name(table, col), table, type_="unique")
        op.create_unique_constraint(f"{table}_{col}_key", table, [col])
