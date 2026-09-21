import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import EnterpriseInventory from "./EnterpriseInventory";

/**
 * A screen offers a write only to a role the server will let press it.
 *
 * Enterprise Inventory is an Operator-visible view (lib/modules OPERATOR_VIEWS),
 * and it offered an Operator every approval, receipt and count on it — each of
 * which enterprise_inventory_routes refuses with 403 once pressed: remnants,
 * slip approve / issue / reject, GRN create / accept and cycle-count create are
 * Admin or Supervisor; cycle-count approve and the CSV import are Admin. The
 * one write that IS every role's is raising a slip (an Operator requests
 * material), and that must stay.
 *
 * Nothing here decides who may write. The server does; this only keeps the
 * screen from promising what the server refuses, and says who can instead.
 */

const b64 = (o: object) => btoa(JSON.stringify(o)).replace(/=+$/, "");
const tokenFor = (role: string) =>
  `${b64({ alg: "HS256" })}.${b64({ sub: "u", role, tenant: "DEFAULT", exp: 4102444800 })}.sig`;

const ITEMS = [{ id: 1, item_code: "BOLT-M8", item_name: "Bolt M8", category: "Fasteners", unit: "pcs",
                 current_stock: 100, reorder_level: 10 }] as never[];

const PENDING_SLIP = {
  id: 7, slip_no: "MIS-7", item_id: 1, item_code: "BOLT-M8", item_name: "Bolt M8", remnant_id: null,
  work_order_ref: "WO-1", requested_qty: 5, issued_qty: 0, requested_by: "Operator",
  approved_by: null, status: "Pending", notes: "", created_at: "2026-09-21T08:00:00",
};
const DRAFT_GRN = { id: 3, grn_no: "GRN-3", purchase_order_ref: "", supplier_name: "Acme", received_by: "",
                    status: "Draft", notes: "", created_at: "2026-09-21T08:00:00", items: [] };
const DRAFT_COUNT = { id: 4, count_no: "CC-4", counted_by: "Sam", status: "Draft", notes: "",
                      created_at: "2026-09-21T08:00:00", items: [] };
const REMNANT = { id: 5, tag_no: "REM-5", item_id: 1, item_code: "BOLT-M8", item_name: "Bolt M8",
                  source_reference: "", original_qty: 10, remaining_qty: 4, unit: "m", location: "",
                  status: "Available", notes: "" };

function stubFetch() {
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    const path = String(url);
    const body = path.includes("/issue-slips") ? [PENDING_SLIP]
      : path.includes("/grns") ? [DRAFT_GRN]
      : path.includes("/cycle-counts") ? [DRAFT_COUNT]
      : path.includes("/remnants") ? [REMNANT]
      : [];
    return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  }));
}

function mount(role: string) {
  localStorage.setItem("token", tokenFor(role));
  stubFetch();
  render(<EnterpriseInventory items={ITEMS} />);
}

const tab = (name: string) => fireEvent.click(screen.getByRole("button", { name }));

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  localStorage.clear();
});

describe("an Operator is offered only what the server lets an Operator do", () => {
  it("is not offered the remnant form or Scrap, and is told who can", async () => {
    mount("Operator");
    expect(screen.queryByRole("button", { name: "Log Remnant" })).toBeNull();
    expect(screen.getByRole("note").textContent).toBe("Only an Admin or Supervisor can log a remnant or scrap one.");
    await screen.findByText("REM-5");
    expect(screen.queryByRole("button", { name: "Scrap" })).toBeNull();
  });

  it("can still raise a slip, but not approve, reject or issue one", async () => {
    mount("Operator");
    tab("Issue Slips");
    // POST /issue-slips is get_current_user: requesting material is every role's.
    expect(screen.getByRole("button", { name: "Raise Slip" })).toBeTruthy();
    await screen.findByText("MIS-7");
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Reject" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Issue & Deduct" })).toBeNull();
    expect(screen.getByText("awaiting an Admin or Supervisor")).toBeTruthy();
  });

  it("is not offered a goods receipt or its acceptance", async () => {
    mount("Operator");
    tab("GRN");
    expect(screen.queryByRole("button", { name: "Create GRN" })).toBeNull();
    await screen.findByText("GRN-3");
    expect(screen.queryByRole("button", { name: /Accept GRN/ })).toBeNull();
    expect(screen.getByRole("note").textContent).toBe("Only an Admin or Supervisor can raise or accept a goods receipt.");
  });

  it("is not offered a count, and is not offered the tabs that only 403", () => {
    mount("Operator");
    tab("Cycle Count");
    expect(screen.queryByRole("button", { name: /Submit Count/ })).toBeNull();
    // GET /inventory/variance-report is Admin+Supervisor and POST /inventory/import-csv
    // is Admin: a tab whose whole content the server refuses is not a tab.
    expect(screen.queryByRole("button", { name: "Variance Report" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Import CSV" })).toBeNull();
  });
});

describe("a Supervisor is offered the Supervisor writes and not the Admin ones", () => {
  it("logs remnants, raises receipts and counts, but does not approve a count or import", async () => {
    mount("Supervisor");
    expect(screen.getByRole("button", { name: "Log Remnant" })).toBeTruthy();
    expect(screen.queryByRole("note")).toBeNull();
    tab("GRN");
    expect(screen.getByRole("button", { name: "Create GRN" })).toBeTruthy();
    await screen.findByText("GRN-3");
    expect(screen.getByRole("button", { name: /Accept GRN/ })).toBeTruthy();
    tab("Cycle Count");
    expect(screen.getByRole("button", { name: /Submit Count/ })).toBeTruthy();
    await screen.findByText("CC-4");
    // PATCH /cycle-counts/{id}/approve is Admin only — the prose above the form
    // already said so; the button used to be offered anyway.
    expect(screen.queryByRole("button", { name: /Approve & Adjust Stock/ })).toBeNull();
    expect(screen.getByText("awaiting an Admin's approval")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Variance Report" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Import CSV" })).toBeNull();
  });

  it("approves, rejects and issues slips", async () => {
    mount("Supervisor");
    tab("Issue Slips");
    await screen.findByText("MIS-7");
    expect(screen.getByRole("button", { name: "Approve" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Reject" })).toBeTruthy();
  });
});

describe("an Admin is offered everything", () => {
  it("including the count approval and the CSV import", async () => {
    mount("Admin");
    tab("Cycle Count");
    await screen.findByText("CC-4");
    expect(screen.getByRole("button", { name: /Approve & Adjust Stock/ })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Import CSV" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Variance Report" })).toBeTruthy();
  });
});
