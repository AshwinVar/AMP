import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import EnterprisePolishSection from "./EnterprisePolishSection";
import type { ReportRequest } from "../lib/phase27-types";

/**
 * The Report Requests list is a log. Nothing in AMP generates the file a request
 * names, and every row used to read "Generated" (the dashboard sent that status
 * and the API stored it; backend test_report_requests_are_not_generated.py). The
 * status now comes from the server ("Logged"), and the view says in words that
 * logging does not produce a file.
 */

const row = (over: Partial<ReportRequest> = {}): ReportRequest => ({
  id: 1,
  report_no: "RPT-1",
  report_type: "OEE Report",
  requested_by: "Admin",
  format: "PDF",
  status: "Logged",
  ...over,
});

function renderSection(reports: ReportRequest[]) {
  render(
    <EnterprisePolishSection
      auditLogs={[]}
      reports={reports}
      health={null}
      summary={null}
      reportForm={{ report_no: "", report_type: "Executive Summary", requested_by: "Admin", format: "PDF", notes: "" }}
      setReportForm={() => {}}
      createReport={() => {}}
    />,
  );
}

describe("EnterprisePolishSection report requests", () => {
  it("says that logging a request does not generate a report file", () => {
    renderSection([]);
    expect(screen.getByText(/does not generate report files/i)).toBeTruthy();
    expect(screen.getByText(/CSV exports of the underlying data are on the Overview page/i)).toBeTruthy();
  });

  it("shows the status the server stored, which is Logged", () => {
    renderSection([row()]);
    expect(screen.getByText("RPT-1")).toBeTruthy();
    expect(screen.getByText("Logged")).toBeTruthy();
    expect(screen.queryByText("Generated")).toBeNull();
  });
});
