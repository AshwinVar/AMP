import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import PurchasingSection from "./PurchasingSection";
import type { PurchaseOrder, Supplier } from "../lib/phase18-types";

/**
 * A reorder-agent draft has no supplier.
 *
 * The reorder agent writes `supplier_id: null` — nobody has chosen a supplier
 * for a PO the system drafted. The API used to refuse to serialise that row at
 * all (GET /purchase-orders answered 500 for the whole list); now that it
 * returns the row, the table must say what is true rather than render the
 * template literal `Supplier ${id}` as "Supplier null", which reads as corrupt
 * data. Only a render catches this: the type allows null, the API is fine, and
 * the cell is simply wrong.
 */

// This suite does not run with vitest `globals`, so testing-library's automatic
// cleanup is not registered; without it a second render stacks on the first.
afterEach(cleanup);

const noop = () => undefined;

function po(over: Partial<PurchaseOrder> = {}): PurchaseOrder {
  return {
    id: 1,
    po_no: "PO-1001",
    supplier_id: 7,
    item_id: 3,
    item_name: "M8 bolt",
    order_quantity: 50,
    received_quantity: 0,
    unit: "pcs",
    expected_delivery_date: "2026-09-24",
    status: "Open",
    notes: null,
    ...over,
  } as PurchaseOrder;
}

const suppliers = [{ id: 7, supplier_code: "SUP-7", supplier_name: "Bharat Fasteners",
  status: "Active" } as unknown as Supplier];

function renderWith(orders: PurchaseOrder[]) {
  return render(
    <PurchasingSection
      suppliers={suppliers}
      purchaseOrders={orders}
      inventoryItems={[]}
      analytics={null}
      supplierForm={{ supplier_code: "", supplier_name: "", contact_person: "", email: "",
        phone: "", category: "", status: "Active" }}
      setSupplierForm={noop}
      poForm={{ po_no: "", supplier_id: "", item_id: "", item_name: "", order_quantity: 0,
        received_quantity: 0, unit: "", expected_delivery_date: "", status: "Open", notes: "" }}
      setPoForm={noop}
      createSupplier={noop}
      updateSupplier={noop}
      createPurchaseOrder={noop}
      updatePurchaseOrder={noop}
      generateOverdueEscalations={noop}
    />,
  );
}

describe("PurchasingSection supplier column", () => {
  it("labels a reorder-agent draft honestly instead of printing 'Supplier null'", () => {
    renderWith([po({ id: 2, po_no: "AUTO-PO-3-1789", supplier_id: null, status: "Approved" })]);
    expect(screen.getByText("No supplier yet")).toBeTruthy();
    expect(screen.queryByText(/Supplier null/)).toBeNull();
  });

  it("still names a real supplier on the row next to it", () => {
    renderWith([
      po({ id: 2, po_no: "AUTO-PO-3-1789", supplier_id: null, status: "Approved" }),
      po({ id: 1, po_no: "PO-1001", supplier_id: 7 }),
    ]);
    expect(screen.getAllByText("Bharat Fasteners").length).toBeGreaterThan(0);
    expect(screen.getByText("No supplier yet")).toBeTruthy();
  });
});

describe("PurchasingSection held rows", () => {
  // Both are Draft POs; only `awaiting_approval` (the backend's answer) differs.
  it("locks a held agent draft and hides Delete; a human Draft keeps its controls", () => {
    render(
      <PurchasingSection
        suppliers={suppliers}
        purchaseOrders={[
          po({ id: 2, po_no: "AUTO-PO-3-1789", supplier_id: null, status: "Draft",
            awaiting_approval: { agent_action_id: 11, agent: "reorder", expired: false } }),
          po({ id: 1, po_no: "PO-HUMAN", status: "Draft", awaiting_approval: null }),
        ]}
        inventoryItems={[]}
        analytics={null}
        supplierForm={{ supplier_code: "", supplier_name: "", contact_person: "", email: "",
          phone: "", category: "", status: "Active" }}
        setSupplierForm={noop}
        poForm={{ po_no: "", supplier_id: "", item_id: "", item_name: "", order_quantity: 0,
          received_quantity: 0, unit: "", expected_delivery_date: "", status: "Open", notes: "" }}
        setPoForm={noop}
        createSupplier={noop}
        updateSupplier={noop}
        createPurchaseOrder={noop}
        updatePurchaseOrder={noop}
        deletePurchaseOrder={noop}
        generateOverdueEscalations={noop}
      />,
    );
    const held = screen.getByText("AUTO-PO-3-1789").closest("tr") as HTMLElement;
    expect((within(held).getByLabelText("Received quantity of AUTO-PO-3-1789") as HTMLInputElement).disabled).toBe(true);
    expect((within(held).getByLabelText("Status of AUTO-PO-3-1789") as HTMLSelectElement).disabled).toBe(true);
    expect(within(held).queryByRole("button", { name: "Delete" })).toBeNull();
    expect(within(held).getByRole("note").textContent).toBe(
      "Awaiting approval: proposed by the reorder agent (action #11). An Admin or Supervisor approves or rejects it in Approvals.",
    );

    const lookalike = screen.getByText("PO-HUMAN").closest("tr") as HTMLElement;
    expect((within(lookalike).getByLabelText("Received quantity of PO-HUMAN") as HTMLInputElement).disabled).toBe(false);
    expect((within(lookalike).getByLabelText("Status of PO-HUMAN") as HTMLSelectElement).disabled).toBe(false);
    expect(within(lookalike).getByRole("button", { name: "Delete" })).toBeTruthy();
    expect(within(lookalike).queryByRole("note")).toBeNull();
  });

  it("renders no Delete on a purchase order when the viewer has no delete handler", () => {
    renderWith([po({ id: 1, po_no: "PO-1001" })]);
    const row = screen.getByText("PO-1001").closest("tr") as HTMLElement;
    expect(within(row).queryByRole("button", { name: "Delete" })).toBeNull();
  });
});
