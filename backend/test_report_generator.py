"""Unit tests for the intelligence-summary text builder (report_generator.py).

build_daily_summary_text renders the downloadable /reports/intelligence-summary.txt
export. Before this file it had NO test coverage.

THE FIX these pin: the estimated downtime loss was rendered inline as
f"{CURRENCY}{value}" — no thousands separator — so a five/six-figure loss printed
"£49740" in this report while the identical figure reads "£49,740" through the
shared money() helper on every card, the weekly report and the scorecard. That is
the exact "one figure, two renderings" inconsistency currency.py exists to kill
(rule-1: reuse the shared helper). The money assertions below compare against
INDEPENDENTLY-derived, comma-grouped strings — never money(x) fed back to itself —
so a regression to the inline form fails here.

Run:  python backend/test_report_generator.py     (exit 0 = pass)
"""
from currency import money
from report_generator import build_daily_summary_text


def _full_summary():
    # A representative summary dict, shaped exactly like build_management_summary's
    # return. Numbers are chosen so the money value has multiple comma groups.
    return {
        "avg_oee": 72,
        "avg_availability": 88,
        "avg_performance": 91,
        "avg_quality": 96,
        "total_downtime_minutes": 340,
        "top_loss_reason": "Mechanical Failure",
        "worst_machine": "Packaging-01",
        "estimated_loss_value": 49740,
    }


def test_loss_value_is_comma_grouped_not_bare_digits():
    # The bug: "£49740" (no separator). The fix: "£49,740" — the SAME string every
    # other money surface prints. The expected literal is hand-written, not money().
    text = build_daily_summary_text(_full_summary(), [], [])
    assert "Estimated Downtime Loss: £49,740" in text, text
    assert "£49740" not in text, "loss printed without a thousands separator"


def test_large_loss_value_gets_every_thousands_separator():
    # A seven-figure loss must group every three digits: 1234567 -> £1,234,567.
    summary = _full_summary()
    summary["estimated_loss_value"] = 1234567
    text = build_daily_summary_text(summary, [], [])
    assert "Estimated Downtime Loss: £1,234,567" in text, text


def test_loss_value_matches_the_shared_money_helper():
    # rule-1: the report's money string must be byte-for-byte what money() produces,
    # so the downloadable report can never drift from the cards again.
    summary = _full_summary()
    summary["estimated_loss_value"] = 802115
    text = build_daily_summary_text(summary, [], [])
    assert f"Estimated Downtime Loss: {money(802115)}" in text, text


def test_zero_loss_renders_a_real_zero():
    summary = _full_summary()
    summary["estimated_loss_value"] = 0
    text = build_daily_summary_text(summary, [], [])
    assert "Estimated Downtime Loss: £0" in text, text


def test_missing_and_none_loss_coalesce_to_zero_pounds():
    # A hand-built summary that omits the key, and one that sets it None, must both
    # render "£0" rather than KeyError / "£None" / a money(None) crash.
    assert "Estimated Downtime Loss: £0" in build_daily_summary_text({}, [], [])
    assert "Estimated Downtime Loss: £0" in build_daily_summary_text(
        {"estimated_loss_value": None}, [], [])


def test_empty_summary_uses_honest_defaults():
    # No summary data at all: percentages fall to 0, labels to "No data" — never a
    # raw None or a KeyError.
    text = build_daily_summary_text({}, [], [])
    assert "Average OEE: 0%" in text
    assert "Availability: 0%" in text
    assert "Total Downtime: 0 minutes" in text
    assert "Top Loss Reason: No data" in text
    assert "Worst Machine: No data" in text


def test_empty_shift_kpis_and_alerts_render_placeholders():
    text = build_daily_summary_text(_full_summary(), [], [])
    assert "No shift data available." in text
    assert "No active alerts." in text


def test_shift_kpis_render_each_row():
    shift_kpis = [
        {"shift_name": "Morning", "target_output": 500, "actual_output": 460,
         "efficiency": 92, "gap": 40},
        {"shift_name": "Night", "target_output": 400, "actual_output": 300,
         "efficiency": 75, "gap": 100},
    ]
    text = build_daily_summary_text(_full_summary(), shift_kpis, [])
    assert "Morning: Target=500 | Actual=460 | Efficiency=92% | Gap=40" in text
    assert "Night: Target=400 | Actual=300 | Efficiency=75% | Gap=100" in text
    assert "No shift data available." not in text


def test_alerts_render_severity_type_machine_and_message():
    alerts = [
        {"type": "Breakdown", "severity": "Critical", "machine": "CNC-01",
         "message": "CNC-01 is currently in breakdown."},
        # An alert with no machine key falls back to "Factory".
        {"type": "Quality Loss", "severity": "Medium",
         "message": "Reject rate is above 5%."},
    ]
    text = build_daily_summary_text(_full_summary(), [], alerts)
    assert "[Critical] Breakdown - CNC-01: CNC-01 is currently in breakdown." in text
    assert "[Medium] Quality Loss - Factory: Reject rate is above 5%." in text
    assert "No active alerts." not in text


def test_header_and_section_structure_present():
    text = build_daily_summary_text(_full_summary(), [], [])
    assert text.startswith("AMP Daily Factory Intelligence Report")
    for section in ("Executive Summary", "Shift KPIs", "Active Alerts"):
        assert section in text, f"missing section: {section}"


if __name__ == "__main__":
    test_loss_value_is_comma_grouped_not_bare_digits()
    test_large_loss_value_gets_every_thousands_separator()
    test_loss_value_matches_the_shared_money_helper()
    test_zero_loss_renders_a_real_zero()
    test_missing_and_none_loss_coalesce_to_zero_pounds()
    test_empty_summary_uses_honest_defaults()
    test_empty_shift_kpis_and_alerts_render_placeholders()
    test_shift_kpis_render_each_row()
    test_alerts_render_severity_type_machine_and_message()
    test_header_and_section_structure_present()
    print("ALL REPORT-GENERATOR TESTS PASSED")
