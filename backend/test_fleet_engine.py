"""Fleet Ops engine: pin the movement, dispatch, charging and KPI arithmetic.

WHY THIS SUITE EXISTS
---------------------
fleet_engine.py is 429 lines of the pure logic behind /fleet — where things are
on the floor (stations_for), who takes which order (dispatch), how the world
advances between two observations (advance/tick), how a human answers a vehicle's
plea (advise), and the mission-control strip (kpis). It had NO dedicated test: the
only coverage was whatever the HTTP-level fleet_routes exercise happened to brush,
which never pins a coordinate, a battery figure or a KPI denominator, and never
drives the crash edges (a NULL battery on a tasked vehicle, an empty fleet's
averages, an unknown station) the function's own comments say it survives.

WHAT IS ASSERTED (every number is derived by hand from the tunables, not read
back from the code under test):

  1  GEOMETRY   _dist is Euclidean; _move_toward steps at the type's speed and
                snaps on arrival; the station grid places a machine exactly.
  2  DISPATCH   nearest ELIGIBLE idle vehicle wins; a vehicle under the battery
                floor is skipped; an unknown origin leaves the order Pending.
  3  MOTION     a ToPickup vehicle picks up on arrival; a Carrying vehicle
                delivers, increments its counter and parks.
  4  CRASH EDGE a tasked vehicle with a NULL battery aborts to charge WITHOUT
                raising (the #776-class TypeError on format(None)), releasing
                its task and raising one LowBattery alert.
  5  CHARGING   charge accrues only at the dock and ends at CHARGED_ENOUGH; an
                idle vehicle that drains past GO_CHARGE_BELOW heads to charge.
  6  ADVISE     resume / return_task / send_to_charge each land the row in the
                documented state; an unknown verb raises ValueError.
  7  KPIS       an empty fleet reports None averages (not a ZeroDivisionError);
                a populated fleet's average battery, average wait and totals are
                the exact hand-computed figures, and a Delivered task missing a
                timestamp — or carrying out-of-order timestamps (assigned before
                requested) — is left out of the wait average but still counted,
                so the average is never dragged below zero.
  8  CLOCK      the lazy tick defaults to 3 s on first read and caps a long
                absence at MAX_TICK_SECONDS instead of replaying it.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_fleet_engine.py
(pure in-memory SQLite; writes no file.)
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import fleet_engine
import models
import tenancy
from database import Base

T = "FLEETTEST"
failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


def approx(a, b, eps=1e-6):
    return a is not None and b is not None and abs(a - b) <= eps


class FixedRng:
    """A random stub with a single, fixed draw — so STUCK_P and the MES-task
    coin-flips resolve the same way every run. 0.99 never triggers the rare
    'stuck' event (STUCK_P_PER_S * 90 s << 0.99); 0.0 fires every gated branch."""

    def __init__(self, value):
        self.value = value

    def random(self):
        return self.value


class _V:
    """A bare stand-in for a FleetVehicle row, enough for the pure _move_toward
    helper (it reads/writes x, y and reads vehicle_type) without a database."""

    def __init__(self, x, y, vehicle_type):
        self.x, self.y, self.vehicle_type = x, y, vehicle_type


def fresh_db():
    """A clean in-memory DB with tenant scoping live and T bound as the current
    tenant, so every fleet_engine query auto-scopes to T (ADR-0002)."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    tenancy.install_scoping()
    db = sessionmaker(bind=engine)()
    tenancy.set_current_tenant(T)
    db.add(models.TenantConfig(tenant_code=T))
    db.commit()
    return db


def anchors():
    """A fresh copy of the four fixed stations (dispatch/advance mutate nothing
    in them, but a copy keeps each test independent of the module constant)."""
    return {k: dict(v) for k, v in fleet_engine.ANCHORS.items()}


# ── 1  GEOMETRY ──────────────────────────────────────────────────────────

def test_dist_and_move_toward():
    # _dist is a plain Euclidean hypotenuse: the 3-4-5 triangle is exactly 5.
    check("dist 3-4-5", approx(fleet_engine._dist(0, 0, 3, 4), 5.0),
          fleet_engine._dist(0, 0, 3, 4))

    # An AMR (1.4 m/s) over 10 s steps 14 m. Target (30,40) is 50 m away, so it
    # covers a fraction f = 14/50 = 0.28 of the leg: new pos (8.4, 11.2), and the
    # metres returned are the 14 it actually moved.
    v = _V(0.0, 0.0, "AMR")
    moved = fleet_engine._move_toward(v, {"x": 30.0, "y": 40.0}, 10.0)
    check("move partial distance", approx(moved, 14.0), moved)
    check("move partial x", approx(v.x, 8.4), v.x)
    check("move partial y", approx(v.y, 11.2), v.y)

    # 40 s at 1.4 m/s is 56 m >= the 50 m leg, so it snaps onto the target and
    # reports the TRUE distance closed (50), never the 56 it could have gone.
    v = _V(0.0, 0.0, "AMR")
    moved = fleet_engine._move_toward(v, {"x": 30.0, "y": 40.0}, 40.0)
    check("move snaps on arrival", approx(moved, 50.0) and approx(v.x, 30.0) and approx(v.y, 40.0),
          f"moved={moved} pos=({v.x},{v.y})")

    # Already at the target: zero distance, zero movement, position untouched.
    v = _V(5.0, 5.0, "AMR")
    moved = fleet_engine._move_toward(v, {"x": 5.0, "y": 5.0}, 10.0)
    check("move zero distance", approx(moved, 0.0) and approx(v.x, 5.0), moved)

    # An unknown vehicle_type falls back to 1.2 m/s: 10 s -> 12 m along a long
    # leg (100 m), so it does NOT snap and x advances to exactly 12.
    v = _V(0.0, 0.0, "Spaceship")
    moved = fleet_engine._move_toward(v, {"x": 100.0, "y": 0.0}, 10.0)
    check("move default speed 1.2", approx(moved, 12.0) and approx(v.x, 12.0), f"moved={moved} x={v.x}")


def test_stations_for_grid():
    db = fresh_db()
    # No machines: only the four fixed anchors, each tagged "anchor".
    st = fleet_engine.stations_for(db)
    check("stations empty roster = 4 anchors",
          set(st) == {"Warehouse", "Packing", "Dispatch", "Charge Dock"}, sorted(st))
    check("anchor kind", all(st[a]["kind"] == "anchor" for a in st), st)

    # One machine, cols = max(1, ceil(sqrt(1))) = 1, rows = 1, cell (0,0):
    #   x = 25 + 50 * (0 + 0.5)/1 = 50.0 ; y = 18 + 64 * (0 + 0.5)/1 = 50.0
    m1 = models.Machine(tenant_code=T, name="Press-1", site="P1", status="Running",
                        utilization=70, downtime="0 min")
    db.add(m1)
    db.commit()
    st = fleet_engine.stations_for(db)
    check("single machine centred", approx(st["Press-1"]["x"], 50.0) and approx(st["Press-1"]["y"], 50.0),
          st.get("Press-1"))
    check("machine carries id+status",
          st["Press-1"]["machine_id"] == m1.id and st["Press-1"]["machine_status"] == "Running",
          st.get("Press-1"))

    # Two machines, cols = max(1, ceil(sqrt(2)=1.41)) = 2, rows = ceil(2/2) = 1.
    #   i=0 (r,c)=(0,0): x = 25 + 50*0.5/2 = 37.5 ; y = 18 + 64*0.5/1 = 50.0
    #   i=1 (r,c)=(0,1): x = 25 + 50*1.5/2 = 62.5 ; y = 50.0
    db.add(models.Machine(tenant_code=T, name="Press-2", site="P1", status="Idle",
                         utilization=40, downtime="0 min"))
    db.commit()
    st = fleet_engine.stations_for(db)
    check("two machines gridded",
          approx(st["Press-1"]["x"], 37.5) and approx(st["Press-2"]["x"], 62.5)
          and approx(st["Press-1"]["y"], 50.0) and approx(st["Press-2"]["y"], 50.0),
          {k: (st[k]["x"], st[k]["y"]) for k in ("Press-1", "Press-2")})
    db.close()


# ── 2  DISPATCH ──────────────────────────────────────────────────────────

def test_dispatch_nearest_and_battery_gate():
    db = fresh_db()
    # Warehouse is (8, 15). A is 2 m away, B is far, C is 1 m away but flat.
    a = models.FleetVehicle(tenant_code=T, name="A", vehicle_type="AMR", status="Idle",
                            x=10.0, y=15.0, battery_pct=90.0)
    b = models.FleetVehicle(tenant_code=T, name="B", vehicle_type="AMR", status="Idle",
                            x=90.0, y=30.0, battery_pct=90.0)
    c = models.FleetVehicle(tenant_code=T, name="C", vehicle_type="AMR", status="Idle",
                            x=9.0, y=15.0, battery_pct=20.0)   # below DISPATCH_MIN_BATTERY (25)
    db.add_all([a, b, c])
    task = models.TransportTask(tenant_code=T, origin="Warehouse", destination="Packing",
                                payload="x", priority=2, status="Pending",
                                requested_at=datetime.utcnow())
    db.add(task)
    db.commit()

    made = fleet_engine.dispatch(db, stations=anchors())
    db.refresh(a); db.refresh(b); db.refresh(c); db.refresh(task)
    check("dispatch assigns exactly one", made == 1, made)
    # C is nearest but under the battery floor, so the nearest ELIGIBLE (A) wins.
    check("nearest eligible wins", task.vehicle_id == a.id, f"vehicle_id={task.vehicle_id} A={a.id}")
    check("task now Assigned", task.status == "Assigned", task.status)
    check("winner is ToPickup with the task", a.status == "ToPickup" and a.current_task_id == task.id,
          f"{a.status}/{a.current_task_id}")
    check("flat vehicle left idle", c.status == "Idle" and c.current_task_id is None, c.status)

    # Nothing Pending now -> a second dispatch is a no-op.
    check("dispatch no pending = 0", fleet_engine.dispatch(db, stations=anchors()) == 0, "")
    db.close()


def test_dispatch_unknown_origin_stays_pending():
    db = fresh_db()
    v = models.FleetVehicle(tenant_code=T, name="V", vehicle_type="AMR", status="Idle",
                            x=8.0, y=15.0, battery_pct=90.0)
    task = models.TransportTask(tenant_code=T, origin="Nowhere", destination="Packing",
                                status="Pending", requested_at=datetime.utcnow())
    db.add_all([v, task])
    db.commit()
    made = fleet_engine.dispatch(db, stations=anchors())
    db.refresh(v); db.refresh(task)
    check("unknown origin dispatches nothing", made == 0, made)
    check("unknown-origin task stays Pending", task.status == "Pending", task.status)
    check("vehicle stays Idle", v.status == "Idle" and v.current_task_id is None, v.status)
    db.close()


# ── 3  MOTION ────────────────────────────────────────────────────────────

def test_advance_pickup():
    db = fresh_db()
    # Already at the pickup point (Warehouse): one tick arrives and flips to Carrying.
    task = models.TransportTask(tenant_code=T, origin="Warehouse", destination="Packing",
                                status="Assigned", requested_at=datetime.utcnow())
    db.add(task); db.commit()
    v = models.FleetVehicle(tenant_code=T, name="V", vehicle_type="AMR", status="ToPickup",
                            x=8.0, y=15.0, battery_pct=80.0, current_task_id=task.id)
    db.add(v); db.commit()
    fleet_engine.advance(db, seconds=1.0, stations=anchors(), rng=FixedRng(0.99))
    db.refresh(v); db.refresh(task)
    check("pickup -> InTransit", task.status == "InTransit" and task.picked_up_at is not None, task.status)
    check("vehicle -> Carrying", v.status == "Carrying", v.status)
    db.close()


def test_advance_delivery():
    db = fresh_db()
    # Already at the drop point (Packing) and well-charged: delivers and parks.
    task = models.TransportTask(tenant_code=T, origin="Warehouse", destination="Packing",
                                status="InTransit", requested_at=datetime.utcnow(),
                                picked_up_at=datetime.utcnow())
    db.add(task); db.commit()
    v = models.FleetVehicle(tenant_code=T, name="V", vehicle_type="AMR", status="Carrying",
                            x=92.0, y=30.0, battery_pct=80.0, current_task_id=task.id,
                            deliveries_total=0)
    db.add(v); db.commit()
    fleet_engine.advance(db, seconds=1.0, stations=anchors(), rng=FixedRng(0.99))
    db.refresh(v); db.refresh(task)
    check("delivery -> Delivered", task.status == "Delivered" and task.completed_at is not None, task.status)
    check("counter incremented", v.deliveries_total == 1, v.deliveries_total)
    # 80% battery is above GO_CHARGE_BELOW (20), so it parks Idle, not Charging.
    check("delivered vehicle parks Idle", v.status == "Idle" and v.current_task_id is None, v.status)
    db.close()


# ── 4  CRASH EDGE: NULL battery on a tasked vehicle (#776-class) ──────────

def test_advance_null_battery_tasked_does_not_crash():
    db = fresh_db()
    task = models.TransportTask(tenant_code=T, origin="Warehouse", destination="Packing",
                                status="Assigned", requested_at=datetime.utcnow())
    db.add(task); db.commit()
    # battery_pct is Float, nullable, with a PYTHON-SIDE default of 100.0: passing
    # battery_pct=None through the ORM would just trip that default, so the only way
    # to reproduce the real legacy/migration/raw-SQL NULL is to write it with raw
    # SQL that bypasses the ORM default. Such a tasked vehicle enters the critical-
    # battery branch (0 <= CRITICAL_BATTERY), where formatting the RAW column would
    # raise TypeError on format(None, '.0f') and 500 the whole tenant's advance.
    v = models.FleetVehicle(tenant_code=T, name="V", vehicle_type="AMR", status="ToPickup",
                            x=8.0, y=15.0, battery_pct=50.0, current_task_id=task.id)
    db.add(v); db.commit()
    db.execute(text("UPDATE fleet_vehicles SET battery_pct = NULL WHERE id = :i"), {"i": v.id})
    db.commit()   # expires the instance, so the next read sees the true NULL
    check("fixture really holds NULL battery",
          db.query(models.FleetVehicle).filter(models.FleetVehicle.id == v.id).first().battery_pct is None,
          "battery not NULL")
    crashed = None
    try:
        fleet_engine.advance(db, seconds=1.0, stations=anchors(), rng=FixedRng(0.99))
    except Exception as exc:  # noqa: BLE001 - the whole point is that nothing escapes
        crashed = exc
    check("NULL-battery advance does not raise", crashed is None, repr(crashed))
    if crashed is None:
        db.refresh(v); db.refresh(task)
        check("task released to queue", task.status == "Pending" and task.vehicle_id is None, task.status)
        check("vehicle sent to charge", v.status == "Charging" and v.current_task_id is None, v.status)
        alerts = db.query(models.FleetAlert).filter(models.FleetAlert.kind == "LowBattery").all()
        check("one LowBattery alert raised", len(alerts) == 1, len(alerts))
    db.close()


# ── 5  CHARGING ──────────────────────────────────────────────────────────

def test_advance_charging():
    db = fresh_db()
    # At the dock (8,85), 94.0% -> +0.4%/s * 3 s = 95.2% >= CHARGED_ENOUGH (95),
    # so charging ends and it returns to Idle.
    v = models.FleetVehicle(tenant_code=T, name="V", vehicle_type="AMR", status="Charging",
                            x=8.0, y=85.0, battery_pct=94.0)
    db.add(v); db.commit()
    fleet_engine.advance(db, seconds=3.0, stations=anchors(), rng=FixedRng(0.99))
    db.refresh(v)
    check("charge accrues to 95.2", approx(v.battery_pct, 95.2), v.battery_pct)
    check("charged vehicle -> Idle", v.status == "Idle", v.status)

    # Charging but NOT yet at the dock: it drives toward the dock and gains NO
    # charge on the way (charge accrues only within 1 m of the dock).
    far = models.FleetVehicle(tenant_code=T, name="Far", vehicle_type="AMR", status="Charging",
                              x=50.0, y=50.0, battery_pct=50.0)
    db.add(far); db.commit()
    fleet_engine.advance(db, seconds=1.0, stations=anchors(), rng=FixedRng(0.99))
    db.refresh(far)
    check("no charge while travelling to dock", approx(far.battery_pct, 50.0), far.battery_pct)
    check("moves toward dock", far.x < 50.0, far.x)
    db.close()


def test_advance_idle_drain_to_charge():
    db = fresh_db()
    # Idle, no task, 20.05% -> -0.001%/s * 60 s = 19.99% < GO_CHARGE_BELOW (20):
    # an idle vehicle that dips below the floor sends itself to charge.
    low = models.FleetVehicle(tenant_code=T, name="Low", vehicle_type="AMR", status="Idle",
                              x=50.0, y=50.0, battery_pct=20.05)
    db.add(low); db.commit()
    fleet_engine.advance(db, seconds=60.0, stations=anchors(), rng=FixedRng(0.99))
    db.refresh(low)
    check("idle drain exact", approx(low.battery_pct, 19.99), low.battery_pct)
    check("drained idle vehicle -> Charging", low.status == "Charging", low.status)

    # A comfortably-charged idle vehicle just sips and stays Idle.
    ok = models.FleetVehicle(tenant_code=T, name="Ok", vehicle_type="AMR", status="Idle",
                             x=50.0, y=50.0, battery_pct=50.0)
    db.add(ok); db.commit()
    fleet_engine.advance(db, seconds=10.0, stations=anchors(), rng=FixedRng(0.99))
    db.refresh(ok)
    check("comfortable idle stays Idle", ok.status == "Idle" and approx(ok.battery_pct, 49.99), ok.battery_pct)
    db.close()


# ── 6  ADVISE ────────────────────────────────────────────────────────────

_stuck_seq = [0]


def _stuck_with_task(db, picked_up):
    task = models.TransportTask(tenant_code=T, origin="Warehouse", destination="Packing",
                                status="InTransit" if picked_up else "Assigned",
                                requested_at=datetime.utcnow(),
                                picked_up_at=datetime.utcnow() if picked_up else None)
    db.add(task); db.commit()
    _stuck_seq[0] += 1   # unique name: (tenant_code, name) is UNIQUE on fleet_vehicles
    v = models.FleetVehicle(tenant_code=T, name=f"V{_stuck_seq[0]}", vehicle_type="AMR",
                            status="Stuck", x=50.0, y=50.0, battery_pct=70.0,
                            current_task_id=task.id)
    db.add(v); db.commit()
    alert = models.FleetAlert(tenant_code=T, vehicle_id=v.id, task_id=task.id, kind="Stuck",
                              severity="High", status="Open")
    db.add(alert); db.commit()
    return v, task, alert


def test_advise_resume_returns_and_charge():
    db = fresh_db()
    # resume on a picked-up task -> back to Carrying, alert resolved with the verb.
    v, task, alert = _stuck_with_task(db, picked_up=True)
    fleet_engine.advise(db, alert, "resume")
    db.refresh(v); db.refresh(alert)
    check("resume -> Carrying", v.status == "Carrying", v.status)
    check("alert resolved w/ advice", alert.status == "Resolved" and alert.advice == "resume",
          f"{alert.status}/{alert.advice}")

    # return_task -> task back to the queue, vehicle Idle, claim dropped.
    v, task, alert = _stuck_with_task(db, picked_up=False)
    fleet_engine.advise(db, alert, "return_task")
    db.refresh(v); db.refresh(task)
    check("return_task frees vehicle", v.status == "Idle" and v.current_task_id is None, v.status)
    check("return_task requeues task", task.status == "Pending" and task.vehicle_id is None, task.status)

    # send_to_charge -> task requeued AND vehicle sent to the dock.
    v, task, alert = _stuck_with_task(db, picked_up=False)
    fleet_engine.advise(db, alert, "send_to_charge")
    db.refresh(v); db.refresh(task)
    check("send_to_charge -> Charging", v.status == "Charging" and v.current_task_id is None, v.status)
    check("send_to_charge requeues task", task.status == "Pending", task.status)

    # An unknown verb is refused loudly, not applied silently.
    v, task, alert = _stuck_with_task(db, picked_up=False)
    raised = False
    try:
        fleet_engine.advise(db, alert, "fly")
    except ValueError:
        raised = True
    check("unknown advice raises ValueError", raised, "no ValueError")
    db.close()


# ── 7  KPIS ──────────────────────────────────────────────────────────────

def test_kpis_empty_fleet():
    db = fresh_db()
    k = fleet_engine.kpis(db)
    check("empty fleet size 0", k["fleet_size"] == 0, k["fleet_size"])
    # The two averages divide by a count that is 0 here: the guard must return
    # None, never raise ZeroDivisionError.
    check("empty avg_battery None", k["avg_battery_pct"] is None, k["avg_battery_pct"])
    check("empty avg_wait None", k["avg_wait_s"] is None, k["avg_wait_s"])
    check("empty delivered_24h 0", k["delivered_24h"] == 0, k["delivered_24h"])
    check("empty distance 0", approx(k["distance_total_m"], 0.0), k["distance_total_m"])
    db.close()


def test_kpis_populated():
    db = fresh_db()
    now = datetime(2025, 1, 2, 12, 0, 0)
    # Two vehicles: batteries 80 & 60 -> avg 70.0 ; odometers 100 & 50 -> total 150.0.
    db.add(models.FleetVehicle(tenant_code=T, name="V1", vehicle_type="AMR", status="Idle",
                               x=0, y=0, battery_pct=80.0, odometer_m=100.0))
    db.add(models.FleetVehicle(tenant_code=T, name="V2", vehicle_type="AMR", status="Charging",
                               x=0, y=0, battery_pct=60.0, odometer_m=50.0))
    # One delivered task with full timestamps: wait = assigned - requested = 300 s.
    db.add(models.TransportTask(tenant_code=T, origin="Warehouse", destination="Packing",
                                status="Delivered",
                                requested_at=now - timedelta(seconds=600),
                                assigned_at=now - timedelta(seconds=300),
                                completed_at=now - timedelta(seconds=10)))
    # A second delivered task with NO assigned_at: counts toward delivered_24h but
    # is excluded from the wait average (no basis for a wait).
    db.add(models.TransportTask(tenant_code=T, origin="Packing", destination="Dispatch",
                                status="Delivered", requested_at=now - timedelta(seconds=400),
                                completed_at=now - timedelta(seconds=5)))
    db.add(models.TransportTask(tenant_code=T, origin="Warehouse", destination="Dispatch",
                                status="Pending", requested_at=now))
    db.commit()
    v1 = db.query(models.FleetVehicle).filter(models.FleetVehicle.name == "V1").first()
    db.add(models.FleetAlert(tenant_code=T, vehicle_id=v1.id, kind="Stuck", status="Open"))
    db.commit()

    k = fleet_engine.kpis(db, now=now)
    check("fleet_size 2", k["fleet_size"] == 2, k["fleet_size"])
    check("by_status split", k["by_status"] == {"Idle": 1, "Charging": 1}, k["by_status"])
    check("avg_battery 70.0", approx(k["avg_battery_pct"], 70.0), k["avg_battery_pct"])
    check("distance 150.0", approx(k["distance_total_m"], 150.0), k["distance_total_m"])
    check("delivered_24h counts both", k["delivered_24h"] == 2, k["delivered_24h"])
    # Only the one task with both timestamps contributes: avg wait = 300 / 1 = 300.0.
    check("avg_wait 300.0", approx(k["avg_wait_s"], 300.0), k["avg_wait_s"])
    check("pending_tasks 1", k["pending_tasks"] == 1, k["pending_tasks"])
    check("open_alerts 1", k["open_alerts"] == 1, k["open_alerts"])
    db.close()


def test_kpis_out_of_order_timestamps_excluded_from_wait():
    # A delivered task whose assigned_at PRECEDES its requested_at is an impossible
    # wait (a clock stepped backwards between the request and the assignment, or a
    # legacy / out-of-order row). It must be left out of the wait average exactly
    # as a task with a missing timestamp is — never averaged in as a negative span,
    # which would drag the mission-control avg_wait below zero (ADR-0010: a figure
    # the data cannot support). But it is still a real delivery, so it counts
    # toward delivered_24h.
    db = fresh_db()
    now = datetime(2025, 1, 2, 12, 0, 0)

    # (a) ONLY an out-of-order task: no measurable wait at all, so avg_wait_s is
    #     None — not a negative number, and not a ZeroDivisionError.
    db.add(models.TransportTask(tenant_code=T, origin="Warehouse", destination="Packing",
                                status="Delivered",
                                requested_at=now - timedelta(seconds=100),   # requested LATER
                                assigned_at=now - timedelta(seconds=300),    # "assigned" EARLIER
                                completed_at=now - timedelta(seconds=10)))
    db.commit()
    k = fleet_engine.kpis(db, now=now)
    check("out-of-order-only avg_wait is None", k["avg_wait_s"] is None, k["avg_wait_s"])
    check("out-of-order task still counted delivered", k["delivered_24h"] == 1, k["delivered_24h"])

    # (b) A valid task (wait = 420 - 60 = 360 s) ALONGSIDE the out-of-order one:
    #     the average is the valid task's 360 s alone (360 / 1), never (360 + -200) / 2 = 80.
    db.add(models.TransportTask(tenant_code=T, origin="Packing", destination="Dispatch",
                                status="Delivered",
                                requested_at=now - timedelta(seconds=420),
                                assigned_at=now - timedelta(seconds=60),
                                completed_at=now - timedelta(seconds=5)))
    db.commit()
    k = fleet_engine.kpis(db, now=now)
    check("avg_wait excludes the out-of-order span", approx(k["avg_wait_s"], 360.0), k["avg_wait_s"])
    check("both deliveries counted", k["delivered_24h"] == 2, k["delivered_24h"])
    db.close()


# ── 8  CLOCK ─────────────────────────────────────────────────────────────

def test_tick_default_and_cap():
    db = fresh_db()
    base = datetime(2025, 1, 2, 12, 0, 0)
    # A clock key never seen before defaults to a 3 s step (a fresh dashboard poll).
    fleet_engine._last_tick.pop("FRESHKEY", None)
    applied = fleet_engine.tick(db, tenant="FRESHKEY", now=base, rng=FixedRng(0.99))
    check("first tick defaults to 3 s", approx(applied, 3.0), applied)

    # A long absence (10,000 s) must be capped at MAX_TICK_SECONDS, not replayed
    # as one giant leap.
    fleet_engine._last_tick["CAPKEY"] = base - timedelta(seconds=10000)
    applied = fleet_engine.tick(db, tenant="CAPKEY", now=base, rng=FixedRng(0.99))
    check("long absence capped", approx(applied, fleet_engine.MAX_TICK_SECONDS), applied)

    # An explicit non-positive step is a no-op that advances nothing.
    applied = fleet_engine.tick(db, tenant="CAPKEY", now=base, seconds=0.0, rng=FixedRng(0.99))
    check("zero tick applies nothing", approx(applied, 0.0), applied)
    db.close()


ALL = [
    test_dist_and_move_toward,
    test_stations_for_grid,
    test_dispatch_nearest_and_battery_gate,
    test_dispatch_unknown_origin_stays_pending,
    test_advance_pickup,
    test_advance_delivery,
    test_advance_null_battery_tasked_does_not_crash,
    test_advance_charging,
    test_advance_idle_drain_to_charge,
    test_advise_resume_returns_and_charge,
    test_kpis_empty_fleet,
    test_kpis_populated,
    test_kpis_out_of_order_timestamps_excluded_from_wait,
    test_tick_default_and_cap,
]


if __name__ == "__main__":
    for t in ALL:
        print(f"\n{t.__name__}")
        t()
    print()
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        raise SystemExit(1)
    print("ALL FLEET-ENGINE TESTS PASSED")
