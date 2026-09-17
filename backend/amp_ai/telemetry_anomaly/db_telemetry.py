"""Bounded, tenant-filtered reads of one machine's telemetry for anomaly scoring.

Reads only; never writes, never caches. Every query filters ``tenant_code``
EXPLICITLY as well as through the ADR-0002 session hook, so a row stamped with
another tenant cannot arrive even when no ambient tenant is bound.

WINDOWS
-------
Both built from ``oee_contract.OeeWindow`` (half-open) and bounded at BOTH ends
in SQL:

    score     [now - 1 h, now)            OeeWindow(days=1/24, now=now)
    baseline  [now - 14 d, now - 1 h)     OeeWindow(days=14 - 1/24, now=score.start)

With no explicit ``now``, the score window ends at the next representable instant
(OeeWindow's rule), so a reading written in the same clock tick is included.
The two windows share the boundary instant and no row. ``windows`` asserts the
baseline starts exactly 14 days before the anchor, so float days can never
open a gap or an overlap. ``series`` counts its buckets back from the same anchor.

SOURCES
-------
* ``iot_telemetry`` rows for the machine, named ``iot:<signal_name>``.
* ``industrial_signals`` rows whose ``machine_id`` is the machine and whose
  quality is "Good", named ``plc:<signal_name>``. ``machine_id`` is copied from
  the device's ``linked_machine_id`` when a row is WRITTEN, so relinking a device
  to another machine does not move its history - old rows stay with the old
  machine, which is what they measured.
* OEM connected-equipment readings are not stored anywhere, so they are not a source.

Value: ``float(signal_value)``; if that does not parse, ``numeric_value``.
Non-finite values (NaN, inf) are dropped, never replaced.

Each window/source query takes at most ``MAX_ROWS_PER_QUERY`` rows, newest first.
When a baseline query hits the cap, rows older than the newest cut-off among the
truncated sources are dropped from every source, so all signals cover the same
span, and ``baseline_start_used`` reports where that span begins.

MACHINE STATE
-------------
``machine_events`` inside [baseline start, now) plus ONE bounded query for the
last event in the STATE_LOOKBACK_DAYS before the baseline starts (MachineEvent
is retained 180 days). If there is none, the first in-window event's
``old_status`` is the state before it. Statuses map through ``machine_status``:
Running -> running, any other canonical status -> not running, anything else ->
unknown. If the event query itself is capped, the state before the oldest event
read is unknown.

``simulated_source``: True if any reading used came from AMP's own demo PLC
fleet (``industrial_demo``), None if iot_telemetry rows were used (the demo
simulator's rows there cannot be told apart from real ones), otherwise False.
"""
import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_

import industrial_demo
import machine_status
import models
from oee_contract import OeeWindow

from . import series as SR

__all__ = ["MAX_ROWS_PER_QUERY", "STATE_LOOKBACK_DAYS", "GOOD_QUALITY", "TelemetryLoad", "windows", "load"]

MAX_ROWS_PER_QUERY = 50_000
STATE_LOOKBACK_DAYS = 180
GOOD_QUALITY = "Good"          # industrial_signals.quality as ingest and the adapters write it
IOT_PREFIX = "iot:"
PLC_PREFIX = "plc:"

_SCORE_DAYS = SR.SCORE_BUCKETS * SR.BUCKET_SECONDS / 86400
_ONE_US = timedelta(microseconds=1)


@dataclass(frozen=True)
class TelemetryLoad:
    score_window: OeeWindow
    baseline_window: OeeWindow
    score_readings: list          # (microseconds before score_window.end, name, value)
    baseline_readings: list
    events: list                  # (microseconds before score_window.end, running: bool | None), oldest first
    initial_running: object       # bool | None: the state before the first event
    truncated: bool
    score_start_used: datetime
    baseline_start_used: datetime
    simulated_source: object      # bool | None
    rows_read: int


def windows(now=None):
    """(score, baseline) OeeWindows for an anchor ``now`` (naive UTC)."""
    if now is not None and (not isinstance(now, datetime) or now.tzinfo is not None):
        raise TypeError("now must be a naive UTC datetime, as stored timestamps are")
    score = OeeWindow(days=_SCORE_DAYS, now=now)
    baseline = OeeWindow(days=SR.BASELINE_DAYS - _SCORE_DAYS, now=score.start)
    if score.end - score.start != timedelta(seconds=SR.SCORE_BUCKETS * SR.BUCKET_SECONDS) \
            or score.end - baseline.start != timedelta(days=SR.BASELINE_DAYS):
        raise AssertionError("telemetry windows do not tile the bucket grid exactly")
    return score, baseline


def _running(status):
    canonical = machine_status.normalize_machine_status(status)
    if canonical is None:
        return None
    return canonical == machine_status.RUNNING


def _value(signal_value, numeric_value):
    try:
        v = float(signal_value)
    except (TypeError, ValueError):
        if numeric_value is None:
            return None
        v = float(numeric_value)
    return v if math.isfinite(v) else None


def _offset(anchor, ts):
    return (anchor - ts) // _ONE_US


def _iot_rows(db, tenant, machine_id, start, end, cap):
    T = models.IoTTelemetry
    return (db.query(T.created_at, T.signal_name, T.signal_value, T.numeric_value)
            .filter(T.tenant_code == tenant, T.machine_id == machine_id,
                    T.created_at >= start, T.created_at < end)
            .order_by(T.created_at.desc(), T.id.desc())
            .limit(cap + 1).all())


def _plc_rows(db, tenant, machine_id, start, end, cap):
    S, D = models.IndustrialSignal, models.IndustrialDevice
    return (db.query(S.created_at, S.signal_name, S.signal_value, S.numeric_value, D.device_code)
            .outerjoin(D, and_(D.id == S.device_id, D.tenant_code == tenant))
            .filter(S.tenant_code == tenant, S.machine_id == machine_id, S.quality == GOOD_QUALITY,
                    S.created_at >= start, S.created_at < end)
            .order_by(S.created_at.desc(), S.id.desc())
            .limit(cap + 1).all())


def _window_readings(db, tenant, machine_id, window, cap, anchor):
    """Readings for one window from both sources, plus (truncated, start actually used, source facts)."""
    iot = _iot_rows(db, tenant, machine_id, window.start, window.end, cap)
    plc = _plc_rows(db, tenant, machine_id, window.start, window.end, cap)
    cutoffs = []
    if len(iot) > cap:
        iot = iot[:cap]
        cutoffs.append(iot[-1].created_at)
    if len(plc) > cap:
        plc = plc[:cap]
        cutoffs.append(plc[-1].created_at)
    start_used = max(cutoffs) if cutoffs else window.start
    readings = []
    used_iot = used_demo = used_plc = False
    for row in iot:
        if row.created_at < start_used:
            continue
        v = _value(row.signal_value, row.numeric_value)
        if v is not None:
            readings.append((_offset(anchor, row.created_at), IOT_PREFIX + row.signal_name, v))
            used_iot = True
    for row in plc:
        if row.created_at < start_used:
            continue
        v = _value(row.signal_value, row.numeric_value)
        if v is not None:
            readings.append((_offset(anchor, row.created_at), PLC_PREFIX + row.signal_name, v))
            used_plc = True
            used_demo = used_demo or industrial_demo.is_demo_device(row.device_code)
    return readings, bool(cutoffs), start_used, (used_iot, used_plc, used_demo), len(iot) + len(plc)


def load(db, tenant, machine_id, *, now=None, row_cap=MAX_ROWS_PER_QUERY) -> TelemetryLoad:
    if not isinstance(tenant, str) or not tenant:
        raise ValueError("tenant must be a non-empty string")
    if isinstance(row_cap, bool) or not isinstance(row_cap, int) or row_cap < 1:
        raise ValueError("row_cap must be a positive int")
    score, baseline = windows(now)
    anchor = score.end

    score_readings, score_cut, score_start, score_facts, n_score = _window_readings(
        db, tenant, machine_id, score, row_cap, anchor)
    base_readings, base_cut, base_start, base_facts, n_base = _window_readings(
        db, tenant, machine_id, baseline, row_cap, anchor)

    E = models.MachineEvent
    event_rows = (db.query(E.created_at, E.old_status, E.new_status)
                  .filter(E.tenant_code == tenant, E.machine_id == machine_id,
                          E.created_at >= baseline.start, E.created_at < score.end)
                  .order_by(E.created_at.desc(), E.id.desc())
                  .limit(row_cap + 1).all())
    events_cut = len(event_rows) > row_cap
    event_rows = list(reversed(event_rows[:row_cap]))
    events = [(_offset(anchor, row.created_at), _running(row.new_status)) for row in event_rows]
    if events_cut:
        initial = None
    else:
        lookback = OeeWindow(days=STATE_LOOKBACK_DAYS, now=baseline.start)
        before = (db.query(E.new_status)
                  .filter(E.tenant_code == tenant, E.machine_id == machine_id,
                          E.created_at >= lookback.start, E.created_at < lookback.end)
                  .order_by(E.created_at.desc(), E.id.desc())
                  .first())
        if before is not None:
            initial = _running(before.new_status)
        elif event_rows:
            initial = _running(event_rows[0].old_status)
        else:
            initial = None

    used_iot = score_facts[0] or base_facts[0]
    used_plc = score_facts[1] or base_facts[1]
    used_demo = score_facts[2] or base_facts[2]
    if used_demo:
        simulated = True
    elif used_iot:
        simulated = None
    elif used_plc:
        simulated = False
    else:
        simulated = None

    return TelemetryLoad(
        score_window=score, baseline_window=baseline,
        score_readings=score_readings, baseline_readings=base_readings,
        events=events, initial_running=initial,
        truncated=score_cut or base_cut or events_cut,
        score_start_used=score_start, baseline_start_used=base_start,
        simulated_source=simulated, rows_read=n_score + n_base + len(event_rows),
    )
