from datetime import datetime

import oee_contract
from currency import money


def _downtime_loss(summary: dict) -> str:
    """The management summary's downtime loss: £ with the tenant's unit value,
    good units without it, and "unknown" when there was no run time to convert.
    It used to print money(estimated_loss_value or 0): £8 a minute the customer
    never set, or a £0 for a loss that was not zero."""
    value = summary.get("estimated_loss_value")
    units = summary.get("estimated_loss_units")
    if value is not None:
        return money(value)
    if units is not None:
        return f"{units:,} good units (set a unit value to see money)"
    return "unknown (no run time to convert the downtime)"


def build_daily_summary_text(summary: dict, shift_kpis: list, alerts: list):
    # `summary` is /analytics/management's own (reports_routes), pooled over THE
    # window, so every windowed line names it. The report used to print the same
    # labels over ALL history, and a week with no production as "Average OEE:
    # 0%" -- a plant that ran and lost everything
    # (test_intelligence_report_is_the_management_week.py). A hand-built summary
    # without has_data counts as measured only if it carries figures at all.
    days = oee_contract.DEFAULT_WINDOW_DAYS
    if summary.get("has_data", "avg_oee" in summary):
        oee_lines = [
            f"Plant OEE, last {days} days (pooled): {summary.get('avg_oee', 0)}%",
            f"Availability: {summary.get('avg_availability', 0)}%",
            f"Performance: {summary.get('avg_performance', 0)}%",
            f"Quality: {summary.get('avg_quality', 0)}%",
        ]
    else:
        oee_lines = [f"Plant OEE, last {days} days: not measured "
                     "(no production recorded in this window)"]
    lines = [
        "AMP Daily Factory Intelligence Report",
        f"Generated: {datetime.utcnow().isoformat()} UTC",
        "",
        "Executive Summary",
        "-----------------",
        *oee_lines,
        f"Total Downtime, last {days} days: {summary.get('total_downtime_minutes', 0)} minutes",
        f"Top Loss Reason, last {days} days: {summary.get('top_loss_reason', 'No data')}",
        f"Worst Machine, last {days} days: {summary.get('worst_machine', 'No data')}",
        # Format through the shared money() helper (currency.py) — the single money
        # renderer every other surface uses — rather than re-spelling "{CURRENCY}{n}"
        # here. The inline version emitted no thousands separator, so a five/six-figure
        # loss printed as "£49740" in this downloadable report while the exact same
        # figure reads "£49,740" on every card, the weekly report and the scorecard
        # (rule-1: reuse the shared helper, don't render a money value a second way).
        # `... or 0` coalesces a missing/None value to a real £0 (build_management_summary
        # always sets an int, but this keeps the pre-existing `.get(.., 0)` null-safety
        # and avoids money(None) raising on a hand-built summary dict).
        # £ only with the tenant's unit value (ADR-0010); units otherwise, and
        # "unknown" when downtime had no run time to convert.
        f"Estimated Downtime Loss, last {days} days: {_downtime_loss(summary)}",
        "",
        "Shift KPIs (all recorded shifts)",
        "--------------------------------",
    ]

    if shift_kpis:
        for shift in shift_kpis:
            # A shift with no target has no efficiency (analytics_engine.shift_attainment).
            eff = "no target" if shift["efficiency"] is None else f"{shift['efficiency']}%"
            lines.append(
                f"{shift['shift_name']}: Target={shift['target_output']} | Actual={shift['actual_output']} | Efficiency={eff} | Gap={shift['gap']}"
            )
    else:
        lines.append("No shift data available.")

    lines += ["", "Active Alerts", "-------------"]

    if alerts:
        for alert in alerts:
            lines.append(f"[{alert['severity']}] {alert['type']} - {alert.get('machine', 'Factory')}: {alert['message']}")
    else:
        lines.append("No active alerts.")

    return "\n".join(lines)
