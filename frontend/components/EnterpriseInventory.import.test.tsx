import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import EnterpriseInventory from "./EnterpriseInventory";

/**
 * A founder previewing a client imported that client's CSV into their OWN workspace.
 *
 * The company switcher works by stamping X-Tenant on every request (lib/api
 * getAuthHeaders / getDownloadHeaders). The CSV import on this screen built its
 * own headers by hand:
 *
 *     headers: { Authorization: `Bearer ${localStorage.getItem("token")}` },
 *
 * so the preview header never left the browser, the backend bound the founder's
 * own tenant, and a founder onboarding a customer by importing their Tally stock
 * ledger wrote every row into the platform workspace instead — while the screen,
 * the header pill and the result banner all named the customer. lib/api.test.ts
 * even records the assumption this broke: "CSV export/import build their own
 * fetch off these." One of them did not.
 *
 * CsvImportButton already had it right (getDownloadHeaders: auth + preview, and
 * NO Content-Type, so the browser writes the multipart boundary). This drives the
 * real screen and inspects the request it sends.
 */

// A JWT-shaped token that has not expired; only its shape and exp are read client-side.
const b64 = (o: object) => btoa(JSON.stringify(o)).replace(/=+$/, "");
const TOKEN = `${b64({ alg: "HS256" })}.${b64({ sub: "founder", role: "Admin", tenant: "DEFAULT", exp: 4102444800 })}.sig`;

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  localStorage.clear();
});

beforeEach(() => {
  localStorage.setItem("token", TOKEN);
});

function stubFetch(importResponse?: () => Response) {
  const calls: { url: string; init: RequestInit }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit = {}) => {
    calls.push({ url: String(url), init });
    if (String(url).includes("/inventory/import-csv") && importResponse) return importResponse();
    const body = String(url).includes("/inventory/import-csv")
      ? { created: 1, updated: 0, skipped: 0, errors: [] }
      : [];
    return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  }));
  return calls;
}

async function uploadOnImportTab() {
  render(<EnterpriseInventory items={[]} />);
  fireEvent.click(screen.getByRole("button", { name: "Import CSV" }));
  const input = document.querySelector('input[type="file"]') as HTMLInputElement;
  const file = new File(["item_code,item_name\nX-1,Thing\n"], "stock.csv", { type: "text/csv" });
  Object.defineProperty(input, "files", { value: [file] });
  fireEvent.submit(input.closest("form") as HTMLFormElement);
}

function importCall(calls: { url: string; init: RequestInit }[]) {
  return calls.find((c) => c.url.includes("/inventory/import-csv"));
}

describe("EnterpriseInventory CSV import — tenant preview", () => {
  it("sends the previewed company, so rows land in the customer's workspace", async () => {
    localStorage.setItem("company", "ACME");
    const calls = stubFetch();
    await uploadOnImportTab();
    await waitFor(() => expect(importCall(calls)).toBeTruthy());

    const headers = importCall(calls)!.init.headers as Record<string, string>;
    expect(headers["X-Tenant"]).toBe("ACME");
    expect(headers.Authorization).toBe(`Bearer ${TOKEN}`);
    // Multipart: the browser must write its own Content-Type with the boundary.
    expect(Object.keys(headers).map((k) => k.toLowerCase())).not.toContain("content-type");
  });

  it("sends no preview header from the founder's own workspace", async () => {
    localStorage.setItem("company", "DEFAULT");
    const calls = stubFetch();
    await uploadOnImportTab();
    await waitFor(() => expect(importCall(calls)).toBeTruthy());

    const headers = importCall(calls)!.init.headers as Record<string, string>;
    expect(headers).not.toHaveProperty("X-Tenant");
  });
});

/**
 * A refused upload is said as a refusal.
 *
 * The handler fed whatever came back into the success panel with no `res.ok`
 * check, so a 403 ({detail: "..."}) rendered as a green "Import complete" with
 * "Created: undefined" — or threw mid-render on `errors.length`. The sibling
 * upload on the GMATS screen checked the status; this one did not.
 */
describe("EnterpriseInventory CSV import — a refusal is not a success", () => {
  it("shows a 403 as refused, with the server's sentence, and no counts", async () => {
    stubFetch(() => new Response(JSON.stringify({ detail: "Only an Admin can import stock" }),
                                 { status: 403, headers: { "Content-Type": "application/json" } }));
    await uploadOnImportTab();
    await screen.findByRole("alert");
    expect(screen.getByRole("alert").textContent).toContain("Import refused — nothing was imported");
    expect(screen.getByRole("alert").textContent).toContain("Only an Admin can import stock");
    expect(screen.queryByText(/Import complete/)).toBeNull();
    expect(screen.queryByText(/Created:/)).toBeNull();
  });

  it("shows a 500 with a non-JSON body as refused, with the status", async () => {
    stubFetch(() => new Response("<html>Bad gateway</html>", { status: 502, headers: { "Content-Type": "text/html" } }));
    await uploadOnImportTab();
    await screen.findByRole("alert");
    expect(screen.getByRole("alert").textContent).toContain("The server refused the upload (502).");
  });

  it("reads a validation refusal's messages", async () => {
    stubFetch(() => new Response(JSON.stringify({ detail: [{ loc: ["body", "file"], msg: "field required" }] }),
                                 { status: 422, headers: { "Content-Type": "application/json" } }));
    await uploadOnImportTab();
    await screen.findByRole("alert");
    expect(screen.getByRole("alert").textContent).toContain("field required");
  });

  it("CONTROL: a 200 with counts still renders the success panel", async () => {
    stubFetch(() => new Response(JSON.stringify({ created: 2, updated: 1, skipped: 0, errors: ["Row 4: bad"] }),
                                 { status: 200, headers: { "Content-Type": "application/json" } }));
    await uploadOnImportTab();
    await screen.findByText("Import completed with warnings");
    expect(screen.getByText("Created: 2")).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
