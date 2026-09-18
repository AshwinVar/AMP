"""A report request is logged, not generated: nothing in AMP produces the file.

THE DEFECT
----------
The Enterprise Polish view has a "Log Report" form (Executive Summary, OEE,
Quality, Inventory or Compliance report; PDF, Excel or CSV) and a "Report
Requests" table with a Status column. Every row read "Generated". Nothing
generates a report: POST /reports stores the row and returns it, and there is no
worker, no file and no download. The browser sent `status: "Generated"` in the
body, the API stored whatever status the client sent (the column default was
"Generated" too), and the browser then wrote an audit entry "Generated report
request". A customer reading the table, or the audit trail, was told an OEE
Report PDF existed.

THE RULE
--------
The server owns a report request's status, and says only what happened: it was
"Logged". A client cannot set it. The view says in words that logging does not
produce a file, and where the real exports are.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_report_requests_are_not_generated.py
"""
import ast
import pathlib

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models
import reports_routes
import schemas
import tenancy
from database import Base

HERE = pathlib.Path(__file__).resolve().parent
FRONTEND = HERE.parent / "frontend"
ADMIN = {"tenant": "DEFAULT", "role": "Admin"}
failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


def _session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _create(db, **body):
    tok = tenancy.set_current_tenant("DEFAULT")
    try:
        return reports_routes.create_report(
            payload=schemas.ReportRequestCreate(**body), db=db, current_user=ADMIN)
    finally:
        tenancy.reset_current_tenant(tok)


def section(title):
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


def main():
    section("1. THE SERVER SAYS WHAT HAPPENED: LOGGED")
    db = _session()
    # Exactly what the Enterprise Polish form used to send.
    row = _create(db, report_no="RPT-1", report_type="OEE Report", requested_by="Admin",
                  format="PDF", status="Generated", notes="")
    check("a request the browser marked 'Generated' is stored as 'Logged'",
          row.status == "Logged", row.status)
    row = _create(db, report_no="RPT-2", report_type="Quality Report")
    check("a request with no status is 'Logged'", row.status == "Logged", row.status)
    check("the create schema does not accept a status from the client",
          "status" not in schemas.ReportRequestCreate.model_fields,
          str(list(schemas.ReportRequestCreate.model_fields)))
    stored = {r.report_no: r.status for r in db.query(models.ReportRequest).all()}
    check("nothing stored claims a report was generated",
          set(stored.values()) == {"Logged"}, str(stored))
    db.close()

    section("2. A ROW WRITTEN WITHOUT THE ROUTE IS NOT 'GENERATED' EITHER")
    db = _session()
    db.add(models.ReportRequest(tenant_code="DEFAULT", report_no="RPT-3", report_type="OEE Report"))
    db.commit()
    check("the column default is 'Logged'",
          db.query(models.ReportRequest).one().status == "Logged",
          db.query(models.ReportRequest).one().status)
    db.close()

    section("3. THE VIEW DOES NOT CLAIM GENERATION")
    page = (FRONTEND / "app" / "dashboard" / "page.tsx").read_text(encoding="utf-8")
    view = (FRONTEND / "components" / "EnterprisePolishSection.tsx").read_text(encoding="utf-8")
    check("the dashboard never sends a report status",
          'status: "Generated"' not in page, "page.tsx still sends status: \"Generated\"")
    check("the audit entry says the request was logged, not generated",
          "Generated report request" not in page and "Logged report request" in page,
          "audit action text")
    check("the view says logging does not produce a file",
          "does not generate" in view, "EnterprisePolishSection.tsx has no such note")

    section("4. CONTROL: THE STRUCTURAL CHECKS READ WHAT THEY CLAIM TO")
    # The string checks above are only as good as the files they read.
    check("page.tsx is the dashboard that posts /reports",
          'apiPost<ReportRequest>("/reports"' in page, "no /reports post found")
    check("EnterprisePolishSection.tsx renders the Report Requests table",
          '"Report Requests"' in view, "table title not found")
    # And the backend module is valid Python that defines the route.
    tree = ast.parse((HERE / "reports_routes.py").read_text(encoding="utf-8"))
    check("reports_routes defines create_report",
          any(isinstance(n, ast.FunctionDef) and n.name == "create_report" for n in ast.walk(tree)))

    print()
    print("=" * 74)
    if failures:
        print(f"{len(failures)} FAILED")
        for f in failures:
            print(f"  - {f}")
    else:
        print("ALL CHECKS PASSED")
    print("=" * 74)
    return 1 if failures else 0


def test_report_requests_are_not_generated():
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
