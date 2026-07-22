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


if __name__ == "__main__":
    test_connectivity_classifies_fresh_stale_dark_and_scores()
    test_connectivity_is_empty_safe()
    test_connectivity_all_fresh_scores_100()
    print("CONNECTIVITY OK: fresh/stale/dark classification; connectivity score; device online rate; "
          "signal good-quality rate; instrumentation coverage; worst-first chase list; empty-safe")
