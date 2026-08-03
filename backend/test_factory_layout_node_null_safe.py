"""A NULL FactoryLayoutNode field must not 500 the floor map or leak into a zone.

FactoryLayoutNode declares node_type/x_position/y_position/width/height/zone as
Column(..., default=...) WITHOUT nullable=False (models.py). The ORM default only
fills a value the *inserter* omitted, so a row written by raw SQL, a migration, or
a PATCH that cleared the field can legitimately hold NULL. Two surfaces broke on
such a row:

  1. FactoryLayoutNodeResponse typed each of those columns as a non-optional
     int/str, so one NULL row raised Pydantic ValidationError during response
     serialisation and 500-ed the WHOLE GET /factory-layout/nodes list — one bad
     row hiding the entire digital-twin floor map — and the PATCH response that
     wrote it. Now healed to each column's OWN declared default on the way out.
  2. /analytics/factory-command-center reads node.zone RAW (it returns a plain
     dict, bypassing the response heal) to bucket nodes per zone, so a NULL zone
     surfaced as a bucket literally labelled `null`. Now coalesced to "Production".

The NULL is API-reachable: FactoryLayoutNodeUpdate types every field Optional and
the PATCH setattr loop over model_dump(exclude_unset=True) persists an explicit
{"zone": null}. Same response-serialisation NULL class already healed on the
machine roster (#375), the operator-execution list (#456) and saas-tenants (#465).

Run:  cd backend && DATABASE_URL="sqlite:///./ci.db" python test_factory_layout_node_null_safe.py   (exit 0 = pass)
"""
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import analytics_routes as AR
import factory_ops_routes as FO
import models
import schemas
import tenancy as T
from database import Base


def _iso_session():
    T.install_scoping()
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _insert_node(db, tenant, node_name, *, machine_id="NULL", node_type="'Machine'",
                 x="50", y="50", width="160", height="100", zone="'Production'"):
    """Insert a layout node directly. Any column may be the bare token NULL — the raw
    path is how a legacy / migration / cleared-field row comes to hold a genuine NULL
    (bypassing the ORM default)."""
    db.execute(
        text(
            "INSERT INTO factory_layout_nodes "
            "(tenant_code, machine_id, node_name, node_type, x_position, y_position, "
            " width, height, zone) VALUES "
            f"(:t, {machine_id}, :n, {node_type}, {x}, {y}, {width}, {height}, {zone})"
        ),
        {"t": tenant, "n": node_name},
    )
    db.commit()
    db.expire_all()


def test_response_heals_null_fields_to_declared_defaults():
    # Serialise a NULL row EXACTLY as FastAPI does (response_model + from_attributes);
    # this is the step that used to raise ValidationError.
    db = _iso_session()
    _insert_node(db, "DEFAULT", "OK-NODE", x="300", y="220", width="200", height="140",
                 node_type="'Cell'", zone="'Assembly'")
    _insert_node(db, "DEFAULT", "NULL-NODE", node_type="NULL", x="NULL", y="NULL",
                 width="NULL", height="NULL", zone="NULL")

    by_name = {n.node_name: schemas.FactoryLayoutNodeResponse.model_validate(n)
               for n in db.query(models.FactoryLayoutNode)
                          .order_by(models.FactoryLayoutNode.id).all()}
    assert set(by_name) == {"OK-NODE", "NULL-NODE"}, set(by_name)

    # The NULL row heals to each column's OWN declared default (50/50/160/100,
    # "Machine", "Production") — independently derived from models.py, NOT 0.
    healed = by_name["NULL-NODE"]
    assert healed.node_type == "Machine", healed.node_type
    assert healed.zone == "Production", healed.zone
    assert healed.x_position == 50, healed.x_position
    assert healed.y_position == 50, healed.y_position
    assert healed.width == 160, healed.width
    assert healed.height == 100, healed.height

    # A real recorded value is NOT rewritten (mode="before" only touches None).
    real = by_name["OK-NODE"]
    assert real.node_type == "Cell" and real.zone == "Assembly", (real.node_type, real.zone)
    assert (real.x_position, real.y_position, real.width, real.height) == (300, 220, 200, 140)
    print("PASS FactoryLayoutNodeResponse heals NULL fields to their declared defaults, preserves real values")


def test_real_zero_position_is_preserved():
    # A genuine 0 coordinate must survive — mode="before" only rewrites None, so the
    # heal must not clobber a real 0 (the honesty rule: a default must not replace a
    # real value). A node legitimately parked at the origin stays at (0, 0).
    db = _iso_session()
    _insert_node(db, "DEFAULT", "ORIGIN", x="0", y="0")
    node = db.query(models.FactoryLayoutNode).filter_by(node_name="ORIGIN").one()
    resp = schemas.FactoryLayoutNodeResponse.model_validate(node)
    assert resp.x_position == 0 and resp.y_position == 0, (resp.x_position, resp.y_position)
    print("PASS a real 0 coordinate is preserved (heal only rewrites None)")


def test_list_endpoint_survives_null_row():
    # End-to-end: the whole GET /factory-layout/nodes list serialises without a 500
    # even with a NULL-field row present, and every good node still comes back.
    db = _iso_session()
    _insert_node(db, "DEFAULT", "OK-NODE")
    _insert_node(db, "DEFAULT", "NULL-NODE", zone="NULL", node_type="NULL", width="NULL")

    tok = T.set_current_tenant("DEFAULT")
    try:
        rows = FO.get_factory_layout_nodes(db=db, current_user={"tenant": "DEFAULT"})
        serialised = [schemas.FactoryLayoutNodeResponse.model_validate(r) for r in rows]
    finally:
        T.reset_current_tenant(tok)

    names = {s.node_name for s in serialised}
    assert names == {"OK-NODE", "NULL-NODE"}, names
    for s in serialised:
        assert isinstance(s.zone, str) and s.zone
        assert isinstance(s.node_type, str) and s.node_type
        assert isinstance(s.width, int)
    print("PASS GET /factory-layout/nodes returns the full list with a NULL-field row (no 500)")


def test_patch_null_zone_is_reachable_and_healed():
    # API-reachability: PATCH {"zone": null} persists a NULL through the handler's
    # setattr loop, and the PATCH response heals it rather than 500-ing.
    db = _iso_session()
    _insert_node(db, "DEFAULT", "PATCH-NODE", zone="'Assembly'")

    tok = T.set_current_tenant("DEFAULT")
    try:
        row = db.query(models.FactoryLayoutNode).filter_by(node_name="PATCH-NODE").one()
        # zone explicitly set to None -> included by exclude_unset (mirrors {"zone": null}).
        updated = FO.update_factory_layout_node(
            node_id=row.id,
            payload=schemas.FactoryLayoutNodeUpdate(zone=None),
            db=db,
            current_user={"tenant": "DEFAULT", "role": "Supervisor"},
        )
        db.expire_all()
        raw_zone = db.execute(
            text("SELECT zone FROM factory_layout_nodes WHERE node_name = 'PATCH-NODE'")
        ).scalar()
        resp = schemas.FactoryLayoutNodeResponse.model_validate(updated)
    finally:
        T.reset_current_tenant(tok)

    assert raw_zone is None, f"expected NULL persisted, got {raw_zone!r}"
    # The PATCH response heals the NULL to the column default rather than 500-ing.
    assert resp.zone == "Production", resp.zone
    print("PASS PATCH {zone: null} persists NULL and the response heals it to 'Production' (no 500)")


def test_command_center_zone_rollup_coalesces_null_zone():
    # The zone rollup reads node.zone RAW (plain-dict endpoint, no response heal), so
    # a NULL zone must be bucketed as "Production", not surfaced as a `null` label —
    # and the node must still be counted (reconciles: sum(zone.nodes) == node count).
    db = _iso_session()
    _insert_node(db, "DEFAULT", "Z-OK", zone="'Assembly'")
    _insert_node(db, "DEFAULT", "Z-NULL", zone="NULL")

    tok = T.set_current_tenant("DEFAULT")
    try:
        result = AR.get_factory_command_center(db=db, current_user={"tenant": "DEFAULT"})
    finally:
        T.reset_current_tenant(tok)

    zones = {z["zone"]: z for z in result["zone_summary"]}
    # No bucket is labelled with a missing value; the NULL folded into "Production".
    assert None not in zones, zones
    assert "Assembly" in zones and "Production" in zones, zones
    assert zones["Production"]["nodes"] == 1, zones["Production"]
    # Every node is accounted for — the parts sum to the whole (rule 3).
    assert sum(z["nodes"] for z in result["zone_summary"]) == 2
    print("PASS /analytics/factory-command-center buckets a NULL zone as 'Production', counts every node")


if __name__ == "__main__":
    test_response_heals_null_fields_to_declared_defaults()
    test_real_zero_position_is_preserved()
    test_list_endpoint_survives_null_row()
    test_patch_null_zone_is_reachable_and_healed()
    test_command_center_zone_rollup_coalesces_null_zone()
    print("ALL FACTORY-LAYOUT-NODE NULL-SAFE TESTS PASSED")
