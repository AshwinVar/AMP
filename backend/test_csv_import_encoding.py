"""CSV upload decoding tests — the onboarding import must not 500 on real files.

Every importer used to do this, with nothing around it::

    content = await file.read()
    text = content.decode("utf-8-sig")

"Save as CSV" in Excel on Windows writes the system ANSI codepage (usually
cp1252), not UTF-8. So the moment a roster held "Müller Press", or a supplier was
"Nováková s.r.o.", the decode raised UnicodeDecodeError — uncaught, so HTTP 500 —
on the first thing a new customer ever does, with nothing telling them what was
wrong. Uploading the .xlsx instead of a CSV produced the same opaque 500.

`csv_safe.read_upload_text` decodes what real spreadsheets emit, and turns the
genuinely unreadable cases into a 400 that says what to fix.

Run:  python backend/test_csv_import_encoding.py     (exit 0 = pass)
"""
import ast
import asyncio
import io
import os
import zipfile

from fastapi import HTTPException, UploadFile
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import machines_routes
import models
from csv_safe import MAX_UPLOAD_BYTES, decode_csv_bytes, read_upload_text
from database import Base

ADMIN = {"tenant": "DEFAULT", "role": "Admin"}
BACKEND = os.path.dirname(os.path.abspath(__file__))


def _fresh_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _upload(payload: bytes, filename="import.csv"):
    return UploadFile(filename=filename, file=io.BytesIO(payload))


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _import_machines(payload: bytes, db):
    return _run(machines_routes.import_machines_csv(
        file=_upload(payload), db=db, current_user=ADMIN))


def test_excel_on_windows_csv_imports_instead_of_500():
    """The regression this file exists for: cp1252 from Excel used to raise
    UnicodeDecodeError -> HTTP 500. It must import, with accents intact."""
    db = _fresh_session()
    payload = "name,line,status\nMüller Press,Line 1,Running\n".encode("cp1252")

    result = _import_machines(payload, db)

    assert result["created"] == 1, result
    assert result["encoding"] == "cp1252", result
    assert db.query(models.Machine).first().name == "Müller Press", "accents must survive"
    print("PASS a cp1252 (Excel/Windows) roster imports, accents intact")


def test_utf8_and_bom_still_win():
    """UTF-8 must still be tried first — a file that is valid UTF-8 must never
    be silently reinterpreted as cp1252 (that is how mojibake gets stored)."""
    db = _fresh_session()
    payload = "name,line,status\nNováková Line,Line 2,Idle\n".encode("utf-8-sig")

    result = _import_machines(payload, db)

    assert result["encoding"] == "utf-8-sig", result
    assert db.query(models.Machine).first().name == "Nováková Line"
    # a BOM must not end up glued to the first header, which would break name lookup
    assert db.query(models.Machine).count() == 1
    print("PASS UTF-8 (with BOM) is preferred and the BOM is stripped")


def test_xlsx_upload_gets_an_actionable_400_not_a_500():
    """Uploading the workbook instead of exporting CSV is the most common
    onboarding mistake; it must say so."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:          # a real .xlsx IS a zip
        z.writestr("xl/workbook.xml", "<workbook/>")
    try:
        _run(read_upload_text(_upload(buf.getvalue(), "machines.xlsx")))
        assert False, "an .xlsx upload must be rejected"
    except HTTPException as e:
        assert e.status_code == 400, e.status_code
        assert "Save As" in e.detail and "CSV" in e.detail, e.detail
    print("PASS an .xlsx upload gets a 400 telling the user to export CSV")


def test_empty_and_oversized_uploads_are_rejected():
    try:
        _run(read_upload_text(_upload(b"   \n")))
        assert False, "an empty file must be rejected"
    except HTTPException as e:
        assert e.status_code == 400 and "empty" in e.detail.lower(), e.detail

    try:
        _run(read_upload_text(_upload(b"x" * 2048), max_bytes=1024))
        assert False, "an oversized file must be rejected"
    except HTTPException as e:
        assert e.status_code == 400 and "too large" in e.detail.lower(), e.detail
    assert MAX_UPLOAD_BYTES == 10 * 1024 * 1024
    print("PASS empty and oversized uploads are 400s, not crashes or OOM")


def test_no_byte_sequence_can_raise():
    """The decoder must be total. If any input can still raise, the 500 is back."""
    for payload in (b"\xff\xfe\x00big5\x81\x8d\x90", bytes(range(256)),
                    "héllo".encode("utf-16")):
        text, encoding = decode_csv_bytes(payload)
        assert isinstance(text, str) and encoding in ("utf-8-sig", "cp1252", "latin-1")
    print("PASS decode_csv_bytes never raises, for any byte sequence")


def test_a_malformed_row_does_not_abort_the_import():
    """Per-row tolerance is the point of a bulk import — one bad line must not
    cost the customer the other 499."""
    db = _fresh_session()
    payload = ("name,line,status\n"
               "Good Press,Line 1,Running\n"
               ",Line 1,Running\n"                       # no name -> skipped
               "Odd Press,Line 1,NotAStatus\n"           # unknown status -> Idle
               "Util Press,Line 1,Running,extra,cols\n"  # ragged row
               ).encode("utf-8")

    result = _import_machines(payload, db)

    assert result["skipped"] == 1, result
    assert result["created"] == 3, result
    names = {m.name for m in db.query(models.Machine).all()}
    assert names == {"Good Press", "Odd Press", "Util Press"}, names
    assert db.query(models.Machine).filter(models.Machine.name == "Odd Press").first().status == "Idle"
    print("PASS a nameless row is skipped and a ragged/unknown-status row still imports")


def _raw_upload_reads(path, src=None):
    """[(lineno, what)] for code that reads/decodes an upload by hand instead of
    going through read_upload_text. AST-based so prose in a docstring is not a
    match."""
    src = src if src is not None else open(path, encoding="utf-8").read()
    hits = []
    for node in ast.walk(ast.parse(src)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        # file.read()
        if (node.func.attr == "read" and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "file"):
            hits.append((node.lineno, "file.read()"))
        # .decode("utf-8-sig") — the BOM-stripping codec exists for spreadsheet
        # files, so it is the upload idiom specifically. Plain .decode("utf-8")
        # is left alone: ai_copilot decodes LLM responses and security decodes
        # hashes, and neither is an upload.
        if node.func.attr == "decode" and node.args:
            first = node.args[0]
            if isinstance(first, ast.Constant) and first.value == "utf-8-sig":
                hits.append((node.lineno, 'decode("utf-8-sig")'))
    return hits


def test_only_csv_safe_reads_uploads():
    """All four importers had the same hand-rolled read+decode, and all four had
    the same 500. Keeping the read in one place is what stops the fifth."""
    offenders = []
    for name in sorted(os.listdir(BACKEND)):
        if not name.endswith(".py") or name.startswith("test_") or name == "csv_safe.py":
            continue
        for lineno, what in _raw_upload_reads(os.path.join(BACKEND, name)):
            offenders.append(f"{name}:{lineno} -> {what}")

    assert not offenders, (
        "These modules read or decode an upload by hand, bypassing the encoding "
        "fallbacks and size limit in csv_safe.read_upload_text — which is exactly "
        "how every importer ended up returning HTTP 500 on an Excel-saved CSV:\n  "
        + "\n  ".join(offenders)
        + "\nUse: text, encoding = await read_upload_text(file)")
    print("PASS uploads are read only through csv_safe.read_upload_text")


def test_the_guard_detects_a_hand_rolled_read():
    """Prove the sweep fails on the old pattern rather than only passing now."""
    old_way = (
        "async def import_thing(file):\n"
        "    content = await file.read()\n"
        '    text = content.decode("utf-8-sig")\n'
        "    return text\n"
    )
    hits = _raw_upload_reads("<mem>", src=old_way)
    assert sorted(w for _, w in hits) == ['decode("utf-8-sig")', "file.read()"], hits
    print("PASS the guard detects the hand-rolled read+decode it replaced")


if __name__ == "__main__":
    test_excel_on_windows_csv_imports_instead_of_500()
    test_utf8_and_bom_still_win()
    test_xlsx_upload_gets_an_actionable_400_not_a_500()
    test_empty_and_oversized_uploads_are_rejected()
    test_no_byte_sequence_can_raise()
    test_a_malformed_row_does_not_abort_the_import()
    test_only_csv_safe_reads_uploads()
    test_the_guard_detects_a_hand_rolled_read()
    print("\nALL CSV IMPORT ENCODING TESTS PASSED")
