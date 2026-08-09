"""bills of materials live in the database, per tenant

Revision ID: 0004_per_tenant_bom
Revises: 0003_tenant_doc_numbers
Create Date: 2026-08-09

WHY
---
The bill of materials was a module-level dict in bom.py: six parts, one
component each, compiled into the application and shared by every customer.
Measured before this change, with two customers who had never heard of each
other and both happening to make a part they call SHAFT-001:

    FACTORY_B completes a work order for 10 of THEIR OWN SHAFT-001:
       FACTORY_B  RM-STEEL-001   stock=80      (was 100)

Their stock moved at 2 per unit — another company's rate. And a product the
dict had never heard of moved nothing at all:

    FACTORY_B completes 50 of VALVE-77 — a product THEY make:
       B-ALLOY-9      stock=500     (unchanged)
       inventory transactions written: 0

CARRYING THE OLD RECIPES FORWARD
--------------------------------
Deleting the dict without migrating its contents would silently stop inventory
moving for the deployment that has been relying on it. So the six legacy recipes
are written into the new tables — but NOT for every tenant, because "every
tenant gets these recipes" is precisely the defect.

A recipe is seeded for a tenant only where it is demonstrably theirs already:
the tenant stocks an InventoryItem whose item_code the recipe names. A tenant
that has never held RM-STEEL-001 does not get a SHAFT-001 recipe, and starts
with an empty recipe book they can fill in — which is the correct state for a
customer whose products we know nothing about.

Idempotent: a (tenant, part_number, revision) that already exists is skipped, so
re-running the migration cannot duplicate or overwrite a recipe someone has
since edited.
"""
import logging

import sqlalchemy as sa
from alembic import op

revision = "0004_per_tenant_bom"
down_revision = "0003_tenant_doc_numbers"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")

# bom.PART_BOM as it stood, verbatim, so the backfill is auditable against the
# code it replaces rather than a paraphrase of it.
LEGACY = {
    "SHAFT-001": {"raw": "RM-STEEL-001", "consume_per_unit": 2, "fg": "FG-SHAFT-001"},
    "PLATE-002": {"raw": "RM-SHEET-002", "consume_per_unit": 1, "fg": "FG-PLATE-002"},
    "BEAR-003":  {"raw": "RM-STEEL-001", "consume_per_unit": 3, "fg": None},
    "GEAR-004":  {"raw": "RM-ALUM-003",  "consume_per_unit": 2, "fg": "FG-GEAR-003"},
    "ASSY-005":  {"raw": None,           "consume_per_unit": 0, "fg": "FG-BRACKET-004"},
    "PKG-006":   {"raw": "PKG-MAT-001",  "consume_per_unit": 1, "fg": None},
}


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())

    if "bills_of_materials" not in tables:
        op.create_table(
            "bills_of_materials",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_code", sa.String(), nullable=False,
                      server_default="DEFAULT"),
            sa.Column("part_number", sa.String(), nullable=False),
            sa.Column("revision", sa.String(), nullable=False, server_default="A"),
            sa.Column("output_item_code", sa.String(), nullable=True),
            sa.Column("effective_from", sa.Date(), nullable=True),
            sa.Column("active", sa.Boolean(), nullable=False,
                      server_default=sa.true()),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("created_by", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_by", sa.String(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("tenant_code", "part_number", "revision",
                                name="uq_bom_tenant_part_revision"),
        )
        op.create_index("ix_bills_of_materials_tenant_code", "bills_of_materials",
                        ["tenant_code"])

    if "bom_components" not in tables:
        op.create_table(
            "bom_components",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_code", sa.String(), nullable=False,
                      server_default="DEFAULT"),
            sa.Column("bom_id", sa.Integer(),
                      sa.ForeignKey("bills_of_materials.id"), nullable=False),
            sa.Column("component_code", sa.String(), nullable=False),
            sa.Column("quantity_per_unit", sa.Float(), nullable=False,
                      server_default="0"),
            sa.Column("unit", sa.String(), nullable=False, server_default=""),
            sa.UniqueConstraint("bom_id", "component_code", name="uq_bom_component"),
        )
        op.create_index("ix_bom_components_tenant_code", "bom_components",
                        ["tenant_code"])
        op.create_index("ix_bom_components_bom_id", "bom_components", ["bom_id"])

    # ---- carry the legacy recipes forward, tenant by tenant -----------------
    if "inventory_items" not in tables:
        log.info("no inventory_items table — nothing to seed recipes against")
        return

    existing = {
        (r[0], r[1]) for r in bind.execute(sa.text(
            "SELECT tenant_code, part_number FROM bills_of_materials")).fetchall()
    }
    stock = {}
    for tenant, code, unit in bind.execute(sa.text(
            "SELECT tenant_code, item_code, unit FROM inventory_items")).fetchall():
        stock.setdefault(tenant, {})[code] = unit

    seeded = 0
    for tenant, codes in stock.items():
        for part, recipe in LEGACY.items():
            raw, fg = recipe["raw"], recipe["fg"]
            # "Demonstrably theirs already": the tenant stocks an item this
            # recipe names. A tenant holding neither the input nor the output
            # has no business being given somebody else's recipe.
            touches = (raw and raw in codes) or (fg and fg in codes)
            if not touches or (tenant, part) in existing:
                continue
            res = bind.execute(sa.text(
                "INSERT INTO bills_of_materials "
                "(tenant_code, part_number, revision, output_item_code, active,"
                " notes, created_by) "
                "VALUES (:t, :p, 'A', :fg, TRUE, :n, 'migration-0004')"
                if bind.dialect.name != "sqlite" else
                "INSERT INTO bills_of_materials "
                "(tenant_code, part_number, revision, output_item_code, active,"
                " notes, created_by) "
                "VALUES (:t, :p, 'A', :fg, 1, :n, 'migration-0004')"),
                {"t": tenant, "p": part, "fg": fg,
                 "n": "Carried forward from the built-in recipe book (0004)."})
            bom_id = res.lastrowid if hasattr(res, "lastrowid") and res.lastrowid \
                else bind.execute(sa.text(
                    "SELECT id FROM bills_of_materials WHERE tenant_code=:t "
                    "AND part_number=:p AND revision='A'"),
                    {"t": tenant, "p": part}).scalar()
            if raw and raw in codes and recipe["consume_per_unit"] > 0:
                bind.execute(sa.text(
                    "INSERT INTO bom_components "
                    "(tenant_code, bom_id, component_code, quantity_per_unit, unit) "
                    "VALUES (:t, :b, :c, :q, :u)"),
                    {"t": tenant, "b": bom_id, "c": raw,
                     "q": float(recipe["consume_per_unit"]),
                     "u": codes.get(raw) or ""})
            seeded += 1

    log.info("carried %d legacy recipe(s) forward into per-tenant bills of "
             "materials", seeded)


def downgrade() -> None:
    """Drops both tables.

    Recipes a customer entered themselves are lost, which is why this is a
    downgrade and not a routine operation. The application falls back to no
    BOM at all — completions move no stock — rather than silently reverting to
    somebody else's recipes.
    """
    op.drop_table("bom_components")
    op.drop_table("bills_of_materials")
