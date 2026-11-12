"""Fleet Ops engine: stations, dispatch, movement, assistance (ADR pending).

WHAT THIS IS. The pure logic behind /fleet: where things are on the floor
(stations_for), which vehicle takes which movement order (dispatch), how the
world advances between two observations (tick/advance), and how a human
answers a vehicle's plea for help (advise). fleet_routes.py is a thin HTTP
skin over these functions; every behaviour worth testing lives here, callable
with a plain Session and no server.

THE SIMULATOR IS NOT A DEMO TOY — IT IS THE ADAPTER CONTRACT. A real AMR
vendor integration (MiR REST, VDA 5050) will do exactly what advance() does:
write x, y, battery_pct, status onto FleetVehicle rows and flip TransportTask
through Pending → Assigned → InTransit → Delivered. The dashboard reads rows,
never the mover, so swapping simulated movers for real ones changes no reads.

TIME IS ADVANCED LAZILY, ON READ. There is no background task: each
GET /fleet/state ticks the world by the real seconds elapsed since the last
tick for that tenant (capped, so a quiet weekend does not replay as one giant
leap). A dashboard polling every 3 s gets smooth motion; a tenant nobody
watches costs nothing; and a test passes `seconds=` explicitly and owns the
clock. The per-tenant clock is process-local by design — it is a *pacing*
device, not state; the state is all on the rows.

COORDINATES: metres on a 100 x 100 grid, origin top-left (matches the SVG the
dashboard draws, where y grows downward). Indoors GPS does not exist; every
indoor positioning system reports a local plant coordinate, so that is the
contract.
"""
import math
import random
from datetime import datetime, timedelta

import models

# ── Tunables ────────────────────────────────────────────────────────────
# Metres/second by vehicle type — published cruise speeds of the class
# (an MiR250-class AMR does ~2 m/s rated, ~1.4 loaded; AGV tape-followers
# crawl; a manned-speed forklift is capped indoors).
SPEED_MPS = {"AMR": 1.4, "AGV": 1.0, "Tug": 1.6, "Forklift": 1.8}
DRAIN_PCT_PER_M = 0.02        # ~1% per 50 m moved
IDLE_DRAIN_PCT_PER_S = 0.001  # parked electronics still sip
CHARGE_PCT_PER_S = 0.4        # ~4 min for a 0→95 charge: demo-compressed
DISPATCH_MIN_BATTERY = 25.0   # below this a vehicle is not given new work
CRITICAL_BATTERY = 10.0       # below this a tasked vehicle aborts to charge
CHARGED_ENOUGH = 95.0         # charging ends here, not at a flappy 100
GO_CHARGE_BELOW = 20.0        # an idle vehicle heads to the dock below this
STUCK_P_PER_S = 0.0008        # per moving vehicle; ~1 event / 20 min moving
MAX_TICK_SECONDS = 90.0       # lazy-tick cap: absence must not replay as a leap
MAX_OPEN_MES_TASKS = 8        # the auto-generator respects a bounded queue
MES_TASK_P_PER_MACHINE_S = 0.004   # ~one movement per running machine / ~4 min

# Fixed logistics anchors every plant has, whatever it makes. Machines are
# added live from the tenant's own roster (stations_for).
ANCHORS = {
    "Warehouse":   {"x": 8.0,  "y": 15.0, "kind": "anchor"},
    "Packing":     {"x": 92.0, "y": 30.0, "kind": "anchor"},
    "Dispatch":    {"x": 92.0, "y": 70.0, "kind": "anchor"},
    "Charge Dock": {"x": 8.0,  "y": 85.0, "kind": "anchor"},
}

VALID_ADVICE = ("resume", "return_task", "send_to_charge")

_rng = random.Random()
_last_tick: dict = {}   # tenant (or "" when unbound) -> datetime of last tick


def stations_for(db):
    """{name: {x, y, kind}} — the floor plan, derived live from the roster.

    The four anchors are fixed; every machine the tenant actually has is
    placed on a deterministic grid in the central production zone (x 25–75,
    y 18–82), ordered by id so a machine keeps its spot as others come and
    go after it. No stations table exists to drift out of date: the map IS
    the roster.
    """
    stations = {k: dict(v) for k, v in ANCHORS.items()}
    machines = db.query(models.Machine).order_by(models.Machine.id.asc()).all()
    n = len(machines)
    if n == 0:
        return stations
    cols = max(1, math.ceil(math.sqrt(n)))
    rows = math.ceil(n / cols)
    for i, m in enumerate(machines):
        r, c = divmod(i, cols)
        x = 25.0 + (50.0 * (c + 0.5) / cols)
        y = 18.0 + (64.0 * (r + 0.5) / rows)
        stations[m.name] = {"x": round(x, 1), "y": round(y, 1),
                            "kind": "machine", "machine_id": m.id,
                            "machine_status": m.status}
    return stations


def _dist(ax, ay, bx, by):
    return math.hypot(bx - ax, by - ay)


def seed_demo_fleet(db):
    """A six-vehicle starter fleet, once. Idempotent: any existing vehicle
    means a fleet is already being managed and nothing is touched."""
    if db.query(models.FleetVehicle).first() is not None:
        return 0
    dock = ANCHORS["Charge Dock"]
    spec = [("AMR-01", "AMR", 96.0), ("AMR-02", "AMR", 82.0),
            ("AMR-03", "AMR", 68.0), ("AGV-01", "AGV", 90.0),
            ("TUG-01", "Tug", 74.0), ("FORK-01", "Forklift", 88.0)]
    for i, (name, vtype, battery) in enumerate(spec):
        db.add(models.FleetVehicle(
            name=name, vehicle_type=vtype, status="Idle",
            x=dock["x"] + 3.0 + (i % 3) * 4.0, y=dock["y"] - (i // 3) * 5.0,
            battery_pct=battery))
    db.commit()
    return len(spec)


def generate_mes_tasks(db, seconds, now=None, rng=None):
    """The idea-1 moment: the plant's own state raises movement orders.

    Each Running machine finishes material at some rate; each finish is a
    "carry the batch to Packing" order, with an occasional "bring raw
    material from the Warehouse" replenishment. Bounded: the generator never
    grows the open auto-queue past MAX_OPEN_MES_TASKS, because an unbounded
    backlog says nothing a bounded one doesn't and buries the manual queue.
    """
    rng = rng or _rng
    now = now or datetime.utcnow()
    open_mes = (db.query(models.TransportTask)
                .filter(models.TransportTask.source == "mes",
                        models.TransportTask.status.in_(("Pending", "Assigned", "InTransit")))
                .count())
    budget = MAX_OPEN_MES_TASKS - open_mes
    if budget <= 0:
        return 0
    running = (db.query(models.Machine)
               .filter(models.Machine.status == "Running")
               .order_by(models.Machine.id.asc()).all())
    created = 0
    p = min(0.95, MES_TASK_P_PER_MACHINE_S * seconds)
    for m in running:
        if created >= budget:
            break
        if rng.random() >= p:
            continue
        if rng.random() < 0.7:
            task = models.TransportTask(
                origin=m.name, destination="Packing",
                payload=f"Finished batch — {m.name}", source="mes",
                priority=2, requested_at=now)
        else:
            task = models.TransportTask(
                origin="Warehouse", destination=m.name,
                payload=f"Raw material — {m.name}", source="mes",
                priority=2, requested_at=now)
        db.add(task)
        created += 1
    if created:
        db.commit()
    return created


def dispatch(db, stations=None, now=None):
    """Assign Pending tasks to Idle vehicles: most urgent first, nearest
    capable vehicle wins. Returns the number of assignments made.

    "Capable" today means Idle with battery above DISPATCH_MIN_BATTERY;
    nearest is straight-line to the task's origin. Deliberately greedy —
    optimal assignment (Hungarian et al.) is a later luxury; nearest-idle is
    what half the shipping fleet managers actually run.
    """
    stations = stations or stations_for(db)
    now = now or datetime.utcnow()
    pending = (db.query(models.TransportTask)
               .filter(models.TransportTask.status == "Pending")
               .order_by(models.TransportTask.priority.desc(),
                         models.TransportTask.requested_at.asc(),
                         models.TransportTask.id.asc())
               .all())
    if not pending:
        return 0
    idle = [v for v in db.query(models.FleetVehicle)
            .filter(models.FleetVehicle.status == "Idle").all()
            if (v.battery_pct or 0) >= DISPATCH_MIN_BATTERY]
    assigned = 0
    for task in pending:
        origin = stations.get(task.origin)
        if origin is None or not idle:
            continue   # unknown station: leave Pending for a human to see
        idle.sort(key=lambda v: _dist(v.x or 0, v.y or 0, origin["x"], origin["y"]))
        vehicle = idle.pop(0)
        task.status = "Assigned"
        task.vehicle_id = vehicle.id
        task.assigned_at = now
        vehicle.status = "ToPickup"
        vehicle.current_task_id = task.id
        assigned += 1
    if assigned:
        db.commit()
    return assigned


def _release_task(task, now):
    """Put a task back in the queue as if never assigned. The payload is
    still wherever it was; only the claim on a vehicle is undone. The VDA
    5050 bookkeeping is cleared with it: a released task's next assignment
    is a NEW order (a vehicle rejects a reused orderId at update 0)."""
    task.status = "Pending"
    task.vehicle_id = None
    task.assigned_at = None
    task.picked_up_at = None
    task.vda_order_id = None
    task.vda_order_update_id = 0


def advance(db, seconds, stations=None, now=None, rng=None):
    """Move the world `seconds` forward: travel, pickups, deliveries,
    battery, charging, and the misfortunes that raise fleet_alerts."""
    rng = rng or _rng
    now = now or datetime.utcnow()
    stations = stations or stations_for(db)
    vehicles = db.query(models.FleetVehicle).order_by(models.FleetVehicle.id.asc()).all()
    tasks = {t.id: t for t in db.query(models.TransportTask)
             .filter(models.TransportTask.status.in_(("Assigned", "InTransit"))).all()}

    for v in vehicles:
        if (v.adapter or "sim") != "sim":
            # An externally-adapted vehicle (VDA 5050) is moved by its own
            # reports, never by the simulator: fleet_vda5050.handle_state owns
            # its position, battery, status and task flips. Simulating it too
            # would have two writers racing over one row.
            continue

        if v.status == "Offline":
            continue

        if v.status == "Stuck":
            # Waiting for a human; electronics keep sipping.
            v.battery_pct = max(0.0, (v.battery_pct or 0) - IDLE_DRAIN_PCT_PER_S * seconds)
            continue

        if v.status == "Charging":
            dock = stations["Charge Dock"]
            d = _dist(v.x or 0, v.y or 0, dock["x"], dock["y"])
            if d > 1.0:
                # "Charging" covers the ride to the dock too — the vocabulary
                # stays at six states and the dashboard shows amber either way.
                _move_toward(v, dock, seconds)
            else:
                v.battery_pct = min(100.0, (v.battery_pct or 0) + CHARGE_PCT_PER_S * seconds)
                if v.battery_pct >= CHARGED_ENOUGH:
                    v.status = "Idle"
            continue

        task = tasks.get(v.current_task_id) if v.current_task_id else None

        if v.status == "Idle" or task is None:
            if v.current_task_id and task is None:
                v.current_task_id = None   # task was cancelled under it
                v.status = "Idle"
            v.battery_pct = max(0.0, (v.battery_pct or 0) - IDLE_DRAIN_PCT_PER_S * seconds)
            if v.battery_pct < GO_CHARGE_BELOW:
                v.status = "Charging"
            continue

        # Tasked and moving. Critical battery aborts first: the task goes
        # back to the queue, the vehicle pleads and limps to the dock.
        if (v.battery_pct or 0) <= CRITICAL_BATTERY:
            db.add(models.FleetAlert(
                vehicle_id=v.id, task_id=task.id, kind="LowBattery",
                severity="Critical",
                message=f"{v.name}: battery {v.battery_pct:.0f}% with task #{task.id} "
                        f"({task.origin} → {task.destination}); task returned to queue."))
            _release_task(task, now)
            v.current_task_id = None
            v.status = "Charging"
            continue

        target_name = task.origin if v.status == "ToPickup" else task.destination
        target = stations.get(target_name)
        if target is None:
            # A station that no longer exists (machine deleted mid-task).
            # Nothing can complete this order: back to the queue it goes,
            # where dispatch will skip it and a person sees it sitting.
            _release_task(task, now)
            v.current_task_id = None
            v.status = "Idle"
            continue

        moved = _move_toward(v, target, seconds)
        v.battery_pct = max(0.0, (v.battery_pct or 0) - DRAIN_PCT_PER_M * moved)
        v.odometer_m = (v.odometer_m or 0) + moved

        arrived = _dist(v.x or 0, v.y or 0, target["x"], target["y"]) <= 0.5
        if arrived and v.status == "ToPickup":
            task.status = "InTransit"
            task.picked_up_at = now
            v.status = "Carrying"
        elif arrived and v.status == "Carrying":
            task.status = "Delivered"
            task.completed_at = now
            v.deliveries_total = (v.deliveries_total or 0) + 1
            v.current_task_id = None
            v.status = "Charging" if v.battery_pct < GO_CHARGE_BELOW else "Idle"
        elif moved > 0 and rng.random() < STUCK_P_PER_S * seconds:
            v.status = "Stuck"
            db.add(models.FleetAlert(
                vehicle_id=v.id, task_id=task.id, kind="Stuck", severity="High",
                message=f"{v.name} stopped at ({v.x:.0f}, {v.y:.0f}) en route to "
                        f"{target_name} — path blocked; needs advice."))

    db.commit()


def _move_toward(v, target, seconds):
    """Straight-line motion at the type's speed. Returns metres moved."""
    speed = SPEED_MPS.get(v.vehicle_type, 1.2)
    step = speed * seconds
    d = _dist(v.x or 0, v.y or 0, target["x"], target["y"])
    if d <= 1e-9:
        return 0.0
    if step >= d:
        v.x, v.y = target["x"], target["y"]
        return d
    f = step / d
    v.x = (v.x or 0) + (target["x"] - (v.x or 0)) * f
    v.y = (v.y or 0) + (target["y"] - (v.y or 0)) * f
    return step


def tick(db, tenant=None, now=None, seconds=None, rng=None):
    """One lazy world-step: elapsed real time (capped), then generate →
    dispatch → advance. Returns the seconds actually applied."""
    now = now or datetime.utcnow()
    key = tenant or ""
    if seconds is None:
        last = _last_tick.get(key)
        seconds = (now - last).total_seconds() if last else 3.0
        seconds = max(0.0, min(MAX_TICK_SECONDS, seconds))
    _last_tick[key] = now
    if seconds <= 0:
        return 0.0
    stations = stations_for(db)
    generate_mes_tasks(db, seconds, now=now, rng=rng)
    dispatch(db, stations=stations, now=now)
    advance(db, seconds, stations=stations, now=now, rng=rng)
    return seconds


def advise(db, alert, advice, now=None):
    """A human answers a plea. Closed vocabulary, applied here so the row
    records exactly what was told to whom and what it did:

      resume          back to work (the usual answer to Stuck: path cleared)
      return_task     give the task back to the queue; vehicle goes Idle
      send_to_charge  give the task back AND send the vehicle to the dock
    """
    now = now or datetime.utcnow()
    if advice not in VALID_ADVICE:
        raise ValueError(f"advice must be one of {', '.join(VALID_ADVICE)}")
    vehicle = db.query(models.FleetVehicle).filter(
        models.FleetVehicle.id == alert.vehicle_id).first()
    task = None
    if alert.task_id:
        task = db.query(models.TransportTask).filter(
            models.TransportTask.id == alert.task_id,
            models.TransportTask.status.in_(("Assigned", "InTransit"))).first()

    if vehicle is not None:
        if advice == "resume":
            if task is not None and vehicle.current_task_id == task.id:
                vehicle.status = "Carrying" if task.picked_up_at else "ToPickup"
            else:
                vehicle.status = "Idle"
        elif advice == "return_task":
            if task is not None:
                _release_task(task, now)
            vehicle.current_task_id = None
            vehicle.status = "Idle"
        elif advice == "send_to_charge":
            if task is not None:
                _release_task(task, now)
            vehicle.current_task_id = None
            vehicle.status = "Charging"

    alert.status = "Resolved"
    alert.advice = advice
    alert.resolved_at = now
    db.commit()


def kpis(db, now=None):
    """The mission-control strip. 24-hour windows, deliberately: "today"
    needs a timezone and the plant's day is IST while the rows are UTC —
    a rolling day answers the same question with no boundary to argue."""
    now = now or datetime.utcnow()
    day_ago = now - timedelta(hours=24)
    vehicles = db.query(models.FleetVehicle).all()
    by_status: dict = {}
    for v in vehicles:
        by_status[v.status] = by_status.get(v.status, 0) + 1
    delivered = (db.query(models.TransportTask)
                 .filter(models.TransportTask.status == "Delivered",
                         models.TransportTask.completed_at >= day_ago).all())
    waits = [(t.assigned_at - t.requested_at).total_seconds()
             for t in delivered if t.assigned_at and t.requested_at]
    return {
        "fleet_size": len(vehicles),
        "by_status": by_status,
        "pending_tasks": db.query(models.TransportTask)
            .filter(models.TransportTask.status == "Pending").count(),
        "active_tasks": db.query(models.TransportTask)
            .filter(models.TransportTask.status.in_(("Assigned", "InTransit"))).count(),
        "delivered_24h": len(delivered),
        "avg_wait_s": round(sum(waits) / len(waits), 1) if waits else None,
        "open_alerts": db.query(models.FleetAlert)
            .filter(models.FleetAlert.status == "Open").count(),
        "avg_battery_pct": round(
            sum((v.battery_pct or 0) for v in vehicles) / len(vehicles), 1)
            if vehicles else None,
        "distance_total_m": round(sum((v.odometer_m or 0) for v in vehicles), 1),
    }
