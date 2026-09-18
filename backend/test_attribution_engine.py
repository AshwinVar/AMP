"""The downtime attribution engine: every covered second into exactly one bucket (ADR-0021).

WHAT THIS PINS
--------------
attribution_engine.attribute is a PURE function: spans, reason logs, disputes
and covered intervals in; attributed segments out. No database, no clock.

  * Every covered second lands in exactly one of AVAILABLE, OEM, FACTORY,
    DISPUTED, UNMEASURED, and the segments tile the covered time exactly. A
    randomized run compares the engine with a per-second reference oracle
    written from the rules below, independently of the engine's sweep.
  * No trusted span holding means UNMEASURED "no_telemetry": never uptime and
    never downtime. A span holds for SPAN_GAP_SECONDS after its last message,
    and never past the next span of the same source.
  * Two holding spans that disagree about availability are DISPUTED.
  * A down episode is attributed by the factory's OWN reasons logged from
    `reason_lead_seconds` before it to its end; generic reasons (MQTT's
    automatic "Breakdown", "Unknown") explain nothing; no explicit reason uses
    the agreed status default; an unmapped reason or reasons for both parties
    is DISPUTED.
  * A resolved dispute overrides everything in its window, an open one makes
    it DISPUTED, and time from the moment an installation was unlinked is
    UNMEASURED "installation_unlinked".
  * Evidence is an allowlist: span id/source/status/start/end clipped to the
    period, reason id/at/text, dispute id/status/resolution bucket, linkage.
  * The engine REFUSES inputs its caller should never hand it (an untrusted
    source, a span or reason outside the read window, a fractional second,
    overlapping disputes): a wrong read is loud, not a quiet wrong statement.

Run: DATABASE_URL="sqlite:///./ci.db" python test_attribution_engine.py
"""
import ast
import io
import os
import random
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import attribution_engine as ae  # noqa: E402
import canonical  # noqa: E402
import contract_money as cm  # noqa: E402
import contract_terms as ct  # noqa: E402
from attribution_engine import (AttributionError, DisputeInput, MachineInput,  # noqa: E402
                                ReasonLog, SpanInput)
from contract_periods import Period  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
GAP = 300
T0 = datetime(2026, 3, 1)
P = Period(T0, datetime(2026, 4, 1))


def at(seconds):
    return T0 + timedelta(seconds=seconds)


def terms(reason_map=None, status_defaults=None, trusted=("mqtt",), lead=600,
          generic=("breakdown", "unknown"), coverage=None, installations=(1, 2)):
    return ct.parse({
        "schema": 1, "currency": "INR", "period_months": 1, "period_fee": "40000.00",
        "timezone": "UTC", "term_months": 12, "coverage": coverage or {"mode": "24x7"},
        "sla_target_pct": "97.00",
        "credit_tiers": [{"below_pct": "97.00", "credit_pct": "5.00"}],
        "min_measured_pct": "90.00", "trusted_sources": list(trusted),
        "status_defaults": status_defaults or {"Breakdown": "OEM", "Maintenance": "FACTORY",
                                               "Offline": "DISPUTED"},
        "reason_map": reason_map if reason_map is not None else {
            "no material": "FACTORY", "power failure": "FACTORY",
            "motor overheating": "OEM"},
        "generic_reasons": list(generic), "reason_lead_seconds": lead,
        "termination_notice_days": 30,
        "covered_installations": [{"installation_id": i, "serial_number": f"S{i}"}
                                  for i in installations],
    })


def span(sid, status, start, end, source="mqtt"):
    return SpanInput(id=sid, source=source, status=status, start=at(start), end=at(end))


def log(lid, when, reason):
    return ReasonLog(id=lid, at=at(when), reason=reason)


def machine(spans=(), reasons=(), inst=1, ended=None, end_reason=None):
    return MachineInput(installation_id=inst, serial_number=f"S{inst}",
                        coverage_ended_at=None if ended is None else at(ended),
                        coverage_end_reason=end_reason, spans=tuple(spans),
                        reasons=tuple(reasons))


def dispute(did, start, end, status="open", bucket=None, inst=1):
    return DisputeInput(id=did, installation_id=inst, status=status, start=at(start),
                        end=at(end), resolution_bucket=bucket)


def run(machines, covered=None, disputes=(), t=None, period=P, gap=GAP):
    t = t or terms()
    covered = covered if covered is not None else [(period.start, period.end)]
    return ae.attribute(list(machines), covered, t, list(disputes), period=period,
                        gap_seconds=gap)


def spans_at(segments, inst=1):
    return [(int((s.start - T0).total_seconds()), int((s.end - T0).total_seconds()),
             s.bucket, s.cause) for s in segments if s.installation_id == inst]


def refused(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except AttributionError as e:
        return str(e)
    return None


def secs(segment):
    return (segment.end - segment.start) // timedelta(seconds=1)


# --------------------------------------------------------------------------
# 1. telemetry: holds, gaps, conflicts, statuses
# --------------------------------------------------------------------------

def test_no_spans_is_all_unmeasured_no_telemetry():
    out = run([machine()])
    assert [(s.bucket, s.cause) for s in out] == [("UNMEASURED", "no_telemetry")], out
    assert out[0].start == P.start and out[0].end == P.end
    assert out[0].evidence == {"telemetry_spans": []}, out[0].evidence
    totals = ae.bucket_totals(out)
    assert totals["unmeasured_seconds"] == totals["covered_seconds"] == 31 * 86400, totals
    sla = cm.evaluate_sla(totals, terms())
    assert sla["sla"]["availability_pct"] is None and sla["credit"]["amount"] is None, sla
    assert sla["sla"]["state"] == "not_evaluable", sla
    print("PASS no spans: the whole period is UNMEASURED no_telemetry; availability and "
          "credit None, never 0")


def test_a_span_holds_for_the_gap_and_no_longer():
    last = 31 * 86400
    out = run([machine([span(1, "Running", 0, 1000), span(2, "Running", 1400, last)])])
    got = spans_at(out)
    assert got == [(0, 1300, "AVAILABLE", "status:Running"),
                   (1300, 1400, "UNMEASURED", "no_telemetry"),
                   (1400, last, "AVAILABLE", "status:Running")], got
    # Exactly at the tolerance there is no gap: [0, 1300) then 1300 onward.
    out = run([machine([span(1, "Running", 0, 1000), span(2, "Running", 1300, last)])])
    # (Two segments, not one: their evidence names different spans.)
    assert [(a, b, k) for a, b, k, _ in spans_at(out)] == [(0, 1300, "AVAILABLE"),
                                                           (1300, last, "AVAILABLE")], \
        spans_at(out)
    print("PASS a span holds SPAN_GAP_SECONDS past its last message; past that is "
          "UNMEASURED, at exactly the tolerance it is not")


def test_a_gap_while_broken_down_is_unmeasured_not_downtime():
    last = 31 * 86400
    out = run([machine([span(1, "Breakdown", 0, 1000), span(2, "Breakdown", 5000, last)])])
    got = spans_at(out)
    assert got[1] == (1300, 5000, "UNMEASURED", "no_telemetry"), got
    assert got[0][2] == "OEM" and got[2][2] == "OEM", got
    print("PASS a gap while in Breakdown is UNMEASURED, not OEM downtime")


def test_a_hold_stops_at_the_next_span_of_the_same_source_only():
    last = 31 * 86400
    out = run([machine([span(1, "Running", 0, 1000), span(2, "Breakdown", 1100, last)])])
    assert spans_at(out)[:2] == [(0, 1100, "AVAILABLE", "status:Running"),
                                 (1100, last, "OEM", "status_default:Breakdown")], \
        spans_at(out)
    # Another source's span does not cut mqtt's hold: both hold, and they disagree.
    t = terms(trusted=("iot", "mqtt"))
    out = run([machine([span(1, "Running", 0, 1000),
                        span(2, "Breakdown", 1100, last, source="iot")])], t=t)
    assert spans_at(out)[:3] == [(0, 1100, "AVAILABLE", "status:Running"),
                                 (1100, 1300, "DISPUTED", "conflicting_status"),
                                 (1300, last, "OEM", "status_default:Breakdown")], \
        spans_at(out)
    print("PASS a hold ends at the next span of its OWN source; a second source that "
          "disagrees makes the overlap DISPUTED conflicting_status")


def test_statuses_idle_available_unrecognised_disputed():
    last = 31 * 86400
    out = run([machine([span(1, "Idle", 0, 1000), span(2, "Weird", 1000, 2000),
                        span(3, "Maintenance", 2000, 3000), span(4, "Offline", 3000, last)])])
    assert [(k, c) for _, _, k, c in spans_at(out)] == [
        ("AVAILABLE", "status:Idle"), ("DISPUTED", "unrecognised_status"),
        ("FACTORY", "status_default:Maintenance"), ("DISPUTED", "status_default:Offline")], \
        spans_at(out)
    # Two sources, Running and Idle: available either way, nothing to dispute.
    t = terms(trusted=("iot", "mqtt"))
    out = run([machine([span(1, "Running", 0, last), span(2, "Idle", 0, last, "iot")])], t=t)
    assert [(k, c) for _, _, k, c in spans_at(out)] == [("AVAILABLE", "status:Idle+Running")]
    # Two sources, Breakdown and Maintenance: different parties by default.
    out = run([machine([span(1, "Breakdown", 0, last),
                        span(2, "Maintenance", 0, last, "iot")])], t=t)
    assert [(k, c) for _, _, k, c in spans_at(out)] == [("DISPUTED", "conflicting_status")]
    print("PASS Idle is AVAILABLE; an unrecognised status is DISPUTED; Running vs Idle is "
          "AVAILABLE; two different down statuses are DISPUTED")


# --------------------------------------------------------------------------
# 2. reasons for a down episode
# --------------------------------------------------------------------------

def _episode(reasons, t=None, status="Breakdown"):
    """The episode under test is `status` on [10000, 20000), between Running.

    Breakdowns at [1000, 1800) and [25000, 25800) widen the reason READ window
    to [400, 25800), so a reason just outside the episode under test is read
    and must be ignored by the rule (the engine refuses reasons outside the
    read window altogether)."""
    last = 31 * 86400
    out = run([machine([span(1, "Breakdown", 1000, 1500), span(2, "Running", 2000, 9990),
                        span(3, status, 10000, 19700), span(4, "Running", 20000, 24990),
                        span(5, "Breakdown", 25000, 25500), span(6, "Running", 26000, last)],
                       reasons)], t=t)
    down = [x for x in spans_at(out) if x[0] == 10000]
    assert down and down[0][1] == 20000, spans_at(out)
    return down[0][2], down[0][3], [s for s in out if s.start == at(10000)][0]


def test_status_defaults_apply_without_an_explicit_reason():
    assert _episode([])[:2] == ("OEM", "status_default:Breakdown")
    assert _episode([], status="Maintenance")[:2] == ("FACTORY", "status_default:Maintenance")
    assert _episode([], status="Offline")[:2] == ("DISPUTED", "status_default:Offline")
    # MQTT's automatic reason and the normaliser's Unknown are generic.
    assert _episode([log(1, 10000, "Breakdown"), log(2, 10001, "  unknown ")])[:2] == \
        ("OEM", "status_default:Breakdown")
    print("PASS no explicit reason uses status_defaults; MQTT's auto 'Breakdown' and "
          "'Unknown' are generic, not explicit")


def test_a_mapped_reason_attributes_the_episode():
    bucket, cause, seg = _episode([log(7, 9400, "No Material")])
    assert (bucket, cause) == ("FACTORY", "reason:no material"), (bucket, cause)
    assert seg.evidence["downtime_logs"] == [
        {"id": 7, "at": canonical.ts(at(9400)), "reason": "No Material"}], seg.evidence
    assert _episode([log(7, 15000, "  NO MATERIAL")])[:2] == ("FACTORY", "reason:no material")
    assert _episode([log(7, 15000, "motor overheating"),
                     log(8, 16000, "Motor Overheating")])[:2] == \
        ("OEM", "reason:motor overheating")
    assert _episode([log(7, 15000, "no material"), log(8, 16000, "power failure")])[:2] == \
        ("FACTORY", "reason:no material; power failure")
    print("PASS a reason in the lead window or the episode attributes it to its mapped "
          "party; the raw text is the evidence")


def test_the_reason_window_is_half_open_and_bounded():
    assert _episode([log(7, 9400, "no material")])[0] == "FACTORY"        # start - lead
    assert _episode([log(7, 9399, "no material")])[0] == "OEM"            # before the window
    assert _episode([log(7, 19999, "no material")])[0] == "FACTORY"
    assert _episode([log(7, 20000, "no material")])[0] == "OEM"           # after the episode
    print("PASS candidates are [episode_start - lead, episode_end): one second outside "
          "either edge is ignored")


def test_unmapped_and_conflicting_reasons_are_disputed():
    assert _episode([log(7, 15000, "operator on lunch")])[:2] == ("DISPUTED", "unmapped_reason")
    assert _episode([log(7, 15000, "no material"), log(8, 15001, "motor overheating")])[:2] \
        == ("DISPUTED", "conflicting_reasons")
    assert _episode([log(7, 15000, "no material"), log(8, 15001, "typo materail")])[:2] \
        == ("DISPUTED", "unmapped_reason")
    print("PASS an unmapped reason is DISPUTED unmapped_reason; reasons for both parties "
          "are DISPUTED conflicting_reasons")


def test_an_episode_is_split_exactly_at_covered_hour_and_period_edges():
    # Weekly coverage 08:00-20:00 every day, UTC. Breakdown 19:00 on day 0 to
    # 09:00 on day 1; the reason is logged at 21:00, OUTSIDE covered hours, but
    # inside the episode, so it still attributes both covered pieces.
    t = terms(coverage={"mode": "weekly", "windows": [
        {"days": [0, 1, 2, 3, 4, 5, 6], "start": "08:00", "end": "20:00"}]})
    import contract_periods as cp
    covered = cp.covered_intervals(P, t, P.start, P.end)
    h = 3600
    out = run([machine([span(1, "Running", 8 * h, 19 * h),
                        span(2, "Breakdown", 19 * h, 33 * h - GAP),
                        span(3, "Running", 33 * h, 31 * 86400)],
                       [log(5, 21 * h, "no material")])], covered=covered, t=t)
    got = spans_at(out)[:3]
    assert got == [(8 * h, 19 * h, "AVAILABLE", "status:Running"),
                   (19 * h, 20 * h, "FACTORY", "reason:no material"),
                   (32 * h, 33 * h, "FACTORY", "reason:no material")], got
    # An episode running past the period end is clipped to it.
    short = Period(T0, at(20000))
    out = run([machine([span(1, "Breakdown", 10000, 30000)])], t=terms(), period=short)
    assert spans_at(out)[-1] == (10000, 20000, "OEM", "status_default:Breakdown")
    print("PASS an episode crossing covered-hour edges is split exactly and keeps one "
          "decision; one crossing the period end is clipped")


# --------------------------------------------------------------------------
# 3. disputes and linkage
# --------------------------------------------------------------------------

def test_disputes_override_in_their_clipped_window():
    last = 31 * 86400
    out = run([machine()], disputes=[dispute(9, 100, 200, "resolved", "OEM")])
    assert spans_at(out) == [(0, 100, "UNMEASURED", "no_telemetry"),
                             (100, 200, "OEM", "agreed_override:#9"),
                             (200, last, "UNMEASURED", "no_telemetry")], spans_at(out)
    assert out[1].evidence == {"disputes": [{"id": 9, "status": "resolved",
                                             "resolution_bucket": "OEM"}]}, out[1].evidence
    out = run([machine([span(1, "Running", 0, last)])],
              disputes=[dispute(4, 100, 200, "open"), dispute(5, 300, 400, "resolution_proposed"),
                        dispute(6, 500, 600, "withdrawn"), dispute(7, 700, 800, inst=2)])
    assert [(a, b, k, c) for a, b, k, c in spans_at(out) if k == "DISPUTED"] == [
        (100, 200, "DISPUTED", "open_dispute:#4"),
        (300, 400, "DISPUTED", "open_dispute:#5")], spans_at(out)
    # Clipped to covered time.
    out = run([machine()], covered=[(at(0), at(150))],
              disputes=[dispute(9, 100, 99999, "resolved", "AVAILABLE")])
    assert spans_at(out) == [(0, 100, "UNMEASURED", "no_telemetry"),
                             (100, 150, "AVAILABLE", "agreed_override:#9")], spans_at(out)
    print("PASS a resolved dispute overrides UNMEASURED; open and resolution-proposed are "
          "DISPUTED; withdrawn and other installations' disputes change nothing; clipped")


def test_dispute_inputs_are_refused_when_they_cannot_be_applied():
    m = [machine()]
    assert refused(run, m, disputes=[dispute(1, 0, 100), dispute(2, 50, 150)])
    assert refused(run, m, disputes=[dispute(1, 0, 100, "resolved", "DISPUTED")])
    assert refused(run, m, disputes=[dispute(1, 0, 100, "resolved", None)])
    assert refused(run, m, disputes=[dispute(1, 100, 100)])
    assert refused(run, m, disputes=[dispute(1, 0, 100, "pending")])
    # Touching disputes are not overlapping; a withdrawn one may overlap anything.
    assert not refused(run, m, disputes=[dispute(1, 0, 100), dispute(2, 100, 200),
                                         dispute(3, 50, 150, "withdrawn")])
    print("PASS overlapping disputes, a DISPUTED or missing resolution, an empty window "
          "and an unknown status are refused, not guessed")


def test_time_after_the_installation_was_unlinked_is_unmeasured():
    last = 31 * 86400
    out = run([machine([span(1, "Running", 0, 5000)], ended=4000, end_reason="machine_changed")])
    assert spans_at(out) == [(0, 4000, "AVAILABLE", "status:Running"),
                             (4000, last, "UNMEASURED", "installation_unlinked")], spans_at(out)
    assert out[1].evidence == {"linkage": {"coverage_ended_at": canonical.ts(at(4000)),
                                           "reason": "machine_changed"}}, out[1].evidence
    # Unlinked before the period: all of it.
    out = run([machine(ended=-10, end_reason="factory_changed")])
    assert [(k, c) for _, _, k, c in spans_at(out)] == [("UNMEASURED", "installation_unlinked")]
    # Unlinked after the period: nothing changes, and no linkage evidence.
    a = run([machine([span(1, "Running", 0, last)])])
    b = run([machine([span(1, "Running", 0, last)], ended=last + 5, end_reason="x")])
    assert a == b, (a, b)
    # A resolved dispute still overrides unlinked time.
    out = run([machine(ended=0, end_reason="x")], disputes=[dispute(3, 10, 20, "resolved", "OEM")])
    assert spans_at(out)[1] == (10, 20, "OEM", "agreed_override:#3"), spans_at(out)
    print("PASS from coverage_ended_at on, covered time is UNMEASURED installation_unlinked; "
          "a stamp after the period changes nothing")


# --------------------------------------------------------------------------
# 4. evidence, merging, read windows, refusals
# --------------------------------------------------------------------------

def test_span_evidence_is_clipped_and_extending_past_the_period_changes_nothing():
    last = 31 * 86400
    a = run([machine([span(11, "Running", -250, last - 100)])])
    assert a[0].evidence == {"telemetry_spans": [
        {"id": 11, "source": "mqtt", "status": "Running",
         "start": canonical.ts(at(-250)), "end": canonical.ts(at(last - 100))}]}, a[0].evidence
    b = run([machine([span(11, "Running", -250, last + 9999)])])
    c = run([machine([span(11, "Running", -250, last + 1)])])
    assert b == c and b[0].evidence["telemetry_spans"][0]["end"] == canonical.ts(P.end), b
    d = run([machine([span(11, "Running", -9999, last + 1)])])
    assert d[0].evidence["telemetry_spans"][0]["start"] == canonical.ts(at(-GAP)), d
    print("PASS span evidence is clipped to [period start - gap, period end]; extending a "
          "span past the period end leaves the output unchanged")


EVIDENCE_KEYS = {
    "telemetry_spans": {"id", "source", "status", "start", "end"},
    "downtime_logs": {"id", "at", "reason"},
    "disputes": {"id", "status", "resolution_bucket"},
    "linkage": {"coverage_ended_at", "reason"},
}


def _evidence_is_allowlisted(evidence):
    assert type(evidence) is dict and set(evidence) <= set(EVIDENCE_KEYS), evidence
    for kind, value in evidence.items():
        rows = value if isinstance(value, list) else [value]
        for row in rows:
            assert set(row) == EVIDENCE_KEYS[kind], (kind, row)


def test_neighbours_merge_only_when_identical_and_touching():
    last = 31 * 86400
    out = run([machine([span(1, "Running", 0, last)])],
              covered=[(at(0), at(100)), (at(100), at(200)), (at(300), at(400))])
    assert spans_at(out) == [(0, 200, "AVAILABLE", "status:Running"),
                             (300, 400, "AVAILABLE", "status:Running")], spans_at(out)
    print("PASS identical touching segments merge; a covered-hours hole keeps them apart")


def test_inputs_outside_the_read_window_are_refused():
    t = terms()
    last = 31 * 86400
    assert ae.span_read_window(P, None, GAP) == (P.start - timedelta(seconds=GAP), P.end)
    assert ae.span_read_window(P, at(50), GAP) == (P.start - timedelta(seconds=GAP), at(50))
    assert ae.span_read_window(P, at(-50), GAP) == (P.start - timedelta(seconds=GAP), P.start)
    # A span that ended before period_start - gap, or starts at the coverage end.
    assert refused(run, [machine([span(1, "Running", -1000, -GAP - 1)])])
    assert not refused(run, [machine([span(1, "Running", -1000, -GAP)])])
    assert refused(run, [machine([span(1, "Running", last, last + 5)])])
    assert refused(run, [machine([span(1, "Running", 100, 200)], ended=100)])
    # An untrusted source is a wrong read, not something to skip quietly.
    assert refused(run, [machine([span(1, "Running", 0, 10, source="iot")])], t=t)
    # Reasons outside every episode's window.
    spans = [span(1, "Breakdown", 1000, 2000)]
    episodes = ae.down_episodes(spans, GAP, P.end)
    assert ae.reason_read_window(episodes, t) == (at(400), at(2300)), \
        ae.reason_read_window(episodes, t)
    assert ae.reason_read_window([], t) is None
    assert refused(run, [machine(spans, [log(1, 399, "no material")])])
    assert refused(run, [machine(spans, [log(1, 2300, "no material")])])
    assert refused(run, [machine([span(1, "Running", 0, 10)], [log(1, 5, "x")])])
    assert not refused(run, [machine(spans, [log(1, 400, "no material")])])
    print("PASS spans outside [period_start - gap, coverage_end), an untrusted source and "
          "reasons outside every episode's window are refused")


def test_malformed_inputs_are_refused():
    frac = SpanInput(id=1, source="mqtt", status="Running", start=at(0).replace(microsecond=5),
                     end=at(10))
    assert refused(run, [machine([frac])])
    assert refused(run, [machine([span(1, "Running", 10, 5)])])
    assert refused(run, [machine([span(1, "Running", 0, 5), span(1, "Idle", 6, 9)])])
    assert refused(run, [machine(), machine()])                                   # same inst
    assert refused(run, [machine(inst=3)])                                         # not covered
    assert refused(run, [machine()], covered=[(at(-1), at(10))])
    assert refused(run, [machine()], covered=[(at(10), at(20)), (at(15), at(30))])
    assert refused(run, [machine()], covered=[(at(10), at(10))])
    assert refused(run, [machine()], gap=-1)
    print("PASS fractional seconds, reversed spans, duplicate ids or installations, an "
          "uncovered installation and bad covered intervals are refused")


# --------------------------------------------------------------------------
# 5. randomized: invariant + a per-second reference oracle
# --------------------------------------------------------------------------

STATUSES = ("Running", "Idle", "Breakdown", "Maintenance", "Offline", "Weird")
REASONS = ("no material", "Motor Overheating", "Breakdown", "unknown", "lunch", "POWER failure")


def _oracle(machine_input, covered, t, disputes, period, gap):
    """(bucket, cause) for every covered second, straight from the written rules."""
    spans = sorted(machine_input.spans, key=lambda s: (s.start, s.id))

    def hold_end(s):
        end = s.end + timedelta(seconds=gap)
        later = [x.start for x in spans if x.source == s.source and x.start > s.start]
        return min([end] + later)

    held = [(s, hold_end(s)) for s in spans]
    until = period.end
    if machine_input.coverage_ended_at is not None:
        until = max(period.start, min(until, machine_input.coverage_ended_at))
    lo = min([s.start for s in spans] + [period.start])
    n = (period.end - lo) // timedelta(seconds=1)
    one = timedelta(seconds=1)

    raws = []
    for i in range(n):
        moment = lo + i * one
        holding = [s for s, end in held if s.start <= moment < end]
        raws.append((holding, {s.status for s in holding}))

    def raw(i):
        return raws[i]

    states = []
    for i in range(n):
        _, statuses = raw(i)
        states.append(next(iter(statuses)) if len(statuses) == 1
                      and next(iter(statuses)) in ct.down_status_keys() else None)

    def episode(i):
        a = i
        while a > 0 and states[a - 1] == states[i]:
            a -= 1
        b = i
        while b + 1 < n and states[b + 1] == states[i]:
            b += 1
        return lo + a * one, min(lo + (b + 1) * one, until)

    live = [d for d in disputes if d.status != "withdrawn"
            and d.installation_id == machine_input.installation_id]
    out = {}
    for c_start, c_end in covered:
        moment = c_start
        while moment < c_end:
            i = (moment - lo) // one
            d = next((d for d in live if d.start <= moment < d.end), None)
            if d is not None and d.status == "resolved":
                got = (d.resolution_bucket, f"agreed_override:#{d.id}")
            elif d is not None:
                got = ("DISPUTED", f"open_dispute:#{d.id}")
            elif moment >= until:
                got = ("UNMEASURED", "installation_unlinked")
            else:
                holding, statuses = raw(i)
                if not holding:
                    got = ("UNMEASURED", "no_telemetry")
                elif len(statuses) > 1:
                    got = (("AVAILABLE", "status:" + "+".join(sorted(statuses)))
                           if statuses <= set(ct.AVAILABLE_STATUSES)
                           else ("DISPUTED", "conflicting_status"))
                else:
                    status = next(iter(statuses))
                    if status in ct.AVAILABLE_STATUSES:
                        got = ("AVAILABLE", f"status:{status}")
                    elif status not in ct.down_status_keys():
                        got = ("DISPUTED", "unrecognised_status")
                    else:
                        e_start, e_end = episode(i)
                        keys = [ct.reason_key(r.reason) for r in machine_input.reasons
                                if e_start - timedelta(seconds=t.reason_lead_seconds)
                                <= r.at < e_end]
                        keys = [k for k in keys if k not in t.generic_reasons]
                        if not keys:
                            got = (t.status_defaults[status], f"status_default:{status}")
                        elif any(k not in t.reason_map for k in keys):
                            got = ("DISPUTED", "unmapped_reason")
                        elif len({t.reason_map[k] for k in keys}) > 1:
                            got = ("DISPUTED", "conflicting_reasons")
                        else:
                            got = (t.reason_map[keys[0]],
                                   "reason:" + "; ".join(sorted(set(keys))))
            out[moment] = got
            moment += one
    return out


def _random_case(rng):
    period = Period(T0, at(rng.choice((3600, 5400, 7200))))
    trusted = rng.choice((("mqtt",), ("iot", "mqtt")))
    t = terms(trusted=trusted, lead=rng.choice((0, 120, 600)),
              reason_map={"no material": "FACTORY", "power failure": "FACTORY",
                          "motor overheating": "OEM"})
    if rng.random() < 0.5:
        covered = [(period.start, period.end)]
    else:
        covered, cursor = [], period.start
        while cursor < period.end:
            s = cursor + timedelta(seconds=rng.randint(1, 900))
            e = s + timedelta(seconds=rng.randint(1, 1500))
            if s >= period.end:
                break
            covered.append((s, min(e, period.end)))
            cursor = min(e, period.end) + timedelta(seconds=1)
        if not covered:
            covered = [(period.start, period.end)]
    machines, disputes, did = [], [], 100
    for inst in (1, 2):
        ended = None
        if rng.random() < 0.3:
            ended = period.start + timedelta(seconds=rng.randint(-100, 8000))
        spans, sid = [], inst * 1000
        for source in trusted:
            cursor = period.start - timedelta(seconds=rng.randint(0, 900))
            while cursor < period.end:
                length = rng.randint(0, 1400)
                sid += 1
                spans.append(SpanInput(id=sid, source=source, status=rng.choice(STATUSES),
                                       start=cursor, end=cursor + timedelta(seconds=length)))
                cursor += timedelta(seconds=length + rng.choice((0, 1, 60, 299, 300, 301, 700)))
        low, high = ae.span_read_window(period, ended, GAP)
        spans = [s for s in spans if s.end >= low and s.start < high]
        episodes = ae.down_episodes(spans, GAP, ae.coverage_end(period, ended))
        window = ae.reason_read_window(episodes, t)
        reasons = []
        if window is not None:
            for k in range(rng.randint(0, 6)):
                when = window[0] + timedelta(
                    seconds=rng.randint(0, (window[1] - window[0]) // timedelta(seconds=1) - 1))
                reasons.append(ReasonLog(id=inst * 100 + k, at=when, reason=rng.choice(REASONS)))
        cursor = period.start
        while rng.random() < 0.6:
            s = cursor + timedelta(seconds=rng.randint(0, 1500))
            e = s + timedelta(seconds=rng.randint(1, 900))
            status = rng.choice(("open", "resolution_proposed", "resolved", "withdrawn"))
            did += 1
            disputes.append(DisputeInput(
                id=did, installation_id=inst, status=status, start=s, end=e,
                resolution_bucket=rng.choice(ct.RESOLUTION_BUCKETS) if status == "resolved"
                else None))
            cursor = e if status != "withdrawn" else s
        machines.append(MachineInput(installation_id=inst, serial_number=f"S{inst}",
                                     coverage_ended_at=ended,
                                     coverage_end_reason="machine_changed" if ended else None,
                                     spans=tuple(spans), reasons=tuple(reasons)))
    return period, t, covered, machines, disputes


def test_randomized_timelines_match_the_oracle_and_tile_covered_time():
    rng = random.Random(20260917)
    one = timedelta(seconds=1)
    cases = 60
    seen = set()
    for case in range(cases):
        period, t, covered, machines, disputes = _random_case(rng)
        out = ae.attribute(machines, covered, t, disputes, period=period, gap_seconds=GAP)
        assert out == sorted(out, key=lambda s: (s.installation_id, s.start)), case
        covered_seconds = sum((e - s) // one for s, e in covered)
        for m in machines:
            mine = [s for s in out if s.installation_id == m.installation_id]
            # Tiling: segments are exactly the covered intervals, cut.
            cursor = iter(mine)
            for c_start, c_end in covered:
                moment = c_start
                while moment < c_end:
                    seg = next(cursor)
                    assert seg.start == moment and seg.end <= c_end and seg.start < seg.end, \
                        (case, seg, moment)
                    moment = seg.end
            assert next(cursor, None) is None, case
            assert sum(secs(s) for s in mine) == covered_seconds, case
            for prev, cur in zip(mine, mine[1:]):
                assert not (prev.end == cur.start and (prev.bucket, prev.cause, prev.evidence)
                            == (cur.bucket, cur.cause, cur.evidence)), (case, prev, cur)
            for s in mine:
                assert s.bucket in ct.BUCKETS and s.cause, s
                _evidence_is_allowlisted(s.evidence)
                canonical.canonical_bytes(s.evidence)
                seen.add(s.cause.split(":")[0] + (":" if ":" in s.cause else ""))
            expected = _oracle(m, covered, t, disputes, period, GAP)
            for s in mine:
                moment = s.start
                while moment < s.end:
                    assert expected[moment] == (s.bucket, s.cause), \
                        (case, m.installation_id, moment, expected[moment], s)
                    moment += one
        totals = ae.bucket_totals(out)
        assert totals["covered_seconds"] == covered_seconds * len(machines), case
        cm.evaluate_sla(totals, t)
    # The comparison is only as strong as the cases it saw: every rule must occur.
    every_cause = {"no_telemetry", "conflicting_status", "unrecognised_status",
                   "installation_unlinked", "status:", "status_default:", "reason:",
                   "unmapped_reason", "conflicting_reasons", "open_dispute:",
                   "agreed_override:"}
    assert every_cause <= seen, f"randomized cases never produced {every_cause - seen}"
    print(f"PASS {cases} randomized timelines: segments tile covered time exactly, never "
          "merge identical neighbours, carry allowlisted evidence, and agree second by "
          "second with the reference oracle")


# --------------------------------------------------------------------------
# 6. structural
# --------------------------------------------------------------------------

def test_the_engine_is_pure_and_float_free():
    path = os.path.join(HERE, "attribution_engine.py")
    source = io.open(path, encoding="utf-8").read()
    tree = ast.parse(source)
    nodes = list(ast.walk(tree))
    functions = {n.name for n in nodes if isinstance(n, ast.FunctionDef)}
    for required in ("attribute", "span_read_window", "down_episodes",
                     "reason_read_window", "episode_decision", "bucket_totals",
                     "coverage_end"):
        assert required in functions, f"structural scan did not find {required}"
    floats = [n.lineno for n in nodes
              if (isinstance(n, ast.Constant) and type(n.value) is float)
              or (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                  and n.func.id == "float")
              or (isinstance(n, (ast.BinOp, ast.AugAssign)) and isinstance(n.op, ast.Div))
              or (isinstance(n, ast.Attribute) and n.attr == "total_seconds")]
    assert not floats, f"float literal, float(), '/' or total_seconds() at lines {floats}"
    imported = set()
    for n in nodes:
        if isinstance(n, ast.Import):
            imported.update(a.name.split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom):
            imported.add((n.module or "").split(".")[0])
    forbidden = imported & {"models", "database", "sqlalchemy", "oee_contract", "tenancy",
                            "time"}
    assert not forbidden, f"attribution_engine imports {forbidden}; it must stay pure"
    names = {n.id for n in nodes if isinstance(n, ast.Name)} | \
        {n.attr for n in nodes if isinstance(n, ast.Attribute)}
    for banned in ("MachineEvent", "OeeWindow", "utcnow", "now"):
        assert banned not in names, f"attribution_engine names {banned}"
    print(f"PASS attribution_engine.py: {len(functions)} functions found; no float, no "
          "database, no clock, no MachineEvent, no OeeWindow")


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items())
             if k.startswith("test_") and callable(v)]
    for test in tests:
        test()
    print(f"ALL {len(tests)} ATTRIBUTION ENGINE TESTS PASSED")
