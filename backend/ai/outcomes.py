"""Did it help? What changed after an approved action, measured (ADR-0029).

AMP has proposed, ranked and approved work since ADR-0005 and has never once
looked back. Two accepted ADRs say so in writing — the Risk Radar cannot report
how often a LIKELY risk became a problem (ADR-0026), and the health score's
weights are "judgement, not evidence" because nothing records what happened
next (ADR-0027). This module is the missing half of that loop:

    APPROVE   AMP freezes the metric the action was meant to move, as it read
              at that instant. That reading is unrecoverable later, which is
              the whole reason the row exists.
    WAIT      nothing is judged until the window has actually elapsed.
    MEASURE   the same metric over the same length of window after the
              decision, frozen once and never rewritten.
    STATE     the two numbers side by side, and the direction they moved.

WHAT IT REFUSES TO CLAIM
------------------------
The verdict is about THE METRIC, not the action. BETTER means the number
improved after the approval, not because of it. A factory is not a laboratory:
downtime falls when the order book empties, stock rises when production stops.
So every fact here carries CORRELATION provenance (ai/evidence.py) and the
summary says, in words, that AMP cannot show the action caused the change.

There is no confidence score and no p-value, because an uncontrolled before and
after cannot produce one. The schema has nowhere to put one either.

NULL IS NOT ZERO
----------------
A window with no reading gives None, and None on either side makes the verdict
NOT MEASURABLE. Writing 0.0 in place of "no reading" would manufacture an
improvement out of an absence, which is the ADR-0014 rule applied to a second
kind of measurement.

WHERE THE MEASUREMENT IS WRITTEN
--------------------------------
`build_outcome_summary` freezes any reading that has come due. A read that
writes is unusual and deserves its reason: the source rows are pruned by
retention, AMP runs no per-tenant scheduler for real (non-simulated) tenants,
and the write is additive, idempotent and only ever fills a row that was empty.
A value, once frozen, is never recomputed — test_action_outcomes.py asserts it.
"""
from datetime import datetime, timedelta

import models
from ai import evidence as ev
from duration import parse_duration_to_minutes

name = "outcomes"

WINDOW_DAYS = 7
# A change smaller than this is NO CHANGE, not an improvement. Both floors are
# judgement, stated here rather than buried: 10% of the baseline, and a per-metric
# absolute floor so a tiny baseline cannot turn noise into a result.
NOISE_FRACTION = 0.10

BETTER, WORSE, NO_CHANGE, NOT_MEASURABLE = "BETTER", "WORSE", "NO CHANGE", "NOT MEASURABLE"
VERDICTS = (BETTER, WORSE, NO_CHANGE, NOT_MEASURABLE)

# What each kind of action was meant to move, and which way is good. A kind that
# is not here is not followed up at all — AMP would rather record nothing than
# watch a number the action has no claim on.
METRICS = {
    "maintenance_task": "downtime_minutes",
    "escalation": "downtime_minutes",
    "purchase_order": "stock_on_hand",
}
METRIC_SPEC = {
    "downtime_minutes": {"label": "Downtime", "unit": "min", "better": "lower", "floor": 5.0,
                         "scope_kind": "machine",
                         "rule": "minutes of logged stoppage on this machine"},
    "stock_on_hand": {"label": "Stock on hand", "unit": "units", "better": "higher", "floor": 1.0,
                      "scope_kind": "item",
                      "rule": "units of this item on hand"},
}

CAUSATION_NOTE = ("AMP measured what changed after the decision. It cannot show the action caused "
                  "the change: nothing else in the plant was held still.")


# ── Measuring ───────────────────────────────────────────────────────

def _downtime_minutes(db, tenant, machine_id, start, end):
    """Logged stoppage minutes for one machine in [start, end). None when the
    machine has no logs in the window at all — which is not the same as zero
    minutes, and must not be reported as an improvement."""
    if not machine_id:
        return None
    rows = (db.query(models.DowntimeLog.duration)
            .filter(models.DowntimeLog.tenant_code == tenant,
                    models.DowntimeLog.machine_id == machine_id,
                    models.DowntimeLog.created_at >= start,
                    models.DowntimeLog.created_at < end)
            .all())
    if not rows:
        # No stoppage logged. For THIS metric an empty window is a real reading
        # of zero only if the machine was reporting at all; AMP cannot tell the
        # two apart from downtime logs alone, so it says zero and the card says
        # what the rule counted. See the honest limits in ADR-0029.
        return 0.0
    return float(sum(parse_duration_to_minutes(r[0]) for r in rows))


def _stock_on_hand(db, tenant, item_id, start, end):
    """Units on hand for one item, as it reads NOW. A point-in-time metric: the
    window bounds when it is read, not what is summed."""
    if not item_id:
        return None
    row = (db.query(models.InventoryItem.current_stock)
           .filter(models.InventoryItem.tenant_code == tenant,
                   models.InventoryItem.id == item_id).first())
    if row is None or row[0] is None:
        return None
    return float(row[0])


_READERS = {"downtime_minutes": _downtime_minutes, "stock_on_hand": _stock_on_hand}


def measure(db, tenant, metric, scope_id, start, end):
    reader = _READERS.get(metric)
    return reader(db, tenant, scope_id, start, end) if reader else None


def judge(metric, baseline, measured) -> str:
    """The verdict for THE METRIC. Never a statement about the action."""
    if baseline is None or measured is None:
        return NOT_MEASURABLE
    spec = METRIC_SPEC[metric]
    delta = measured - baseline
    if abs(delta) <= max(spec["floor"], abs(baseline) * NOISE_FRACTION):
        return NO_CHANGE
    improved = delta < 0 if spec["better"] == "lower" else delta > 0
    return BETTER if improved else WORSE


# ── Recording ───────────────────────────────────────────────────────

def _scope_of(db, tenant, action):
    """(scope_id, scope_label) for the thing this action was meant to change."""
    metric = METRICS.get(action.ref_kind)
    if metric is None:
        return None, None
    if METRIC_SPEC[metric]["scope_kind"] == "machine":
        machine_id = action.related_machine_id
        if not machine_id and action.ref_kind == "maintenance_task":
            task = (db.query(models.MaintenanceTask)
                    .filter(models.MaintenanceTask.tenant_code == tenant,
                            models.MaintenanceTask.id == action.ref_id).first())
            machine_id = task.machine_id if task else None
        if not machine_id:
            return None, None
        row = (db.query(models.Machine.name)
               .filter(models.Machine.tenant_code == tenant,
                       models.Machine.id == machine_id).first())
        return machine_id, (row[0] if row else None)
    po = (db.query(models.PurchaseOrder)
          .filter(models.PurchaseOrder.tenant_code == tenant,
                  models.PurchaseOrder.id == action.ref_id).first())
    if po is None or not po.item_id:
        return None, None
    return po.item_id, po.item_name


def record_baseline(db, tenant, action, now=None):
    """Freeze what the metric read when a human approved. Returns the row, or
    None when this kind of action has nothing AMP can honestly follow up.

    Never raises: an outcome record is evidence AMP would like to have, not a
    precondition for approving work. A failure here must not block a decision a
    person has already made (the caller wraps it, and the test proves it).
    """
    metric = METRICS.get(action.ref_kind)
    if metric is None:
        return None
    existing = (db.query(models.ActionOutcome)
                .filter(models.ActionOutcome.action_id == action.id).first())
    if existing is not None:
        return existing               # one row per action; re-approval cannot fork it
    scope_id, scope_label = _scope_of(db, tenant, action)
    if scope_id is None:
        return None
    at = now or datetime.utcnow()
    spec = METRIC_SPEC[metric]
    row = models.ActionOutcome(
        tenant_code=tenant, action_id=action.id, metric=metric,
        scope_kind=spec["scope_kind"], scope_id=scope_id, scope_label=scope_label,
        window_days=WINDOW_DAYS,
        baseline_value=measure(db, tenant, metric, scope_id, at - timedelta(days=WINDOW_DAYS), at),
        baseline_at=at,
    )
    db.add(row)
    return row


def freeze_due(db, tenant, now=None) -> int:
    """Measure every outcome whose window has elapsed and that has no reading yet.

    Idempotent by construction: it only selects rows with `measured_at IS NULL`,
    and a value once written is never recomputed. Returns how many it froze.
    """
    at = now or datetime.utcnow()
    due = (db.query(models.ActionOutcome)
           .filter(models.ActionOutcome.tenant_code == tenant,
                   models.ActionOutcome.measured_at.is_(None))
           .order_by(models.ActionOutcome.id).all())
    frozen = 0
    for row in due:
        end = row.baseline_at + timedelta(days=row.window_days)
        if at < end:
            continue                  # the window has not elapsed; nothing to judge yet
        row.measured_value = measure(db, tenant, row.metric, row.scope_id, row.baseline_at, end)
        row.measured_at = end
        row.verdict = judge(row.metric, row.baseline_value, row.measured_value)
        frozen += 1
    if frozen:
        # COMMIT, or none of this is true. Without it the reading is recomputed
        # on every read from whatever the source rows say at that moment, and
        # "frozen once" becomes a comment rather than a behaviour — which is
        # exactly what test_action_outcomes.py section 5 caught: the same
        # outcome read 20 minutes, then 520, after more stoppages were logged.
        # The session a GET holds has nothing else pending, so this commits this
        # write and nothing else.
        db.commit()
    return frozen


# ── The read-model ──────────────────────────────────────────────────

def _fact(key, label, value, prov, unit="", source="", window="", detail=""):
    return ev.Fact(key=key, label=label, value=value, provenance=prov, unit=unit,
                   source=source, window=window, detail=detail).to_dict(key)


def _row_view(row, action, at):
    spec = METRIC_SPEC[row.metric]
    window = f"{row.window_days} days each side of the decision"
    waiting = row.measured_at is None
    days_left = 0
    if waiting:
        days_left = max(0, (row.baseline_at + timedelta(days=row.window_days) - at).days)
    facts = [
        _fact(f"outcome.{row.id}.baseline", f"{spec['label']} before", row.baseline_value,
              ev.MEASURED if row.baseline_value is not None else ev.UNKNOWN, spec["unit"],
              "downtime_logs" if row.metric == "downtime_minutes" else "inventory_items",
              f"the {row.window_days} days before the decision",
              detail=spec["rule"]),
    ]
    if not waiting:
        facts.append(_fact(f"outcome.{row.id}.after", f"{spec['label']} after", row.measured_value,
                           ev.MEASURED if row.measured_value is not None else ev.UNKNOWN, spec["unit"],
                           "downtime_logs" if row.metric == "downtime_minutes" else "inventory_items",
                           f"the {row.window_days} days after the decision", detail=spec["rule"]))
        if row.baseline_value is not None and row.measured_value is not None:
            # The CHANGE is a correlation and nothing more: two measurements with
            # an action between them, and a plant that did not stand still.
            facts.append(_fact(f"outcome.{row.id}.change", f"{spec['label']} change",
                               round(row.measured_value - row.baseline_value, 1), ev.CORRELATION,
                               spec["unit"], "the two measurements above", window,
                               detail=CAUSATION_NOTE))
    return {
        "id": row.id,
        "action_id": row.action_id,
        "action": (action.summary if action else None),
        "agent": (action.agent if action else None),
        "decided_at": (action.decided_at.isoformat() if action and action.decided_at else None),
        "metric": row.metric,
        "metric_label": spec["label"],
        "unit": spec["unit"],
        "rule": spec["rule"],
        "better": spec["better"],
        "scope_kind": row.scope_kind,
        "scope_label": row.scope_label,
        "window_days": row.window_days,
        "baseline_value": row.baseline_value,
        "measured_value": row.measured_value,
        "change": (None if waiting or row.baseline_value is None or row.measured_value is None
                   else round(row.measured_value - row.baseline_value, 1)),
        # TOO EARLY is a state of the FOLLOW-UP, not a verdict about the metric,
        # so it is reported separately and `verdict` stays null until it is real.
        "waiting": waiting,
        "days_left": days_left if waiting else 0,
        "verdict": row.verdict,
        "facts": facts,
    }


def build_outcome_summary(db, tenant: str, now=None, freeze=True) -> dict:
    """Every approved action AMP is following up, and what changed after it."""
    at = now or datetime.utcnow()
    if freeze:
        freeze_due(db, tenant, now=at)
    rows = (db.query(models.ActionOutcome)
            .filter(models.ActionOutcome.tenant_code == tenant)
            .order_by(models.ActionOutcome.id.desc()).limit(50).all())
    actions = {}
    if rows:
        for a in (db.query(models.AgentAction)
                  .filter(models.AgentAction.tenant_code == tenant,
                          models.AgentAction.id.in_([r.action_id for r in rows])).all()):
            actions[a.id] = a
    views = [_row_view(r, actions.get(r.action_id), at) for r in rows]
    counts = {v: sum(1 for x in views if x["verdict"] == v) for v in VERDICTS}
    waiting = sum(1 for x in views if x["waiting"])
    measured = len(views) - waiting

    if not views:
        state, headline = ev.NO_DATA, ("No approved action has been followed up yet. AMP starts "
                                       "measuring the moment one is approved.")
    elif not measured:
        state = ev.INSUFFICIENT_HISTORY
        headline = (f"{waiting} approved action{'s' if waiting != 1 else ''} "
                    f"{'are' if waiting != 1 else 'is'} still inside the window AMP measures. "
                    "Nothing is judged before it has elapsed.")
    else:
        state = ev.OK
        headline = (f"Of {measured} followed up, {counts[BETTER]} got better, {counts[WORSE]} got "
                    f"worse, {counts[NO_CHANGE]} did not move, and {counts[NOT_MEASURABLE]} could "
                    f"not be measured.")
    return {
        "generated_at": at.isoformat(),
        "state": state,
        "headline": headline,
        "window_days": WINDOW_DAYS,
        "followed_up": len(views),
        "waiting": waiting,
        "measured": measured,
        "counts": counts,
        "outcomes": views,
        # On the card, next to the counts, every time.
        "note": CAUSATION_NOTE,
    }


def say_outcomes(summary) -> tuple:
    """One sentence for the Copilot, with the caveat attached to it."""
    if summary["state"] == ev.NO_DATA:
        return summary["headline"], "agentactivity"
    return f"{summary['headline']} {CAUSATION_NOTE}", "agentactivity"
