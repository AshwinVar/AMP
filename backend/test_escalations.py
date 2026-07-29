"""Escalation-queue read-model tests (ADR-0005 / ADR-0007).

The open-issue backlog over the escalations table: how many are open, how severe,
who owns them, agent-raised vs manual, how long the oldest has festered, plus a
30-day resolution scorecard whose timing comes from the real resolved_at
timestamp. Covers the backlog breakdowns and their reconciliation (severity /
department / source / aging each sum to the open count), the independently-derived
resolution rate and average time-to-resolve, the "resolved but untimed" edge, the
Cancelled-is-not-resolved rule, the window bound, and the empty / None-field edges.

Run:  python backend/test_escalations.py     (exit 0 = pass)
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from ai import escalations
from ai.escalations import WINDOW_DAYS, STALE_OPEN_DAYS, AGENT_SOURCE


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _esc(title, severity="Medium", status="Open", source="Manual", department="Maintenance",
         owner="Lead", machine_id=None, created_days_ago=1, resolved_days_ago=None):
    now = datetime.utcnow()
    created = now - timedelta(days=created_days_ago)
    resolved = now - timedelta(days=resolved_days_ago) if resolved_days_ago is not None else None
    return models.Escalation(
        title=title, severity=severity, status=status, source=source,
        department=department, owner=owner, machine_id=machine_id,
        created_at=created, resolved_at=resolved)


def _add(db, specs):
    for e in specs:
        db.add(e)
    db.commit()


def test_backlog_breakdowns_reconcile_and_rank():
    """Five open + two closed escalations, hand-counted. Every backlog breakdown
    (severity / department / source / aging) must sum to the open count (rule 3),
    and the most urgent (severity, then oldest) must lead the queue."""
    db = _fresh_session()
    _add(db, [
        # Open backlog (5): one Critical (8d old), one High (agent, 3d), three Medium/Low.
        _esc("A", severity="Critical", status="In Progress", department="Maintenance", created_days_ago=8),
        _esc("B", severity="High", status="Open", source=AGENT_SOURCE, department="Quality", created_days_ago=3),
        _esc("C", severity="Medium", status="Proposed", source=AGENT_SOURCE, department="Maintenance", created_days_ago=1),
        _esc("D", severity="Low", status="Open", department="Procurement", created_days_ago=0),
        _esc("E", severity="Medium", status="Open", department="Maintenance", created_days_ago=15),
        # Closed (2) — excluded from the open backlog entirely.
        _esc("R", severity="High", status="Resolved", created_days_ago=5, resolved_days_ago=3),
        _esc("X", severity="High", status="Cancelled", created_days_ago=4),
    ])

    r = escalations.build_escalation_summary(db, "DEFAULT")

    # Open backlog = the 5 non-closed rows.
    assert r["open"] == 5, r["open"]
    assert r["urgent_open"] == 2, r["urgent_open"]          # Critical + High
    assert r["agent_raised"] == 2 and r["manual_raised"] == 3
    assert r["pending_approval"] == 1                       # the one Proposed
    assert r["oldest_open_days"] == 15                      # E is the oldest open

    # Every breakdown reconciles to the open count (rule 3).
    assert sum(s["count"] for s in r["by_severity"]) == r["open"]
    assert sum(d["count"] for d in r["by_department"]) == r["open"]
    assert sum(a["count"] for a in r["aging"]) == r["open"]
    assert r["agent_raised"] + r["manual_raised"] == r["open"]

    # Severity counts, independently derived.
    sev = {s["severity"]: s["count"] for s in r["by_severity"]}
    assert sev == {"Critical": 1, "High": 1, "Medium": 2, "Low": 1}, sev
    # Severity rows are in triage order.
    assert [s["severity"] for s in r["by_severity"]] == ["Critical", "High", "Medium", "Low"]

    # Department counts: Maintenance 3 (A, C, E), Quality 1 (B), Procurement 1 (D).
    dept = {d["department"]: d["count"] for d in r["by_department"]}
    assert dept == {"Maintenance": 3, "Quality": 1, "Procurement": 1}, dept

    # Aging buckets: D=0 (0-1), C=1 (0-1), B=3 (2-7), A=8 (8-30), E=15 (8-30).
    aging = {a["bucket"]: a["count"] for a in r["aging"]}
    assert aging == {"0-1 days": 2, "2-7 days": 1, "8-30 days": 2}, aging

    # Most urgent first: the Critical (A), then oldest within severity.
    assert r["needs_attention"]["title"] == "A"
    assert [q["title"] for q in r["queue"]][:2] == ["A", "B"]
    assert r["tone"] == "bad"                               # high-priority open -> loud
    print("PASS backlog breakdowns reconcile, ranking and tone are correct")


def test_resolution_scorecard_uses_real_timestamps():
    """Of the escalations raised in the window, the resolution rate is resolved /
    raised and the average time-to-resolve is the real resolved_at - created_at,
    independently derived. A Resolved row with no resolved_at counts as resolved
    but stays out of the timing average (never scored 0h)."""
    db = _fresh_session()
    _add(db, [
        # Raised in-window, resolved with real timestamps.
        _esc("t1", status="Resolved", created_days_ago=10, resolved_days_ago=8),   # 2 days -> 48h
        _esc("t2", status="Resolved", created_days_ago=6, resolved_days_ago=5),    # 1 day  -> 24h
        # Resolved but UNTIMED (no resolved_at) — counted resolved, excluded from avg.
        _esc("t3", status="Resolved", created_days_ago=4, resolved_days_ago=None),
        # Still open (raised in-window) and one cancelled.
        _esc("t4", status="Open", created_days_ago=2),
        _esc("t5", status="Cancelled", created_days_ago=3),
    ])

    r = escalations.build_escalation_summary(db, "DEFAULT")
    res = r["resolution"]

    assert res["raised"] == 5, res["raised"]               # all 5 created in-window
    assert res["resolved"] == 3, res["resolved"]           # t1, t2, t3
    assert res["cancelled"] == 1
    assert res["resolution_rate"] == 60                    # 3/5 = 60%
    # Only t1 (48h) and t2 (24h) can be timed -> avg 36.0h, worst 48.0h.
    assert res["timed_resolutions"] == 2, res["timed_resolutions"]
    assert res["avg_resolution_hours"] == 36.0, res["avg_resolution_hours"]
    assert res["worst_resolution_hours"] == 48.0, res["worst_resolution_hours"]

    # One still-open escalation remains in the backlog.
    assert r["open"] == 1
    print("PASS resolution rate and measured time-to-resolve are honest")


def test_window_bound_keeps_old_closed_out_but_old_open_in():
    """An escalation resolved long before the window drops out of the resolution
    cohort; an escalation still OPEN from long ago stays in the backlog (any age)."""
    db = _fresh_session()
    _add(db, [
        # Resolved well outside the window — must not inflate raised/resolved.
        _esc("old-closed", status="Resolved",
             created_days_ago=WINDOW_DAYS + 40, resolved_days_ago=WINDOW_DAYS + 35),
        # Still open from long ago — belongs in the backlog and its aging.
        _esc("old-open", severity="Medium", status="Open", created_days_ago=WINDOW_DAYS + 20),
    ])

    r = escalations.build_escalation_summary(db, "DEFAULT")
    assert r["resolution"]["raised"] == 0                  # nothing raised inside the window
    assert r["resolution"]["resolved"] == 0
    assert r["resolution"]["resolution_rate"] is None      # no cohort -> no rate, not 0%
    assert r["open"] == 1                                  # the long-open one is still counted
    assert r["oldest_open_days"] == WINDOW_DAYS + 20
    assert r["tone"] == "warn"                             # open, not high-priority, but stale
    assert r["needs_attention"]["title"] == "old-open"
    print("PASS window bound excludes old closed, keeps old open")


def _force_status(db, title, raw):
    """Set a status the ORM cannot write.

    `status = Column(String, default="Open")` is a PYTHON-side default, so
    passing status=None to the model writes "Open" and the case under test never
    exists. Raw SQL is the only way to build the row — which is also how it
    arises in production: a migration, a bulk import, or a manual fix-up. The
    repo ships 16 raw .sql migrations, and five other test files do exactly this.
    """
    db.execute(text("UPDATE escalations SET status = :s WHERE title = :t"),
               {"s": raw, "t": title})
    db.commit()
    db.expire_all()


def test_an_aged_escalation_with_no_status_is_still_open():
    """THE BUG. The bounded scan was `NOT (lower(status) IN (...))`, and on a NULL
    status lower(NULL) is NULL, so the IN is UNKNOWN and its negation is UNKNOWN —
    never TRUE. An escalation older than the window failed the created_at branch
    too, so it vanished from the queue entirely.

    Measured before the fix, on one Critical never-resolved 60-day-old row:

        open 0 | urgent_open 0 | oldest_open_days None | queue []
        verdict "No escalations on record."

    while /analytics/dashboard, /analytics/system-health and /saas/tenant-activity
    all counted it open — and this module's own _is_closed(None) returns False.
    """
    db = _fresh_session()
    _add(db, [_esc("no-status", severity="Critical", status="Open",
                   created_days_ago=WINDOW_DAYS + 30)])
    _force_status(db, "no-status", None)

    r = escalations.build_escalation_summary(db, "DEFAULT")
    assert r["open"] == 1, r["open"]
    assert r["urgent_open"] == 1, r["urgent_open"]
    assert r["oldest_open_days"] == WINDOW_DAYS + 30, r["oldest_open_days"]
    assert r["needs_attention"]["title"] == "no-status"
    assert [q["title"] for q in r["queue"]] == ["no-status"]
    assert "No escalations" not in r["verdict"], r["verdict"]
    print("PASS an aged NULL-status escalation stays in the backlog")


def test_the_prefilter_agrees_with_the_python_that_adjudicates_it():
    """The SQL is only a prefilter; _is_closed makes the real call. When the two
    disagree the row never reaches the Python, so the disagreement is invisible.

    This pins them together over the statuses that differ: NULL and whitespace
    both normalise to "" in _norm, which is not in CLOSED_STATUSES, so both are
    OPEN — and both must survive the scan from outside the window.
    """
    for raw in (None, "", "   ", "\t"):
        db = _fresh_session()
        _add(db, [_esc("odd", severity="Medium", status="Open",
                       created_days_ago=WINDOW_DAYS + 10)])
        _force_status(db, "odd", raw)

        assert escalations._is_closed(raw) is False, raw     # the Python basis: OPEN
        r = escalations.build_escalation_summary(db, "DEFAULT")
        assert r["open"] == 1, (raw, r["open"])              # ...so the scan must fetch it
        assert sum(a["count"] for a in r["aging"]) == 1, raw
    print("PASS NULL and whitespace statuses agree with _is_closed")


def test_widening_the_filter_does_not_drag_closed_escalations_back_in():
    """The fix errs WIDE on purpose, so the risk it introduces is over-inclusion.

    A whitespace-padded closed status is the case that probes it: trim() makes
    SQL see "resolved" exactly as _norm() does, so an old resolved escalation
    stays out of the backlog rather than reappearing as a 40-day-old open item.
    """
    db = _fresh_session()
    _add(db, [
        _esc("padded-closed", status="Resolved",
             created_days_ago=WINDOW_DAYS + 40, resolved_days_ago=WINDOW_DAYS + 38),
        _esc("upper-closed", status="CANCELLED", created_days_ago=WINDOW_DAYS + 40),
    ])
    _force_status(db, "padded-closed", "  Resolved  ")

    r = escalations.build_escalation_summary(db, "DEFAULT")
    assert r["open"] == 0, r["open"]
    assert r["queue"] == [] and r["needs_attention"] is None
    assert r["resolution"]["raised"] == 0                    # both are outside the window
    print("PASS a padded/cased closed status stays closed")


def test_empty_and_none_fields_are_safe():
    """No escalations -> honest zeros / None, no divide-by-zero. A row with a NULL
    created_at (the nullable timestamp) is bucketed as freshly open rather than
    crashing the date arithmetic, and still reconciles to the open count."""
    empty = escalations.build_escalation_summary(_fresh_session(), "DEFAULT")
    assert empty["open"] == 0
    assert empty["oldest_open_days"] is None
    assert empty["needs_attention"] is None
    assert empty["by_severity"] == [] and empty["aging"] == []
    assert empty["resolution"]["resolution_rate"] is None
    assert empty["resolution"]["avg_resolution_hours"] is None
    assert empty["tone"] == "warn" and "No escalations" in empty["verdict"]

    db = _fresh_session()
    nulldate = _esc("no-date", severity="Low", department="Ops", status="Open")
    nulldate.created_at = None                              # a NULL timestamp must not crash
    _add(db, [nulldate, _esc("dated", severity="Medium", department="Ops", status="Open", created_days_ago=0)])
    r = escalations.build_escalation_summary(db, "DEFAULT")
    assert r["open"] == 2
    assert sum(s["count"] for s in r["by_severity"]) == 2
    assert sum(d["count"] for d in r["by_department"]) == 2
    assert sum(a["count"] for a in r["aging"]) == 2         # NULL-dated row buckets as 0 days open
    assert r["tone"] == "good"                              # open, none high-priority, fresh
    print("PASS empty tables and a NULL created_at are safe")


def test_clear_queue_reads_good():
    """A queue with only closed escalations reports zero open and a clear-queue
    verdict — the resolution scorecard still reflects the window's throughput."""
    db = _fresh_session()
    _add(db, [
        _esc("done-1", status="Resolved", created_days_ago=5, resolved_days_ago=4),   # 24h
        _esc("gone-1", status="Cancelled", created_days_ago=6),
    ])
    r = escalations.build_escalation_summary(db, "DEFAULT")
    assert r["open"] == 0
    assert r["needs_attention"] is None and r["queue"] == []
    assert r["tone"] == "good" and "clear" in r["verdict"]
    assert r["resolution"]["resolved"] == 1
    assert r["resolution"]["avg_resolution_hours"] == 24.0
    print("PASS an all-closed queue reads clear with an honest scorecard")


def test_summary_exposes_the_card_contract():
    # The Escalations queue card (EscalationQueueCard.tsx) renders exactly these
    # keys; pin them so a read-model edit can't silently break the card.
    db = _fresh_session()
    r = escalations.build_escalation_summary(db, "DEFAULT")
    for k in ("days", "stale_open_days", "open", "pending_approval", "urgent_open",
              "agent_raised", "manual_raised", "oldest_open_days", "by_severity",
              "by_department", "aging", "needs_attention", "queue", "resolution",
              "verdict", "tone"):
        assert k in r, f"escalation-summary missing card key {k!r}"
    for k in ("raised", "resolved", "cancelled", "resolution_rate",
              "timed_resolutions", "avg_resolution_hours", "worst_resolution_hours"):
        assert k in r["resolution"], f"resolution scorecard missing key {k!r}"
    print("PASS escalation-summary exposes the keys the queue card renders")


if __name__ == "__main__":
    test_backlog_breakdowns_reconcile_and_rank()
    test_resolution_scorecard_uses_real_timestamps()
    test_window_bound_keeps_old_closed_out_but_old_open_in()
    test_an_aged_escalation_with_no_status_is_still_open()
    test_the_prefilter_agrees_with_the_python_that_adjudicates_it()
    test_widening_the_filter_does_not_drag_closed_escalations_back_in()
    test_empty_and_none_fields_are_safe()
    test_clear_queue_reads_good()
    test_summary_exposes_the_card_contract()
    print("\nAll escalation-queue tests passed.")
