import { describe, expect, it, vi } from "vitest";

import { MAX_PAGE, askForMore, emptyListState, moreFor, nextDepth, pagedList, pagedPath } from "./paged-list";

/**
 * The client side of "a list is a page" (ADR-0036): the request for a deeper
 * page, what the screen remembers per path (total, default page size, the
 * depth the user asked for), and how far "Show more" reaches -- never past
 * the tenant's total or the backend's ceiling (paging.MAX_PAGE).
 */

describe("pagedPath", () => {
  it("asks for the default page when no depth was chosen", () => {
    expect(pagedPath("/work-orders")).toBe("/work-orders");
    expect(pagedPath("/work-orders", undefined)).toBe("/work-orders");
    expect(pagedPath("/work-orders", 0)).toBe("/work-orders");
  });

  it("asks for a chosen depth, on a bare path and on one that already has a query", () => {
    expect(pagedPath("/work-orders", 400)).toBe("/work-orders?limit=400");
    expect(pagedPath("/notifications?unread=true", 1)).toBe("/notifications?unread=true&limit=1");
  });

  it("never asks past the backend ceiling", () => {
    expect(pagedPath("/work-orders", 999999)).toBe(`/work-orders?limit=${MAX_PAGE}`);
    expect(MAX_PAGE).toBe(2000);
  });
});

describe("pagedList", () => {
  it("returns the rows and remembers the total and the default page size per path", async () => {
    const get = vi.fn(async () => ({ data: [{ id: 1 }, { id: 2 }], total: 1234 }));
    const state = emptyListState();
    const rows = await pagedList(get, state, "/work-orders");
    expect(rows).toHaveLength(2);
    expect(get).toHaveBeenCalledWith("/work-orders");
    expect(state.totals["/work-orders"]).toBe(1234);
    expect(state.pages["/work-orders"]).toBe(2);
  });

  it("asks at the depth the user chose, and keeps the page size it learned from the default page", async () => {
    const get = vi.fn(async (p: string) => ({ data: p.includes("limit") ? [1, 2, 3, 4] : [1, 2], total: 9 }));
    const state = emptyListState();
    await pagedList(get, state, "/documents");
    state.depth["/documents"] = 4;
    const rows = await pagedList(get, state, "/documents");
    expect(get).toHaveBeenLastCalledWith("/documents?limit=4");
    expect(rows).toHaveLength(4);
    expect(state.pages["/documents"]).toBe(2);
    expect(state.totals["/documents"]).toBe(9);
  });

  it("a total the backend did not send stays unknown, and a non-array body is an empty list", async () => {
    const get = vi.fn(async () => ({ data: null as unknown as number[], total: null }));
    const state = emptyListState();
    state.totals["/reports"] = 5;
    const rows = await pagedList(get, state, "/reports");
    expect(rows).toEqual([]);
    expect(state.totals["/reports"]).toBeNull();
  });
});

describe("nextDepth", () => {
  it("adds one default page on top of what is shown", () => {
    expect(nextDepth(200, 1234, 200)).toBe(400);
    expect(nextDepth(400, 1234, 200)).toBe(600);
  });

  it("stops at the tenant's total", () => {
    expect(nextDepth(200, 250, 200)).toBe(250);
    expect(nextDepth(250, 250, 200)).toBeNull();
  });

  it("stops at the backend ceiling", () => {
    expect(nextDepth(1900, 5000, 200)).toBe(2000);
    expect(nextDepth(2000, 5000, 200)).toBeNull();
  });

  it("offers nothing when the count is unknown or the page is everything", () => {
    expect(nextDepth(200, null, 200)).toBeNull();
    expect(nextDepth(200, undefined, 200)).toBeNull();
    expect(nextDepth(7, 7, 500)).toBeNull();
  });

  it("falls back to doubling when the page size was never learned", () => {
    expect(nextDepth(300, 1000, 0)).toBe(600);
  });
});

describe("moreFor and askForMore", () => {
  it("say how many rows the next batch adds, and record the wish", () => {
    const state = emptyListState();
    state.totals["/work-orders"] = 1234;
    state.pages["/work-orders"] = 200;
    expect(moreFor(state, "/work-orders", 200)).toBe(200);
    expect(askForMore(state, "/work-orders", 200)).toBe(400);
    expect(state.depth["/work-orders"]).toBe(400);
    expect(moreFor(state, "/work-orders", 400)).toBe(200);
  });

  it("offer nothing, and change nothing, when the page is everything", () => {
    const state = emptyListState();
    state.totals["/documents"] = 7;
    state.pages["/documents"] = 7;
    expect(moreFor(state, "/documents", 7)).toBeNull();
    expect(askForMore(state, "/documents", 7)).toBeNull();
    expect(state.depth).toEqual({});
  });

  it("the last batch is only what is left", () => {
    const state = emptyListState();
    state.totals["/suppliers"] = 520;
    state.pages["/suppliers"] = 500;
    expect(moreFor(state, "/suppliers", 500)).toBe(20);
    expect(askForMore(state, "/suppliers", 500)).toBe(520);
  });
});
