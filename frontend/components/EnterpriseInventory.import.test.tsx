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

function stubFetch() {
  const calls: { url: string; init: RequestInit }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit = {}) => {
    calls.push({ url: String(url), init });
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
