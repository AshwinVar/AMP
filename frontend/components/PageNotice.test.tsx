import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import PageNotice from "./PageNotice";

/**
 * The one notice every paged section shows (ADR-0036): says how many of the
 * tenant's rows the page shows, points at the CSV export when there is one,
 * and says NOTHING when the page is the whole list or the count is unknown.
 */

afterEach(cleanup);

describe("PageNotice: a page says it is one", () => {
  it("says how many of the tenant's rows the page shows, and where the rest are", () => {
    render(<PageNotice shown={200} total={1234} noun="work orders" exportName="Work orders" />);
    const notice = screen.getByTestId("page-notice");
    expect(notice.textContent).toContain("Showing the newest 200 of 1,234 work orders");
    expect(notice.textContent).toContain("Work orders CSV export (Reports)");
  });

  it("ends the sentence when there is no export to point at", () => {
    render(<PageNotice shown={300} total={301} noun="recommendations" />);
    const notice = screen.getByTestId("page-notice");
    expect(notice.textContent).toBe("Showing the newest 300 of 301 recommendations.");
  });

  it("says nothing when the page is the whole list", () => {
    render(<PageNotice shown={7} total={7} noun="documents" />);
    expect(screen.queryByTestId("page-notice")).toBeNull();
  });

  it("says nothing when the count is not known", () => {
    render(<PageNotice shown={7} total={null} noun="documents" />);
    expect(screen.queryByTestId("page-notice")).toBeNull();
    render(<PageNotice shown={7} total={undefined} noun="documents" />);
    expect(screen.queryByTestId("page-notice")).toBeNull();
  });

  it("carries the section's own test id and classes when asked", () => {
    render(<PageNotice shown={2} total={9} noun="items" testId="inventory-page-notice" className="mb-4" />);
    const notice = screen.getByTestId("inventory-page-notice");
    expect(notice.className).toContain("mb-4");
  });
});

describe("PageNotice: the page can grow", () => {
  it("offers the next batch when the screen can ask for one, and asks on click", () => {
    const onMore = vi.fn();
    render(<PageNotice shown={200} total={1234} noun="work orders" more={200} onMore={onMore} />);
    const button = screen.getByRole("button", { name: "Show the next 200" });
    fireEvent.click(button);
    expect(onMore).toHaveBeenCalledTimes(1);
  });

  it("offers only what is left when the next batch would pass the total", () => {
    render(<PageNotice shown={500} total={520} noun="suppliers" more={20} onMore={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Show the next 20" })).toBeTruthy();
  });

  it("offers nothing without a batch to add, or without a way to ask", () => {
    render(<PageNotice shown={2000} total={5000} noun="audit entries" more={null} onMore={vi.fn()} />);
    expect(screen.queryByRole("button")).toBeNull();
    cleanup();
    render(<PageNotice shown={200} total={1234} noun="work orders" more={200} />);
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.getByTestId("page-notice").textContent).toContain("Showing the newest 200 of 1,234 work orders");
  });
});
