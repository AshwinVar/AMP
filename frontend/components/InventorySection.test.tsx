import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import InventorySection from "./InventorySection";

/**
 * The inventory table shows the newest page of items (500 by default), not the
 * whole book. When the tenant has more, the table must say so, and point at
 * the CSV export, which is never paged; when it does not -- or when the count
 * is not known -- it must say nothing, because a notice that cries wolf on a
 * complete list is as misleading as a page that looks complete.
 */

function item(i: number) {
  return {
    id: i, item_code: `INV-${i}`, item_name: `Part ${i}`, category: "Raw", supplier: "Local",
    unit: "kg", current_stock: 10, reorder_level: 1, location: "A1",
  };
}

function renderSection(items: ReturnType<typeof item>[], total: number | null | undefined) {
  return render(
    <InventorySection
      items={items as never}
      total={total}
      transactions={[]}
      analytics={null}
      itemForm={{ item_code: "", item_name: "", category: "", supplier: "", unit: "", current_stock: 0, reorder_level: 0, location: "" }}
      setItemForm={vi.fn()}
      transactionForm={{ item_id: "", transaction_type: "Issue", quantity: 0, reference: "", notes: "" }}
      setTransactionForm={vi.fn()}
      createItem={vi.fn()}
      updateItem={vi.fn()}
      createTransaction={vi.fn()}
      generateLowStockEscalations={vi.fn()}
    />,
  );
}

describe("InventorySection: a page says it is one", () => {
  it("says how many of the tenant's items the page shows, and where the rest are", () => {
    renderSection([item(1), item(2)], 1234);
    const notice = screen.getByTestId("inventory-page-notice");
    expect(notice.textContent).toContain("Showing the newest 2 of 1,234 items");
    expect(notice.textContent).toContain("CSV export");
  });

  it("says nothing when the page is the whole list", () => {
    renderSection([item(1), item(2)], 2);
    expect(screen.queryByTestId("inventory-page-notice")).toBeNull();
  });

  it("says nothing when the count is not known", () => {
    renderSection([item(1), item(2)], null);
    expect(screen.queryByTestId("inventory-page-notice")).toBeNull();
    renderSection([item(3)], undefined);
    expect(screen.queryByTestId("inventory-page-notice")).toBeNull();
  });
});
