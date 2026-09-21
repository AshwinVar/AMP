import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * The GMATS inventory offers stock-in, proformas, invoicing, cancellation and
 * free-spares issue only to a role gmats_inventory_routes lets do them (Admin
 * or Supervisor). It gated the Admin-only corrections and voids by `isAdmin`
 * and left these ungated, so an Operator — the screen is Operator-visible —
 * was offered every one and refused with 403.
 */

const apiGet = vi.fn();
const apiGetWithTotal = vi.fn();

vi.mock("../lib/api", () => ({
  apiGet: (p: string) => apiGet(p),
  apiGetWithTotal: (p: string) => apiGetWithTotal(p),
  apiPost: vi.fn(),
  apiPatch: vi.fn(),
  apiDelete: vi.fn(),
  API_URL: "",
  getDownloadHeaders: () => ({}),
}));

import GmatsInventory from "./GmatsInventory";

const ITEM = {
  id: 1, item_code: "AF-001", item_name: "Air filter", category: "Spares", unit: "pcs",
  physical_stock: 10, reserved_stock: 2, available_stock: 8, reorder_level: 1, purchase_rate: 100,
  location: null, supplier: null, aliases: [], reorder_needed: false,
};
const PROFORMA = { id: 9, proforma_no: "PF-9", customer_name: "ABC", status: "Open",
                   created_at: "2026-09-21T08:00:00", lines: [] };

beforeEach(() => {
  apiGet.mockImplementation((p: string) => Promise.resolve(p.includes("/gmats/items") ? [ITEM] : {}));
  apiGetWithTotal.mockResolvedValue({ data: [PROFORMA], total: 1 });
});

afterEach(cleanup);

describe("GMATS inventory offers writes by role", () => {
  it("offers an Operator no stock-in, proforma, invoice, cancel or free-spares issue, and says who can", async () => {
    render(<GmatsInventory tenant="GMATS" isAdmin={false} canWrite={false} />);
    await screen.findByText("Air filter");
    expect(screen.queryByRole("button", { name: "+ Stock" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Proforma (Reserve)" }));
    expect(screen.queryByRole("button", { name: "Create Proforma (Reserve)" })).toBeNull();
    expect(screen.getByRole("note").textContent).toBe("Only an Admin or Supervisor can raise, invoice or cancel a proforma.");
    await screen.findByText("PF-9");
    expect(screen.queryByRole("button", { name: /Generate Tax Invoice/ })).toBeNull();
    expect(screen.queryByRole("button", { name: "Cancel" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Free Spares (MIN)" }));
    expect(screen.queryByRole("button", { name: /Issue Free Spares/ })).toBeNull();
    expect(screen.getByRole("note").textContent).toBe("Only an Admin or Supervisor can issue free spares.");
  });

  it("offers a Supervisor those writes, and still not the Admin corrections", async () => {
    render(<GmatsInventory tenant="GMATS" isAdmin={false} canWrite />);
    await screen.findByText("Air filter");
    expect(screen.getByRole("button", { name: "+ Stock" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Edit" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Del" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Proforma (Reserve)" }));
    expect(screen.getByRole("button", { name: "Create Proforma (Reserve)" })).toBeTruthy();
    await screen.findByText("PF-9");
    expect(screen.getByRole("button", { name: /Generate Tax Invoice/ })).toBeTruthy();
    expect(screen.queryByRole("note")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Free Spares (MIN)" }));
    expect(screen.getByRole("button", { name: /Issue Free Spares/ })).toBeTruthy();
  });

  it("defaults to offering nothing when the caller says nothing about the role", async () => {
    // Fail closed: a mount that forgets the props is an Operator's view, never an Admin's.
    render(<GmatsInventory tenant="GMATS" />);
    await screen.findByText("Air filter");
    expect(screen.queryByRole("button", { name: "+ Stock" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Edit" })).toBeNull();
  });
});
