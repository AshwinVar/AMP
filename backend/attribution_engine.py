"""Agreed downtime attribution: every covered second of a machine into one bucket (ADR-0020).

WHAT THIS MODULE IS
-------------------
The pure core of a downtime attribution statement. Given, for each covered
machine, its per-source status spans and the factory's OWN downtime reasons,
plus the period's covered intervals, the agreed terms and the statement's
disputes, `attribute` returns segments that tile the covered time exactly, each
in one bucket with a cause and its evidence:

    AVAILABLE    the machine reported Running or Idle
    OEM          a down episode the terms attribute to the machine maker
    FACTORY      a down episode the terms attribute to the factory
    DISPUTED     the evidence does not settle it (yet)
    UNMEASURED   no data: never counted as uptime and never as downtime

The attribution from the factory's own MES reasons is the differentiator.
Metering and shared usage ledgers already exist (SteamChain, PayperChain,
Linxfour, Rockwell US10747201B2) and nothing here claims novelty for them. A
freedom-to-operate review is needed before commercial launch.

It is PURE: no database, no clock, no float. contract_statements reads the rows
with the windows this module computes (`span_read_window`,
`reason_read_window`) and hands them in.

THE RULES
---------
Status timeline (per machine, trusted sources only):
  * A span holds its status on [span_start, min(span_end + SPAN_GAP_SECONDS,
    start of the next span of the SAME source)). Where no span holds, the time
    is UNMEASURED "no_telemetry".
  * Holding spans that agree: Running/Idle -> AVAILABLE ("status:<s>"); a down
    status (the vocabulary minus Running and Idle) -> a down episode; anything
    else -> DISPUTED "unrecognised_status".
  * Holding spans that disagree -> DISPUTED "conflicting_status", except when
    every status is an AVAILABLE one (Running from one source, Idle from
    another): available either way, so AVAILABLE "status:Idle+Running".

Down episodes:
  * A maximal run of ONE down status in the timeline, touching pieces merged,
    clipped to the coverage end (the period end, or the instant the
    installation was unlinked if earlier; nothing about the machine is read
    from that instant on).
  * Candidates: the factory's DowntimeLog rows with at in
    [episode_start - reason_lead_seconds, episode_end). Generic reasons are
    dropped. Then: no explicit reason -> status_defaults[status]
    ("status_default:<s>"); any unmapped -> DISPUTED "unmapped_reason"; mapped
    to both parties -> DISPUTED "conflicting_reasons"; otherwise that party
    ("reason:<key>[; <key>...]").
  * DowntimeLog.created_at is set by the server, so a reason cannot be
    backdated; one logged after the episode is not a candidate.

Precedence for each covered second:
    resolved dispute  > open or resolution-proposed dispute
    > installation unlinked > no telemetry > conflicting status
    > available > unrecognised status / down attribution

Neighbouring segments with identical (bucket, cause, evidence) merge. For every
machine, covered seconds == AVAILABLE + OEM + FACTORY + DISPUTED + UNMEASURED;
`attribute` checks it and raises if not.

EVIDENCE (an allowlist; nothing else ever enters a statement)
    telemetry_spans  id, source, status, start, end   (clipped to
                     [period_start - SPAN_GAP_SECONDS, period_end]: a span
                     ending up to one gap before the period still holds into
                     it, and extending a span past the period end changes
                     nothing)
    downtime_logs    id, at, reason (the raw text; canonical form NFC-normalises)
    disputes         id, status, resolution_bucket
    linkage          coverage_ended_at, reason
No notes, durations, machine names, utilisation, counts, operators or work
orders.

REFUSALS
--------
A wrong read must be loud, not a quiet wrong statement. `attribute` raises
AttributionError for: a span from an untrusted source; a span outside
[period_start - gap, coverage_end); a reason outside every episode's window; a
fractional second; a span ending before it starts; duplicate span, reason or
dispute ids; a machine the terms do not cover; covered intervals outside the
period or overlapping; overlapping non-withdrawn disputes on one installation;
a resolved dispute without a resolution bucket, or with DISPUTED.

KNOWN EDGE (documented, deterministic). Holds are cut by the next span of the
same source among the spans READ. Two spans of one source can only overlap if
two writers raced; if the earlier one ended more than a gap before the period,
it is not read, and cannot cut the other's hold. An episode's start is likewise
found among the spans read, i.e. those reaching within one gap of the period.

Run the tests: DATABASE_URL="sqlite:///./ci.db" python backend/test_attribution_engine.py
"""
import bisect
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Tuple

import canonical
import contract_money
import contract_terms as ct

ONE_SECOND = timedelta(seconds=1)

DISPUTE_OPEN = "open"
DISPUTE_RESOLUTION_PROPOSED = "resolution_proposed"
DISPUTE_RESOLVED = "resolved"
DISPUTE_WITHDRAWN = "withdrawn"
DISPUTE_STATUSES = (DISPUTE_OPEN, DISPUTE_RESOLUTION_PROPOSED, DISPUTE_RESOLVED,
                    DISPUTE_WITHDRAWN)
# Disputes that make their window DISPUTED until settled.
OPEN_DISPUTE_STATUSES = (DISPUTE_OPEN, DISPUTE_RESOLUTION_PROPOSED)

# Which statement total each bucket adds to. contract_money owns the keys.
BUCKET_TOTAL_KEYS = {
    ct.AVAILABLE: "available_seconds",
    ct.OEM: "oem_seconds",
    ct.FACTORY: "factory_seconds",
    ct.DISPUTED: "disputed_seconds",
    ct.UNMEASURED: "unmeasured_seconds",
}
if set(BUCKET_TOTAL_KEYS) != set(ct.BUCKETS) or \
        {"covered_seconds", *BUCKET_TOTAL_KEYS.values()} != set(contract_money.TOTAL_KEYS):
    raise ImportError("attribution_engine: buckets and statement totals disagree")

# Piece kinds of the status timeline.
_NO_DATA = "no_data"
_CONFLICT = "conflict"
_AVAILABLE = "available"
_DOWN = "down"
_UNRECOGNISED = "unrecognised"


class AttributionError(ValueError):
    """The inputs cannot be attributed without guessing. Never swallowed."""


@dataclass(frozen=True)
class SpanInput:
    id: int
    source: str
    status: str
    start: datetime          # naive UTC, whole seconds (span_start)
    end: datetime            # naive UTC, whole seconds (span_end, the last message)


@dataclass(frozen=True)
class ReasonLog:
    id: int
    at: datetime             # naive UTC, whole seconds (DowntimeLog.created_at)
    reason: str


@dataclass(frozen=True)
class DisputeInput:
    id: int
    installation_id: int
    status: str              # open | resolution_proposed | resolved | withdrawn
    start: datetime          # half-open [start, end)
    end: datetime
    resolution_bucket: Optional[str]


@dataclass(frozen=True)
class MachineInput:
    installation_id: int
    serial_number: str
    coverage_ended_at: Optional[datetime]
    coverage_end_reason: Optional[str]
    spans: Tuple[SpanInput, ...]
    reasons: Tuple[ReasonLog, ...]


@dataclass(frozen=True)
class Episode:
    start: datetime
    end: datetime
    status: str


@dataclass(frozen=True)
class Segment:
    installation_id: int
    start: datetime
    end: datetime
    bucket: str
    cause: str
    evidence: dict           # canonical-ready

    @property
    def seconds(self):
        return (self.end - self.start) // ONE_SECOND


# ── input checks ────────────────────────────────────────────────────────────

def _instant(value, name):
    if type(value) is not datetime or value.tzinfo is not None or value.microsecond:
        raise AttributionError(
            f"{name} must be a naive UTC datetime in whole seconds, not {value!r}")
    return value


def _int_id(value, name):
    if type(value) is not int:
        raise AttributionError(f"{name} must be an int, not {value!r}")
    return value


def _gap(gap_seconds):
    if type(gap_seconds) is not int or gap_seconds < 0:
        raise AttributionError(f"gap_seconds must be a non-negative int, not {gap_seconds!r}")
    return timedelta(seconds=gap_seconds)


def _check_period(period):
    _instant(period.start, "period.start")
    _instant(period.end, "period.end")
    if period.start >= period.end:
        raise AttributionError(f"period {period} is empty")


def _check_spans(spans):
    ids = set()
    for s in spans:
        _int_id(s.id, "span id")
        if type(s.source) is not str or type(s.status) is not str:
            raise AttributionError(f"span #{s.id}: source and status must be text")
        _instant(s.start, f"span #{s.id} start")
        _instant(s.end, f"span #{s.id} end")
        if s.end < s.start:
            raise AttributionError(f"span #{s.id} ends before it starts")
        if s.id in ids:
            raise AttributionError(f"span #{s.id} is given twice")
        ids.add(s.id)
    return sorted(spans, key=lambda s: (s.start, s.id))


def _check_reasons(reasons):
    ids = set()
    for r in reasons:
        _int_id(r.id, "reason id")
        _instant(r.at, f"reason #{r.id} at")
        if type(r.reason) is not str:
            raise AttributionError(f"reason #{r.id}: the reason must be text")
        if r.id in ids:
            raise AttributionError(f"reason #{r.id} is given twice")
        ids.add(r.id)
    return sorted(reasons, key=lambda r: (r.at, r.id))


def _check_covered(covered, period):
    out = []
    for item in covered:
        start, end = item
        _instant(start, "covered start")
        _instant(end, "covered end")
        if not period.start <= start < end <= period.end:
            raise AttributionError(
                f"covered interval [{start}, {end}) is empty or outside the period")
        if out and start < out[-1][1]:
            raise AttributionError("covered intervals must be sorted and must not overlap")
        out.append((start, end))
    return out


def _check_disputes(disputes):
    """Non-withdrawn disputes, validated, grouped by installation, sorted."""
    ids = set()
    live = {}
    for d in disputes:
        _int_id(d.id, "dispute id")
        _int_id(d.installation_id, f"dispute #{d.id} installation_id")
        if d.status not in DISPUTE_STATUSES:
            raise AttributionError(f"dispute #{d.id} has unknown status {d.status!r}")
        _instant(d.start, f"dispute #{d.id} start")
        _instant(d.end, f"dispute #{d.id} end")
        if d.end <= d.start:
            raise AttributionError(f"dispute #{d.id} has an empty window")
        if d.id in ids:
            raise AttributionError(f"dispute #{d.id} is given twice")
        ids.add(d.id)
        if d.status == DISPUTE_WITHDRAWN:
            continue
        if d.status == DISPUTE_RESOLVED and d.resolution_bucket not in ct.RESOLUTION_BUCKETS:
            raise AttributionError(
                f"resolved dispute #{d.id} has resolution bucket {d.resolution_bucket!r}; "
                "a resolution must be one of " + ", ".join(ct.RESOLUTION_BUCKETS))
        if d.resolution_bucket is not None and d.resolution_bucket not in ct.RESOLUTION_BUCKETS:
            raise AttributionError(
                f"dispute #{d.id} proposes resolution bucket {d.resolution_bucket!r}")
        live.setdefault(d.installation_id, []).append(d)
    for installation_id, rows in live.items():
        rows.sort(key=lambda d: (d.start, d.id))
        for prev, cur in zip(rows, rows[1:]):
            if cur.start < prev.end:
                raise AttributionError(
                    f"disputes #{prev.id} and #{cur.id} overlap on installation "
                    f"{installation_id}; which one settles the overlap is not agreed")
    return live


# ── windows the caller reads with ──────────────────────────────────────────

def coverage_end(period, coverage_ended_at):
    """Where AMP stops reading this machine for the period: the period end, or
    the instant the installation was unlinked if that is earlier (never before
    the period start)."""
    if coverage_ended_at is None:
        return period.end
    _instant(coverage_ended_at, "coverage_ended_at")
    return max(period.start, min(period.end, coverage_ended_at))


def span_read_window(period, coverage_ended_at, gap_seconds):
    """(end_at_least, start_before): read spans with span_end >= end_at_least
    and span_start < start_before. Both bounds, always."""
    return period.start - _gap(gap_seconds), coverage_end(period, coverage_ended_at)


def reason_read_window(episodes, terms):
    """(lo, hi): read reasons with lo <= created_at < hi. None when there is no
    down episode, so nothing need be read."""
    if not episodes:
        return None
    lead = timedelta(seconds=terms.reason_lead_seconds)
    return min(e.start for e in episodes) - lead, max(e.end for e in episodes)


# ── the status timeline ─────────────────────────────────────────────────────

def _holds(spans, gap):
    """(start, end, span) for each span: its status holds on [start, end)."""
    starts = {}
    for s in spans:
        starts.setdefault(s.source, set()).add(s.start)
    starts = {source: sorted(values) for source, values in starts.items()}
    holds = []
    for s in spans:
        end = s.end + gap
        same_source = starts[s.source]
        i = bisect.bisect_right(same_source, s.start)
        if i < len(same_source):
            end = min(end, same_source[i])
        if end > s.start:
            holds.append((s.start, end, s))
    return holds


def _pieces(holds):
    """The timeline cut at every hold edge: (start, end, holding spans)."""
    points = sorted({h[0] for h in holds} | {h[1] for h in holds})
    order = sorted(holds, key=lambda h: (h[0], h[2].id))
    pieces, active, k = [], [], 0
    for a, b in zip(points, points[1:]):
        while k < len(order) and order[k][0] <= a:
            active.append(order[k])
            k += 1
        active = [h for h in active if h[1] > a]
        pieces.append((a, b, tuple(h[2] for h in active)))
    return pieces


def _state(holding):
    statuses = {s.status for s in holding}
    if not statuses:
        return _NO_DATA, None
    if len(statuses) == 1:
        status = next(iter(statuses))
        if status in ct.AVAILABLE_STATUSES:
            return _AVAILABLE, status
        if status in ct.down_status_keys():
            return _DOWN, status
        return _UNRECOGNISED, status
    if statuses <= set(ct.AVAILABLE_STATUSES):
        return _AVAILABLE, "+".join(sorted(statuses))
    return _CONFLICT, None


def _episodes(pieces, until):
    episodes = []
    for a, b, holding in pieces:
        kind, status = _state(holding)
        if kind != _DOWN:
            continue
        if episodes and episodes[-1].end == a and episodes[-1].status == status:
            episodes[-1] = Episode(episodes[-1].start, b, status)
        else:
            episodes.append(Episode(a, b, status))
    return [Episode(e.start, min(e.end, until), e.status) for e in episodes if e.start < until]


def down_episodes(spans, gap_seconds, until):
    """The down episodes of these spans, clipped to `until` (coverage_end)."""
    _instant(until, "until")
    return _episodes(_pieces(_holds(_check_spans(spans), _gap(gap_seconds))), until)


def episode_decision(status, logs, terms):
    """(bucket, cause) for a down episode of `status` with these candidate logs."""
    explicit = [k for k in (ct.reason_key(log.reason) for log in logs)
                if k not in terms.generic_reasons]
    if not explicit:
        return terms.status_defaults[status], f"status_default:{status}"
    if any(k not in terms.reason_map for k in explicit):
        return ct.DISPUTED, "unmapped_reason"
    parties = {terms.reason_map[k] for k in explicit}
    if len(parties) > 1:
        return ct.DISPUTED, "conflicting_reasons"
    return parties.pop(), "reason:" + "; ".join(sorted(set(explicit)))


# ── evidence ────────────────────────────────────────────────────────────────

def _span_evidence(s, period, gap):
    return {"id": s.id, "source": s.source, "status": s.status,
            "start": canonical.ts(max(s.start, period.start - gap)),
            "end": canonical.ts(min(s.end, period.end))}


def _log_evidence(log):
    return {"id": log.id, "at": canonical.ts(log.at), "reason": log.reason}


def _dispute_evidence(d):
    return {"disputes": [{"id": d.id, "status": d.status,
                          "resolution_bucket": d.resolution_bucket}]}


# ── attribution ─────────────────────────────────────────────────────────────

def _attribute_machine(m, covered, terms, disputes, period, gap):
    until = coverage_end(period, m.coverage_ended_at)
    spans = _check_spans(m.spans)
    low = period.start - gap
    for s in spans:
        if s.source not in terms.trusted_sources:
            raise AttributionError(
                f"span #{s.id} is from {s.source!r}, which the terms do not trust; the "
                "read must filter by trusted_sources")
        if s.end < low or s.start >= until:
            raise AttributionError(
                f"span #{s.id} [{s.start}, {s.end}] is outside the read window "
                f"[{low}, {until}); the read must bound both ends")
    pieces = _pieces(_holds(spans, gap))
    episodes = _episodes(pieces, until)
    reasons = _check_reasons(m.reasons)
    window = reason_read_window(episodes, terms)
    for r in reasons:
        if window is None or not window[0] <= r.at < window[1]:
            raise AttributionError(
                f"reason #{r.id} at {r.at} is outside the reason window {window}; the read "
                "must bound both ends")

    lead = timedelta(seconds=terms.reason_lead_seconds)
    decisions = []
    for ep in episodes:
        logs = [r for r in reasons if ep.start - lead <= r.at < ep.end]
        bucket, cause = episode_decision(ep.status, logs, terms)
        decisions.append((bucket, cause, [_log_evidence(r) for r in logs]))

    piece_starts = [p[0] for p in pieces]
    episode_starts = [e.start for e in episodes]
    dispute_starts = [d.start for d in disputes]
    points = sorted({p for piece in pieces for p in piece[:2]} | {until}
                    | {d.start for d in disputes} | {d.end for d in disputes})

    def classify(a):
        i = bisect.bisect_right(dispute_starts, a) - 1
        if i >= 0 and a < disputes[i].end:
            d = disputes[i]
            if d.status == DISPUTE_RESOLVED:
                return d.resolution_bucket, f"agreed_override:#{d.id}", _dispute_evidence(d)
            return ct.DISPUTED, f"open_dispute:#{d.id}", _dispute_evidence(d)
        if a >= until:
            return ct.UNMEASURED, "installation_unlinked", {"linkage": {
                "coverage_ended_at": canonical.ts(m.coverage_ended_at),
                "reason": m.coverage_end_reason}}
        i = bisect.bisect_right(piece_starts, a) - 1
        holding = pieces[i][2] if i >= 0 and a < pieces[i][1] else ()
        kind, status = _state(holding)
        evidence = {"telemetry_spans": [_span_evidence(s, period, gap) for s in holding]}
        if kind == _NO_DATA:
            return ct.UNMEASURED, "no_telemetry", evidence
        if kind == _CONFLICT:
            return ct.DISPUTED, "conflicting_status", evidence
        if kind == _AVAILABLE:
            return ct.AVAILABLE, f"status:{status}", evidence
        if kind == _UNRECOGNISED:
            return ct.DISPUTED, "unrecognised_status", evidence
        j = bisect.bisect_right(episode_starts, a) - 1
        if j < 0 or not episodes[j].start <= a < episodes[j].end:
            raise AttributionError(f"internal: no episode holds {a} on installation "
                                   f"{m.installation_id}")
        bucket, cause, logs = decisions[j]
        evidence["downtime_logs"] = logs
        return bucket, cause, evidence

    segments = []
    for c_start, c_end in covered:
        lo = bisect.bisect_right(points, c_start)
        hi = bisect.bisect_left(points, c_end)
        cuts = [c_start] + points[lo:hi] + [c_end]
        for a, b in zip(cuts, cuts[1:]):
            bucket, cause, evidence = classify(a)
            last = segments[-1] if segments else None
            if last is not None and last.end == a and \
                    (last.bucket, last.cause, last.evidence) == (bucket, cause, evidence):
                segments[-1] = Segment(m.installation_id, last.start, b, bucket, cause,
                                       evidence)
            else:
                segments.append(Segment(m.installation_id, a, b, bucket, cause, evidence))

    covered_seconds = sum((e - s) // ONE_SECOND for s, e in covered)
    if sum(s.seconds for s in segments) != covered_seconds:
        raise AttributionError(
            f"installation {m.installation_id}: attributed seconds do not add up to the "
            f"{covered_seconds} covered seconds")
    return segments


def attribute(machines, covered, terms, disputes, *, period, gap_seconds):
    """Segments for every machine, sorted by (installation_id, start).

    machines     MachineInput per covered installation (a subset of the terms'
                 covered_installations, serial numbers matching)
    covered      the period's covered intervals, sorted (start, end) tuples
    terms        parsed contract_terms.Terms
    disputes     DisputeInput for the statement (withdrawn ones are ignored)
    period       contract_periods.Period
    gap_seconds  telemetry_coverage.SPAN_GAP_SECONDS
    """
    _check_period(period)
    gap = _gap(gap_seconds)
    covered = _check_covered(covered, period)
    live = _check_disputes(disputes)
    serials = {c.installation_id: c.serial_number for c in terms.covered_installations}
    seen = set()
    out = []
    for m in sorted(machines, key=lambda m: m.installation_id):
        _int_id(m.installation_id, "installation_id")
        if m.installation_id in seen:
            raise AttributionError(f"installation {m.installation_id} is given twice")
        seen.add(m.installation_id)
        if serials.get(m.installation_id) != m.serial_number:
            raise AttributionError(
                f"installation {m.installation_id} ({m.serial_number!r}) is not covered by "
                "these terms under that serial number")
        out.extend(_attribute_machine(m, covered, terms, live.get(m.installation_id, []),
                                      period, gap))
    return out


def bucket_totals(segments):
    """contract_money.TOTAL_KEYS, in seconds, over these segments."""
    totals = {k: 0 for k in contract_money.TOTAL_KEYS}
    for s in segments:
        seconds = s.seconds
        totals[BUCKET_TOTAL_KEYS[s.bucket]] += seconds
        totals["covered_seconds"] += seconds
    return totals
