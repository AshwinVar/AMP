"""Escalation queue — the open-issue backlog and how fast it clears (ADR-0005 / ADR-0007).

The Escalation agent (ADR-0004 / ADR-0005) raises escalations onto the stream and
the briefing / handover cards count how many are open — but nothing summarises the
*queue itself*: how big the backlog is, how severe, who owns it, how long the
oldest has festered, and — the question an ops lead actually asks — are we clearing
them faster than they arrive? This read-model reframes the escalations table into
that picture:

  * The open backlog now — count, severity mix, owning department, agent-raised vs
    manual, a pending-approval count (agent proposals awaiting a human), and an
    aging profile (how long each has been open).
  * Resolution over the last 30 days — of the escalations RAISED in the window, how
    many are now resolved, and how long they took to close. Resolution time is
    honest: it reads the real ``resolved_at`` timestamp the update route stamps,
    so "avg time to resolve" is measured, never inferred (ADR-0007).

A read-model over the escalations table — auto-scoped to the tenant (ADR-0002;
Escalation is in ``tenancy.SCOPED_MODELS``); it adds no storage. The scan is bounded
in SQL to the resolution window OR the still-open backlog, so an ever-growing history
of closed escalations isn't re-read on every poll (rule 4).

Honesty notes:
  * "Open" = not Resolved / Cancelled. A Cancelled escalation was withdrawn, not
    worked off, so it is neither open backlog nor a resolution.
  * Resolution time uses ``resolved_at - created_at`` (real timestamps). An
    escalation marked Resolved without a resolved_at can't be timed, so it is
    counted resolved but held out of the timing average, never scored as 0h.
  * The severity / department / source / aging breakdowns each sum to the open
    count (rule 3, asserted in the tests).
"""
from collections import Counter
from datetime import datetime, timedelta

from sqlalchemy import func, or_

import models

name = "escalations"

WINDOW_DAYS = 30       # resolution look-back — escalations are a slow-moving, monthly discipline
TOP_N = 10
STALE_OPEN_DAYS = 7    # an escalation open longer than this is festering, not fresh

# Terminal states: an escalation here is off the queue. Resolved was worked off
# (and stamps resolved_at); Cancelled was withdrawn. Matched lowercased so a
# vocabulary drift ("canceled" / "Closed") can't silently keep a closed item open.
CLOSED_STATUSES = ("resolved", "cancelled", "canceled", "closed")
RESOLVED = "resolved"
PROPOSED = "proposed"          # agent-proposed, awaiting a human decision
AGENT_SOURCE = "Escalation agent"

SEVERITY_ORDER = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}
_SEVERITIES = ["Critical", "High", "Medium", "Low"]
_URGENT = {"Critical", "High"}   # the severities an ops lead triages first
AGING_BUCKETS = [("0-1 days", 0, 1), ("2-7 days", 2, 7), ("8-30 days", 8, 30), ("30+ days", 31, None)]


def _norm(status) -> str:
    return (status or "").strip().lower()


def _is_closed(status) -> bool:
    return _norm(status) in CLOSED_STATUSES


def _is_resolved(status) -> bool:
    return _norm(status) == RESOLVED


def _aging_bucket(days_open: int) -> str:
    for label, lo, hi in AGING_BUCKETS:
        if days_open >= lo and (hi is None or days_open <= hi):
            return label
    return AGING_BUCKETS[0][0]   # days_open is clamped >= 0, so this is unreachable


def build_escalation_summary(db, tenant: str) -> dict:
    """The escalation queue: the open backlog (severity / department / source /
    aging breakdowns that each reconcile to the open count), the single most
    urgent one to action, and a 30-day resolution scorecard (raised, resolved,
    resolution rate, and the measured average time to close). escalations is
    auto-scoped (ADR-0002); the scan is bounded in SQL to the window or the open
    backlog. Empty-safe: zeros / None, no divide-by-zero, no invented timing."""
    now = datetime.utcnow()
    cutoff = now - timedelta(days=WINDOW_DAYS)

    # Bounded in SQL: escalations raised inside the resolution window OR still open
    # (any age). Old closed escalations outside the window feed no number here, so
    # excluding them changes nothing — it just keeps the poll a range scan (rule 4).
    rows = (db.query(models.Escalation).filter(or_(
        models.Escalation.created_at >= cutoff,
        ~func.lower(models.Escalation.status).in_(CLOSED_STATUSES),
    )).all())

    def _days_open(e) -> int:
        base = e.created_at or now
        return max(0, (now.date() - base.date()).days)

    # ── The open backlog now ──────────────────────────────────────────
    active = [e for e in rows if not _is_closed(e.status)]
    open_count = len(active)

    by_severity = Counter((e.severity or "Unknown") for e in active)
    by_department = Counter((e.department or "—") for e in active)
    agent_raised = sum(1 for e in active if e.source == AGENT_SOURCE)
    manual_raised = open_count - agent_raised
    pending_approval = sum(1 for e in active if _norm(e.status) == PROPOSED)
    urgent_open = sum(c for s, c in by_severity.items() if s in _URGENT)

    aging = Counter(_aging_bucket(_days_open(e)) for e in active)
    oldest_open_days = max((_days_open(e) for e in active), default=None)

    # Severity rows: known severities in triage order, then any unrecognised label
    # (still counted, so the parts sum to open_count — rule 3).
    severity_rows = [{"severity": s, "count": by_severity[s]} for s in _SEVERITIES if by_severity.get(s)]
    severity_rows += [{"severity": s, "count": by_severity[s]}
                      for s in sorted(by_severity) if s not in SEVERITY_ORDER]
    department_rows = [{"department": d, "count": c} for d, c in by_department.most_common()]
    aging_rows = [{"bucket": label, "count": aging[label]}
                  for label, _, _ in AGING_BUCKETS if aging.get(label)]

    # The queue to work: most severe first, then longest open.
    machine_names = {m.id: m.name for m in db.query(models.Machine).all()}
    queue = sorted(active, key=lambda e: (SEVERITY_ORDER.get(e.severity, 99), -_days_open(e)))
    queue_rows = [{
        "id": e.id,
        "title": e.title,
        "severity": e.severity,
        "department": e.department,
        "owner": e.owner,
        "status": e.status,
        "source": e.source,
        "machine": machine_names.get(e.machine_id) if e.machine_id is not None else None,
        "days_open": _days_open(e),
        "agent_raised": e.source == AGENT_SOURCE,
        "created_at": e.created_at.isoformat() if e.created_at else None,
    } for e in queue[:TOP_N]]
    needs_attention = queue_rows[0] if queue_rows else None

    # ── Resolution over the window ────────────────────────────────────
    # Cohort = escalations RAISED in the window (created_at inside it); of those,
    # the share now resolved. A clean denominator: resolved ⊆ raised, so the rate
    # can't exceed 100 and reconciles with the raised count.
    raised_window = [e for e in rows if e.created_at and e.created_at >= cutoff]
    raised = len(raised_window)
    resolved_rows = [e for e in raised_window if _is_resolved(e.status)]
    resolved = len(resolved_rows)
    cancelled = sum(1 for e in raised_window if _norm(e.status) in ("cancelled", "canceled"))
    resolution_rate = round(resolved / raised * 100) if raised else None

    # Time to resolve — measured from the real resolved_at timestamp only. A row
    # marked Resolved with no resolved_at (or a clock-skew negative span) can't be
    # timed, so it stays in `resolved` but out of the average, never scored 0h.
    durations = [(e.resolved_at - e.created_at).total_seconds() / 3600
                 for e in resolved_rows
                 if e.resolved_at and e.created_at and e.resolved_at >= e.created_at]
    avg_resolution_hours = round(sum(durations) / len(durations), 1) if durations else None
    worst_resolution_hours = round(max(durations), 1) if durations else None

    # ── Verdict ───────────────────────────────────────────────────────
    if not rows:
        verdict, tone = "No escalations on record.", "warn"
    elif open_count == 0:
        verdict, tone = "No open escalations — the queue is clear.", "good"
    elif urgent_open:
        oldest = (f", oldest open {oldest_open_days} day{'s' if oldest_open_days != 1 else ''}"
                  if oldest_open_days else "")
        verdict = (f"{open_count} open escalation{'s' if open_count != 1 else ''}, "
                   f"{urgent_open} high-priority{oldest}.")
        tone = "bad"
    elif oldest_open_days is not None and oldest_open_days > STALE_OPEN_DAYS:
        verdict = (f"{open_count} open escalation{'s' if open_count != 1 else ''}, none high-priority, "
                   f"but the oldest has been open {oldest_open_days} days.")
        tone = "warn"
    else:
        verdict = (f"{open_count} open escalation{'s' if open_count != 1 else ''}, none high-priority.")
        tone = "good"

    return {
        "days": WINDOW_DAYS,
        "stale_open_days": STALE_OPEN_DAYS,
        # Open backlog (the breakdowns each sum to `open` — rule 3).
        "open": open_count,
        "pending_approval": pending_approval,
        "urgent_open": urgent_open,
        "agent_raised": agent_raised,
        "manual_raised": manual_raised,
        "oldest_open_days": oldest_open_days,
        "by_severity": severity_rows,
        "by_department": department_rows,
        "aging": aging_rows,
        "needs_attention": needs_attention,
        "queue": queue_rows,
        # 30-day resolution scorecard (honest timing from resolved_at).
        "resolution": {
            "raised": raised,
            "resolved": resolved,
            "cancelled": cancelled,
            "resolution_rate": resolution_rate,
            "timed_resolutions": len(durations),
            "avg_resolution_hours": avg_resolution_hours,
            "worst_resolution_hours": worst_resolution_hours,
        },
        "verdict": verdict,
        "tone": tone,
    }
