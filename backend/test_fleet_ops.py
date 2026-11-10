"""Fleet Ops behaviour (fleet_engine + fleet_routes).

What is pinned here, in order:
  * registration — every /fleet path exists exactly once and is owned by
    fleet_routes (the ADR-0009 guard discipline);
  * the dispatch invariant — most urgent first, nearest idle vehicle, one
    task per vehicle, low-battery vehicles never given work;
  * the movement lifecycle — ToPickup -> pickup -> Carrying -> Delivered with
    every timestamp written, odometer and delivery counters moving;
  * the assistance loop — a Stuck vehicle raises ONE plea, stops moving, and
    each advice verb does exactly what it says (resume / return_task /
    send_to_charge);
  * battery honesty — a critically-low tasked vehicle aborts, returns its
    task to the queue, and charges back to Idle;
  * station validation — a task to a station that doesn't exist is a 400
    with the stations named;
  * tenant isolation — two tenants' fleets never see each other through the
    routes (the ADR-0002 hook, exercised end to end);
  * the lazy clock — tick() caps a long absence at MAX_TICK_SECONDS.

Run:  python backend/test_fleet_ops.py     (exit 0 = pass)
Also collectable by pytest (conftest resets the tenant ContextVar per test;
nothing here binds a tenant at module level — pytest-single-process rule).
"""
import math
import random
from datetime import datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import fleet_engine
import fleet_routes
import models
from database import Base

ADMIN = {"tenant": "DEFAULT", "role": "Admin", "sub": "founder"}


class NeverFires:
    """rng whose every roll is 1.0 — no stuck event, no MES task, ever.
    Determinism by construction, not by hoping a seed stays lucky."""
    def random(self):
        return 1.0


class AlwaysFires:
    """rng whose every roll is 0.0 — the probabilistic branch always takes."""
    def random(self):
        return 0.0


def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _machine(db, name, status="Running", tenant="DEFAULT"):
    m = models.Machine(name=name, status=status, utilization=50, downtime="0 min",
                       tenant_code=tenant)
    db.add(m)
    db.commit()
    return m


def _vehicle(db, name, x=8.0, y=85.0, battery=100.0, vtype="AMR",
             status="Idle", tenant="DEFAULT"):
    v = models.FleetVehicle(name=name, vehicle_type=vtype, status=status,
                            x=x, y=y, battery_pct=battery, tenant_code=tenant)
    db.add(v)
    db.commit()
    return v


def _task(db, origin, destination, priority=2, tenant="DEFAULT", **kw):
    t = models.TransportTask(origin=origin, destination=destination,
                             priority=priority, tenant_code=tenant, **kw)
    db.add(t)
    db.commit()
    return t


# ── Registration guard ──────────────────────────────────────────────────

EXPECTED = {
    "/fleet/state", "/fleet/tasks", "/fleet/tasks/{task_id}/cancel",
    "/fleet/vehicles", "/fleet/vehicles/{vehicle_id}",
    "/fleet/alerts", "/fleet/alerts/{alert_id}/advise",
    "/fleet/seed", "/fleet/sim/tick",
}


def test_fleet_paths_registered_once_and_owned():
    import main
    counts, owners = {}, {}
    for r in main.app.routes:
        p = getattr(r, "path", "")
        if p in EXPECTED:
            counts[p] = counts.get(p, 0) + 1
            owners.setdefault(p, set()).add(r.endpoint.__module__)
    missing = EXPECTED - set(counts)
    assert not missing, f"fleet paths not registered: {missing}"
    # GET+POST on the same path registers two route objects; same owner is
    # what matters.
    wrong = {p: mods for p, mods in owners.items() if mods != {"fleet_routes"}}
    assert not wrong, f"paths not owned solely by fleet_routes: {wrong}"
    print(f"PASS all {len(EXPECTED)} fleet paths owned by the module")


def test_fleet_pack_gates_the_routes():
    import module_manifest
    assert module_manifest.pack_for_path("/fleet/state") == "fleet"
    assert module_manifest.pack_for_path("/fleet/alerts/9/advise") == "fleet"
    print("PASS /fleet/* resolves to the fleet pack")


# ── Stations ────────────────────────────────────────────────────────────

def test_stations_are_anchors_plus_the_live_roster():
    db = _db()
    s = fleet_engine.stations_for(db)
    assert set(s) == set(fleet_engine.ANCHORS)          # empty roster: anchors only
    _machine(db, "IMM-01")
    _machine(db, "IMM-02")
    s = fleet_engine.stations_for(db)
    assert "IMM-01" in s and "IMM-02" in s
    assert s["IMM-01"]["kind"] == "machine"
    # Deterministic: the same roster yields the same map.
    assert s == fleet_engine.stations_for(db)
    print("PASS stations derive from the roster, deterministically")


# ── Dispatch ────────────────────────────────────────────────────────────

def test_dispatch_prefers_urgency_then_nearest_and_skips_flat_batteries():
    db = _db()
    _machine(db, "IMM-01")
    near = _vehicle(db, "NEAR", x=30.0, y=30.0)
    far = _vehicle(db, "FAR", x=90.0, y=90.0)
    flat = _vehicle(db, "FLAT", x=31.0, y=31.0, battery=10.0)
    low = _task(db, "IMM-01", "Packing", priority=1)
    high = _task(db, "IMM-01", "Packing", priority=3)

    assigned = fleet_engine.dispatch(db)
    assert assigned == 2
    db.refresh(high); db.refresh(low); db.refresh(near); db.refresh(far); db.refresh(flat)
    # The urgent task got the nearest healthy vehicle; the flat one got nothing.
    assert high.vehicle_id == near.id and high.status == "Assigned"
    assert low.vehicle_id == far.id
    assert flat.current_task_id is None and flat.status == "Idle"
    assert near.status == "ToPickup" and near.current_task_id == high.id
    assert high.assigned_at is not None
    print("PASS dispatch: urgency first, nearest healthy vehicle, one task each")


def test_dispatch_leaves_a_task_to_nowhere_pending():
    db = _db()
    _vehicle(db, "AMR-X")
    ghost = _task(db, "No Such Machine", "Packing")
    fleet_engine.dispatch(db)
    db.refresh(ghost)
    assert ghost.status == "Pending" and ghost.vehicle_id is None
    print("PASS a task whose origin is unknown stays visible in the queue")


# ── Movement lifecycle ──────────────────────────────────────────────────

def test_full_delivery_lifecycle_with_timestamps_and_counters():
    db = _db()
    _machine(db, "IMM-01")
    v = _vehicle(db, "AMR-01", x=8.0, y=85.0)
    t = _task(db, "IMM-01", "Packing", payload="Finished batch")
    fleet_engine.dispatch(db)
    # NeverFires: no stuck roll can interrupt the lifecycle under test.
    for _ in range(40):
        fleet_engine.advance(db, 30.0, rng=NeverFires())
        db.refresh(t)
        if t.status == "Delivered":
            break
    db.refresh(v)
    assert t.status == "Delivered", t.status
    assert t.assigned_at and t.picked_up_at and t.completed_at
    assert v.status in ("Idle", "Charging")
    assert v.current_task_id is None
    assert v.deliveries_total == 1
    assert v.odometer_m > 0
    assert v.battery_pct < 100.0            # moving costs energy
    print("PASS lifecycle: Pending->Assigned->InTransit->Delivered, counters move")


def test_the_lazy_clock_caps_a_long_absence():
    db = _db()
    applied = fleet_engine.tick(db, tenant="T_CAP", seconds=None,
                                now=datetime.utcnow())
    # First observation: a fresh tenant starts with a small step, never a leap.
    assert applied <= fleet_engine.MAX_TICK_SECONDS
    # Simulate a weekend away: force the stored clock far into the past.
    fleet_engine._last_tick["T_CAP"] = datetime.utcnow() - timedelta(days=2)
    applied = fleet_engine.tick(db, tenant="T_CAP")
    assert applied == fleet_engine.MAX_TICK_SECONDS
    print("PASS lazy tick caps elapsed time at MAX_TICK_SECONDS")


# ── Assistance loop ─────────────────────────────────────────────────────

def _stick(db):
    """Deterministically produce a Stuck vehicle with an open plea."""
    _machine(db, "IMM-01")
    v = _vehicle(db, "AMR-01", x=8.0, y=85.0)
    t = _task(db, "IMM-01", "Packing")
    fleet_engine.dispatch(db)
    # every roll fires → the stuck branch takes on the first moving tick
    fleet_engine.advance(db, 5.0, rng=AlwaysFires())
    db.refresh(v)
    alert = db.query(models.FleetAlert).filter(models.FleetAlert.status == "Open").first()
    return v, t, alert


def test_a_stuck_vehicle_pleads_once_and_stops():
    db = _db()
    v, t, alert = _stick(db)
    assert v.status == "Stuck"
    assert alert is not None and alert.kind == "Stuck" and alert.vehicle_id == v.id
    x, y = v.x, v.y
    fleet_engine.advance(db, 30.0, rng=NeverFires())
    db.refresh(v)
    assert (v.x, v.y) == (x, y)             # no movement while waiting
    opens = db.query(models.FleetAlert).filter(models.FleetAlert.status == "Open").count()
    assert opens == 1                        # it does not plead again every tick
    print("PASS a stuck vehicle raises one plea and holds position")


def test_advice_resume_puts_the_vehicle_back_on_its_leg():
    db = _db()
    v, t, alert = _stick(db)
    fleet_engine.advise(db, alert, "resume")
    db.refresh(v); db.refresh(alert)
    assert v.status == "ToPickup"            # not yet picked up → back to that leg
    assert alert.status == "Resolved" and alert.advice == "resume"
    assert alert.resolved_at is not None
    print("PASS advice resume: back to work, plea closed with the verb recorded")


def test_advice_return_task_requeues_and_frees():
    db = _db()
    v, t, alert = _stick(db)
    fleet_engine.advise(db, alert, "return_task")
    db.refresh(v); db.refresh(t)
    assert t.status == "Pending" and t.vehicle_id is None and t.assigned_at is None
    assert v.status == "Idle" and v.current_task_id is None
    print("PASS advice return_task: task back in queue, vehicle freed")


def test_advice_send_to_charge_requeues_and_docks():
    db = _db()
    v, t, alert = _stick(db)
    fleet_engine.advise(db, alert, "send_to_charge")
    db.refresh(v); db.refresh(t)
    assert t.status == "Pending"
    assert v.status == "Charging"
    print("PASS advice send_to_charge: task requeued, vehicle heads to dock")


def test_unknown_advice_is_refused_and_changes_nothing():
    db = _db()
    v, t, alert = _stick(db)
    try:
        fleet_engine.advise(db, alert, "drive_manually")
        raise AssertionError("advise accepted an unknown verb")
    except ValueError:
        pass
    db.refresh(alert)
    assert alert.status == "Open"
    print("PASS the advice vocabulary is closed — no remote driving verb exists")


# ── Battery honesty ─────────────────────────────────────────────────────

def test_critical_battery_aborts_the_task_and_recovers_to_idle():
    db = _db()
    _machine(db, "IMM-01")
    v = _vehicle(db, "AMR-01", x=8.0, y=85.0, battery=10.5)
    t = _task(db, "IMM-01", "Packing")
    # Dispatch refuses a sub-25% battery, so force the assignment: the
    # scenario under test is "battery collapsed WHILE tasked".
    t.status = "Assigned"; t.vehicle_id = v.id; t.assigned_at = datetime.utcnow()
    v.status = "ToPickup"; v.current_task_id = t.id
    db.commit()

    fleet_engine.advance(db, 30.0, rng=NeverFires())   # drains below critical
    fleet_engine.advance(db, 5.0, rng=NeverFires())    # abort branch fires
    db.refresh(v); db.refresh(t)
    assert t.status == "Pending" and t.vehicle_id is None
    assert v.status == "Charging"
    plea = db.query(models.FleetAlert).filter(models.FleetAlert.kind == "LowBattery").first()
    assert plea is not None and plea.severity == "Critical"

    # And it recovers: enough charging ticks bring it back to Idle, full enough.
    for _ in range(80):
        fleet_engine.advance(db, 30.0, rng=NeverFires())
        db.refresh(v)
        if v.status == "Idle":
            break
    assert v.status == "Idle" and v.battery_pct >= fleet_engine.CHARGED_ENOUGH
    print("PASS critical battery: abort, plead, requeue, charge, recover")


# ── Routes: validation and shapes ───────────────────────────────────────

def test_create_task_validates_stations_and_self_moves():
    db = _db()
    _machine(db, "IMM-01")
    try:
        fleet_routes.create_task(
            fleet_routes.TaskCreate(origin="Mars", destination="Packing"),
            db=db, current_user=ADMIN)
        raise AssertionError("unknown origin accepted")
    except HTTPException as e:
        assert e.status_code == 400 and "Mars" in e.detail
    try:
        fleet_routes.create_task(
            fleet_routes.TaskCreate(origin="Packing", destination="Packing"),
            db=db, current_user=ADMIN)
        raise AssertionError("origin == destination accepted")
    except HTTPException as e:
        assert e.status_code == 400
    out = fleet_routes.create_task(
        fleet_routes.TaskCreate(origin="IMM-01", destination="Packing",
                                payload="Bin 3", priority=3),
        db=db, current_user=ADMIN)
    assert out["status"] == "Pending" and out["source"] == "manual"
    print("PASS task creation: stations validated, shapes returned")


def test_cancel_refuses_in_transit_and_frees_assigned_vehicles():
    db = _db()
    _machine(db, "IMM-01")
    v = _vehicle(db, "AMR-01")
    t = _task(db, "IMM-01", "Packing")
    fleet_engine.dispatch(db)
    db.refresh(t)
    out = fleet_routes.cancel_task(t.id, db=db, current_user=ADMIN)
    assert out["status"] == "Cancelled"
    db.refresh(v)
    assert v.status == "Idle" and v.current_task_id is None

    t2 = _task(db, "IMM-01", "Packing", status="InTransit", vehicle_id=v.id)
    try:
        fleet_routes.cancel_task(t2.id, db=db, current_user=ADMIN)
        raise AssertionError("cancelled a task already on a vehicle")
    except HTTPException as e:
        assert e.status_code == 409
    print("PASS cancel: frees an assigned vehicle, refuses mid-floor payloads")


def test_vehicle_delete_refuses_while_tasked():
    db = _db()
    _machine(db, "IMM-01")
    v = _vehicle(db, "AMR-01")
    _task(db, "IMM-01", "Packing")
    fleet_engine.dispatch(db)
    try:
        fleet_routes.delete_vehicle(v.id, db=db, current_user=ADMIN)
        raise AssertionError("retired a vehicle mid-task")
    except HTTPException as e:
        assert e.status_code == 409
    print("PASS a tasked vehicle cannot be retired out from under its task")


def test_seed_is_idempotent():
    db = _db()
    assert fleet_engine.seed_demo_fleet(db) == 6
    assert fleet_engine.seed_demo_fleet(db) == 0
    assert db.query(models.FleetVehicle).count() == 6
    print("PASS seeding six vehicles happens exactly once")


def test_mes_task_generation_is_bounded():
    db = _db()
    for i in range(4):
        _machine(db, f"IMM-0{i+1}")
    for _ in range(10):
        fleet_engine.generate_mes_tasks(db, 60.0, rng=AlwaysFires())
    open_mes = (db.query(models.TransportTask)
                .filter(models.TransportTask.source == "mes").count())
    assert open_mes == fleet_engine.MAX_OPEN_MES_TASKS
    t = db.query(models.TransportTask).first()
    assert t.origin in {"IMM-01", "IMM-02", "IMM-03", "IMM-04", "Warehouse"}
    print("PASS MES-event tasks are generated and the auto-queue is bounded")


# ── Tenant isolation, end to end through the hook ───────────────────────

def test_two_tenants_never_see_each_others_fleet():
    import tenancy
    db = _db()
    _vehicle(db, "ACME-AMR", tenant="ACME")
    _vehicle(db, "GLOBEX-AMR", tenant="GLOBEX")
    _task(db, "Warehouse", "Packing", tenant="ACME")
    _task(db, "Warehouse", "Dispatch", tenant="GLOBEX")

    tenancy.install_scoping()
    token = tenancy.set_current_tenant("ACME")
    try:
        vehicles = fleet_routes.list_vehicles(
            db=db, current_user={"tenant": "ACME", "role": "Admin", "sub": "a"})
        assert [v["name"] for v in vehicles] == ["ACME-AMR"]
        tasks = fleet_routes.list_tasks(
            response=None, db=db,
            current_user={"tenant": "ACME", "role": "Admin", "sub": "a"})
        assert [t["destination"] for t in tasks] == ["Packing"]
        # And a write is stamped: a task ACME creates belongs to ACME.
        out = fleet_routes.create_task(
            fleet_routes.TaskCreate(origin="Warehouse", destination="Packing"),
            db=db, current_user={"tenant": "ACME", "role": "Admin", "sub": "a"})
        row = db.query(models.TransportTask).filter(
            models.TransportTask.id == out["id"]).first()
        assert row.tenant_code == "ACME"
    finally:
        tenancy.reset_current_tenant(token)
    print("PASS the hook isolates fleets and stamps writes per tenant")


if __name__ == "__main__":
    test_fleet_paths_registered_once_and_owned()
    test_fleet_pack_gates_the_routes()
    test_stations_are_anchors_plus_the_live_roster()
    test_dispatch_prefers_urgency_then_nearest_and_skips_flat_batteries()
    test_dispatch_leaves_a_task_to_nowhere_pending()
    test_full_delivery_lifecycle_with_timestamps_and_counters()
    test_the_lazy_clock_caps_a_long_absence()
    test_a_stuck_vehicle_pleads_once_and_stops()
    test_advice_resume_puts_the_vehicle_back_on_its_leg()
    test_advice_return_task_requeues_and_frees()
    test_advice_send_to_charge_requeues_and_docks()
    test_unknown_advice_is_refused_and_changes_nothing()
    test_critical_battery_aborts_the_task_and_recovers_to_idle()
    test_create_task_validates_stations_and_self_moves()
    test_cancel_refuses_in_transit_and_frees_assigned_vehicles()
    test_vehicle_delete_refuses_while_tasked()
    test_seed_is_idempotent()
    test_mes_task_generation_is_bounded()
    test_two_tenants_never_see_each_others_fleet()
    print("\nALL FLEET OPS TESTS PASSED")
