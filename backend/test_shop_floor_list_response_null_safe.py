"""The three oldest shop-floor lists survive a legacy NULL row (response heal).

GET /downtime-logs, GET /shifts and GET /production-records each serialise their
rows through a response model whose fields are typed non-optional, while the
columns behind them are declared ``nullable=False`` WITHOUT a Python-side default:

    DowntimeLog.reason / .duration           Column(String,  nullable=False)
    ShiftData.target_output / .actual_output Column(Integer, nullable=False)
    ProductionRecord.planned_minutes / .runtime_minutes / .ideal_cycle_time_seconds
                    / .total_count / .good_count / .rejected_count
                                             Column(Integer, nullable=False)

A ``nullable=False`` constraint is NOT retro-applied to a row written by raw SQL,
a migration, or a legacy insert — exactly the reason analytics_engine coalesces
these same columns with ``... or 0`` and normalize_downtime_reason coalesces the
reason to "Unknown". So a genuine NULL is reachable, and because the three
response models typed the fields non-optional and carried NO heal, one such NULL
row raised Pydantic ValidationError during serialisation and 500-ed the WHOLE
list — one bad row hiding every good one, permanently, until repaired in the DB.

This is the identical defect already healed on MachineResponse (#375),
QualityInspectionResponse (#423/#447), ProductionPlanResponse (#465) and a dozen
others. These three were missed because their model class names (DowntimeLog /
ShiftData / ProductionRecord) don't match their schema stems (Downtime / Shift /
Production), so the response-null-heal guard's name-based sweep never inspected
them.

The heal coalesces each NULL to the SAME value every other reader already uses —
0 for the counts/minutes (matching calculate_oee_from_record / build_shift_kpis /
the CSV exports), "Unknown" for the reason (matching normalize_downtime_reason),
"" for the duration (matching the CSV's blank cell, which parse_duration_to_minutes
reads as 0) — so the list stays on the same basis as the OEE/attainment figures it
reconciles with. A real recorded value, including a real 0, is untouched.

SQLite enforces NOT NULL, so a real on-disk NULL can only predate the constraint;
the exact attribute state a loaded legacy-NULL row presents (the attribute is None
when read) is reproduced here with a transient ORM instance and validated exactly
as the FastAPI response layer validates each row the list handler returns — which
is the serialisation step that used to raise. (Same technique the sibling
test_production_planning_null_safe.py uses for its own nullable=False column.)

Run:  cd backend && DATABASE_URL="sqlite:///./ci.db" python test_shop_floor_list_response_null_safe.py   (exit 0 = pass)
"""
import models
import schemas
from duration import parse_duration_to_minutes


def _serialise_list(rows, schema_cls):
    """Mirror FastAPI's response_model=List[schema_cls]: validate every row. This
    is the exact step that raised ResponseValidationError (-> 500) on a NULL row."""
    return [schema_cls.model_validate(r) for r in rows]


# ── DowntimeResponse ─────────────────────────────────────────────────────────

def test_downtime_response_heals_null_reason_and_duration():
    bad = models.DowntimeLog(id=1, machine_id=5, reason=None, duration=None, notes=None)
    good = models.DowntimeLog(id=2, machine_id=5, reason="Tool Change",
                              duration="2 hrs 15 min", notes="ok")
    bad_s = schemas.DowntimeResponse.model_validate(bad)     # was a 500
    good_s = schemas.DowntimeResponse.model_validate(good)   # must NOT raise

    # NULL reason -> the canonical label for an unlabelled stop (normalize_downtime_reason).
    assert bad_s.reason == "Unknown", repr(bad_s.reason)
    # NULL duration renders blank (not recorded) and reads back as 0 minutes, exactly
    # as the reports CSV and parse_duration_to_minutes already treat a NULL duration.
    assert bad_s.duration == "", repr(bad_s.duration)
    assert parse_duration_to_minutes(bad_s.duration) == 0
    # A real reason/duration passes through verbatim.
    assert good_s.reason == "Tool Change"
    # Independently derived: 2 hrs 15 min = 2*60 + 15 = 135 (NOT 2, the old hour bug).
    assert good_s.duration == "2 hrs 15 min"
    assert parse_duration_to_minutes(good_s.duration) == 135
    print("PASS DowntimeResponse heals NULL reason->Unknown / duration->'' , preserves real values")


def test_downtime_list_one_null_row_does_not_hide_the_rest():
    rows = [
        models.DowntimeLog(id=1, machine_id=5, reason="Breakdown", duration="30 min", notes="x"),
        models.DowntimeLog(id=2, machine_id=5, reason=None, duration=None, notes=None),  # legacy
    ]
    out = _serialise_list(rows, schemas.DowntimeResponse)    # must not raise on the NULL row
    assert len(out) == 2, len(out)                           # the good row is not hidden
    assert {r.reason for r in out} == {"Breakdown", "Unknown"}
    assert all(isinstance(r.reason, str) and isinstance(r.duration, str) for r in out)
    print("PASS GET /downtime-logs: a legacy NULL row no longer 500s the whole list")


# ── ShiftResponse ────────────────────────────────────────────────────────────

def test_shift_response_heals_null_outputs():
    bad = models.ShiftData(id=1, shift_name="Night", target_output=None, actual_output=None)
    bad_s = schemas.ShiftResponse.model_validate(bad)
    assert bad_s.target_output == 0 and bad_s.actual_output == 0, (bad_s.target_output, bad_s.actual_output)
    assert isinstance(bad_s.target_output, int)
    # A real recorded 0 target with a real 500 actual is NOT confused with a healed NULL.
    real = schemas.ShiftResponse.model_validate(
        models.ShiftData(id=2, shift_name="Day", target_output=0, actual_output=500))
    assert real.target_output == 0 and real.actual_output == 500
    print("PASS ShiftResponse heals NULL target/actual->0, preserves a real 0 and real value")


def test_shift_list_one_null_row_does_not_hide_the_rest():
    rows = [
        models.ShiftData(id=1, shift_name="Day", target_output=600, actual_output=550),
        models.ShiftData(id=2, shift_name="Legacy", target_output=None, actual_output=None),
    ]
    out = _serialise_list(rows, schemas.ShiftResponse)
    assert len(out) == 2, len(out)
    by_name = {r.shift_name: r for r in out}
    assert by_name["Day"].actual_output == 550               # real value intact
    assert by_name["Legacy"].target_output == 0 and by_name["Legacy"].actual_output == 0
    print("PASS GET /shifts: a legacy NULL row no longer 500s the whole list")


# ── ProductionResponse ───────────────────────────────────────────────────────

_PROD_FIELDS = ("planned_minutes", "runtime_minutes", "ideal_cycle_time_seconds",
                "total_count", "good_count", "rejected_count")


def test_production_response_heals_null_counts():
    bad = models.ProductionRecord(id=1, machine_id=5, planned_minutes=None, runtime_minutes=None,
                                  ideal_cycle_time_seconds=None, total_count=None, good_count=None,
                                  rejected_count=None)
    bad_s = schemas.ProductionResponse.model_validate(bad)
    for field in _PROD_FIELDS:
        assert getattr(bad_s, field) == 0, (field, getattr(bad_s, field))
        assert isinstance(getattr(bad_s, field), int)
    # Real values survive, a real 0 among them would too (mode="before" only rewrites None).
    real = schemas.ProductionResponse.model_validate(
        models.ProductionRecord(id=2, machine_id=5, planned_minutes=480, runtime_minutes=400,
                                ideal_cycle_time_seconds=60, total_count=500, good_count=480,
                                rejected_count=20))
    assert (real.planned_minutes, real.total_count, real.good_count, real.rejected_count) == (480, 500, 480, 20)
    print("PASS ProductionResponse heals NULL counts/minutes->0, preserves real values")


def test_production_list_one_null_row_does_not_hide_the_rest():
    rows = [
        models.ProductionRecord(id=1, machine_id=5, planned_minutes=480, runtime_minutes=400,
                                ideal_cycle_time_seconds=60, total_count=500, good_count=480,
                                rejected_count=20),
        models.ProductionRecord(id=2, machine_id=5, planned_minutes=None, runtime_minutes=None,
                                ideal_cycle_time_seconds=None, total_count=None, good_count=None,
                                rejected_count=None),
    ]
    out = _serialise_list(rows, schemas.ProductionResponse)
    assert len(out) == 2, len(out)
    assert sorted(r.total_count for r in out) == [0, 500]    # healed 0 + the real 500
    assert all(isinstance(getattr(r, f), int) for r in out for f in _PROD_FIELDS)
    print("PASS GET /production-records: a legacy NULL row no longer 500s the whole list")


if __name__ == "__main__":
    test_downtime_response_heals_null_reason_and_duration()
    test_downtime_list_one_null_row_does_not_hide_the_rest()
    test_shift_response_heals_null_outputs()
    test_shift_list_one_null_row_does_not_hide_the_rest()
    test_production_response_heals_null_counts()
    test_production_list_one_null_row_does_not_hide_the_rest()
    print("ALL SHOP-FLOOR LIST NULL-SAFE TESTS PASSED")
