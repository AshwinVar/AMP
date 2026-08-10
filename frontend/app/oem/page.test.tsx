import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * The OEM portal page (ADR-0017).
 *
 * Two page-level decisions are pinned here, because neither lives in lib/ and
 * both are things a real person would run into on their first day:
 *
 *   1. A factory user who reaches /oem is TOLD where to go, rather than shown an
 *      empty manufacturer portal that looks like a broken product.
 *   2. That decision follows the SERVER's 401, not the token's own claim. The
 *      backend re-reads the principal from the database on every request, so a
 *      suspended manufacturer account still carries a token that says
 *      `principal: "oem"` — and a page that trusts the claim would show that
 *      person a fleet screen that then fails to load, with no explanation.
 */

const push = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));

const fetchMock = vi.fn();
vi.stubGlobal("fetch", fetchMock);

import OemPortalPage from "./page";

function jsonResponse(status: number, body: unknown) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response;
}

function tokenWith(payload: Record<string, unknown>) {
  return `header.${btoa(JSON.stringify(payload))}.signature`;
}

beforeEach(() => {
  push.mockReset();
  fetchMock.mockReset();
  localStorage.clear();
});

afterEach(() => {
  localStorage.clear();
});

describe("who lands here", () => {
  it("sends a signed-out visitor to the login page", async () => {
    render(<OemPortalPage />);
    await waitFor(() => expect(push).toHaveBeenCalledWith("/login"));
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("tells a FACTORY user where their equipment settings actually are", async () => {
    // A factory token is a valid session — it is simply the wrong portal, and
    // the backend says so with a 401 "Not an OEM session".
    localStorage.setItem("token", tokenWith({ tenant: "FACTORY_A", role: "Admin" }));
    fetchMock.mockResolvedValue(jsonResponse(401, { detail: "Not an OEM session" }));

    render(<OemPortalPage />);

    await waitFor(() =>
      expect(screen.getByText(/This is the manufacturer portal/)).toBeTruthy(),
    );
    expect(screen.getByText(/Connected Equipment/)).toBeTruthy();
    // And NOT an error banner — nothing went wrong, they are just in the wrong
    // place, and "Request failed (401)" would send them to support.
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("follows the SERVER's 401 even when the token claims to be an OEM", async () => {
    // The token says `principal: "oem"` — a suspended account still holds one.
    // A page that trusted the claim would render an empty fleet.
    localStorage.setItem("token", tokenWith({ principal: "oem", oem: "OEM_ALPHA" }));
    fetchMock.mockResolvedValue(jsonResponse(401, { detail: "This account no longer exists" }));

    render(<OemPortalPage />);

    await waitFor(() =>
      expect(screen.getByText(/This is the manufacturer portal/)).toBeTruthy(),
    );
  });
});

describe("a real failure is not an empty fleet", () => {
  it("shows the error rather than 'no installations recorded'", async () => {
    // A 500 is NOT a wrong-portal answer, so it must reach the user as an error.
    // Rendering "No installations recorded for this manufacturer yet" would be a
    // plausible sentence a service desk would believe.
    localStorage.setItem("token", tokenWith({ principal: "oem", oem: "OEM_ALPHA" }));
    fetchMock.mockResolvedValue(jsonResponse(500, { detail: "database is down" }));

    render(<OemPortalPage />);

    await waitFor(() => expect(screen.getByRole("alert")).toBeTruthy());
    expect(screen.getByRole("alert").textContent).toContain("database is down");
    expect(screen.queryByText(/No installations recorded/)).toBeNull();
    expect(screen.queryByText(/This is the manufacturer portal/)).toBeNull();
  });
});
