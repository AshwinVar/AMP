"""Agent oversight metrics test (/agent-actions/stats).

The stats handler used to hydrate the ENTIRE agent_actions log for the tenant on
every poll and tally it in Python — the rule-4 antipattern on a growing,
append-only table (agent_actions never windows). It now aggregates in SQL
(GROUP BY counts). This pins that the SQL aggregates return the SAME numbers the
Python tally did, and covers the edges the grouping must get right:

  * status/agent tallies and the auto-approved count match hand-derived numbers;
  * `total` counts every row, INCLUDING a row whose status column is a genuine
    NULL (its own GROUP BY bucket) — so total still equals the row count even
    though that row is in none of proposed/approved/rejected;
  * `auto_approved` counts only decided_by == "auto-policy" (a NULL decided_by,
    left by a still-Proposed action, must NOT count);
  * an empty log returns all-zeros / empty by_agent, not a crash;
  * the handler is tenant-scoped — another tenant's actions never leak in.

Run:  DATABASE_URL="sqlite:///./ci.db" python backend/test_agent_stats.py
"""
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from agent_routes import agent_action_stats


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _action(agent, status, decided_by=None, tenant="DEFAULT"):
    return models.AgentAction(
        tenant_code=tenant, agent=agent, action_type="open_task",
        summary="x", ref_kind="maintenance_task", status=status, decided_by=decided_by,
    )


def test_stats_match_hand_derived_numbers():
    db = _fresh_session()
    db.add_all([
        # maintenance: 2 proposed, 1 approved (auto), 1 rejected (human)
        _action("maintenance", "Proposed"),
        _action("maintenance", "Proposed"),
        _action("maintenance", "Approved", decided_by="auto-policy"),
        _action("maintenance", "Rejected", decided_by="alice"),
        # reorder: 1 approved (auto), 1 approved (human)
        _action("reorder", "Approved", decided_by="auto-policy"),
        _action("reorder", "Approved", decided_by="bob"),
        # a DIFFERENT tenant's noise that must never be counted for DEFAULT
        _action("maintenance", "Approved", decided_by="auto-policy", tenant="OTHER"),
    ])
    db.commit()

    stats = agent_action_stats(db=db, current_user={"tenant": "DEFAULT"})

    # 6 DEFAULT rows (the OTHER-tenant row is excluded)
    assert stats["total"] == 6, stats
    assert stats["proposed"] == 2, stats
    assert stats["approved"] == 3, stats        # 1 maintenance + 2 reorder
    assert stats["rejected"] == 1, stats
    # auto-policy decisions only: maintenance approve + reorder approve = 2
    # (the human-decided approve/reject and the two Proposed must NOT count)
    assert stats["auto_approved"] == 2, stats
    assert stats["by_agent"] == {"maintenance": 4, "reorder": 2}, stats["by_agent"]
    print("PASS stats match hand-derived numbers, tenant-scoped")


def test_total_counts_a_null_status_row():
    # A row with a genuine NULL status (as a bulk insert or migration-added column
    # would leave it) belongs to no proposed/approved/rejected bucket, but `total`
    # must still count it — total == the number of rows, always.
    db = _fresh_session()
    db.add_all([
        _action("maintenance", "Proposed"),
        _action("reorder", "Approved", decided_by="auto-policy"),
    ])
    db.commit()
    # Force a real NULL into status (the ORM default="Proposed" would fill a None).
    db.execute(text(
        "INSERT INTO agent_actions (tenant_code, agent, action_type, summary, ref_kind, status) "
        "VALUES ('DEFAULT', 'quality', 'open_task', 'x', 'maintenance_task', NULL)"
    ))
    db.commit()

    stats = agent_action_stats(db=db, current_user={"tenant": "DEFAULT"})
    assert stats["total"] == 3, stats           # 2 + the NULL-status row
    assert stats["proposed"] == 1, stats
    assert stats["approved"] == 1, stats
    assert stats["rejected"] == 0, stats
    # the NULL-status row still shows up under its agent in by_agent
    assert stats["by_agent"] == {"maintenance": 1, "reorder": 1, "quality": 1}, stats["by_agent"]
    # proposed + approved + rejected (+ the null bucket) reconcile to total
    assert stats["proposed"] + stats["approved"] + stats["rejected"] < stats["total"], stats
    print("PASS a NULL-status row is counted in total but no status bucket")


def test_empty_log_is_all_zeros():
    db = _fresh_session()
    stats = agent_action_stats(db=db, current_user={"tenant": "DEFAULT"})
    assert stats == {
        "total": 0, "proposed": 0, "approved": 0, "rejected": 0,
        "auto_approved": 0, "by_agent": {},
    }, stats
    print("PASS an empty log returns all-zeros, no crash")


if __name__ == "__main__":
    test_stats_match_hand_derived_numbers()
    test_total_counts_a_null_status_row()
    test_empty_log_is_all_zeros()
    print("ALL AGENT STATS TESTS PASSED")
