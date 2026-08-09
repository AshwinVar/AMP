"""MQTT ingest is correctly tenanted WITHOUT the ADR-0002 ORM hooks installed.

WHY THIS FILE EXISTS SEPARATELY FROM test_mqtt_tenant_identity.py
-----------------------------------------------------------------
`tenancy.install_scoping()` registers process-wide SQLAlchemy listeners and
cannot be un-registered, so one process cannot test both environments. The other
file installs them (it needs them for the OEE read-model section, which reads the
way an HTTP request does). This file deliberately does NOT.

That is not a hypothetical environment. `mqtt_listener.py` is a documented
standalone entry point — `python mqtt_listener.py` — that wires
`mqtt_service.on_message` to a paho client and loops. It imports paho and
mqtt_service, and nothing in that chain calls `install_scoping()`. `main.py`
does, at line 235; the standalone runner does not.

WHAT THIS CATCHES THAT THE OTHER FILE CANNOT
--------------------------------------------
With the hooks installed, two of them quietly do ingest's job for it:

  * `do_orm_execute` adds `tenant_code = :tenant` to every SELECT of a scoped
    model, so `get_or_create_machine`'s explicit tenant filter is redundant.
  * `before_flush` stamps `tenant_code` on any new scoped object that has none,
    so the explicit `tenant_code=machine.tenant_code` on each child row is
    redundant too.

Measured: with the hooks installed, deleting the tenant filter from
`get_or_create_machine` and deleting the tenant stamp from all three child-row
writes were mutations that SURVIVED the entire suite. Both are one-line changes
that reintroduce the original cross-tenant bug on the standalone path.

The explicit controls are therefore the primary boundary and the hooks are the
backstop, not the other way round. This file is what makes that statement
testable rather than a claim in a comment.

Run: DATABASE_URL="sqlite:///./ci.db" python test_mqtt_ingest_without_orm_hooks.py
"""
import io
import json
import sys
from contextlib import redirect_stdout

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models
import mqtt_service
import tenancy
from database import Base

# Deliberately absent: tenancy.install_scoping(). See the module docstring.
# The PRECONDITION check below proves it is absent rather than trusting it.

failures = []


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"  [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


class _Msg:
    def __init__(self, topic, payload):
        self.topic = topic
        self.payload = json.dumps(payload).encode()


def setup():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    mqtt_service.SessionLocal = sessionmaker(bind=engine)
    mqtt_service.safe_broadcast = lambda event: None
    db = mqtt_service.SessionLocal()
    for t in ("FACTORY_A", "FACTORY_B"):
        db.add(models.TenantConfig(tenant_code=t))
    # Both factories run a machine called CNC-01. This is the whole point.
    for t in ("FACTORY_A", "FACTORY_B"):
        db.add(models.Machine(tenant_code=t, site="", name="CNC-01",
                              status="Running", utilization=50, downtime="0 min"))
    db.commit()
    return db


def send(tenant, **fields):
    payload = {"machine": "CNC-01", "status": "Running", "utilization": 50,
               "downtime": "0 min"}
    payload.update(fields)
    with redirect_stdout(io.StringIO()):
        mqtt_service.on_message(None, None,
                                _Msg(f"flowmes/{tenant}/-/machines", payload))


def test_ingest_is_tenanted_without_the_orm_hooks():
    """Kept as a function, not module-level statements, so pytest's
    single-process collection can import this file without executing it — the
    convention every other suite in backend/ follows."""
    print("=" * 70)
    print("INGEST WITH THE ADR-0002 HOOKS ABSENT (the mqtt_listener.py environment)")
    print("=" * 70)

    # The hooks must genuinely not be registered, or every assertion below is
    # measuring the hooks rather than the ingest code. Checked, not assumed.
    _probe = create_engine("sqlite://", connect_args={"check_same_thread": False},
                       poolclass=StaticPool)
    Base.metadata.create_all(_probe)
    _ps = sessionmaker(bind=_probe)()
    _ps.add(models.Machine(tenant_code="X", site="", name="probe", status="Idle",
                       utilization=0, downtime="0 min"))
    _ps.commit()
    _tok = tenancy.set_current_tenant("SOMEONE_ELSE")
    _leaked = _ps.query(models.Machine).count()
    tenancy.reset_current_tenant(_tok)
    _ps.close()
    if _leaked != 1:
        message = ("tenancy.install_scoping() is already registered in this "
                   "process, so the hooks would silently do ingest's job and "
                   "every assertion below would pass without measuring "
                   "anything. The per-file runner gives this suite a clean "
                   "process; pytest's shared one cannot.")
        try:
            import pytest
            pytest.skip(message, allow_module_level=False)
        except ImportError:
            print(f"  SKIP  {message}")
            return
    print("  PASS  PRECONDITION: the ORM scoping hook is NOT active here")

    db = setup()

    # FACTORY_B publishes a breakdown. FACTORY_A must be untouched.
    send("FACTORY_B", status="Breakdown", utilization=3, downtime="45 min",
         total_count=100, good_count=90, rejected_count=10)
    db.expire_all()

    by_tenant = {m.tenant_code: m for m in db.query(models.Machine).all()}
    check("FACTORY_B's own CNC-01 took the reading",
          by_tenant["FACTORY_B"].status == "Breakdown"
          and by_tenant["FACTORY_B"].utilization == 3,
          f"status={by_tenant['FACTORY_B'].status} util={by_tenant['FACTORY_B'].utilization}")
    check("FACTORY_A's CNC-01 was NOT touched by FACTORY_B's packet",
          by_tenant["FACTORY_A"].status == "Running"
          and by_tenant["FACTORY_A"].utilization == 50,
          f"status={by_tenant['FACTORY_A'].status} util={by_tenant['FACTORY_A'].utilization}")

    for model, label in ((models.ProductionRecord, "ProductionRecord"),
                     (models.DowntimeLog, "DowntimeLog"),
                     (models.MachineEvent, "MachineEvent")):
        rows = db.query(model).all()
        check(f"{label} rows all belong to FACTORY_B",
          len(rows) >= 1 and all(r.tenant_code == "FACTORY_B" for r in rows),
          f"n={len(rows)} tenants={sorted({r.tenant_code for r in rows})}")

    # CONTROL: the assertions above are also satisfied by an ingest that wrote
    # nothing at all. Prove each child table actually received a row.
    counts = {m.__name__: db.query(m).count() for m in
          (models.ProductionRecord, models.DowntimeLog, models.MachineEvent)}
    check("CONTROL: all three child tables actually received a row",
          all(v == 1 for v in counts.values()), str(counts))

    # A machine CREATED by ingest must carry its tenant without the before_flush
    # stamp to fall back on — this is the exact path where the column default
    # "DEFAULT" used to win.
    send("FACTORY_A", machine="NEW-99", status="Running", utilization=42)
    db.expire_all()
    created = db.query(models.Machine).filter(models.Machine.name == "NEW-99").all()
    check("a machine created by ingest carries its own tenant, not the column default",
          len(created) == 1 and created[0].tenant_code == "FACTORY_A",
          f"n={len(created)} tenant={created[0].tenant_code if created else None}")

    db.close()

    assert not failures, chr(10).join(failures)
    print()
    print("=" * 70)
    print("INGEST IS CORRECTLY TENANTED WITHOUT THE ORM HOOKS")


if __name__ == "__main__":
    test_ingest_is_tenanted_without_the_orm_hooks()
