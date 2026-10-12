import { describe, expect, it } from "vitest";

import { csvCell, exportFilename, toCsv } from "./chart-export";

describe("csvCell", () => {
  it("writes plain values as themselves", () => {
    expect(csvCell("IMM-03")).toBe("IMM-03");
    expect(csvCell(5050)).toBe("5050");
    expect(csvCell(0)).toBe("0");
  });

  it("writes a missing value as empty, NOT as zero", () => {
    // THE RULE THE WHOLE FILE EXISTS FOR. An hour nobody measured must reach
    // the spreadsheet as a gap. A zero there says the plant ran and made
    // nothing, which is a different claim and the one AMP refuses to make.
    expect(csvCell(null)).toBe("");
    expect(csvCell(undefined)).toBe("");
    expect(csvCell(0)).not.toBe("");
  });

  it("neutralises a cell a spreadsheet would run as a formula", () => {
    // =, +, - and @ start a formula in Excel, Sheets and LibreOffice. A part
    // name is user input, and this export is opened by the customer.
    for (const danger of ["=1+1", "+1", "-1", "@SUM(A1)", "=cmd|'/c calc'!A1"]) {
      expect(csvCell(danger).startsWith("'")).toBe(true);
    }
  });

  it("quotes anything that would break the row or the column", () => {
    expect(csvCell("Ele clip, blue")).toBe('"Ele clip, blue"');
    expect(csvCell('say "hi"')).toBe('"say ""hi"""');
    expect(csvCell("two\nlines")).toBe('"two\nlines"');
  });

  it("quotes a dangerous value that also needs quoting, in that order", () => {
    // The apostrophe must be INSIDE the quotes, or the guard is outside the
    // cell and does nothing.
    expect(csvCell('=A1,"x"')).toBe('"\'=A1,""x"""');
  });
});

describe("toCsv", () => {
  const headers = ["hour", "parts", "kwh"];

  it("writes a header row and one row per record", () => {
    const csv = toCsv(headers, [
      { hour: 0, parts: 206, kwh: 3.8 },
      { hour: 1, parts: 204, kwh: 3.6 },
    ]);
    expect(csv.split("\r\n")[0]).toBe("hour,parts,kwh");
    expect(csv.split("\r\n")[1]).toBe("0,206,3.8");
    expect(csv.trimEnd().split("\r\n")).toHaveLength(3);
  });

  it("keeps a column that a row is missing, as an empty cell", () => {
    // Columns must line up. A row that silently loses a cell shifts every
    // later column by one, which reads as data rather than as an error.
    const csv = toCsv(headers, [{ hour: 5, parts: 180 }]);
    expect(csv.split("\r\n")[1]).toBe("5,180,");
  });

  it("carries a measured gap through as a gap", () => {
    const csv = toCsv(headers, [{ hour: 3, parts: 175, kwh: null }]);
    expect(csv.split("\r\n")[1]).toBe("3,175,");
  });

  it("ends every line with CRLF, because Excel on Windows opens these", () => {
    expect(toCsv(["a"], [{ a: 1 }])).toBe("a\r\n1\r\n");
  });

  it("writes a header even with no rows", () => {
    expect(toCsv(headers, [])).toBe("hour,parts,kwh\r\n");
  });
});

describe("exportFilename", () => {
  it("names the chart and the day", () => {
    expect(exportFilename("Production per hour", "2026-10-09")).toBe(
      "amp-production-per-hour-2026-10-09.csv",
    );
  });

  it("cannot be talked into a path", () => {
    // The browser takes the download name from here. A slash or a leading dot
    // is a path, not a label.
    const name = exportFilename("../../etc/passwd", "2026-10-09");
    expect(name).not.toContain("/");
    expect(name).not.toContain("..");
    expect(name.startsWith("amp-")).toBe(true);
  });

  it("still produces a usable name from nothing", () => {
    expect(exportFilename("", "")).toBe("amp-chart-export.csv");
    expect(exportFilename("!!!", "???")).toBe("amp-chart-export.csv");
  });
});
