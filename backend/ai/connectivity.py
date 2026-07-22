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

A pure read-model over machines + iot_telemetry + industrial_devices +
industrial_signals, auto-scoped to the tenant (ADR-0002); it adds no storage.
Freshness math runs on real ORM datetimes (never a SQL aggregate that SQLite
would hand back as a string): a distinct-ids probe finds the ever-reported set,
a bounded lookback window supplies the most-recent timestamp per machine.
"""
from collections import Counter
from datetime import datetime, timedelta

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

_STATE_RANK = {"dark": 0, "stale": 1, "fresh": 2}
# A silence we can't measure (reported before the lookback window) sorts as the
# most silent of the stale machines — larger than any real minute count.
_UNMEASURED_SILENCE = 10 ** 9


def _is_online(status) -> bool:
    return (status or "").strip().lower() == "online"


def _is_good(quality) -> bool:
    return (quality or "").strip().lower() == "good"


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

    # Machines that have EVER reported telemetry — distinguishes DARK (never seen)
    # from STALE (seen once, gone quiet). Cheap: distinct ids, no row transfer.
    ever = {
        mid for (mid,) in db.query(models.IoTTelemetry.machine_id).distinct().all()
        if mid is not None
    }

    # Most-recent signal + in-window signal count per machine, from real datetimes.
    recent = db.query(models.IoTTelemetry).filter(models.IoTTelemetry.created_at >= window_start).all()
    last_seen: dict = {}
    signal_count: Counter = Counter()
    for t in recent:
        if t.machine_id is None or not t.created_at:
            continue
        signal_count[t.machine_id] += 1
        if t.machine_id not in last_seen or t.created_at > last_seen[t.machine_id]:
            last_seen[t.machine_id] = t.created_at

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
