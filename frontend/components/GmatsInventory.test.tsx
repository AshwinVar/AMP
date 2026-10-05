import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * GMATS issues a proforma invoice. It does not raise tax invoices.
 *
 * The button that deducted the stock said "Generate Tax Invoice", created a
 * second document (INV-7000+) and opened a printable GST invoice in a new
 * window. GMATS does not raise its tax invoices from AMP, so the screen was
 * offering a legal document the business does not issue from here.
 *
 * The deduction is unchanged — that is the point of the button — but now the
 * proforma is the only document, and nothing prints.
 *
 * The screen is the whole change, so these render it: a backend test cannot see
 * a button label, a printed window, or a status pill that renders empty because
 * a value is missing from its lookup (the defect class of #581).
 */

const apiGet = vi.fn();
const apiGetWithTotal = vi.fn();
const apiPost = vi.fn();
const apiPatch = vi.fn();
const apiDelete = vi.fn();

vi.mock("../lib/api", () => ({
  apiGet: (url: string) => apiGet(url),
  apiGetWithTotal: (url: string) => apiGetWithTotal(url),
  apiPost: (url: string, body: unknown) => apiPost(url, body),
  apiPatch: (url: string, body: unknown) => apiPatch(url, body),
  apiDelete: (url: string) => apiDelete(url),
  API_URL: "http://test",
  getDownloadHeaders: () => ({}),
}));

import GmatsInventory from "./GmatsInventory";

// This suite does not run with vitest `globals`, so testing-library's automatic
// cleanup is not registered; without it a second render stacks on the first.
afterEach(cleanup);

const ITEM = {
  id: 1, item_code: "CMP-001", item_name: "Compressor valve", category: "General", unit: "Nos",
  physical_stock: 10, reserved_stock: 3, available_stock: 7, reorder_level: 2, purchase_rate: 450,
  location: "Rack A", supplier: "ACME", aliases: [], reorder_needed: false,
};
const SUMMARY = {
  items: 1, total_physical: 10, total_reserved: 3, total_available: 7,
  reorder_needed: 0, open_proformas: 1,
};
const OPEN_PROFORMA = {
  id: 42, proforma_no: "PI-1042", customer_name: "Sharma Engineering", status: "Open",
  created_at: "2026-10-05T09:00:00", lines: [{ item_id: 1, item_name: "Compressor valve", qty: 3 }],
};
// One issued under the new flow, one under the retired tax-invoice flow. Both are
// the same state, and production holds 8 of the second kind.
const ISSUED = [
  { ...OPEN_PROFORMA, id: 43, proforma_no: "PI-1043", status: "Issued" },
  { ...OPEN_PROFORMA, id: 44, proforma_no: "PI-1044", status: "Invoiced" },
];

beforeEach(() => {
  apiGet.mockReset(); apiGetWithTotal.mockReset();
  apiPost.mockReset(); apiPatch.mockReset(); apiDelete.mockReset();
  apiPost.mockResolvedValue({ id: 42, proforma_no: "PI-1042", status: "Issued" });
  apiPatch.mockResolvedValue({ ok: true });
  apiGet.mockImplementation((url: string) => {
    if (url.includes("/gmats/items")) return Promise.resolve([ITEM]);
    if (url.includes("/gmats/summary")) return Promise.resolve(SUMMARY);
    return Promise.resolve([]);
  });
  // The proforma lists are pages (ADR-0036), so they come through apiGetWithTotal.
  apiGetWithTotal.mockImplementation((url: string) =>
    Promise.resolve(url.includes("status=")
      ? { data: ISSUED, total: ISSUED.length }
      : { data: [OPEN_PROFORMA], total: 1 }));
  vi.spyOn(window, "confirm").mockReturnValue(true);
  vi.spyOn(window, "open").mockReturnValue(null);
});

const openTab = (name: RegExp) => fireEvent.click(screen.getByRole("button", { name }));
const count = (el: HTMLElement, re: RegExp) => ((el.textContent ?? "").match(re) ?? []).length;

describe("the button that deducts the stock", () => {
  it("is a Proforma Invoice, and issuing prints nothing", async () => {
    render(<GmatsInventory tenant="GMATS" isAdmin canWrite />);
    openTab(/^Proforma \(Reserve\)$/);

    const button = await screen.findByRole("button", { name: /Generate Proforma Invoice/ });
    expect(screen.queryByRole("button", { name: /Tax Invoice/ })).toBeNull();

    fireEvent.click(button);

    await waitFor(() => expect(apiPost).toHaveBeenCalledWith("/gmats/proformas/42/issue", {}));
    // No second document, and no print window: "no invoice needed".
    expect(apiPost.mock.calls.some(([url]) => String(url).includes("/invoice"))).toBe(false);
    expect(window.open).not.toHaveBeenCalled();
  });

  it("is not offered for a proforma that is already issued", async () => {
    apiGetWithTotal.mockResolvedValue({ data: [{ ...OPEN_PROFORMA, status: "Issued" }], total: 1 });
    render(<GmatsInventory tenant="GMATS" isAdmin canWrite />);
    openTab(/^Proforma \(Reserve\)$/);

    expect(await screen.findByText("PI-1042")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Generate Proforma Invoice/ })).toBeNull();
    // The pill renders the word, rather than the empty box a status missing from
    // its lookup produces (#581). The tab of the same name is a button, not a pill.
    const pills = screen.getAllByText("Issued").filter((el) => el.tagName === "SPAN");
    expect(pills).toHaveLength(1);
  });
});

describe("the issued list", () => {
  it("asks for both spellings and shows the legacy ones", async () => {
    render(<GmatsInventory tenant="GMATS" isAdmin canWrite />);
    openTab(/^Issued$/);

    await waitFor(() => expect(apiGetWithTotal).toHaveBeenCalledWith(
      "/gmats/proformas?tenant=GMATS&status=Issued,Invoiced"));
    expect(await screen.findByText("PI-1043")).toBeTruthy();
    expect(screen.getByText("PI-1044")).toBeTruthy();
    // A row issued before the change still renders its own status word.
    expect(screen.getByText("Invoiced")).toBeTruthy();
    expect(screen.queryByText(/INV-/)).toBeNull();
  });

  it("lets an Admin undo an issue, and offers nobody else the button", async () => {
    render(<GmatsInventory tenant="GMATS" isAdmin canWrite />);
    openTab(/^Issued$/);

    const undo = (await screen.findAllByRole("button", { name: /Undo issue/ }))[0];
    fireEvent.click(undo);
    await waitFor(() => expect(apiPatch).toHaveBeenCalledWith("/gmats/proformas/43/undo-issue", {}));

    cleanup();
    render(<GmatsInventory tenant="GMATS" isAdmin={false} canWrite />);
    openTab(/^Issued$/);
    expect(await screen.findByText("PI-1043")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Undo issue/ })).toBeNull();
  });
});

describe("the module no longer talks about tax invoices", () => {
  it("has no tax-invoice tab, heading or copy", async () => {
    const { container } = render(<GmatsInventory tenant="GMATS" isAdmin canWrite />);
    expect(screen.queryByRole("button", { name: /^Tax Invoice$/ })).toBeNull();
    expect(screen.getByRole("button", { name: /^Issued$/ })).toBeTruthy();

    openTab(/^Proforma \(Reserve\)$/);
    await screen.findByRole("button", { name: /Generate Proforma Invoice/ });
    expect(count(container, /tax invoice/gi)).toBe(0);

    openTab(/^Issued$/);
    await screen.findByText("Issued Proformas");
    // The one permitted mention is the sentence that says it does NOT happen.
    expect(count(container, /tax invoice/gi)).toBe(count(container, /No tax invoice is raised/gi));
  });
});
