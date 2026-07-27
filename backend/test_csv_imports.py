"""Onboarding CSV import tests — machines + suppliers (the mirror of the
/reports exports, in the style of the inventory Tally import).

Pins: create/update/skip counts, upsert keys (machine name, supplier_code),
flexible headers, status canonicalisation + utilization clamping for machines,
per-row error capture, and empty-file safety.

Run:  python backend/test_csv_imports.py     (exit 0 = pass)
"""
import asyncio
import io

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import machines_routes
import models
import orders_routes
from database import Base


class _Upload:
    """Minimal stand-in for FastAPI's UploadFile (async read of fixed bytes)."""

    def __init__(self, text: str):
        self._data = text.encode("utf-8")

    async def read(self):
        return self._data


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def test_machines_import_upserts_canonicalises_and_reports():
    db = _fresh_session()
    db.add(models.Machine(name="CNC-1", status="Running", utilization=77, line="SMT"))
    db.commit()

    csv_text = (
        "name,line,status,utilization\n"
        "CNC-1,IC,running,150\n"          # update: line moves, status canonicalised, util clamped
        "PRESS-9,SMT,Idle,40\n"           # create
        ",X,Idle,10\n"                    # no name -> skipped
        "LATHE-3,,bogus-status,abc\n"     # create; unknown status -> falls back, bad util -> 0
    )
    r = asyncio.run(machines_routes.import_machines_csv(
        file=_Upload(csv_text), db=db, current_user={}))

    assert r["created"] == 2 and r["updated"] == 1 and r["skipped"] == 1
    assert r["errors"] == []

    cnc = db.query(models.Machine).filter(models.Machine.name == "CNC-1").first()
    assert cnc.line == "IC"
    assert cnc.status == "Running"        # canonicalised from "running"
    assert cnc.utilization == 100         # clamped from 150

    lathe = db.query(models.Machine).filter(models.Machine.name == "LATHE-3").first()
    assert lathe is not None and lathe.utilization == 0
    assert db.query(models.Machine).count() == 3


def test_suppliers_import_upserts_by_code():
    db = _fresh_session()
    db.add(models.Supplier(supplier_code="SUP-1", supplier_name="Old Name", status="Active"))
    db.commit()

    csv_text = (
        "supplier_code,supplier_name,email,category,status\n"
        "SUP-1,Acme Metals,sales@acme.test,Metals,Active\n"   # update by code
        "SUP-2,Bolt & Co,,Fasteners,\n"                        # create; blank status -> Active
        ",No Code,,,\n"                                        # skipped (no code)
    )
    r = asyncio.run(orders_routes.import_suppliers_csv(
        file=_Upload(csv_text), db=db, current_user={}))

    assert r["created"] == 1 and r["updated"] == 1 and r["skipped"] == 1
    s1 = db.query(models.Supplier).filter(models.Supplier.supplier_code == "SUP-1").first()
    assert s1.supplier_name == "Acme Metals" and s1.email == "sales@acme.test"
    s2 = db.query(models.Supplier).filter(models.Supplier.supplier_code == "SUP-2").first()
    assert s2.status == "Active"
    assert db.query(models.Supplier).count() == 2


def test_flexible_headers_and_empty_file_are_safe():
    db = _fresh_session()
    # Tally-style headers on machines; a header-only file on suppliers.
    r1 = asyncio.run(machines_routes.import_machines_csv(
        file=_Upload("Machine,Line\nOVEN-1,SMT\n"), db=db, current_user={}))
    assert r1["created"] == 1
    m = db.query(models.Machine).first()
    assert m.name == "OVEN-1" and m.status == "Idle"          # safe default

    r2 = asyncio.run(orders_routes.import_suppliers_csv(
        file=_Upload("supplier_code,supplier_name\n"), db=db, current_user={}))
    assert r2 == {"created": 0, "updated": 0, "skipped": 0, "errors": []}


if __name__ == "__main__":
    test_machines_import_upserts_canonicalises_and_reports()
    print("ok  machines import: upsert by name, canonical status, clamped util, skip/report")
    test_suppliers_import_upserts_by_code()
    print("ok  suppliers import: upsert by code, blank status -> Active, skip")
    test_flexible_headers_and_empty_file_are_safe()
    print("ok  flexible headers + empty file safe")
    print("\nALL CSV IMPORT TESTS PASSED")
