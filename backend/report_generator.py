from datetime import datetime


def build_daily_summary_text(summary: dict, shift_kpis: list, alerts: list):
    lines = [
        "FlowMES Daily Factory Intelligence Report",
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
        f"Estimated Downtime Loss: £{summary.get('estimated_loss_value', 0)}",
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
