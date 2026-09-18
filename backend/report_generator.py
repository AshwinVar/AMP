from datetime import datetime

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
    lines = [
        "AMP Daily Factory Intelligence Report",
        f"Generated: {datetime.utcnow().isoformat()} UTC",
        "",
        "Executive Summary",
        "-----------------",
        f"Average OEE: {summary.get('avg_oee', 0)}%",
        f"Availability: {summary.get('avg_availability', 0)}%",
        f"Performance: {summary.get('avg_performance', 0)}%",
        f"Quality: {summary.get('avg_quality', 0)}%",
        f"Total Downtime: {summary.get('total_downtime_minutes', 0)} minutes",
        f"Top Loss Reason: {summary.get('top_loss_reason', 'No data')}",
        f"Worst Machine: {summary.get('worst_machine', 'No data')}",
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
        f"Estimated Downtime Loss: {_downtime_loss(summary)}",
        "",
        "Shift KPIs",
        "----------",
    ]

    if shift_kpis:
        for shift in shift_kpis:
            lines.append(
                f"{shift['shift_name']}: Target={shift['target_output']} | Actual={shift['actual_output']} | Efficiency={shift['efficiency']}% | Gap={shift['gap']}"
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
