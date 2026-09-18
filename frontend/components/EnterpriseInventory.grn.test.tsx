import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import EnterpriseInventory from "./EnterpriseInventory";

/**
 * A goods receipt's quantities decide its inspection result.
 *
 * The GRN form offered a status dropdown (defaulting to "Accepted") and sent a
 * rejected count of 0 whatever happened: 80 of 100 accepted was recorded as
 * "Accepted, 0 rejected", and a line set to "Rejected" with 100 accepted put
 * everything into stock (backend test_grn_quantities_decide_inspection.py). The
 * server now derives both from received and accepted; the form shows what it
 * will record, sends neither, and says why when a line is refused.
 */

const b64 = (o: object) => btoa(JSON.stringify(o)).replace(/=+$/, "");
const TOKEN = `${b64({ alg: "HS256" })}.${b64({ sub: "sup", role: "Supervisor", tenant: "DEFAULT", exp: 4102444800 })}.sig`;
const ITEMS = [{ id: 1, item_code: "BOLT-M8", item_name: "Bolt M8" }] as never[];

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  localStorage.clear();
});

beforeEach(() => {
  localStorage.setItem("token", TOKEN);
});

function stubFetch(grnStatus = 200, grnBody: unknown = { id: 1, grn_no: "GRN-3000" }) {
  const posts: unknown[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit = {}) => {
    if (String(url).includes("/grns") && init.method === "POST") {
      posts.push(JSON.parse(String(init.body)));
      return new Response(JSON.stringify(grnBody), { status: grnStatus, headers: { "Content-Type": "application/json" } });
    }
    return new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } });
  }));
  return posts;
}

function openGrnForm() {
  render(<EnterpriseInventory items={ITEMS} />);
  fireEvent.click(screen.getByRole("button", { name: "GRN" }));
}

function fillLine(received: string, accepted: string) {
  fireEvent.change(screen.getByPlaceholderText("Supplier name"), { target: { value: "Acme" } });
  const itemSelect = screen.getAllByRole("combobox")[0];
  fireEvent.change(itemSelect, { target: { value: "1" } });
  fireEvent.change(screen.getByPlaceholderText("Received qty"), { target: { value: received } });
  fireEvent.change(screen.getByPlaceholderText("Accepted qty"), { target: { value: accepted } });
}

describe("GRN form — the quantities decide the inspection result", () => {
  it("offers no inspection-status choice, and shows what 80 of 100 will record", () => {
    stubFetch();
    openGrnForm();
    fillLine("100", "80");
    expect(screen.queryByRole("option", { name: "Rejected" })).toBeNull();
    expect(screen.getByLabelText("Line 1 inspection result").textContent).toBe("Partial · 20 rejected");
  });

  it("reads Accepted, all-rejected and over-accepted from the numbers", () => {
    stubFetch();
    openGrnForm();
    fillLine("100", "100");
    expect(screen.getByLabelText("Line 1 inspection result").textContent).toBe("Accepted");
    fillLine("50", "0");
    expect(screen.getByLabelText("Line 1 inspection result").textContent).toBe("Rejected (all 50)");
    fillLine("10", "12");
    expect(screen.getByLabelText("Line 1 inspection result").textContent).toBe("More accepted than received");
  });

  it("sends neither a rejected count nor a status", async () => {
    const posts = stubFetch();
    openGrnForm();
    fillLine("100", "80");
    fireEvent.submit(screen.getByPlaceholderText("Supplier name").closest("form") as HTMLFormElement);
    await waitFor(() => expect(posts.length).toBe(1));
    const line = (posts[0] as { items: Record<string, unknown>[] }).items[0];
    expect(line).toEqual({ item_id: 1, lot_no: "", received_qty: 100, accepted_qty: 80 });
  });

  it("says why a refused line was refused, and gives the button back", async () => {
    stubFetch(400, { detail: "Line 1: accepted quantity (12) cannot exceed the quantity received (10)" });
    openGrnForm();
    fillLine("10", "12");
    fireEvent.submit(screen.getByPlaceholderText("Supplier name").closest("form") as HTMLFormElement);
    expect((await screen.findByRole("alert")).textContent).toContain("cannot exceed the quantity received");
    expect(screen.getByRole("button", { name: "Create GRN" })).toBeTruthy();
  });
});
