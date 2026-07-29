"""/oee/summary issued one query per machine it named.

The endpoint reads the latest 100 production records and labels each with its
machine name via ``record.machine.name``. That attribute is a lazy relationship,
so every distinct machine in the page cost its own SELECT:

    1 query for the records + 1 per distinct machine

Measured before the fix, on one request:

    25 machines across 100 records ->  26 statements
    100 records, all distinct      -> 101 statements

Wall-clock on SQLite understates this badly (18.6 ms), because a local file read
is nearly free. Production is Postgres over a network, where each of those is a
round trip — which is why this test counts STATEMENTS rather than milliseconds.
That number is the same on every backend.

Run:  python backend/test_oee_summary_queries.py     (exit 0 = pass)
"""
import os
import tempfile

os.environ.setdefault("DATABASE_URL", "sqlite:///./ci.db")

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

import analytics_routes
import models
import tenancy as T


def _counting_session(machines: int, records: int):
    """A session that tallies every statement the ORM sends."""
    T.install_scoping()
    path = os.path.join(tempfile.mkdtemp(), "oee.db")
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine)()

    for i in range(machines):
        db.add(models.Machine(name=f"CNC-{i}", status="Running", utilization=70,
                              downtime="0 min", tenant_code="DEFAULT"))
    db.commit()
    ids = [m.id for m in db.query(models.Machine).all()]
    for i in range(records):
        db.add(models.ProductionRecord(
            machine_id=ids[i % machines], planned_minutes=480, runtime_minutes=400,
            ideal_cycle_time_seconds=30, total_count=600, good_count=560,
            rejected_count=40, tenant_code="DEFAULT"))
    db.commit()
    # A fresh request has a cold identity map; without this the machines are
    # already loaded and the lazy hit never happens, which would make this test
    # pass against the very bug it exists to catch.
    db.expire_all()

    counter = {"n": 0}

    @event.listens_for(engine, "before_cursor_execute")
    def _count(conn, cursor, statement, parameters, context, executemany):
        counter["n"] += 1

    return db, counter


def _run(db):
    return analytics_routes.oee_summary(db=db, current_user={"tenant": "DEFAULT", "role": "Admin"})


def test_one_page_costs_a_constant_number_of_queries():
    # 100 records spread over 25 machines. Lazily this was 26 statements.
    db, counter = _counting_session(machines=25, records=150)
    rows = _run(db)

    assert len(rows) == 100, len(rows)
    assert counter["n"] <= 2, (
        f"/oee/summary issued {counter['n']} statements for one page — the machine "
        "label is being lazy-loaded per row. Eager-load the relationship."
    )
    print(f"PASS 100 rows over 25 machines in {counter['n']} statements")
    db.close()


def test_the_worst_case_does_not_scale_with_the_page():
    # Every record on its own machine — the shape that made it 101 statements.
    db, counter = _counting_session(machines=120, records=120)
    rows = _run(db)

    assert len(rows) == 100, len(rows)
    assert counter["n"] <= 2, (
        f"/oee/summary issued {counter['n']} statements when every row named a "
        "different machine — the query count still tracks the page size."
    )
    print(f"PASS 100 rows over 100 distinct machines in {counter['n']} statements")
    db.close()


def test_the_machine_names_are_still_right():
    """Eager loading must not change a single value in the response.

    The whole point of the relationship is the label, so a fix that bounded the
    query count but mislabelled rows would be worse than the bug.
    """
    db, _ = _counting_session(machines=25, records=150)
    rows = _run(db)

    by_id = {m.id: m.name for m in db.query(models.Machine).all()}
    for row in rows:
        assert row["machine_name"] == by_id[row["machine_id"]], row
    assert len({r["machine_name"] for r in rows}) == 25
    print("PASS every row still carries its own machine's name")
    db.close()


def test_a_record_whose_machine_is_missing_still_renders():
    """machine_id is a nullable FK with no ON DELETE, so a deleted machine leaves
    orphan records behind. The endpoint falls back to "Machine {id}" — eager
    loading must keep that path working rather than raising on the None."""
    db, _ = _counting_session(machines=2, records=4)
    tok = T.set_current_tenant("DEFAULT")
    try:
        orphan = models.ProductionRecord(
            machine_id=99_999, planned_minutes=480, runtime_minutes=400,
            ideal_cycle_time_seconds=30, total_count=600, good_count=560,
            rejected_count=40, tenant_code="DEFAULT")
        db.add(orphan)
        db.commit()
        db.expire_all()
        rows = _run(db)
        orphan_row = next(r for r in rows if r["machine_id"] == 99_999)
        assert orphan_row["machine_name"] == "Machine 99999", orphan_row
    finally:
        T.reset_current_tenant(tok)
    print("PASS an orphan record falls back to its machine id")
    db.close()


if __name__ == "__main__":
    test_one_page_costs_a_constant_number_of_queries()
    test_the_worst_case_does_not_scale_with_the_page()
    test_the_machine_names_are_still_right()
    test_a_record_whose_machine_is_missing_still_renders()
    print("oee-summary query budget: OK")
