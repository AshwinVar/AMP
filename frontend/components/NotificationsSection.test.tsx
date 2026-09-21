import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import NotificationsSection from "./NotificationsSection";

/**
 * The unread count is the tenant's, not the page's (ADR-0036).
 *
 * /notifications is a page of the newest 500. The section used to count the
 * unread rows in that page and show the number as THE unread count; a plant
 * past 500 notifications saw a confident, understated figure. The dashboard
 * now asks the backend (/notifications?unread=true&limit=1, whose
 * X-Total-Count is the whole unread count) and hands it down as unreadTotal.
 * The page count is only the fallback when the total is not known.
 */

function notif(id: number, status: string | null) {
  return { id, notification_type: "System", severity: "Info", title: `N${id}`, message: "m",
           status, created_at: "2026-09-21T00:00:00" };
}

afterEach(cleanup);

describe("NotificationsSection counts unread over the whole tenant", () => {
  const rows = [notif(1, "Unread"), notif(2, "Read"), notif(3, null)] as never;

  it("uses the tenant-wide unread total for 'Mark all read (N)' when it is known", () => {
    render(<NotificationsSection notifications={rows} generateNotifications={vi.fn()}
      updateNotification={vi.fn()} markAllRead={vi.fn()} total={734} unreadTotal={412} />);
    expect(screen.getByRole("button", { name: "Mark all read (412)" })).toBeTruthy();
  });

  it("falls back to the rows on the page (a NULL status is unread) when the total is not known", () => {
    render(<NotificationsSection notifications={rows} generateNotifications={vi.fn()}
      updateNotification={vi.fn()} markAllRead={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Mark all read (2)" })).toBeTruthy();
  });

  it("says the list is a page when the tenant has more notifications than it shows", () => {
    render(<NotificationsSection notifications={rows} generateNotifications={vi.fn()}
      updateNotification={vi.fn()} total={734} unreadTotal={0} />);
    expect(screen.getByTestId("page-notice").textContent).toContain("Showing the newest 3 of 734 notifications");
    expect(screen.queryByRole("button", { name: /Mark all read/ })).toBeNull();
  });

  it("says nothing about paging when the page is everything", () => {
    render(<NotificationsSection notifications={rows} generateNotifications={vi.fn()}
      updateNotification={vi.fn()} total={3} unreadTotal={2} />);
    expect(screen.queryByTestId("page-notice")).toBeNull();
  });
});
