// Every chart on the board exports its own data, for every user.
//
// WHY THIS IS A SHARED FILE AND NOT A BUTTON. A chart's export is only useful
// if it carries the SAME numbers the chart drew — including the gaps. A series
// with a missing hour must export an empty cell, not a zero, or the
// spreadsheet says the plant ran and made nothing in an hour nobody measured.
// That is the same rule the charts keep (plant_board.py refuses to invent
// zeros), and it has to survive the trip into Excel or it was never a rule.

/** A cell as it goes into a spreadsheet. null and undefined become empty. */
export type Cell = string | number | boolean | null | undefined;

/**
 * Escape one cell for CSV, including the formula-injection guard.
 *
 * A cell beginning = + - @ (or tab / carriage return) is executed as a FORMULA
 * by Excel, Google Sheets and LibreOffice when the file is opened. A part name
 * of `=cmd|'...'` in an export a customer opens is a remote-code path that
 * starts with somebody typing a part name. Prefixing with an apostrophe makes
 * the spreadsheet treat it as text; the apostrophe is not shown in the cell.
 *
 * AMP's backend already does this for its own exports (#369). The frontend
 * generating CSV in the browser is a second place the same rule has to hold,
 * because the rule belongs to CSV, not to whichever process wrote it.
 */
export function csvCell(value: Cell): string {
  if (value === null || value === undefined) return "";
  let text = String(value);
  if (/^[=+\-@\t\r]/.test(text)) text = "'" + text;
  if (/[",\n\r]/.test(text)) text = '"' + text.replace(/"/g, '""') + '"';
  return text;
}

/**
 * Rows to CSV text. `headers` fixes the column order; a key missing from a row
 * exports as empty rather than being skipped, so every row has every column.
 */
export function toCsv(headers: string[], rows: Record<string, Cell>[]): string {
  const lines = [headers.map(csvCell).join(",")];
  for (const row of rows) {
    lines.push(headers.map((h) => csvCell(row[h])).join(","));
  }
  // CRLF: Excel on Windows is where these are opened, and a bare LF makes it
  // render the whole file as one row in some versions.
  return lines.join("\r\n") + "\r\n";
}

/**
 * A filename that sorts, says what it is, and cannot escape its directory.
 *
 * Browsers take the download name from here, and a name carrying a slash or
 * leading dots is a path, not a label. Everything outside a small safe set
 * becomes a hyphen.
 */
export function exportFilename(chart: string, on: string): string {
  const safe = (s: string) =>
    s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
  const name = safe(chart) || "chart";
  const when = safe(on) || "export";
  return `amp-${name}-${when}.csv`;
}

/**
 * Hand the browser a CSV file. Returns false when the browser cannot.
 *
 * The UTF-8 BOM is deliberate: without it Excel reads the file as the system
 * codepage and a rupee sign becomes mojibake — on an export whose whole
 * purpose is money. Numbers are unaffected.
 */
export function downloadCsv(filename: string, csv: string): boolean {
  try {
    const blob = new Blob(["﻿" + csv], {
      type: "text/csv;charset=utf-8;",
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    // Revoked on a later tick: revoking synchronously races the download in
    // Safari and the file arrives empty.
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    return true;
  } catch {
    return false;
  }
}
