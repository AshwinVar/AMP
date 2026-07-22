"""Edge connectivity — is the plant actually reporting? (ADR-0007).

Every other read-model trusts that the numbers are flowing. This one asks the
prior question: *is the OT edge alive?* A machine whose telemetry went silent an
hour ago isn't "running fine" — it's dark, and every downstream metric for it is
stale. So this reframes the raw IoT stream (iot_telemetry) and the registered
edge estate (industrial_devices / industrial_signals) into a connectivity health
picture the person on call actually manages by:

  * FRESH  — reported telemetry within the freshness window (default 15 min).
  * STALE  — has reported before, but not recently: the signal dropped.
  * DARK   — no telemetry on record at all: never instrumented, or never seen.

From those it derives a headline connectivity score (% of machines reporting
fresh), the device online rate, the signal good-quality rate, instrumentation
coverage, and a worst-first chase list — dark machines first, then the longest
silences — so triage reconnects the plant before trusting the dashboards.

A drill-down (``build_connection_detail``) then takes one machine off that chase
list and answers the engineer's question: what am I blind on while this is
silent, and what do I go and fix?

A pure read-model over machines + iot_telemetry + industrial_devices +
industrial_signals (+ work_orders in the drill-down), auto-scoped to the tenant
(ADR-0002); it adds no storage.
Freshness math runs on real datetimes: a per-machine GROUP BY over the lookback
window supplies each machine's most-recent timestamp and in-window signal count
(SQLAlchemy hands back func.max on a DateTime column as a datetime, not a string),
and a DARK-vs-STALE probe bounded to just the machines with no in-window signal
avoids a DISTINCT over the whole, ever-growing telemetry history on every poll.
"""
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from statistics import median

from sqlalchemy import func

import models

name = "connectivity"

# A machine that hasn't reported within this many minutes is not "fresh" — the
# live view of it is stale. 15 min suits a plant on a minutes-scale telemetry
# cadence without flapping on a single missed message.
STALE_AFTER_MINUTES = 15
# How far back we pull telemetry rows to find each machine's most-recent signal.
# Anything older than this is, by definition, well past the freshness window, so
# we don't need the row — the ever-reported probe still marks it STALE not DARK.
LOOKBACK_HOURS = 24
TOP_N = 8
# How many raw reads the drill-down hands back as evidence.
RECENT_N = 10
# A tag silent for more than this many multiples of its OWN normal cadence has
# dropped — a 5-minute tag quiet for 40 minutes is 8x overdue, which is a fault,
# not jitter. 3x tolerates a couple of missed messages before crying wolf.
DROPPED_CADENCE_MULTIPLE = 3
# Work-order statuses that mean the job is done with — everything else is still
# live on the machine, and therefore still blind while the machine is silent.
_CLOSED_WO_STATUSES = {"completed", "cancelled", "canceled", "closed"}

_STATE_RANK = {"dark": 0, "stale": 1, "fresh": 2}
# A silence we can't measure (reported before the lookback window) sorts as the
# most silent of the stale machines — larger than any real minute count.
_UNMEASURED_SILENCE = 10 ** 9


def _is_online(status) -> bool:
    return (status or "").strip().lower() == "online"


def _is_good(quality) -> bool:
    return (quality or "").strip().lower() == "good"


def _ago(minutes: float) -> str:
    """A silence in minutes, said the way an engineer says it."""
    if minutes < 60:
        return f"{round(minutes)}m"
    if minutes < 60 * 24:
        return f"{minutes / 60:.1f}h"
    return f"{minutes / 60 / 24:.1f}d"


def _cadence_minutes(times):
    """A tag's normal reporting cadence: the median gap between consecutive reads,
    in minutes. The median (not the mean) so one long outage doesn't redefine
    "normal". ``None`` when there aren't two distinct reads to measure."""
    if len(times) < 2:
        return None
    ordered = sorted(times)
    gaps = [(b - a).total_seconds() / 60 for a, b in zip(ordered, ordered[1:])]
    gaps = [g for g in gaps if g > 0]
    return round(median(gaps), 1) if gaps else None


def build_connectivity_summary(db, tenant: str) -> dict:
    """Fleet edge-connectivity health: the headline connectivity score, per-state
    counts (fresh / stale / dark), device online rate, signal good-quality rate,
    instrumentation coverage, a worst-first per-machine chase list, and the
    offline devices to reconnect. Every table is auto-scoped (ADR-0002).
    Empty-safe: zeros, no divide-by-zero, rates default to 100 when nothing to
    measure (an empty plant isn't "disconnected")."""
    now = datetime.utcnow()
    window_start = now - timedelta(hours=LOOKBACK_HOURS)

    machines = db.query(models.Machine).all()
    names = {m.id: m.name for m in machines}
    line_of = {m.id: (m.line or "") for m in machines}

    # Most-recent signal + in-window signal count per machine, aggregated in SQL
    # (one row per machine) rather than hydrating every telemetry row in the window
    # on each 30s poll. func.max on a DateTime column comes back as a real datetime,
    # so the freshness math still runs on datetimes, not SQL strings.
    agg = (db.query(models.IoTTelemetry.machine_id,
                    func.max(models.IoTTelemetry.created_at),
                    func.count())
           .filter(models.IoTTelemetry.created_at >= window_start,
                   models.IoTTelemetry.machine_id.isnot(None))
           .group_by(models.IoTTelemetry.machine_id).all())
    last_seen = {mid: last for mid, last, _ in agg}
    signal_count = {mid: cnt for mid, _, cnt in agg}

    # DARK vs STALE only matters for machines with NO in-window signal — probe just
    # those ids for any historical telemetry, instead of a DISTINCT over the whole
    # (unbounded, ever-growing) telemetry history on every poll.
    candidates = [m.id for m in machines if m.id not in last_seen]
    ever = set()
    if candidates:
        ever = {mid for (mid,) in
                db.query(models.IoTTelemetry.machine_id)
                .filter(models.IoTTelemetry.machine_id.in_(candidates)).distinct().all()}

    # Devices: the registered edge estate. Linked machines = instrumentation coverage.
    devices = db.query(models.IndustrialDevice).all()
    linked_machine_ids = {d.linked_machine_id for d in devices if d.linked_machine_id is not None}

    rows = []
    fresh = stale = dark = 0
    for m in machines:
        last = last_seen.get(m.id)
        if last is not None:
            mins = round((now - last).total_seconds() / 60, 1)
            state = "fresh" if mins <= STALE_AFTER_MINUTES else "stale"
            last_at = last.isoformat()
        elif m.id in ever:
            state, mins, last_at = "stale", None, None   # reported, but before the window
        else:
            state, mins, last_at = "dark", None, None     # never reported

        if state == "fresh":
            fresh += 1
        elif state == "stale":
            stale += 1
        else:
            dark += 1

        rows.append({
            "machine_id": m.id,
            "name": names.get(m.id, f"#{m.id}"),
            "line": line_of.get(m.id, ""),
            "state": state,
            "last_signal_minutes": mins,
            "last_signal_at": last_at,
            "signals": signal_count.get(m.id, 0),
            "linked": m.id in linked_machine_ids,
        })

    # Worst first: DARK, then STALE by longest silence, then FRESH. Unmeasured
    # silence (older than lookback) ranks as the most silent stale machine.
    rows.sort(key=lambda r: (
        _STATE_RANK[r["state"]],
        -(r["last_signal_minutes"] if r["last_signal_minutes"] is not None else _UNMEASURED_SILENCE),
    ))

    machines_tracked = len(machines)
    reporting = fresh + stale
    connectivity_score = round(fresh / machines_tracked * 100, 1) if machines_tracked else 100.0

    # Device online rate + protocol / status breakdowns.
    dev_total = len(devices)
    dev_online = sum(1 for d in devices if _is_online(d.status))
    by_protocol = [
        {"protocol": p, "count": c}
        for p, c in Counter((d.protocol or "Unknown") for d in devices).most_common()
    ]
    by_status = [
        {"status": s, "count": c}
        for s, c in Counter((d.status or "Unknown") for d in devices).most_common()
    ]
    offline_devices = [
        {
            "device_code": d.device_code,
            "device_name": d.device_name,
            "device_type": d.device_type,
            "protocol": d.protocol,
            "status": d.status,
            "linked_machine": names.get(d.linked_machine_id) if d.linked_machine_id else None,
        }
        for d in devices if not _is_online(d.status)
    ][:TOP_N]

    # Signal good-quality rate over the lookback window — bad-quality reads mean
    # the link is up but the data is suspect.
    signals = db.query(models.IndustrialSignal).filter(models.IndustrialSignal.created_at >= window_start).all()
    sig_total = len(signals)
    sig_good = sum(1 for s in signals if _is_good(s.quality))

    # Instrumentation coverage: machines wired to at least one edge device.
    linked = sum(1 for m in machines if m.id in linked_machine_ids)

    # The connection to fix first: the worst (dark, else stalest) machine.
    needs_attention = rows[0] if rows and rows[0]["state"] != "fresh" else None

    return {
        "stale_after_minutes": STALE_AFTER_MINUTES,
        "lookback_hours": LOOKBACK_HOURS,
        "machines_tracked": machines_tracked,
        "reporting": reporting,
        "fresh": fresh,
        "stale": stale,
        "dark": dark,
        "connectivity_score": connectivity_score,
        "devices": {
            "total": dev_total,
            "online": dev_online,
            "offline": dev_total - dev_online,
            "online_rate": round(dev_online / dev_total * 100, 1) if dev_total else 100.0,
            "by_protocol": by_protocol,
            "by_status": by_status,
        },
        "signal_quality": {
            "total": sig_total,
            "good": sig_good,
            "bad": sig_total - sig_good,
            "good_rate": round(sig_good / sig_total * 100, 1) if sig_total else 100.0,
        },
        "instrumentation": {
            "linked": linked,
            "coverage": round(linked / machines_tracked * 100, 1) if machines_tracked else 0.0,
        },
        "by_machine": rows[:TOP_N],
        "needs_attention": needs_attention,
        "offline_devices": offline_devices,
    }


def build_connection_detail(db, tenant: str, machine_id: int) -> dict:
    """Drill-down for one machine's edge connection: *what am I blind on while
    this is silent, and what do I go and fix?*

    The summary says a machine is stale or dark. This turns that into an
    engineer's work order — the connection's own state and last signal, its
    normal reporting cadence and how far past it the silence has run, a per-tag
    breakdown (a machine can keep reporting utilisation while its temperature tag
    quietly dies — partial blindness the headline can't show), the edge devices
    wired to it with their protocol and status, the read quality on those
    devices, the open work orders whose live progress is unverified while it's
    quiet, and a ranked list of blind spots.

    Composes machines + iot_telemetry + industrial_devices + industrial_signals +
    work_orders (auto-scoped, ADR-0002); adds no storage. Returns
    ``found: False`` with a zeroed shape when the machine isn't in the tenant."""
    now = datetime.utcnow()
    window_start = now - timedelta(hours=LOOKBACK_HOURS)

    machine = db.query(models.Machine).filter(models.Machine.id == machine_id).first()
    if machine is None:
        return {
            "found": False, "machine_id": machine_id, "name": None, "line": "", "status": None,
            "stale_after_minutes": STALE_AFTER_MINUTES, "lookback_hours": LOOKBACK_HOURS,
            "state": "dark", "last_signal_at": None, "last_signal_minutes": None,
            "signals": 0, "cadence_minutes": None, "overdue_multiple": None,
            "by_signal": [], "dropped_signals": 0, "signal_tags": 0,
            "devices": [], "linked": False,
            "signal_quality": {"total": 0, "good": 0, "bad": 0, "good_rate": 100.0},
            "open_work_orders": {"count": 0, "orders": []},
            "blind_spots": [], "recent": [],
        }

    # This machine's telemetry inside the lookback window, plus an ever-reported
    # probe so a machine that fell silent before the window is STALE, not DARK.
    reads = (db.query(models.IoTTelemetry)
             .filter(models.IoTTelemetry.machine_id == machine_id,
                     models.IoTTelemetry.created_at >= window_start).all())
    reads = [t for t in reads if t.created_at]
    ever = bool(reads) or db.query(models.IoTTelemetry.id).filter(
        models.IoTTelemetry.machine_id == machine_id).first() is not None

    last = max((t.created_at for t in reads), default=None)
    if last is not None:
        silence = round((now - last).total_seconds() / 60, 1)
        state = "fresh" if silence <= STALE_AFTER_MINUTES else "stale"
        last_at = last.isoformat()
    elif ever:
        state, silence, last_at = "stale", None, None
    else:
        state, silence, last_at = "dark", None, None

    # Cadence: how often this connection normally speaks, and how far past that
    # the current silence has run. "40 minutes quiet" means nothing without it.
    cadence = _cadence_minutes([t.created_at for t in reads])
    overdue = round(silence / cadence, 1) if (cadence and silence is not None) else None

    # Per-tag breakdown — which signals are still alive and which have dropped.
    per_tag: dict = defaultdict(list)
    for t in reads:
        per_tag[(t.signal_name or "unnamed").strip() or "unnamed"].append(t)
    by_signal = []
    for tag, rows in per_tag.items():
        newest = max(rows, key=lambda t: t.created_at)
        tag_cadence = _cadence_minutes([t.created_at for t in rows])
        quiet = round((now - newest.created_at).total_seconds() / 60, 1)
        by_signal.append({
            "signal_name": tag,
            "reads": len(rows),
            "last_value": newest.signal_value,
            "unit": newest.unit,
            "source": newest.source,
            "last_at": newest.created_at.isoformat(),
            "silent_minutes": quiet,
            "cadence_minutes": tag_cadence,
            "dropped": bool(tag_cadence and quiet > tag_cadence * DROPPED_CADENCE_MULTIPLE),
        })
    # Worst first: dropped tags lead, then the longest silence.
    by_signal.sort(key=lambda s: (not s["dropped"], -s["silent_minutes"]))
    dropped = [s for s in by_signal if s["dropped"]]

    # The edge estate wired to this machine, with the read quality per device.
    devices = db.query(models.IndustrialDevice).filter(
        models.IndustrialDevice.linked_machine_id == machine_id).all()
    device_ids = [d.id for d in devices]
    sigs = []
    if device_ids:
        sigs = (db.query(models.IndustrialSignal)
                .filter(models.IndustrialSignal.device_id.in_(device_ids),
                        models.IndustrialSignal.created_at >= window_start).all())
    per_device: dict = defaultdict(lambda: {"total": 0, "good": 0, "last": None})
    for s in sigs:
        agg = per_device[s.device_id]
        agg["total"] += 1
        agg["good"] += 1 if _is_good(s.quality) else 0
        if s.created_at and (agg["last"] is None or s.created_at > agg["last"]):
            agg["last"] = s.created_at

    device_rows = []
    for d in devices:
        agg = per_device.get(d.id, {"total": 0, "good": 0, "last": None})
        device_rows.append({
            "device_code": d.device_code,
            "device_name": d.device_name,
            "device_type": d.device_type,
            "protocol": d.protocol,
            "ip_address": d.ip_address,
            "topic": d.topic,
            "status": d.status,
            "online": _is_online(d.status),
            "signals": agg["total"],
            "bad_signals": agg["total"] - agg["good"],
            "last_signal_at": agg["last"].isoformat() if agg["last"] else None,
        })
    device_rows.sort(key=lambda d: (d["online"], -d["bad_signals"]))

    sig_total = len(sigs)
    sig_good = sum(1 for s in sigs if _is_good(s.quality))

    # What the silence costs: the jobs on this machine whose live progress is
    # unverified while it isn't reporting.
    open_wos = [
        w for w in db.query(models.WorkOrder).filter(models.WorkOrder.machine_id == machine_id).all()
        if (w.status or "").strip().lower() not in _CLOSED_WO_STATUSES
    ]
    orders = [{
        "work_order_no": w.work_order_no,
        "part_number": w.part_number,
        "status": w.status,
        "target": w.target_quantity or 0,
        "actual": w.actual_quantity or 0,
    } for w in sorted(open_wos, key=lambda w: w.work_order_no or "")[:TOP_N]]

    # Blind spots, worst first — each one grounded in a number above.
    blind: list = []
    if not devices:
        blind.append({"severity": "high", "message":
                      "No edge device is registered against this machine — nothing is wired to report it."})
    offline = [d for d in device_rows if not d["online"]]
    if offline:
        blind.append({"severity": "high", "message":
                      f"{len(offline)} linked device{'s are' if len(offline) != 1 else ' is'} offline: "
                      + ", ".join(d["device_name"] for d in offline[:3]) + "."})
    if state == "dark":
        blind.append({"severity": "high", "message":
                      "Never reported telemetry — every live number shown for this machine is assumed, not measured."})
    elif state == "stale":
        blind.append({"severity": "high", "message":
                      "Telemetry has stopped" + (f" ({_ago(silence)} silent)" if silence is not None else "")
                      + f" — it still shows as \"{machine.status or 'unknown'}\", but that state is unverified."})
    if state != "fresh" and orders:
        blind.append({"severity": "high", "message":
                      f"{len(open_wos)} open work order{'s' if len(open_wos) != 1 else ''} on this machine "
                      "cannot report live progress while it is silent."})
    if dropped:
        blind.append({"severity": "medium", "message":
                      f"{len(dropped)} of {len(by_signal)} signal tags stopped reporting ("
                      + ", ".join(s["signal_name"] for s in dropped[:3])
                      + ") while the connection is otherwise up — partial blindness."})
    if sig_total and sig_good < sig_total:
        blind.append({"severity": "medium", "message":
                      f"{sig_total - sig_good} of {sig_total} device reads came back bad quality — "
                      "the link is up but the values are suspect."})

    return {
        "found": True,
        "machine_id": machine_id,
        "name": machine.name,
        "line": machine.line or "",
        "status": machine.status,
        "stale_after_minutes": STALE_AFTER_MINUTES,
        "lookback_hours": LOOKBACK_HOURS,
        "state": state,
        "last_signal_at": last_at,
        "last_signal_minutes": silence,
        "signals": len(reads),
        "cadence_minutes": cadence,
        "overdue_multiple": overdue,
        "by_signal": by_signal[:TOP_N],
        "dropped_signals": len(dropped),
        "signal_tags": len(by_signal),   # total tags (pre-truncation) — the honest denominator
        "devices": device_rows,
        "linked": bool(devices),
        "signal_quality": {
            "total": sig_total,
            "good": sig_good,
            "bad": sig_total - sig_good,
            "good_rate": round(sig_good / sig_total * 100, 1) if sig_total else 100.0,
        },
        "open_work_orders": {"count": len(open_wos), "orders": orders},
        "blind_spots": blind,
        "recent": [{
            "signal_name": t.signal_name,
            "signal_value": t.signal_value,
            "unit": t.unit,
            "source": t.source,
            "at": t.created_at.isoformat(),
        } for t in sorted(reads, key=lambda t: t.created_at, reverse=True)[:RECENT_N]],
    }
