"""The VDA 5050 adapter (fleet_vda5050) against a spec-faithful vehicle.

What is pinned here, in order:
  * topic parsing — only our interface/version/shape resolves; everything
    else is None (a parser that trusts its subscription breaks later);
  * fail-closed identity — an unregistered manufacturer/serial resolves to
    no row (the mqtt_service lesson: never auto-create an owner);
  * registration — adapter and identity validation on POST /fleet/vehicles,
    including the duplicate-identity 409;
  * the order round trip WITHOUT A BROKER — dispatch assigns, the heartbeat
    publishes exactly one order, the simulated vehicle (vda5050_vehicle_sim's
    pure core) drives it, and its state reports flip the task Assigned ->
    InTransit -> Delivered with position, odometer and delivery counters
    moving on the row;
  * the assistance loop on the wire — a FATAL error raises ONE plea and
    parks the row Stuck; resume re-publishes the SAME orderId with a higher
    orderUpdateId (which the vehicle accepts; update 0 again would be
    rejected as stale — also pinned); return_task cancels on the wire and
    requeues with the bookkeeping cleared;
  * separation of movers — fleet_engine.advance() never touches a vda5050
    row, and a connection drop/restore round-trips Offline correctly.

Run:  python backend/test_fleet_vda5050.py     (exit 0 = pass)
Also collectable by pytest (nothing binds a tenant at module level).
"""
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import fleet_engine
import fleet_routes
import fleet_vda5050
import models
from database import Base
from vda5050_vehicle_sim import Vehicle

ADMIN = {"tenant": "DEFAULT", "role": "Admin", "sub": "founder"}


def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _machine(db, name, status="Running", tenant="DEFAULT"):
    m = models.Machine(tenant_code=tenant, name=name, status=status, downtime="0 min")
    db.add(m)
    db.commit()
    return m


def _register(db, name="CARTER-01", manufacturer="RobotCompany", serial="carter-01"):
    body = fleet_routes.VehicleCreate(
        name=name, vehicle_type="AMR", adapter="vda5050",
        vda_manufacturer=manufacturer, vda_serial=serial)
    out = fleet_routes.create_vehicle(body, db=db, current_user=ADMIN)
    return db.query(models.FleetVehicle).filter(
        models.FleetVehicle.id == out["id"]).one()


def _task(db, origin="Warehouse", destination="CNC-01", priority=2):
    t = models.TransportTask(origin=origin, destination=destination,
                             payload="test crate", priority=priority)
    db.add(t)
    db.commit()
    return t


class Wire:
    """A captured publish() — the only I/O the handlers are allowed."""
    def __init__(self):
        self.messages = []

    def __call__(self, topic, body):
        self.messages.append((topic, body))

    def last(self, channel):
        for topic, body in reversed(self.messages):
            if topic.endswith("/" + channel):
                return topic, body
        return None, None

    def count(self, channel):
        return sum(1 for t, _ in self.messages if t.endswith("/" + channel))


def test_topic_parsing():
    assert fleet_vda5050.parse_topic("uagv/v2/RobotCompany/carter-01/state") == \
        ("RobotCompany", "carter-01", "state")
    assert fleet_vda5050.parse_topic("uagv/v2/RobotCompany/carter-01/connection") == \
        ("RobotCompany", "carter-01", "connection")
    for bad in ("flowmes/DEFAULT/-/machines",            # another subsystem's scheme
                "uagv/v1/RobotCompany/carter-01/state",  # wrong major version
                "uagv/v2/RobotCompany/state",            # too few segments
                "uagv/v2/RobotCompany/carter-01/state/x",  # too many
                "uagv/v2//carter-01/state",              # empty manufacturer
                "", None):
        assert fleet_vda5050.parse_topic(bad) is None, bad
    print("PASS only our topic shape parses; everything else is None")


def test_unregistered_identity_is_nobody():
    db = _db()
    _register(db)                      # somebody IS registered…
    assert fleet_vda5050.vehicle_for(db, "RobotCompany", "carter-99") is None
    assert fleet_vda5050.vehicle_for(db, "Mallory", "carter-01") is None
    found = fleet_vda5050.vehicle_for(db, "RobotCompany", "carter-01")
    assert found is not None and found.adapter == "vda5050"
    print("PASS an unregistered identity resolves to no row; the registered one resolves")


def test_registration_validation():
    db = _db()
    try:
        fleet_routes.create_vehicle(
            fleet_routes.VehicleCreate(name="X", adapter="teleport"),
            db=db, current_user=ADMIN)
        raise AssertionError("unknown adapter was accepted")
    except HTTPException as e:
        assert e.status_code == 400 and "adapter" in e.detail
    try:
        fleet_routes.create_vehicle(
            fleet_routes.VehicleCreate(name="X", adapter="vda5050",
                                       vda_manufacturer="RobotCompany"),
            db=db, current_user=ADMIN)
        raise AssertionError("vda5050 without a serial was accepted")
    except HTTPException as e:
        assert e.status_code == 400
    try:
        fleet_routes.create_vehicle(
            fleet_routes.VehicleCreate(name="X", adapter="vda5050",
                                       vda_manufacturer="Robot/Company",
                                       vda_serial="s-1"),
            db=db, current_user=ADMIN)
        raise AssertionError("a '/' in the manufacturer was accepted")
    except HTTPException as e:
        assert e.status_code == 400

    v = _register(db)
    assert v.vda_identity == "RobotCompany/carter-01"
    try:
        _register(db, name="CARTER-01-AGAIN")   # same identity, new name
        raise AssertionError("a duplicate VDA identity was accepted")
    except HTTPException as e:
        assert e.status_code == 409
    print("PASS registration validates adapter and identity; duplicates are 409")


def test_order_round_trip_without_a_broker():
    db = _db()
    _machine(db, "CNC-01")
    vehicle = _register(db)
    vehicle.x, vehicle.y = 8.0, 85.0
    task = _task(db)
    wire = Wire()

    sim = Vehicle("RobotCompany", "carter-01", x=8.0, y=85.0, speed=2.0)

    # Heartbeat 1: idle vehicle, pending work -> dispatch assigns, order goes out.
    fleet_vda5050.handle_state(db, vehicle, sim.state_message(), wire)
    db.refresh(task)
    assert task.status == "Assigned" and task.vehicle_id == vehicle.id
    assert task.vda_order_id and task.vda_order_id.startswith(f"amp-task-{task.id}-")
    topic, order = wire.last("order")
    assert topic == "uagv/v2/RobotCompany/carter-01/order"
    assert order["orderId"] == task.vda_order_id and order["orderUpdateId"] == 0
    assert [n["sequenceId"] for n in order["nodes"]] == [0, 2, 4]
    assert all(n["released"] for n in order["nodes"])
    assert vehicle.status == "ToPickup"

    # Heartbeat 2, same world: NOT a second order (publish-once is the contract).
    fleet_vda5050.handle_state(db, vehicle, sim.state_message(), wire)
    assert wire.count("order") == 1

    # The vehicle accepts and drives to the pickup node.
    assert sim.accept_order(order)
    for _ in range(600):
        sim.step(1.0)
        if sim.last_node_seq >= fleet_vda5050.SEQ_PICKUP:
            break
    fleet_vda5050.handle_state(db, vehicle, sim.state_message(), wire)
    db.refresh(task)
    assert task.status == "InTransit" and task.picked_up_at is not None
    assert vehicle.status == "Carrying"
    stations = fleet_engine.stations_for(db)
    assert abs((vehicle.x or 0) - stations["Warehouse"]["x"]) < 2.0

    # …then to the dropoff, and the delivery lands on the row.
    before_km = vehicle.odometer_m or 0
    for _ in range(600):
        sim.step(1.0)
        if not sim.driving and not sim.nodes:
            break
    fleet_vda5050.handle_state(db, vehicle, sim.state_message(), wire)
    db.refresh(task)
    assert task.status == "Delivered" and task.completed_at is not None
    assert task.vda_order_id is None
    assert vehicle.status == "Idle" and vehicle.current_task_id is None
    assert (vehicle.deliveries_total or 0) == 1
    assert (vehicle.odometer_m or 0) > before_km
    assert abs((vehicle.x or 0) - stations["CNC-01"]["x"]) < 1.0
    print("PASS the full order round trip: dispatch -> order -> drive -> "
          "InTransit -> Delivered, all on the rows")


def test_fatal_error_is_one_plea_and_resume_republishes():
    db = _db()
    _machine(db, "CNC-01")
    vehicle = _register(db)
    task = _task(db)
    wire = Wire()
    sim = Vehicle("RobotCompany", "carter-01", x=8.0, y=85.0, speed=2.0)

    fleet_vda5050.handle_state(db, vehicle, sim.state_message(), wire)
    _, order = wire.last("order")
    sim.accept_order(order)
    sim.step(5.0)
    sim.inject_fatal()

    fleet_vda5050.handle_state(db, vehicle, sim.state_message(), wire)
    assert vehicle.status == "Stuck"
    pleas = db.query(models.FleetAlert).filter(
        models.FleetAlert.kind == "Stuck", models.FleetAlert.status == "Open").all()
    assert len(pleas) == 1 and "navigationBlocked" in pleas[0].message

    # The same error on the next heartbeat does NOT file a second plea,
    # and an error that clears does not un-stick the row while the plea is
    # open — the human owns the verb.
    fleet_vda5050.handle_state(db, vehicle, sim.state_message(), wire)
    sim.clear_errors()
    fleet_vda5050.handle_state(db, vehicle, sim.state_message(), wire)
    assert vehicle.status == "Stuck"
    assert db.query(models.FleetAlert).filter(
        models.FleetAlert.status == "Open").count() == 1

    # resume: row side via the engine, wire side via on_advice — the SAME
    # orderId, a HIGHER orderUpdateId, which the vehicle accepts…
    fleet_engine.advise(db, pleas[0], "resume")
    fleet_vda5050.on_advice(db, vehicle, "resume", wire)
    db.refresh(task)
    _, republished = wire.last("order")
    assert republished["orderId"] == task.vda_order_id
    assert republished["orderUpdateId"] == 1 == task.vda_order_update_id
    assert sim.accept_order(republished)
    # …while replaying the ORIGINAL update would be rejected as stale (the
    # reason every new assignment mints a fresh orderId).
    assert not sim.accept_order(order)
    assert vehicle.status in ("ToPickup", "Carrying")
    print("PASS a fatal error is one plea; resume re-publishes update+1 and "
          "a stale update is rejected")


def test_return_task_cancels_on_the_wire():
    db = _db()
    _machine(db, "CNC-01")
    vehicle = _register(db)
    task = _task(db)
    wire = Wire()
    sim = Vehicle("RobotCompany", "carter-01", x=8.0, y=85.0, speed=2.0)

    fleet_vda5050.handle_state(db, vehicle, sim.state_message(), wire)
    _, order = wire.last("order")
    sim.accept_order(order)
    sim.inject_fatal()
    fleet_vda5050.handle_state(db, vehicle, sim.state_message(), wire)
    plea = db.query(models.FleetAlert).filter(models.FleetAlert.status == "Open").one()

    fleet_engine.advise(db, plea, "return_task")
    fleet_vda5050.on_advice(db, vehicle, "return_task", wire)
    db.refresh(task)
    assert task.status == "Pending" and task.vehicle_id is None
    assert task.vda_order_id is None and (task.vda_order_update_id or 0) == 0
    topic, action = wire.last("instantActions")
    assert topic == "uagv/v2/RobotCompany/carter-01/instantActions"
    assert action["actions"][0]["actionType"] == "cancelOrder"
    sim.accept_instant_actions(action)
    assert not sim.nodes and not sim.driving
    print("PASS return_task requeues with cleared bookkeeping and cancels on the wire")


def test_engine_never_moves_a_vda_vehicle():
    db = _db()
    _machine(db, "CNC-01", status="Idle")   # Idle: no MES task generation
    vehicle = _register(db)
    task = _task(db)
    wire = Wire()
    sim = Vehicle("RobotCompany", "carter-01", x=40.0, y=40.0, battery=80.0)
    fleet_vda5050.handle_state(db, vehicle, sim.state_message(), wire)
    db.refresh(task)
    assert task.status == "Assigned"
    assert (vehicle.x, vehicle.y) == (40.0, 40.0)   # the report is the row

    fleet_engine.advance(db, seconds=600.0)
    db.refresh(vehicle)
    db.refresh(task)
    assert (vehicle.x, vehicle.y) == (40.0, 40.0)
    assert vehicle.battery_pct == 80.0
    assert task.status == "Assigned"        # no simulated pickup either
    print("PASS advance() leaves a vda5050 vehicle entirely alone")


def test_connection_round_trip():
    db = _db()
    _machine(db, "CNC-01")
    vehicle = _register(db)
    task = _task(db)
    wire = Wire()
    sim = Vehicle("RobotCompany", "carter-01", x=8.0, y=85.0, speed=2.0)
    fleet_vda5050.handle_state(db, vehicle, sim.state_message(), wire)

    fleet_vda5050.handle_connection(db, vehicle, {"connectionState": "CONNECTIONBROKEN"})
    assert vehicle.status == "Offline"
    db.refresh(task)
    assert task.status == "Assigned"        # the task is kept, deliberately

    fleet_vda5050.handle_connection(db, vehicle, {"connectionState": "ONLINE"})
    assert vehicle.status == "ToPickup"     # restored to the task-appropriate state
    print("PASS a connection drop parks the row Offline and keeps the task; "
          "ONLINE restores it")


def test_state_facts_are_clamped():
    db = _db()
    vehicle = _register(db)
    wire = Wire()
    state = Vehicle("RobotCompany", "carter-01").state_message()
    state["batteryState"]["batteryCharge"] = 150.0
    state["agvPosition"]["x"] = 12.5
    state["agvPosition"]["y"] = 20.0
    fleet_vda5050.handle_state(db, vehicle, state, wire)
    assert vehicle.battery_pct == 100.0
    assert (vehicle.x, vehicle.y) == (12.5, 20.0)
    state["batteryState"]["batteryCharge"] = "--"
    fleet_vda5050.handle_state(db, vehicle, state, wire)
    assert vehicle.battery_pct == 100.0     # garbage keeps the last good value
    print("PASS battery is clamped to [0, 100] and garbage readings are ignored")


if __name__ == "__main__":
    test_topic_parsing()
    test_unregistered_identity_is_nobody()
    test_registration_validation()
    test_order_round_trip_without_a_broker()
    test_fatal_error_is_one_plea_and_resume_republishes()
    test_return_task_cancels_on_the_wire()
    test_engine_never_moves_a_vda_vehicle()
    test_connection_round_trip()
    test_state_facts_are_clamped()
    print("ALL PASS")
