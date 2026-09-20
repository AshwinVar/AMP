"""The Root-Cause Explorer: where the output went, and what AMP cannot explain (ADR-0025).

"Why are we behind?" has an honest answer and a dishonest one. The dishonest one
picks the biggest number on the screen and calls it the cause. The honest one
says: here is the gap, here are the losses AMP can MEASURE in the same window,
here is how much of the gap they account for, and here is the part nothing in
the data explains.

WHAT IS MEASURED (each a mechanism, not an opinion)

  Quality       units rejected. A rejected unit is a unit not made good. This is
                arithmetic over recorded counts.
  Performance   the line ran slower than its own ideal cycle time: the units the
                runtime could have produced at that cycle, minus the units it
                did. Arithmetic over recorded runtime and counts.
  Availability  planned time that was not running, in minutes, and the units that
                time could have produced at the ideal cycle. Split into the
                minutes that HAVE a logged stoppage reason and the minutes that
                do not.

WHAT IS LABELLED, AND HOW (ai/evidence.py)

  CAUSE CONFIRMED        the loss is measured, on this plant, in this window, by
                         a mechanism that is arithmetic: scrap, slow running, and
                         the minutes a logged stoppage actually took.
  LIKELY CONTRIBUTOR     measured on the plant, attributed to a machine or a
                         reason by a rule (a stoppage reason's share of minutes).
  CORRELATED EVENT       something true in the same window with no measured link
                         to this gap: an item out of stock, an overdue task.
  INSUFFICIENT EVIDENCE  planned time that was not running with no reason logged,
                         and the part of the gap nothing above accounts for.

THE TWO DENOMINATORS, SAID OUT LOUD. The plan gap is measured against the PLAN.
The losses are measured against the plant's IDEAL CYCLE. They are not the same
denominator: a plan can be missed while the machines that ran were at full speed,
and a machine can lose capacity on a day nothing was planned. So the explorer
reports both, states the share of the gap the measured losses would cover, and
never presents that share as an accounting identity.

Composes existing read-models and the production records they already read; adds
no storage and no new figure of its own.
"""
from collections import defaultdict
from datetime import datetime

import models
import oee_contract
from ai import evidence as ev
from ai.cost import build_cost_summary
from ai.inventory import build_inventory_summary
from ai.maintenance import build_maintenance_summary
from ai.schedule import build_schedule_adherence
from ai.twin import _recent_production
from currency import CURRENCY
from duration import parse_duration_to_minutes

name = "root_cause"

M, D, R, U = ev.MEASURED, ev.DERIVED, ev.RULE, ev.UNKNOWN
WINDOW_DAYS = 7
W7 = f"last {WINDOW_DAYS} days"
TOP_REASONS = 5


def _fact(key, label, value, prov, unit="", source="", window=W7, detail=""):
    return ev.Fact(key=key, label=label, value=value, provenance=prov, unit=unit, source=source,
                   window=window, detail=detail).to_dict(key)


def _ideal_rate(records):
    """Units per minute at the ideal cycle, weighted by the runtime each record
    ran. None when no record carries an ideal cycle time: no rate, no estimate."""
    minutes = sum(r.runtime_minutes or 0 for r in records if (r.ideal_cycle_time_seconds or 0) > 0)
    if not minutes:
        return None
    units = sum((r.runtime_minutes or 0) * 60 / r.ideal_cycle_time_seconds
                for r in records if (r.ideal_cycle_time_seconds or 0) > 0)
    return units / minutes


def _mechanisms(records):
    """The three measured mechanisms, per machine and in total. Pure arithmetic
    over the records the OEE contract already reads."""
    per_machine = defaultdict(lambda: {"quality": 0, "performance": 0, "availability_minutes": 0,
                                       "availability_units": 0, "planned": 0, "runtime": 0})
    for r in records:
        mid = r.machine_id
        row = per_machine[mid]
        planned, runtime = r.planned_minutes or 0, r.runtime_minutes or 0
        ideal = r.ideal_cycle_time_seconds or 0
        row["planned"] += planned
        row["runtime"] += runtime
        row["quality"] += r.rejected_count or 0
        if ideal > 0:
            row["performance"] += max(0, int(runtime * 60 / ideal) - (r.total_count or 0))
            lost = max(0, planned - runtime)
            row["availability_minutes"] += lost
            row["availability_units"] += int(lost * 60 / ideal)
        else:
            row["availability_minutes"] += max(0, planned - runtime)
    return per_machine


def _logged_downtime(db, tenant, window):
    """(minutes by machine, minutes by reason, total) from the stoppage log, over
    the SAME window the production records cover."""
    logs = (db.query(models.DowntimeLog)
            .filter(models.DowntimeLog.tenant_code == tenant,
                    models.DowntimeLog.created_at >= window.start,
                    models.DowntimeLog.created_at < window.end).all())
    by_machine, by_reason, total = defaultdict(int), defaultdict(int), 0
    for d in logs:
        minutes = parse_duration_to_minutes(d.duration)
        total += minutes
        by_machine[d.machine_id] += minutes
        by_reason[(d.reason or "Unknown").strip() or "Unknown"] += minutes
    return by_machine, by_reason, total


def _contributor(key, label, mechanism, units, cause_label, basis, facts, minutes=None, money=None):
    return {"key": key, "label": label, "mechanism": mechanism, "units": units, "minutes": minutes,
            "money": money, "cause_label": cause_label, "basis": basis, "facts": facts}


def explain_production_gap(db, tenant: str, now=None) -> dict:
    """Why the plant is behind: the gap, the measured losses, and the remainder
    nothing explains. Production records and stoppages are read for THIS tenant."""
    window = oee_contract.OeeWindow(WINDOW_DAYS, now=now)
    records = _recent_production(db, days=WINDOW_DAYS, now=window.end)
    plan = build_schedule_adherence(db, tenant)
    cost = build_cost_summary(db, tenant)
    unit_value = cost["unit_value_gbp"] if cost["priced"] else None
    machine_names = {m.id: m.name for m in
                     db.query(models.Machine).filter(models.Machine.tenant_code == tenant).all()}

    gap_state = ev.OK
    if not plan["total"]:
        gap_state = ev.NOT_CONFIGURED
    elif not plan["planned_units"]:
        gap_state = ev.INSUFFICIENT_HISTORY
    gap_units = max(0, plan["planned_units"] - plan["actual_units"]) if gap_state == ev.OK else None

    if not records:
        return {
            "generated_at": (now or datetime.utcnow()).isoformat(), "days": WINDOW_DAYS,
            "state": ev.NO_DATA,
            "headline": "No production was recorded in this window, so there is nothing to explain yet.",
            "gap": {"state": gap_state, "planned_units": plan["planned_units"],
                    "actual_units": plan["actual_units"], "gap_units": gap_units},
            "contributors": [], "by_machine": [], "measured_loss_units": 0, "attributed_units": 0,
            "unattributed_units": 0, "measured_loss_money": None, "attributed_money": None,
            "unexplained_units": gap_units, "attributed_share_of_gap": None, "currency": None,
            "denominator_note": _DENOMINATOR_NOTE, "facts": [],
        }

    per_machine = _mechanisms(records)
    by_machine_minutes, by_reason_minutes, logged_minutes = _logged_downtime(db, tenant, window)
    rate = _ideal_rate(records)

    quality_units = sum(row["quality"] for row in per_machine.values())
    performance_units = sum(row["performance"] for row in per_machine.values())
    availability_minutes = sum(row["availability_minutes"] for row in per_machine.values())
    availability_units = sum(row["availability_units"] for row in per_machine.values())
    logged_share = min(1.0, logged_minutes / availability_minutes) if availability_minutes else 0.0
    logged_units = int(round(availability_units * logged_share))
    unlogged_minutes = max(0, availability_minutes - logged_minutes)

    def money(units):
        return round(units * unit_value) if unit_value is not None and units is not None else None

    contributors = []
    if performance_units:
        contributors.append(_contributor(
            "performance", "Slow running", "performance", performance_units, ev.CAUSE_CONFIRMED,
            "the units the runtime could have produced at the machines' own ideal cycle, minus the units made",
            [_fact("rc.performance_units", "Units lost to slow running", performance_units, D, "units",
                   "production_records")],
            money=money(performance_units)))
    if quality_units:
        contributors.append(_contributor(
            "quality", "Scrap and rework", "quality", quality_units, ev.CAUSE_CONFIRMED,
            "units recorded as rejected; a rejected unit is a unit not made good",
            [_fact("rc.quality_units", "Units rejected", quality_units, M, "units", "production_records")],
            money=money(quality_units)))
    if logged_minutes:
        top = sorted(by_reason_minutes.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_REASONS]
        facts = [_fact("rc.logged_minutes", "Stoppage minutes logged", logged_minutes, M, "min", "downtime_logs")]
        for i, (reason, minutes) in enumerate(top, 1):
            facts.append(_fact(f"rc.reason_{i}", f"{reason}: minutes", minutes, M, "min", "downtime_logs"))
        contributors.append(_contributor(
            "downtime_logged", "Logged stoppages", "availability", logged_units, ev.CAUSE_CONFIRMED,
            "minutes recorded against a stoppage reason, valued at the plant's ideal cycle",
            facts, minutes=logged_minutes, money=money(logged_units)))
        for i, (reason, minutes) in enumerate(top, 1):
            share = minutes / logged_minutes if logged_minutes else 0
            units = int(round(logged_units * share))
            contributors.append(_contributor(
                f"reason.{reason.lower().replace(' ', '_')}", reason, "availability", units,
                ev.LIKELY_CONTRIBUTOR,
                "this reason's share of the logged stoppage minutes, valued at the plant's ideal cycle",
                [_fact(f"rc.reason_{i}_minutes", f"{reason}: minutes", minutes, M, "min", "downtime_logs")],
                minutes=minutes, money=money(units)))
    if unlogged_minutes:
        units = int(round(unlogged_minutes * rate)) if rate else None
        contributors.append(_contributor(
            "downtime_unlogged", "Planned time not running, with no reason logged", "availability", units,
            ev.INSUFFICIENT_EVIDENCE,
            "planned minutes that were not runtime and carry no stoppage reason; AMP cannot say what happened",
            [_fact("rc.unlogged_minutes", "Unexplained non-running minutes", unlogged_minutes, M, "min",
                   "production_records minus downtime_logs")],
            minutes=unlogged_minutes, money=money(units)))

    # Things true in the same window with NO measured link to this gap.
    stock = build_inventory_summary(db, tenant)
    if stock["out_of_stock"]:
        lead = stock["items"][0]["item_name"] if stock["items"] else ""
        contributors.append(_contributor(
            "stock.out", f"{stock['out_of_stock']} item(s) out of stock", "supply", None, ev.CORRELATED_EVENT,
            "true in this window; AMP has no measured link from this shortage to the units not made",
            [_fact("rc.stock_out", "Items out of stock", stock["out_of_stock"], M, "items", "inventory_items",
                   "now", detail=lead)]))
    maint = build_maintenance_summary(db, tenant)
    if maint["overdue"]:
        contributors.append(_contributor(
            "maintenance.overdue", f"{maint['overdue']} maintenance task(s) overdue", "maintenance", None,
            ev.CORRELATED_EVENT,
            "true in this window; AMP has no measured link from overdue maintenance to the units not made",
            [_fact("rc.maint_overdue", "Overdue maintenance tasks", maint["overdue"], D, "tasks",
                   "maintenance_tasks", "now")]))

    # TWO DIFFERENT IDEAS, KEPT APART. What AMP can MEASURE as lost capacity, and
    # how much of that has a RECORDED REASON. The first version reported only the
    # second and called it "explained", so a plant whose stoppages are mostly
    # unlogged read "82% of the gap explained" while the largest block of lost
    # time had no reason at all. For an SME plant, that block IS the finding.
    attributed_units = performance_units + quality_units + logged_units
    unattributed_units = max(0, availability_units - logged_units)
    measured_loss_units = performance_units + quality_units + availability_units
    unexplained = max(0, gap_units - attributed_units) if gap_units is not None else None
    share = round(min(100, attributed_units / gap_units * 100)) if gap_units else None

    by_machine = sorted(
        ({"machine_id": mid, "machine": machine_names.get(mid, f"#{mid}"),
          "quality_units": row["quality"], "performance_units": row["performance"],
          "availability_minutes": row["availability_minutes"],
          "logged_minutes": by_machine_minutes.get(mid, 0),
          "units": row["quality"] + row["performance"]
                   + int(round(row["availability_units"]
                               * min(1.0, (by_machine_minutes.get(mid, 0) / row["availability_minutes"])
                                     if row["availability_minutes"] else 0)))}
         for mid, row in per_machine.items()),
        key=lambda m: -m["units"])

    facts = [
        _fact("rc.gap_units", "Units short of the plans that came due", gap_units, D, "units", "production_plans")
        if gap_units is not None else
        _fact("rc.gap_units", "Units short of plan", None, U, "units", "production_plans",
              detail=("no production plan is set, so there is no gap to explain"
                      if gap_state == ev.NOT_CONFIGURED else "no plan has come due yet in this window")),
        _fact("rc.measured_loss_units", "Lost capacity AMP can measure", measured_loss_units, D, "units",
              "production_records + downtime_logs"),
        _fact("rc.attributed_units", "...of which a reason is recorded", attributed_units, D, "units",
              "production_records + downtime_logs"),
        _fact("rc.unattributed_units", "...with no reason recorded", unattributed_units, U, "units",
              "production_records minus downtime_logs",
              detail="planned time that was not running and carries no stoppage reason"),
    ]
    if unexplained is not None:
        facts.append(_fact("rc.unexplained_units", "Units the data does not explain", unexplained, U, "units",
                           "production_plans vs measured losses",
                           detail="measured losses do not account for this part of the gap"))

    # PARTIAL DATA means what it means everywhere else in AMP: part of the plant
    # did not report (OEE contract s4). Unlogged stoppage REASONS are a different
    # thing -- the card reports them as their own figure, labelled UNKNOWN -- and
    # folding them into this state would put "part of the plant did not report"
    # in front of a plant where every machine reported and nobody typed a reason.
    coverage = oee_contract.coverage(db, tenant, window)
    state = ev.OK if coverage.get("complete") else ev.PARTIAL_DATA
    return {
        "generated_at": (now or datetime.utcnow()).isoformat(), "days": WINDOW_DAYS,
        # The card's own data state is about DATA (did every machine report), and
        # the gap carries its own state (is there a plan at all). Conflating them
        # hid a factory's missing machine behind "no plan is set".
        "state": state,
        "headline": _headline(gap_units, gap_state, measured_loss_units, attributed_units,
                              unattributed_units, contributors),
        "gap": {"state": gap_state, "planned_units": plan["planned_units"], "actual_units": plan["actual_units"],
                "gap_units": gap_units, "gap_money": money(gap_units)},
        "coverage": coverage,
        "contributors": contributors,
        "by_machine": by_machine,
        "measured_loss_units": measured_loss_units,
        "attributed_units": attributed_units,
        "unattributed_units": unattributed_units,
        "measured_loss_money": money(measured_loss_units),
        "attributed_money": money(attributed_units),
        "unexplained_units": unexplained,
        "attributed_share_of_gap": share,
        "currency": CURRENCY if unit_value is not None else None,
        "denominator_note": _DENOMINATOR_NOTE,
        "facts": facts,
    }


_DENOMINATOR_NOTE = (
    "The gap is measured against the plan; the losses are measured against the machines' own ideal cycle. "
    "They are different denominators, so the share below is an indication of size, not an accounting identity."
)


def _headline(gap_units, gap_state, measured, attributed, unattributed, contributors):
    """What AMP measured, how much of it has a reason, and what is still missing."""
    biggest = next((c for c in contributors if c["cause_label"] == ev.CAUSE_CONFIRMED and c["units"]), None)
    lead = f"biggest: {biggest['label'].lower()}, {biggest['units']:,} units" if biggest else \
        "no single measured loss stands out"
    measured_part = (f"AMP measured {measured:,} units of lost capacity in the window: {attributed:,} with a "
                     f"reason recorded ({lead})")
    if unattributed:
        measured_part += (f", and {unattributed:,} with none \u2014 planned time that was not running and "
                          "carries no stoppage reason")
    if gap_state == ev.NOT_CONFIGURED:
        return f"No production plan is set, so there is no gap to explain. {measured_part}."
    if gap_units is None:
        return f"No plan has come due yet in this window. {measured_part}."
    if not gap_units:
        return f"The plans that came due were met. {measured_part}."
    return f"{gap_units:,} units short of the plans that came due. {measured_part}."
