"""The existing rule-based failure-risk scorer, run on a MachineHistory.

This is the BASELINE the model must beat. It is the only failure-risk module
that imports ``predictive_engine``, and it adds no scoring logic of its own: it
reduces a history to the same five per-machine counters
``ai.prediction._history_aggregates`` computes in SQL and hands them, with the
machine's status and utilisation, to ``predictive_engine.calculate_predictive_risk``.
test_amp_ai_failure_risk_rule_parity.py pins it against the live path.

``predictive_engine`` imports ``work_order_status``, which imports ``models``
(and so SQLAlchemy). Importing this module therefore loads the ORM layer, but it
never opens a database connection (test_amp_ai_failure_risk_build.py checks the
build does not).

DIFFERENCES FROM THE LIVE ``ai.prediction.assess_from_db`` (documented, intended)
-------------------------------------------------------------------------------
* Window ``[as_of - 30d, as_of)``, bounded at BOTH ends; the live query has no
  upper bound (it runs at "now", where nothing is later).
* Status and utilisation are the last MachineEvent before ``as_of`` (via
  ``history.status_at``), not the Machine row, which a historical as-of cannot
  read. Unknown status -> ``None`` and utilisation ``None``, which the engine
  reads like the Machine column defaults: no status points, utilisation 0.
* ``work_orders=[]``: open-order pressure is not part of a machine's history.
  Both sides of every comparison use the same empty list.

The ``ai`` package cannot be imported here (its ``__init__`` imports about forty
modules), so the window length is restated as ``RULE_WINDOW_DAYS`` and pinned
equal to ``ai.prediction.RISK_WINDOW_DAYS`` by the parity test.
"""
from datetime import timedelta
from types import SimpleNamespace

from duration import parse_duration_to_minutes
from predictive_engine import calculate_predictive_risk

from .history import BREAKDOWN, status_at, truncate

__all__ = ["RULE_WINDOW_DAYS", "rule_aggregates", "rule_row", "rule_score"]

RULE_WINDOW_DAYS = 30


def _in_window(records, start, end):
    return [r for r in records if start <= r[0] < end]


def rule_aggregates(history, as_of) -> dict:
    """The five counters _history_aggregates builds, for this machine, over [as_of - 30d, as_of)."""
    view = history if history.window_end == as_of else truncate(history, as_of)
    start, end = as_of - timedelta(days=RULE_WINDOW_DAYS), as_of
    downtime = _in_window(view.downtime, start, end)
    events = _in_window(view.events, start, end)
    production = _in_window(view.production, start, end)
    return {
        "downtime_minutes": sum(parse_duration_to_minutes(duration) for _, _, duration in downtime),
        "downtime_events": len(downtime),
        "breakdown_events": (sum(1 for _, reason, _ in downtime if str(reason).lower() == "breakdown")
                             + sum(1 for e in events if e[2] == BREAKDOWN)),
        "rejects": sum(int(r[6] or 0) for r in production),
        "totals": sum(int(r[4] or 0) for r in production),
    }


def rule_row(history, as_of) -> dict:
    """The engine's full row (score, level, reasons) for this machine at ``as_of``."""
    view = history if history.window_end == as_of else truncate(history, as_of)
    state = status_at(view, as_of)
    machine = SimpleNamespace(id=history.machine_id, name=history.name,
                              status=state[0] if state else None,
                              utilization=state[1] if state else None)
    counters = rule_aggregates(view, as_of)
    aggregates = {key: {history.machine_id: value} for key, value in counters.items()}
    rows = calculate_predictive_risk([machine], (), (), (), [], aggregates=aggregates)
    return rows[0]


def rule_score(history, as_of) -> float:
    """The rule scorer's 0-100 risk score, as a float."""
    return float(rule_row(history, as_of)["risk_score"])
