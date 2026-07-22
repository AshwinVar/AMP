"""Edge connectivity read-model tests (ADR-0007).

Reframes iot_telemetry + industrial_devices + industrial_signals into a
connectivity health picture: fresh / stale / dark machines, the connectivity
score, device online rate, signal good-quality rate, instrumentation coverage,
and a worst-first chase list. Run:  python backend/test_connectivity.py  (exit 0 = pass)
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base
from ai import connectivity


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _machine(db, id_, name, line="SMT"):
    db.add(models.Machine(id=id_, name=name, status="Running", utilization=90, line=line))


def _telemetry(db, machine_id, minutes_ago, signal_name="utilization"):
    db.add(models.IoTTelemetry(
        machine_id=machine_id, signal_name=signal_name, signal_value="88",
        numeric_value=88, source="MQTT",
        created_at=datetime.utcnow() - timedelta(minutes=minutes_ago),
    ))


def _device(db, code, name, status="Online", protocol="MQTT", linked=None):
    db.add(models.IndustrialDevice(
        device_code=code, device_name=name, device_type="PLC",
        protocol=protocol, status=status, linked_machine_id=linked,
    ))


def _signal(db, device_id, quality, minutes_ago=5):
    db.add(models.IndustrialSignal(
        device_id=device_id, signal_name="temp", signal_value="70",
        numeric_value=70, quality=quality, source_protocol="MQTT",
        created_at=datetime.utcnow() - timedelta(minutes=minutes_ago),
    ))


def test_connectivity_classifies_fresh_stale_dark_and_scores():
    db = _fresh_session()
    _machine(db, 1, "SMT-Reflow-01", "SMT")   # fresh: reported 2 min ago
    _machine(db, 2, "IC-Test-01", "IC")        # stale: last signal 90 min ago
    _machine(db, 3, "SMT-Printer-01", "SMT")   # stale: reported, but before lookback
    _machine(db, 4, "SMT-AOI-01", "SMT")       # dark: never reported

    _telemetry(db, 1, minutes_ago=2)
    _telemetry(db, 1, minutes_ago=8)           # two fresh reads -> signals count 2
    _telemetry(db, 2, minutes_ago=90)          # within lookback but past freshness
    _telemetry(db, 3, minutes_ago=60 * 30)     # 30h ago: older than 24h lookback

    # Devices: one online, one offline; machine 1 instrumented.
    _device(db, "DEV-1", "Reflow PLC", status="Online", protocol="MQTT", linked=1)
    _device(db, "DEV-2", "AOI Gateway", status="Offline", protocol="OPC-UA", linked=4)
    db.commit()
    # signals need device ids after commit
    dev1 = db.query(models.IndustrialDevice).filter_by(device_code="DEV-1").first()
    _signal(db, dev1.id, "Good")
    _signal(db, dev1.id, "Good")
    _signal(db, dev1.id, "Bad")
    db.commit()

    r = connectivity.build_connectivity_summary(db, "DEFAULT")

    assert r["machines_tracked"] == 4
    assert r["fresh"] == 1 and r["stale"] == 2 and r["dark"] == 1
    assert r["reporting"] == 3
    # connectivity score = fresh / tracked = 1/4 = 25%
    assert r["connectivity_score"] == 25.0

    by = {m["name"]: m for m in r["by_machine"]}
    assert by["SMT-Reflow-01"]["state"] == "fresh"
    assert by["SMT-Reflow-01"]["signals"] == 2 and by["SMT-Reflow-01"]["linked"] is True
    assert by["IC-Test-01"]["state"] == "stale" and by["IC-Test-01"]["last_signal_minutes"] is not None
    assert by["SMT-Printer-01"]["state"] == "stale" and by["SMT-Printer-01"]["last_signal_minutes"] is None
    assert by["SMT-AOI-01"]["state"] == "dark" and by["SMT-AOI-01"]["linked"] is True

    # worst-first: dark leads, then stalest. Dark machine tops the list + attention.
    assert r["by_machine"][0]["state"] == "dark"
    assert r["needs_attention"]["name"] == "SMT-AOI-01"

    # devices: 1 of 2 online = 50%; the offline one is on the chase list.
    assert r["devices"]["total"] == 2 and r["devices"]["online"] == 1
    assert r["devices"]["online_rate"] == 50.0
    assert [d["device_code"] for d in r["offline_devices"]] == ["DEV-2"]
    protocols = {p["protocol"]: p["count"] for p in r["devices"]["by_protocol"]}
    assert protocols == {"MQTT": 1, "OPC-UA": 1}

    # signal quality: 2 good of 3 = 66.7%.
    assert r["signal_quality"]["total"] == 3 and r["signal_quality"]["good"] == 2
    assert r["signal_quality"]["good_rate"] == 66.7

    # instrumentation coverage: machines 1 and 4 linked = 2/4 = 50%.
    assert r["instrumentation"]["linked"] == 2 and r["instrumentation"]["coverage"] == 50.0


def test_connectivity_is_empty_safe():
    # No machines, no devices, no telemetry -> zeros, no divide-by-zero, rates
    # default to 100 (an empty plant is not "disconnected").
    r = connectivity.build_connectivity_summary(_fresh_session(), "DEFAULT")
    assert r["machines_tracked"] == 0 and r["by_machine"] == []
    assert r["connectivity_score"] == 100.0
    assert r["devices"]["online_rate"] == 100.0 and r["signal_quality"]["good_rate"] == 100.0
    assert r["instrumentation"]["coverage"] == 0.0
    assert r["needs_attention"] is None and r["offline_devices"] == []


def test_connectivity_all_fresh_scores_100():
    db = _fresh_session()
    _machine(db, 1, "SMT-Reflow-01")
    _machine(db, 2, "IC-Test-01")
    _telemetry(db, 1, minutes_ago=1)
    _telemetry(db, 2, minutes_ago=3)
    db.commit()
    r = connectivity.build_connectivity_summary(db, "DEFAULT")
    assert r["fresh"] == 2 and r["stale"] == 0 and r["dark"] == 0
    assert r["connectivity_score"] == 100.0
    assert r["needs_attention"] is None


def test_connection_detail_explains_a_silent_machine():
    # A machine that has gone quiet: its state, the silence measured against its
    # own cadence, the tags that dropped, the offline device, the unreported work
    # order, and the blind spots that follow from all of it.
    db = _fresh_session()
    _machine(db, 1, "IC-Test-01", "IC")
    # Reported utilisation every 5 minutes, then stopped 45 minutes ago.
    for mins in (45, 50, 55, 60):
        _telemetry(db, 1, minutes_ago=mins, signal_name="utilization")
    # A second tag that died much earlier — partial blindness within the window.
    for mins in (300, 305, 310):
        _telemetry(db, 1, minutes_ago=mins, signal_name="temperature")
    _device(db, "DEV-9", "IC Test PLC", status="Offline", protocol="OPC-UA", linked=1)
    db.add(models.WorkOrder(work_order_no="WO-500", part_number="P-1", batch_number="B-1",
                            machine_id=1, target_quantity=100, actual_quantity=20, status="In Progress"))
    db.commit()
    dev = db.query(models.IndustrialDevice).filter_by(device_code="DEV-9").first()
    _signal(db, dev.id, "Good", minutes_ago=50)
    _signal(db, dev.id, "Bad", minutes_ago=55)
    db.commit()

    r = connectivity.build_connection_detail(db, "DEFAULT", 1)

    assert r["found"] is True and r["name"] == "IC-Test-01" and r["line"] == "IC"
    # Stale: last read 45 min ago, past the 15-minute freshness window.
    assert r["state"] == "stale"
    assert 44 <= r["last_signal_minutes"] <= 47
    assert r["signals"] == 7

    # Cadence is the MEDIAN gap (5 min), so the 4h hole between the two tags
    # doesn't redefine "normal"; the silence is ~9x overdue.
    assert r["cadence_minutes"] == 5.0
    assert r["overdue_multiple"] is not None and r["overdue_multiple"] >= 8

    # Per-tag: temperature dropped (5h quiet against its own 5-min cadence),
    # utilization is also overdue — both flagged, worst (longest silent) first.
    tags = {s["signal_name"]: s for s in r["by_signal"]}
    assert tags["temperature"]["dropped"] is True and tags["temperature"]["reads"] == 3
    assert tags["temperature"]["cadence_minutes"] == 5.0
    assert r["by_signal"][0]["signal_name"] == "temperature"   # longest silence leads
    assert r["dropped_signals"] == 2

    # The device wired to it, and its read quality (1 good of 2).
    assert r["linked"] is True and len(r["devices"]) == 1
    assert r["devices"][0]["device_code"] == "DEV-9" and r["devices"][0]["online"] is False
    assert r["devices"][0]["signals"] == 2 and r["devices"][0]["bad_signals"] == 1
    assert r["signal_quality"]["good_rate"] == 50.0

    # What the silence costs: the open job going unreported.
    assert r["open_work_orders"]["count"] == 1
    assert r["open_work_orders"]["orders"][0]["work_order_no"] == "WO-500"

    # Blind spots name the offline device, the stopped telemetry, the unreported
    # work order, the dropped tags and the suspect reads.
    msgs = " ".join(b["message"] for b in r["blind_spots"])
    assert "offline" in msgs and "IC Test PLC" in msgs
    assert "Telemetry has stopped" in msgs and "unverified" in msgs
    assert "open work order" in msgs
    assert "partial blindness" in msgs and "bad quality" in msgs
    assert r["blind_spots"][0]["severity"] == "high"

    # The raw evidence, newest first.
    assert len(r["recent"]) == 7 and r["recent"][0]["signal_name"] == "utilization"


def test_connection_detail_flags_a_dark_uninstrumented_machine():
    # Never reported, no device linked: dark, and the two structural blind spots.
    db = _fresh_session()
    _machine(db, 7, "SMT-AOI-01")
    db.commit()
    r = connectivity.build_connection_detail(db, "DEFAULT", 7)
    assert r["found"] is True and r["state"] == "dark"
    assert r["signals"] == 0 and r["by_signal"] == [] and r["recent"] == []
    assert r["cadence_minutes"] is None and r["overdue_multiple"] is None
    assert r["linked"] is False and r["devices"] == []
    msgs = " ".join(b["message"] for b in r["blind_spots"])
    assert "No edge device is registered" in msgs and "Never reported telemetry" in msgs


def test_connection_detail_is_clean_for_a_healthy_machine():
    db = _fresh_session()
    _machine(db, 2, "SMT-Reflow-01")
    _telemetry(db, 2, minutes_ago=1)
    _telemetry(db, 2, minutes_ago=6)
    _device(db, "DEV-3", "Reflow PLC", status="Online", linked=2)
    db.commit()
    dev = db.query(models.IndustrialDevice).filter_by(device_code="DEV-3").first()
    _signal(db, dev.id, "Good", minutes_ago=2)
    db.commit()

    r = connectivity.build_connection_detail(db, "DEFAULT", 2)
    assert r["state"] == "fresh" and r["dropped_signals"] == 0
    assert r["signal_quality"]["good_rate"] == 100.0
    assert r["blind_spots"] == []


def test_connection_detail_handles_a_machine_that_is_not_there():
    r = connectivity.build_connection_detail(_fresh_session(), "DEFAULT", 999)
    assert r["found"] is False and r["machine_id"] == 999 and r["name"] is None
    assert r["blind_spots"] == [] and r["devices"] == [] and r["by_signal"] == []
    assert r["open_work_orders"] == {"count": 0, "orders": []}


if __name__ == "__main__":
    test_connectivity_classifies_fresh_stale_dark_and_scores()
    test_connectivity_is_empty_safe()
    test_connectivity_all_fresh_scores_100()
    test_connection_detail_explains_a_silent_machine()
    test_connection_detail_flags_a_dark_uninstrumented_machine()
    test_connection_detail_is_clean_for_a_healthy_machine()
    test_connection_detail_handles_a_machine_that_is_not_there()
    print("CONNECTIVITY OK: fresh/stale/dark classification; connectivity score; device online rate; "
          "signal good-quality rate; instrumentation coverage; worst-first chase list; empty-safe; "
          "drill-down cadence/overdue, dropped tags, linked devices, unreported work orders, blind spots")
