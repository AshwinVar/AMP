"""Per-source machine status spans: what AMP actually heard, and when (ADR-0021).

WHAT A SPAN IS
--------------
One row of machine_telemetry_spans says: `source` reported `status` for this
machine from `span_start` to `span_end`, both the server's RECEIVE time in
whole UTC seconds. A device's own timestamp is never used: it is whatever the
gateway's clock says, and a statement that two parties accept cannot rest on a
clock neither of them controls.

WHY IT EXISTS (critic finding C2)
---------------------------------
MachineEvent is written only when a status differs from the shared
Machine.status, so it cannot be filtered by source: MQTT reports Running, a
manual PATCH sets Breakdown, MQTT reports Breakdown, and the second MQTT report
leaves no trace. A timeline filtered to MQTT would show Running throughout —
downtime silently counted as uptime. MachineEvent is also pruned at 180 days.
A span is written from EVERY status-bearing message of its source, independent
of Machine.status, and kept 400 days (retention.py).

THE RULE (record_message)
-------------------------
  * A message with no status writes nothing. It says nothing about status; the
    time stays "no data" rather than becoming an invented "Idle".
  * A recognised status is stored canonically (machine_status); anything else is
    stored raw, cut to 32 characters, and the attribution engine disputes it.
  * If the newest span of (tenant, machine, source) has the same status and the
    message arrives within SPAN_GAP_SECONDS of its end, it is EXTENDED: one
    UPDATE that always adds one to message_count and moves span_end only when
    the message is at least SPAN_WRITE_RESOLUTION_SECONDS past it (one write per
    30 s per machine and source, not one per message). span_end never moves
    backwards.
  * Otherwise a new span starts at the message.

WHAT A SPAN DOES NOT PROVE
--------------------------
Every source here is provisioned or posted by the FACTORY (its MQTT gateway, its
HTTP ingest token, its simulator). Statuses are NOT authenticated as coming from
the machine's maker. The contract's `trusted_sources`, `min_measured_pct`, the
visible evidence and disputes are the remedies; this module does not pretend
otherwise.

SETTLE_SECONDS: a span's end can lag its last message by the write resolution,
and a status holds for the gap after it, so a statement waits this long after
its period ends before anything is computed.
"""
from datetime import datetime, timedelta

from sqlalchemy import case, update

import canonical
import models
import oem_auth
from machine_status import normalize_machine_status

# A status holds for this long after the last message that reported it.
SPAN_GAP_SECONDS = 300
# span_end is rewritten only when a message moves it by at least this much.
SPAN_WRITE_RESOLUTION_SECONDS = 30
# Statements wait this long after a period ends (late writes cannot change it).
SETTLE_SECONDS = SPAN_GAP_SECONDS + SPAN_WRITE_RESOLUTION_SECONDS + 60

STATUS_MAX_CHARS = 32

MQTT = "mqtt"
IOT = "iot"
INDUSTRIAL_GATEWAY = "industrial_gateway"
SIMULATOR = "simulator"
# The writers. "manual" is deliberately absent: the manual status PATCH is a
# person's statement, not telemetry, and writes no span.
SOURCES = (MQTT, IOT, INDUSTRIAL_GATEWAY, SIMULATOR)


def received_at():
    """The writers' one clock: now, naive UTC, whole seconds (tests pin it)."""
    return canonical.utc_seconds(datetime.utcnow())


def span_status(raw):
    """The status a span records for an inbound value, or None for no status."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    return normalize_machine_status(text) or text[:STATUS_MAX_CHARS]


def record_message(db, tenant_code, machine_id, source, status, at):
    """Record one message's status for (tenant, machine, source) at `at`.

    Writes inside the caller's transaction (flushes, never commits). Returns the
    span row written or extended, or None when the message carried no status.
    Raises ValueError for a blank or OEM-sentinel tenant, an unknown source, or a
    non-integer machine id; CanonicalError when `at` is not a datetime."""
    if (not isinstance(tenant_code, str) or not tenant_code.strip()
            or oem_auth.is_sentinel(tenant_code)):
        raise ValueError("a span belongs to a factory tenant")
    if source not in SOURCES:
        raise ValueError(f"unknown span source {source!r}; one of {SOURCES}")
    if type(machine_id) is not int:
        raise ValueError("machine_id must be an integer")
    at = canonical.utc_seconds(at)
    text = span_status(status)
    if text is None:
        return None

    S = models.MachineTelemetrySpan
    # Bounded at both ends: only a span that could still be extended matters.
    latest = (db.query(S)
                .filter(S.tenant_code == tenant_code,
                        S.machine_id == machine_id,
                        S.source == source,
                        S.span_start <= at,
                        S.span_end >= at - timedelta(seconds=SPAN_GAP_SECONDS))
                .order_by(S.span_start.desc(), S.id.desc())
                .first())
    if latest is not None and latest.status == text:
        values = {"message_count": S.message_count + 1}
        if at - latest.span_end >= timedelta(seconds=SPAN_WRITE_RESOLUTION_SECONDS):
            values["span_end"] = case((S.span_end < at, at), else_=S.span_end)
        db.execute(update(S).where(S.id == latest.id, S.tenant_code == tenant_code)
                   .values(**values)
                   .execution_options(synchronize_session=False))
        db.flush()
        db.refresh(latest)
        return latest

    row = S(tenant_code=tenant_code, machine_id=machine_id, source=source, status=text,
            span_start=at, span_end=at, message_count=1)
    db.add(row)
    db.flush()
    return row
