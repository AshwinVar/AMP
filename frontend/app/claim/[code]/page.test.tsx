import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

/**
 * The page a QR on a machine opens (ADR-0019).
 *
 * The property under test is what this page does NOT do. Scanning a sticker, or
 * following a link somebody forwarded, must not attach equipment to anybody's
 * workspace — it may only fill in the box. Everything after that is the same two
 * deliberate presses as typing the code by hand.
 *
 * The other three cases are the ones a real shop floor produces: a phone camera
 * with no AMP session, a manufacturer who scanned their own sticker, and a
 * factory Admin who can get on with it.
 */

const push = vi.fn();
let route: Record<string, string> = { code: "AMP-7K2QM-4XPWD-9TBHF" };

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push }),
  useParams: () => route,
}));

const apiGet = vi.fn();
const apiPost = vi.fn();
let token: string | null = null;

vi.mock("../../../lib/api", () => ({
  apiGet: (p: string) => apiGet(p),
  apiPost: (p: string, b: unknown) => apiPost(p, b),
  getToken: () => token,
}));

let oemSession = false;
vi.mock("../../../lib/oem", () => ({ isOemSession: () => oemSession }));

import ClaimPage from "./page";

beforeEach(() => {
  push.mockReset();
  apiGet.mockReset();
  apiPost.mockReset();
  localStorage.clear();
  token = null;
  oemSession = false;
  route = { code: "AMP-7K2QM-4XPWD-9TBHF" };
});

describe("opening a scanned claim link", () => {
  it("CLAIMS NOTHING on arrival", async () => {
    // The whole point. A GET would already be too much, and a POST would mean a
    // forwarded link attaches a stranger's machine to somebody's workspace.
    token = "factory.token";
    render(<ClaimPage />);
    await waitFor(() =>
      expect(screen.getByDisplayValue("AMP-7K2QM-4XPWD-9TBHF")).toBeTruthy(),
    );
    expect(apiPost).not.toHaveBeenCalled();
    expect(apiGet).not.toHaveBeenCalled();
  });

  it("fills the code in, so nobody retypes an oily label", () => {
    token = "factory.token";
    render(<ClaimPage />);
    const box = screen.getByDisplayValue(
      "AMP-7K2QM-4XPWD-9TBHF",
    ) as HTMLInputElement;
    expect(box.tagName).toBe("INPUT");
    // ...and the lookup is still a press.
    expect(screen.getByRole("button", { name: /Look up machine/ })).toBeTruthy();
  });

  it("asks an anonymous visitor to sign in, and remembers where to return", () => {
    render(<ClaimPage />);
    screen.getByRole("button", { name: /Sign in/ }).click();
    expect(localStorage.getItem("afterLogin")).toBe(
      "/claim/AMP-7K2QM-4XPWD-9TBHF",
    );
    expect(push).toHaveBeenCalledWith("/login");
    // CONTROL: no claim form is offered to somebody with no session.
    expect(screen.queryByRole("button", { name: /Look up machine/ })).toBeNull();
  });

  it("tells a MANUFACTURER this is their customer's action, not theirs", () => {
    token = "oem.token";
    oemSession = true;
    render(<ClaimPage />);
    expect(
      screen.getByText(/added by the factory that receives it/),
    ).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Look up machine/ })).toBeNull();
  });

  it("survives a URL-encoded code", () => {
    // A QR generator that percent-encodes the dashes must not produce a code
    // that silently fails to match anything.
    token = "factory.token";
    route = { code: "AMP%2D7K2QM%2D4XPWD%2D9TBHF" };
    render(<ClaimPage />);
    expect(screen.getByDisplayValue("AMP-7K2QM-4XPWD-9TBHF")).toBeTruthy();
  });
});
