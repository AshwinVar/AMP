"""Agent Impact read-model tests (ADR-0005).

The impact rollup counts the fleet's concrete outputs (tasks/POs/escalations
that weren't rejected), the autonomy rate, the human decision backlog, and a
7-day window — all tenant-scoped.

Run:  python backend/test_impact.py     (exit 0 = pass)
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from ai import impact


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _action(agent, ref_kind, status, decided_by=None, tenant="DEFAULT"):
    return models.AgentAction(tenant_code=tenant, agent=agent, action_type="x", summary="x",
                              ref_kind=ref_kind, ref_id=1, status=status, decided_by=decided_by)


def test_impact_rolls_up_outputs_autonomy_and_backlog():
    db = _fresh_session()
    db.add_all([
        _action("reorder", "purchase_order", "Approved", "auto-policy"),   # auto-approved output
        _action("maintenance", "maintenance_task", "Proposed"),            # pending output + backlog
        _action("quality", "maintenance_task", "Approved", "alice"),       # human-approved output
        _action("escalation", "escalation", "Rejected", "bob"),            # rejected -> not an output
        _action("reorder", "purchase_order", "Approved", "auto-policy", tenant="GMATS"),  # other tenant
    ])
    db.commit()

    imp = impact.build_impact(db, "DEFAULT")
    assert imp["total_actions"] == 4                                       # GMATS action excluded
    assert set(imp["agents_active"]) == {"reorder", "maintenance", "quality", "escalation"}
    assert imp["auto_approved"] == 1 and imp["pending_backlog"] == 1
    # outputs: PO(approved)=1, maintenance_task(proposed+approved)=2, escalation(rejected)=0
    assert imp["outputs"]["purchase_orders"] == 1
    assert imp["outputs"]["maintenance_tasks"] == 2
    assert imp["outputs"]["escalations"] == 0
    # decisions = approved(2) + rejected(1) = 3; auto(1)/3 -> 33%
    assert imp["auto_rate"] == 33
    assert imp["last_7_days"]["total"] == 4                                # all just created
    assert "agent" in imp["headline"]
    # per-agent contribution (ROI view): reorder led with 1 auto-approved PO
    by_agent = {a["agent"]: a for a in imp["by_agent"]}
    assert set(by_agent) == {"reorder", "maintenance", "quality", "escalation"}
    assert by_agent["reorder"]["actions"] == 1 and by_agent["reorder"]["auto_approved"] == 1
    assert by_agent["reorder"]["outputs"]["purchase_orders"] == 1
    assert by_agent["maintenance"]["pending"] == 1
    assert by_agent["quality"]["approved"] == 1
    assert all(a["name"] for a in imp["by_agent"])                         # display names present

    # empty tenant -> zeroed, no divide-by-zero
    empty = impact.build_impact(db, "NOBODY")
    assert empty["total_actions"] == 0 and empty["auto_rate"] == 0 and empty["agents_active"] == []
    assert empty["by_agent"] == []


def test_impact_matches_full_scan_and_windows_recent():
    """The rollup is aggregated in SQL (GROUP BY) rather than by hydrating the
    whole growing agent_actions table (rule 4). This guards that rewrite: it
    recomputes every headline from a full Python scan of the raw rows and asserts
    the two agree exactly, confirms the 7-day slice excludes older rows, and
    checks the per-agent parts sum back to the plant totals (rule 3)."""
    db = _fresh_session()
    old = datetime.utcnow() - timedelta(days=30)   # outside the 7-day recent window
    rows = [
        _action("reorder", "purchase_order", "Approved", "auto-policy"),
        _action("reorder", "purchase_order", "Proposed"),
        _action("maintenance", "maintenance_task", "Approved", "alice"),
        _action("maintenance", "note", "Proposed"),                   # unmapped ref_kind -> not an output
        _action("quality", "escalation", "Rejected", "auto-policy"),  # auto-rejected: auto, but not an output
        _action("quality", "maintenance_task", "Approved", "auto-policy"),
    ]
    old_a = _action("reorder", "purchase_order", "Approved", "auto-policy")
    old_a.created_at = old                                            # lifetime, but not "recent"
    old_b = _action("maintenance", "maintenance_task", "Proposed")
    old_b.created_at = old
    db.add_all(rows + [old_a, old_b])
    db.commit()

    imp = impact.build_impact(db, "DEFAULT")

    # --- independent recompute over the raw rows (the pre-rewrite full scan) ---
    label = {"maintenance_task": "maintenance_tasks",
             "purchase_order": "purchase_orders", "escalation": "escalations"}
    all_rows = db.query(models.AgentAction).filter_by(tenant_code="DEFAULT").all()
    exp_total = len(all_rows)
    exp_approved = sum(1 for r in all_rows if r.status == "Approved")
    exp_rejected = sum(1 for r in all_rows if r.status == "Rejected")
    exp_auto = sum(1 for r in all_rows if r.decided_by == "auto-policy")
    exp_pending = sum(1 for r in all_rows if r.status == "Proposed")
    exp_outputs = {"maintenance_tasks": 0, "purchase_orders": 0, "escalations": 0}
    for r in all_rows:
        if r.status in ("Proposed", "Approved") and label.get(r.ref_kind):
            exp_outputs[label[r.ref_kind]] += 1

    # SQL aggregation == full scan, and the counts are what we hand-derived above.
    assert imp["total_actions"] == exp_total == 8
    assert imp["approved"] == exp_approved == 4
    assert imp["rejected"] == exp_rejected == 1
    assert imp["auto_approved"] == exp_auto == 4
    assert imp["pending_backlog"] == exp_pending == 3
    assert imp["outputs"] == exp_outputs == {"maintenance_tasks": 3, "purchase_orders": 3, "escalations": 0}
    assert imp["auto_rate"] == round(4 / (4 + 1) * 100) == 80        # auto / decided

    # 7-day slice excludes the two old rows: 6 recent of 8 lifetime.
    assert imp["last_7_days"]["total"] == 6
    assert imp["last_7_days"]["approved"] == 3 and imp["last_7_days"]["proposed"] == 2

    # Reconciliation: the per-agent breakdown sums back to the plant totals (rule 3).
    assert sum(a["actions"] for a in imp["by_agent"]) == imp["total_actions"]
    assert sum(a["approved"] for a in imp["by_agent"]) == imp["approved"]
    assert sum(a["auto_approved"] for a in imp["by_agent"]) == imp["auto_approved"]
    assert sum(a["pending"] for a in imp["by_agent"]) == imp["pending_backlog"]
    for lbl in exp_outputs:
        assert sum(a["outputs"][lbl] for a in imp["by_agent"]) == imp["outputs"][lbl]

    # by_agent is busiest-first and deterministic (equal-action agents by key).
    counts = [a["actions"] for a in imp["by_agent"]]
    assert counts == sorted(counts, reverse=True)
    tie = [a["agent"] for a in imp["by_agent"] if a["actions"] == 3]
    assert tie == sorted(tie)                                         # maintenance before reorder


if __name__ == "__main__":
    test_impact_rolls_up_outputs_autonomy_and_backlog()
    test_impact_matches_full_scan_and_windows_recent()
    print("IMPACT OK: outputs + autonomy rate + backlog + 7-day window; tenant-scoped; "
          "SQL aggregation matches a full scan; per-agent parts reconcile; no divide-by-zero")
