"""WIP aging read-model tests (ADR-0007).

`build_wip_aging` scores the open work-order backlog by what the data honestly
measures: age from created_at, lateness against planned_end (a WO with no
planned_end is `undated`, never assumed on time), an aging profile, a per-state
breakdown, and an oldest-first chase list. These tests pin the numbers and the
reconciliation (buckets and per-state counts each sum to `open`), the closed-
status vocabulary, empty safety, the verdict tones, and the card contract.

Run:  python backend/test_wip_aging.py     (exit 0 = pass)
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from ai import flow

NOW = datetime.utcnow()


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _wo(no, age_days, status="Planned", state="RAW", planned_end=None,
        target=100, actual=0):
    w = models.WorkOrder(work_order_no=no, part_number=f"P-{no}", batch_number=f"B-{no}",
                         target_quantity=target, actual_quantity=actual,
                         status=status, material_state=state, planned_end=planned_end)
    w.created_at = NOW - timedelta(days=age_days)
    return w


def test_aging_lateness_and_reconciliation():
    db = _fresh_session()
    db.add_all([
        _wo("W1", 1),                                                        # fresh RAW
        _wo("W2", 5, state="SEMI", planned_end=NOW - timedelta(days=1),
            target=100, actual=60),                                          # late SEMI, 60%
        _wo("W3", 20, state="RAW"),                                          # stale, undated
        _wo("W4", 10, status="Completed", state="FIN"),                      # closed -> out
        _wo("W5", 3, status="cancelled"),                                    # closed (lowercase) -> out
    ])
    db.commit()

    r = flow.build_wip_aging(db, "DEFAULT")
    assert r["open"] == 3                       # W1, W2, W3
    assert r["late"] == 1                       # only W2 has a blown planned_end
    assert r["undated"] == 2                    # W1, W3 have no planned_end
    assert r["stale"] == 1 and r["oldest_days"] == 20

    # rule 3: buckets and per-state counts each sum to open
    assert sum(b["count"] for b in r["aging"]) == r["open"]
    assert sum(s["count"] for s in r["by_state"]) == r["open"]
    by_state = {s["state"]: s for s in r["by_state"]}
    assert by_state["RAW"]["count"] == 2 and by_state["RAW"]["oldest_days"] == 20
    assert by_state["RAW"]["avg_age_days"] == 10.5              # (1 + 20) / 2
    assert by_state["SEMI"]["count"] == 1

    # chase: oldest first, with the late flag and fill progress carried
    assert [c["work_order_no"] for c in r["chase"]] == ["W3", "W2", "W1"]
    w2 = r["chase"][1]
    assert w2["late"] is True and w2["progress"] == 60

    assert r["tone"] == "bad"
    assert r["verdict"] == "1 open work order past its planned end, oldest open 20 days."


def test_stale_without_late_reads_warn():
    db = _fresh_session()
    db.add(_wo("S1", 16))
    db.commit()
    r = flow.build_wip_aging(db, "DEFAULT")
    assert r["late"] == 0 and r["stale"] == 1
    assert r["tone"] == "warn"
    assert "open longer than 14 days (oldest 16)" in r["verdict"]


def test_fresh_backlog_reads_good_and_empty_is_safe():
    db = _fresh_session()
    db.add(_wo("F1", 2, planned_end=NOW + timedelta(days=3)))
    db.commit()
    r = flow.build_wip_aging(db, "DEFAULT")
    assert r["open"] == 1 and r["late"] == 0 and r["undated"] == 0
    assert r["tone"] == "good"
    assert r["verdict"] == "1 open work order, all fresh, none late."

    e = flow.build_wip_aging(_fresh_session(), "DEFAULT")
    assert e["open"] == 0 and e["late"] == 0 and e["oldest_days"] is None
    assert e["aging"] == [] and e["by_state"] == [] and e["chase"] == []
    assert e["tone"] == "good" and "backlog is clear" in e["verdict"]


def test_zero_target_progress_is_none_not_crash():
    db = _fresh_session()
    db.add(_wo("Z1", 4, target=0, actual=5))
    db.commit()
    r = flow.build_wip_aging(db, "DEFAULT")
    assert r["chase"][0]["progress"] is None    # no target -> no % invented


def test_exposes_the_card_contract():
    r = flow.build_wip_aging(_fresh_session(), "DEFAULT")
    for k in ("open", "late", "undated", "stale", "stale_days", "oldest_days",
              "aging", "by_state", "chase", "verdict", "tone"):
        assert k in r, f"wip-aging missing card key {k!r}"
    print("PASS wip-aging exposes the card contract")


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run()
