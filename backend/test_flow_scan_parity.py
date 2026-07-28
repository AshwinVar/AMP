"""Reference oracle: the two work-order read-models must not change their numbers.

ai/flow.py read the ENTIRE work_orders table twice per dashboard round:

  * build_flow_summary counted and summed every order to draw the WIP pipeline;
  * build_wip_aging loaded every order to look at the open ones.

work_orders grows one row per production order forever, while open WIP is
bounded by how much work is actually in the plant. Measured on 20,000 orders of
which 788 were open: 324 ms and 390 ms per call, and the aging view hydrated
20,000 rows to use 788 of them.

Moving the counting into SQL and the open-filter into a WHERE clause is only
safe if the numbers come out identical, so this file keeps BOTH pre-change
implementations verbatim and asserts the new ones agree field for field. The
fixture is built to hit the normalisation the Python filter did for free:
NULL status, empty status, mixed case, surrounding whitespace, an unknown
material_state (folds to RAW), NULL created_at and NULL planned_end.

Same approach as #405 and #380.
"""
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///./ci.db")

from datetime import datetime, timedelta

import models
from ai.flow import (
    AGING_BUCKETS,
    STALE_DAYS,
    TOP_N,
    _CLOSED,
    _STAGES,
    _bucket,
    _is_closed,
    build_flow_summary,
    build_wip_aging,
)


def reference_flow_summary(db, tenant: str) -> dict:
    """Work orders grouped by material state (RAW -> SEMI -> FIN) with counts and
    quantities, so the UI can draw the two-line WIP pipeline. work_orders is
    auto-scoped (ADR-0002)."""
    wos = db.query(models.WorkOrder).all()
    agg = {s["key"]: {"count": 0, "target": 0, "actual": 0} for s in _STAGES}
    for w in wos:
        key = w.material_state if w.material_state in agg else "RAW"
        agg[key]["count"] += 1
        agg[key]["target"] += w.target_quantity or 0
        agg[key]["actual"] += w.actual_quantity or 0

    stages = [{**s, **agg[s["key"]]} for s in _STAGES]
    return {
        "total": len(wos),
        "wip": agg["RAW"]["count"] + agg["SEMI"]["count"],   # not yet finished
        "finished": agg["FIN"]["count"],
        "stages": stages,
    }


TOP_N = 8
STALE_DAYS = 14        # an open WO older than this is festering, not fresh
# Terminal states — matched lowercased so vocabulary drift ("Complete"/"Closed")
# can't silently keep a finished order in the open backlog.


def reference_wip_aging(db, tenant: str) -> dict:
    """The open work-order backlog scored by what the data can honestly measure:
    each open WO's age (from created_at), whether it has blown its planned_end
    (late — a real in-data deadline; a WO with no planned_end is reported in
    ``undated`` rather than assumed on time), an aging profile, a per-material-
    state breakdown (where work stalls: RAW = intake, SEMI = stuck between
    lines), and an oldest-first chase list with fill progress. work_orders is
    auto-scoped (ADR-0002); adds no storage. Empty-safe. The aging buckets and
    the per-state counts each sum to ``open`` (rule 3, asserted in tests)."""
    now = datetime.utcnow()
    today = now.date()
    open_wos = [w for w in db.query(models.WorkOrder).all() if not _is_closed(w.status)]

    def _age(w) -> int:
        base = w.created_at or now
        return max(0, (today - base.date()).days)

    late = [w for w in open_wos if w.planned_end and w.planned_end < now]
    undated = sum(1 for w in open_wos if not w.planned_end)
    aging: dict = {}
    state_agg: dict = {}
    for w in open_wos:
        a = _age(w)
        aging[_bucket(a)] = aging.get(_bucket(a), 0) + 1
        st = w.material_state if w.material_state in ("RAW", "SEMI", "FIN") else "RAW"
        s = state_agg.setdefault(st, {"count": 0, "age_sum": 0, "oldest": 0})
        s["count"] += 1
        s["age_sum"] += a
        s["oldest"] = max(s["oldest"], a)

    oldest_days = max((_age(w) for w in open_wos), default=None)
    stale = sum(1 for w in open_wos if _age(w) > STALE_DAYS)

    chase = sorted(open_wos, key=lambda w: (-_age(w), w.work_order_no or ""))[:TOP_N]
    chase_rows = [{
        "work_order_no": w.work_order_no,
        "part_number": w.part_number,
        "material_state": w.material_state,
        "status": w.status,
        "age_days": _age(w),
        "late": bool(w.planned_end and w.planned_end < now),
        "planned_end": w.planned_end.isoformat() if w.planned_end else None,
        "target_quantity": w.target_quantity or 0,
        "actual_quantity": w.actual_quantity or 0,
        "progress": (round((w.actual_quantity or 0) / w.target_quantity * 100)
                     if w.target_quantity else None),
    } for w in chase]

    by_state = [{
        "state": st,
        "count": s["count"],
        "avg_age_days": round(s["age_sum"] / s["count"], 1) if s["count"] else 0.0,
        "oldest_days": s["oldest"],
    } for st, s in sorted(state_agg.items(), key=lambda kv: kv[0])]
    aging_rows = [{"bucket": label, "count": aging[label]}
                  for label, _, _ in AGING_BUCKETS if aging.get(label)]

    n_late = len(late)
    if not open_wos:
        verdict, tone = "No open work orders — the backlog is clear.", "good"
    elif n_late:
        verdict = (f"{n_late} open work order{'s' if n_late != 1 else ''} past "
                   f"{'their' if n_late != 1 else 'its'} planned end"
                   + (f", oldest open {oldest_days} days" if oldest_days else "") + ".")
        tone = "bad"
    elif stale:
        verdict = (f"{stale} work order{'s' if stale != 1 else ''} open longer than "
                   f"{STALE_DAYS} days (oldest {oldest_days}).")
        tone = "warn"
    else:
        verdict = f"{len(open_wos)} open work order{'s' if len(open_wos) != 1 else ''}, all fresh, none late."
        tone = "good"

    return {
        "open": len(open_wos),
        "late": n_late,
        "undated": undated,          # open WOs with no planned_end — can't be judged late
        "stale": stale,
        "stale_days": STALE_DAYS,
        "oldest_days": oldest_days,
        "aging": aging_rows,
        "by_state": by_state,
        "chase": chase_rows,
        "verdict": verdict,
        "tone": tone,
    }


# ---------------------------------------------------------------- the parity test

import json
import tempfile

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def _fresh_db():
    path = os.path.join(tempfile.mkdtemp(), "flow.db")
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _seed_every_branch(db):
    m = models.Machine(name="M1", status="Running", utilization=70,
                       downtime="0 min", tenant_code="DEFAULT")
    db.add(m)
    db.flush()

    now = datetime.utcnow()

    def wo(no, status, state, age_days=10, planned_end_offset=None,
           target=100, actual=40, created=True):
        db.add(models.WorkOrder(
            work_order_no=no, part_number=f"P-{no}", batch_number="B",
            machine_id=m.id, target_quantity=target, actual_quantity=actual,
            status=status, material_state=state,
            planned_end=(now + timedelta(days=planned_end_offset)
                         if planned_end_offset is not None else None),
            created_at=(now - timedelta(days=age_days)) if created else None,
            tenant_code="DEFAULT"))

    # --- open, in every guise the Python filter accepted ---
    wo("OPEN-plain", "Released", "RAW", age_days=3, planned_end_offset=5)
    wo("OPEN-null-status", None, "SEMI", age_days=40, planned_end_offset=-2)   # late
    wo("OPEN-empty-status", "", "FIN", age_days=1)                             # undated
    wo("OPEN-unknown-state", "In Progress", "MYSTERY", age_days=99)            # folds to RAW
    wo("OPEN-null-state", "Planned", None, age_days=15)                        # folds to RAW
    wo("OPEN-no-created", "Planned", "RAW", created=False)                     # age falls back to now
    wo("OPEN-zero-target", "Planned", "SEMI", age_days=8, target=0, actual=0)  # progress None

    # --- closed, including the normalisation the filter did for free ---
    wo("CLOSED-plain", "Completed", "FIN", age_days=200)
    wo("CLOSED-upper-pad", "  COMPLETED  ", "FIN", age_days=201)
    wo("CLOSED-mixed", "Done", "FIN", age_days=202)
    wo("CLOSED-cancelled", "Cancelled", "RAW", age_days=203)
    wo("CLOSED-canceled-us", "canceled", "SEMI", age_days=204)
    wo("CLOSED-closed", "closed", "FIN", age_days=205)
    # A tab-padded status: Python's .strip() removes it, SQL's trim() does not.
    # This is the row that proves the SQL predicate must stay WIDER than
    # _is_closed and that the Python re-filter still makes the final call.
    wo("CLOSED-tab-padded", chr(9) + "Completed" + chr(10), "FIN", age_days=206)
    db.commit()

    # status and material_state are Column(String, default=...), so passing None
    # through the ORM inserts the DEFAULT, not NULL — the rows above named
    # "null" were quietly "Planned"/"RAW" until this. A real NULL still reaches
    # the table from raw SQL, a migration, or an update that cleared the field
    # (the class of row #375/#395/#401 keep healing), and it is exactly the row a
    # careless SQL predicate drops: `NULL NOT IN (...)` is UNKNOWN, so the order
    # silently vanishes from the open backlog. Write it the way those paths do.
    db.execute(
        models.WorkOrder.__table__.update()
        .where(models.WorkOrder.work_order_no == "OPEN-null-status")
        .values(status=None)
    )
    db.execute(
        models.WorkOrder.__table__.update()
        .where(models.WorkOrder.work_order_no == "OPEN-null-state")
        .values(material_state=None)
    )
    db.commit()

    assert db.query(models.WorkOrder).filter(
        models.WorkOrder.status.is_(None)).count() == 1, "the NULL-status row did not persist"
    assert db.query(models.WorkOrder).filter(
        models.WorkOrder.material_state.is_(None)).count() == 1, "the NULL-state row did not persist"


def _compare(db, label):
    for fn_new, fn_ref, name in (
        (build_flow_summary, reference_flow_summary, "flow_summary"),
        (build_wip_aging, reference_wip_aging, "wip_aging"),
    ):
        expected = fn_ref(db, "DEFAULT")
        actual = fn_new(db, "DEFAULT")
        if actual != expected:
            for key in expected:
                if actual.get(key) != expected[key]:
                    raise AssertionError(
                        f"{label}/{name}: '{key}' changed\n"
                        f"  was: {json.dumps(expected[key], default=str)[:400]}\n"
                        f"  now: {json.dumps(actual.get(key), default=str)[:400]}")
            raise AssertionError(f"{label}/{name}: keys differ")
    return build_flow_summary(db, "DEFAULT"), build_wip_aging(db, "DEFAULT")


def test_matches_reference_on_every_branch():
    db = _fresh_db()
    _seed_every_branch(db)
    summary, aging = _compare(db, "every-branch")

    # Guard the guard: if the fixture stops exercising the normalisation, the
    # parity assertion would pass while proving far less.
    assert summary["total"] == 14, summary
    assert aging["open"] == 7, f"the seven CLOSED- rows must not count as open: {aging['open']}"
    states = {row["state"] for row in aging["by_state"]}
    assert states <= {"RAW", "SEMI", "FIN"}, f"unknown state leaked through: {states}"
    raw = next(r for r in aging["by_state"] if r["state"] == "RAW")
    assert raw["count"] == 4, f"MYSTERY/None states must fold into RAW: {aging['by_state']}"
    print("  every-branch parity OK - 14 orders, 7 open")
    db.close()


def test_matches_reference_when_everything_is_closed():
    db = _fresh_db()
    m = models.Machine(name="M", status="Idle", utilization=0, downtime="0 min", tenant_code="DEFAULT")
    db.add(m)
    db.flush()
    for i in range(5):
        db.add(models.WorkOrder(work_order_no=f"C{i}", part_number="P", batch_number="B",
                                machine_id=m.id, target_quantity=10, actual_quantity=10,
                                status="Completed", material_state="FIN",
                                created_at=datetime.utcnow(), tenant_code="DEFAULT"))
    db.commit()
    summary, aging = _compare(db, "all-closed")
    assert summary["total"] == 5 and aging["open"] == 0
    print("  all-closed parity OK")
    db.close()


def test_matches_reference_on_an_empty_database():
    db = _fresh_db()
    summary, aging = _compare(db, "empty")
    assert summary["total"] == 0 and aging["open"] == 0
    print("  empty-database parity OK")
    db.close()


if __name__ == "__main__":
    test_matches_reference_on_every_branch()
    test_matches_reference_when_everything_is_closed()
    test_matches_reference_on_an_empty_database()
    print("flow work-order scan parity: OK")
