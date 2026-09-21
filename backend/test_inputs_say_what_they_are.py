"""Three inputs AMP accepted and read by nothing now say what they are.

THE DEFECT FAMILY (the #687 shape, found by the 2026-09-21 sweep)
------------------------------------------------------------------
A value a client can send, stored and acknowledged, that nothing then reads:
  * ComplianceDocument.storage_link -- where the document lives -- was stored
    and never shown, and the dashboard's form never asked for it;
  * PlcSignalMapping rows were stored with `enabled: "Yes"` and nothing applied
    them: no code routes `source_signal` into `mes_field` or runs
    `transform_rule`, while README advertised "PLC signal mapping";
  * CostRecord.reference_type / reference_id were written by the costing form
    and read by no figure.

THE RULE THIS FILE PINS
-----------------------
  1. A storage link is shown as a link, so it must be one: http(s) or nothing.
     Blank -> NULL; `javascript:` / `file:` / free text -> refused with the
     reason, on create AND on update.
  2. Every mapping row the API returns says `applied: false`, on create and on
     the list, and nothing in the ingest path reads the mapping table.
  3. The cost reference is data: accepted as before, and the costing figures
     still group by cost_type and department only (nothing sums by reference).

Run:  DATABASE_URL="sqlite:///./ci.db" python backend/test_inputs_say_what_they_are.py
"""
import os
import sys
from datetime import date

import pydantic
from fastapi import Response
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import factory_ops_routes
import industrial_iot_routes
import models
import schemas
from database import Base

HERE = os.path.dirname(os.path.abspath(__file__))
failures = []


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}" + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


def _session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _doc(**kw):
    kw.setdefault("document_no", "SOP-001")
    kw.setdefault("title", "Line start-up")
    kw.setdefault("document_type", "SOP")
    kw.setdefault("department", "Production")
    kw.setdefault("owner", "QA Lead")
    kw.setdefault("review_due_date", date(2026, 12, 1))
    return schemas.ComplianceDocumentCreate(**kw)


def main_():
    print("=" * 74)
    print("1. A DOCUMENT'S STORAGE LINK IS A LINK: HTTP(S) OR NOTHING")
    print("=" * 74)
    check("an https link is kept", _doc(storage_link="https://dms.example.com/sop-001").storage_link
          == "https://dms.example.com/sop-001")
    check("an http link is kept", _doc(storage_link="http://intranet/sop").storage_link == "http://intranet/sop")
    check("...trimmed", _doc(storage_link="  https://dms.example.com/x  ").storage_link == "https://dms.example.com/x")
    check("no link -> None", _doc().storage_link is None)
    check("a blank link -> None (the dashboard's old form always sent one)", _doc(storage_link="   ").storage_link is None)
    for bad in ("javascript:alert(1)", "file:///C:/sops/sop.pdf", "ftp://files/sop.pdf", "S:\\shared\\sop.pdf"):
        try:
            _doc(storage_link=bad)
            check(f"{bad[:12]}... is refused", False, "no ValidationError")
        except pydantic.ValidationError as exc:
            check(f"{bad[:12]}... is refused, with the reason", "http(s)" in str(exc), str(exc)[:160])
    try:
        schemas.ComplianceDocumentUpdate(storage_link="javascript:alert(1)")
        check("...and on update too", False, "no ValidationError")
    except pydantic.ValidationError as exc:
        check("...and on update too", "http(s)" in str(exc), str(exc)[:160])
    check("an update may clear it", schemas.ComplianceDocumentUpdate(storage_link="").storage_link is None)
    db = _session()
    created = factory_ops_routes.create_document(
        _doc(storage_link="https://dms.example.com/sop-001"), db=db, current_user={"role": "Admin", "sub": "qa"})
    row = db.query(models.ComplianceDocument).filter_by(document_no="SOP-001").first()
    check("the route stores the link and the response carries it",
          row is not None and row.storage_link == "https://dms.example.com/sop-001"
          and getattr(created, "storage_link", None) == "https://dms.example.com/sop-001", str(getattr(created, "storage_link", None)))
    db.close()
    page = os.path.join(HERE, "..", "frontend", "components", "DocumentsSection.tsx")
    with open(page, encoding="utf-8") as f:
        src = f.read()
    check("the Documents screen asks for the link and shows it as one",
          'placeholder="Link to the document' in src and "documentLink(row.storage_link)" in src and "<a href=" in src)
    check("...and opens only http(s) (a stored javascript: link is not a link)", "/^https?:\\/\\//i" in src)

    print()
    print("=" * 74)
    print("2. A PLC SIGNAL MAPPING SAYS IT IS STORED, NOT APPLIED")
    print("=" * 74)
    db = _session()
    device = models.IndustrialDevice(device_code="PLC-1", device_name="Press PLC", device_type="PLC", protocol="MQTT",
                                     status="Registered")
    db.add(device)
    db.commit()
    created = industrial_iot_routes.create_plc_signal_mapping(
        schemas.PlcSignalMappingCreate(mapping_code="MAP-1", device_id=device.id, source_signal="DB1.DBW0",
                                       mes_field="utilization", transform_rule="x/10"),
        db=db, current_user={"role": "Admin", "sub": "eng"})
    out = schemas.PlcSignalMappingResponse.model_validate(created)
    check("the created mapping answers applied: false", out.applied is False and out.enabled == "Yes", str(out))
    listed = industrial_iot_routes.get_plc_signal_mappings(Response(), None, 0, db=db, current_user={"role": "Admin"})
    check("...and so does every listed row",
          listed and all(schemas.PlcSignalMappingResponse.model_validate(m).applied is False for m in listed),
          str(len(listed)))
    check("the response model's default is the only source of the flag (no column pretends otherwise)",
          "applied" not in {c.name for c in models.PlcSignalMapping.__table__.columns})
    db.close()
    for name in ("industrial_iot_routes.py", "industrial_adapters.py", "mqtt_service.py"):
        with open(os.path.join(HERE, name), encoding="utf-8") as f:
            body = f.read()
        ingest = body.split("def create_industrial_signal")[1].split("\n@router")[0] if "def create_industrial_signal" in body else body
        check(f"{name}: the ingest path never reads the mapping table",
              "PlcSignalMapping" not in ingest and "mes_field" not in ingest and "transform_rule" not in ingest)
    with open(os.path.join(HERE, "..", "README.md"), encoding="utf-8") as f:
        readme = f.read()
    check("README no longer advertises a mapping AMP does not apply",
          "PLC signal mapping" not in readme.replace("PLC signal-mapping *record*", ""), "")
    check("...and says what the record is", "not applied by AMP" in readme)

    print()
    print("=" * 74)
    print("3. THE COST REFERENCE IS DATA, AND THE FIGURES SAY WHAT THEY GROUP BY")
    print("=" * 74)
    rec = schemas.CostRecordCreate(cost_no="C-1", cost_type="Material", reference_type="WorkOrder", reference_id=42,
                                   description="steel", amount=100)
    check("the reference is still accepted", rec.reference_type == "WorkOrder" and rec.reference_id == 42)
    check("...and documented as data no figure groups by",
          "AS DATA" in (schemas.CostRecordCreate.__doc__ or "") and "group" in (schemas.CostRecordCreate.__doc__ or ""))
    with open(os.path.join(HERE, "costing_routes.py"), encoding="utf-8") as f:
        costing = f.read()
    check("the costing figures group by type and department and by nothing else",
          "group_by(models.CostRecord.cost_type)" in costing and "group_by(models.CostRecord.department)" in costing
          and "reference" not in costing)
    with open(os.path.join(HERE, "..", "frontend", "components", "CostingSection.tsx"), encoding="utf-8") as f:
        costing_ui = f.read()
    check("the costing table shows the reference as data, with what the figures group by",
          '"Reference"' in costing_ui and "row.reference_type" in costing_ui and "group by type and department" in costing_ui)

    print()
    if failures:
        print(f"{len(failures)} FAILED")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


def test_inputs_say_what_they_are():
    assert main_() == 0, failures


if __name__ == "__main__":
    sys.exit(main_())
