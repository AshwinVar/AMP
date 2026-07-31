"""GET /iot/telemetry and /industrial/signals survive a NULL numeric_value (response heal).

IoTTelemetry.numeric_value and IndustrialSignal.numeric_value are both declared
``Column(Integer, default=0)`` WITHOUT ``nullable=False`` — the ORM default only
fills a value the *inserter* omitted, so a row written by raw SQL, a migration, or
a legacy insert can legitimately hold NULL. IoTTelemetryResponse /
IndustrialSignalResponse type numeric_value as a non-optional ``int``, so a single
such NULL row raised Pydantic ValidationError during response serialisation and
500-ed the WHOLE list endpoint — one bad reading hiding every good signal. This is
the same class already healed on the machine / quality-inspection / maintenance-task
/ operator-execution lists (#423/#447).

The response models now coalesce a NULL numeric_value to the column's own declared
default of 0 on the way out. A real recorded value — including a real 0 — is
untouched (the mode="before" heal only rewrites None).

Run:  cd backend && DATABASE_URL="sqlite:///./ci.db" python test_industrial_signal_response_null_safe.py   (exit 0 = pass)
"""
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import industrial_iot_routes as IIR
import models
import schemas
import tenancy as T
from database import Base


def _iso_session():
    T.install_scoping()
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _seed_normal_telemetry(db, tenant, numeric_value):
    """A fully-populated telemetry row — proves real values (incl. a real 0) survive."""
    tok = T.set_current_tenant(tenant)
    try:
        row = models.IoTTelemetry(
            machine_id=1, signal_name="temperature", signal_value=str(numeric_value),
            numeric_value=numeric_value, unit="C", source="MQTT",
        )
        db.add(row)
        db.commit()
    finally:
        T.reset_current_tenant(tok)


def _insert_null_telemetry(db, tenant):
    """A legacy telemetry row that left numeric_value NULL."""
    db.execute(
        text(
            "INSERT INTO iot_telemetry "
            "(tenant_code, machine_id, signal_name, signal_value, numeric_value, unit, source) "
            "VALUES (:t, 1, 'temperature', 'sensor-fault', NULL, 'C', 'MQTT')"
        ),
        {"t": tenant},
    )
    db.commit()
    db.expire_all()


def _seed_normal_signal(db, tenant, numeric_value):
    tok = T.set_current_tenant(tenant)
    try:
        row = models.IndustrialSignal(
            device_id=1, machine_id=1, signal_name="flow_rate",
            signal_value=str(numeric_value), numeric_value=numeric_value,
            unit="L/min", quality="Good", source_protocol="Modbus TCP",
        )
        db.add(row)
        db.commit()
    finally:
        T.reset_current_tenant(tok)


def _insert_null_signal(db, tenant):
    db.execute(
        text(
            "INSERT INTO industrial_signals "
            "(tenant_code, device_id, machine_id, signal_name, signal_value, "
            " numeric_value, unit, quality, source_protocol) "
            "VALUES (:t, 1, 1, 'flow_rate', 'sensor-fault', NULL, 'L/min', 'Good', 'Modbus TCP')"
        ),
        {"t": tenant},
    )
    db.commit()
    db.expire_all()


def test_iot_telemetry_response_heals_null_numeric_value():
    # Serialise EXACTLY as FastAPI does (response_model + from_attributes); this is
    # the step that used to raise ValidationError on a NULL numeric_value.
    db = _iso_session()
    _seed_normal_telemetry(db, "DEFAULT", numeric_value=42)
    _seed_normal_telemetry(db, "DEFAULT", numeric_value=0)   # a REAL recorded 0
    _insert_null_telemetry(db, "DEFAULT")

    rows = db.query(models.IoTTelemetry).order_by(models.IoTTelemetry.id).all()
    serialised = [schemas.IoTTelemetryResponse.model_validate(r) for r in rows]
    values = [s.numeric_value for s in serialised]

    # Independently-derived: [42 (real), 0 (real), 0 (healed NULL)] — three rows, none dropped.
    assert values == [42, 0, 0], values
    # The healed NULL row still carries its other columns (not blanked).
    healed = serialised[2]
    assert healed.signal_value == "sensor-fault", healed.signal_value
    print("PASS IoTTelemetryResponse heals a NULL numeric_value to 0, preserves a real 0 and real values")


def test_industrial_signal_response_heals_null_numeric_value():
    db = _iso_session()
    _seed_normal_signal(db, "DEFAULT", numeric_value=88)
    _seed_normal_signal(db, "DEFAULT", numeric_value=0)      # a REAL recorded 0
    _insert_null_signal(db, "DEFAULT")

    rows = db.query(models.IndustrialSignal).order_by(models.IndustrialSignal.id).all()
    serialised = [schemas.IndustrialSignalResponse.model_validate(r) for r in rows]
    values = [s.numeric_value for s in serialised]

    assert values == [88, 0, 0], values
    healed = serialised[2]
    assert healed.signal_value == "sensor-fault", healed.signal_value
    print("PASS IndustrialSignalResponse heals a NULL numeric_value to 0, preserves a real 0 and real values")


def test_list_endpoints_survive_null_row():
    # End-to-end: the whole GET list serialises without a 500 even with a legacy NULL
    # row present, and every good reading still comes back.
    db = _iso_session()
    _seed_normal_telemetry(db, "DEFAULT", numeric_value=42)
    _insert_null_telemetry(db, "DEFAULT")
    _seed_normal_signal(db, "DEFAULT", numeric_value=88)
    _insert_null_signal(db, "DEFAULT")

    tok = T.set_current_tenant("DEFAULT")
    try:
        telemetry_rows = IIR.get_iot_telemetry(db=db, current_user={"tenant": "DEFAULT"})
        signal_rows = IIR.get_industrial_signals(db=db, current_user={"tenant": "DEFAULT"})
        telemetry = [schemas.IoTTelemetryResponse.model_validate(r) for r in telemetry_rows]
        signals = [schemas.IndustrialSignalResponse.model_validate(r) for r in signal_rows]
    finally:
        T.reset_current_tenant(tok)

    # Both the good row and the NULL row come back; the NULL heals to 0.
    assert len(telemetry) == 2, len(telemetry)
    assert len(signals) == 2, len(signals)
    assert all(isinstance(s.numeric_value, int) for s in telemetry)
    assert all(isinstance(s.numeric_value, int) for s in signals)
    assert sorted(s.numeric_value for s in telemetry) == [0, 42]
    assert sorted(s.numeric_value for s in signals) == [0, 88]
    print("PASS GET /iot/telemetry and /industrial/signals return the full list with a NULL row present (no 500)")


if __name__ == "__main__":
    test_iot_telemetry_response_heals_null_numeric_value()
    test_industrial_signal_response_heals_null_numeric_value()
    test_list_endpoints_survive_null_row()
    print("ALL INDUSTRIAL-SIGNAL-RESPONSE NULL-SAFE TESTS PASSED")
