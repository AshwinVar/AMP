import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

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
