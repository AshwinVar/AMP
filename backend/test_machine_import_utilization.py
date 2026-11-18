"""A machine CSV import never loses a row over a bad utilization cell.

THE DEFECT
----------
POST /machines/import-csv parsed the utilization column with a hand-rolled
``int(float(cell))`` guarded by ``except (TypeError, ValueError)``. That is the
exact parse the rest of the codebase learned NOT to write: a non-finite cell —
``inf`` / ``-inf`` / ``Infinity`` / an overflow literal like ``1e999``, all of
which a spreadsheet export or a disconnected analog sensor really emits — makes
``int(float("inf"))`` raise **OverflowError**, which is NOT ``TypeError`` or
``ValueError``. So it escaped the inner guard, hit the per-row ``except
Exception`` handler, and LOST the whole machine (name, line, status) over one bad
utilization cell — while a ``nan`` cell (which raises ValueError) defaulted
cleanly to 0. The same OverflowError class was already fixed for the MQTT counts
(mqtt_service._non_negative_int), the production CSV import (#774), and the
shared payload_fields.int_field guard; the machine roster import was the
straggler.

THE RULE
--------
Utilization is parsed and clamped through the ONE shared helper
(machine_status.clamp_utilization) — the same call POST /machines uses — so the
two paths cannot disagree. Any non-finite / non-numeric reading maps to the
documented new-machine default of 0 (the row is KEPT, not lost); a real value is
rounded and clamped into [0, 100].

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_machine_import_utilization.py
"""
import asyncio

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import machines_routes
import models
import schemas
from database import Base

failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


class _Upload:
    """Stand-in for FastAPI's UploadFile (async read of fixed bytes)."""

    def __init__(self, text: str):
        self._data = text.encode("utf-8")

    async def read(self):
        return self._data


def _session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _import(db, text):
    return asyncio.run(machines_routes.import_machines_csv(file=_Upload(text), db=db, current_user={}))


def _util(db, name):
    m = db.query(models.Machine).filter(models.Machine.name == name).first()
    return None if m is None else m.utilization


def main():
    print("=" * 74)
    print("1. A NON-FINITE UTILIZATION CELL KEEPS THE MACHINE (default 0), NOT LOSES IT")
    print("=" * 74)
    db = _session()
    # Every spelling of a non-finite reading a spreadsheet / sensor emits. Before
    # the fix each made int(float(cell)) raise OverflowError and the WHOLE row was
    # reported as an error and dropped. Each must now create the machine with 0.
    r = _import(db, "name,status,utilization\n"
                    "INF-1,Running,inf\n"
                    "INF-2,Running,-inf\n"
                    "INF-3,Running,Infinity\n"
                    "INF-4,Running,1e999\n"       # Excel's overflow literal -> int(inf)
                    "NAN-1,Running,nan\n")         # ValueError path: always defaulted to 0
    check("all five rows were created, none lost to an error",
          (r["created"], r["updated"], r["errors"]) == (5, 0, []), str(r))
    for name in ("INF-1", "INF-2", "INF-3", "INF-4", "NAN-1"):
        check(f"{name} kept with utilization defaulted to 0", _util(db, name) == 0, repr(_util(db, name)))
    db.close()

    print()
    print("=" * 74)
    print("2. A REAL VALUE IS ROUNDED AND CLAMPED INTO [0, 100]")
    print("=" * 74)
    db = _session()
    # Independently-derived expectations (machine_status.clamp_utilization):
    #   "85"   -> round(85.0)   = 85
    #   "85.6" -> round(85.6)   = 86   (round, not the old int()/truncate -> 85)
    #   "150"  -> clamp         = 100  (a percentage can't exceed 100)
    #   "-5"   -> clamp         = 0    (nor drop below 0)
    #   "0"    ->               = 0    (a real measured zero, unchanged)
    #   ""     -> no reading    = 0    (documented new-machine default)
    r = _import(db, "name,status,utilization\n"
                    "M85,Running,85\n"
                    "M856,Running,85.6\n"
                    "M150,Running,150\n"
                    "MNEG,Running,-5\n"
                    "MZERO,Idle,0\n"
                    "MBLANK,Idle,\n")
    check("every row created, none errored", (r["created"], r["errors"]) == (6, []), str(r))
    expected = {"M85": 85, "M856": 86, "M150": 100, "MNEG": 0, "MZERO": 0, "MBLANK": 0}
    for name, want in expected.items():
        check(f"{name} utilization == {want}", _util(db, name) == want, repr(_util(db, name)))
    db.close()

    print()
    print("=" * 74)
    print("3. THE IMPORT AND POST /machines STORE THE SAME CLAMPED VALUE (one helper)")
    print("=" * 74)
    # The import must not diverge from the create endpoint: both go through
    # clamp_utilization, so the same raw utilization yields the same stored value.
    db = _session()
    _import(db, "name,status,utilization\nVIA_CSV,Running,150\n")
    machines_routes.create_machine(
        schemas.MachineCreate(name="VIA_POST", status="Running", utilization=150, downtime="0 min"),
        db=db, current_user={"role": "Admin"})
    check("150 clamps to 100 on BOTH paths (reconciled)",
          _util(db, "VIA_CSV") == _util(db, "VIA_POST") == 100,
          f"csv={_util(db, 'VIA_CSV')} post={_util(db, 'VIA_POST')}")
    db.close()

    print()
    print("=" * 74)
    print("4. THE UPDATE PATH: A NON-FINITE CELL HEALS THE ROW, IT DOES NOT ROLL IT BACK")
    print("=" * 74)
    db = _session()
    db.add(models.Machine(name="CNC-1", status="Running", utilization=70, line="SMT"))
    db.commit()
    # Before the fix, the OverflowError rolled back the whole savepoint, so CNC-1
    # stayed Running/70 AND the row was reported as an error. Now the status change
    # lands and utilization heals to 0.
    r = _import(db, "name,status,utilization\nCNC-1,Idle,inf\n")
    check("the update succeeded, not errored", (r["updated"], r["errors"]) == (1, []), str(r))
    m = db.query(models.Machine).filter(models.Machine.name == "CNC-1").one()
    check("CNC-1 status updated to Idle", m.status == "Idle", m.status)
    check("CNC-1 utilization healed to 0", m.utilization == 0, repr(m.utilization))
    db.close()

    print()
    print("=" * 74)
    print("5. EMPTY FILE (HEADER ONLY) IS A CLEAN NO-OP")
    print("=" * 74)
    db = _session()
    r = _import(db, "name,status,utilization\n")
    check("nothing created/updated, no errors",
          (r["created"], r["updated"], r["skipped"], r["errors"]) == (0, 0, 0, []), str(r))
    db.close()

    print()
    print("=" * 74)
    if failures:
        print(f"{len(failures)} FAILED")
        for f in failures:
            print(f"  - {f}")
    else:
        print("ALL CHECKS PASSED")
    print("=" * 74)
    return 1 if failures else 0


def test_machine_import_utilization_is_clamped_and_never_loses_a_row():
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
