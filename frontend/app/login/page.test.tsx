import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * Where a successful sign-in lands you (ADR-0017).
 *
 * A manufacturer and a factory user come through the same door. Sending an OEM
 * to /dashboard gives them a shop floor that refuses them at every endpoint —
 * the backend rejects an OEM token on factory routes — which reads as a broken
 * product rather than as the wrong page. This is the one line that prevents it,
 * and it is exactly the kind of line that survives a refactor by accident.
 */

const push = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));

const fetchMock = vi.fn();
vi.stubGlobal("fetch", fetchMock);

import LoginPage from "./page";

function tokenWith(payload: Record<string, unknown>) {
  return `header.${btoa(JSON.stringify(payload))}.signature`;
}

function loginResolves(claims: Record<string, unknown>, role = "Admin") {
  fetchMock.mockResolvedValue({
    ok: true,
    json: async () => ({
      access_token: tokenWith(claims),
      role,
      tenant: claims.tenant ?? "DEFAULT",
    }),
  } as unknown as Response);
}

async function signIn() {
  render(<LoginPage />);
  const [username, password] = screen.getAllByRole("textbox").concat(
    Array.from(document.querySelectorAll('input[type="password"]')) as HTMLElement[],
  );
  fireEvent.change(username, { target: { value: "someone" } });
  fireEvent.change(password, { target: { value: "secret" } });
  fireEvent.click(screen.getByRole("button", { name: /sign in|log ?in/i }));
}

beforeEach(() => {
  push.mockReset();
  fetchMock.mockReset();
  localStorage.clear();
});

afterEach(() => {
  localStorage.clear();
});

describe("post-login routing", () => {
  it("sends a MANUFACTURER to the OEM portal", async () => {
    loginResolves({ principal: "oem", oem: "OEM_ALPHA", sub: "alpha.svc" }, "OEM_ADMIN");
    await signIn();
    await waitFor(() => expect(push).toHaveBeenCalledWith("/oem"));
  });

  it("sends a FACTORY user to the dashboard", async () => {
    loginResolves({ tenant: "FACTORY_A", role: "Admin", sub: "ops" });
    await signIn();
    await waitFor(() => expect(push).toHaveBeenCalledWith("/dashboard"));
  });

  it("does not treat a factory Admin as an OEM because of a role string", async () => {
    // CONTROL for the claim the decision reads. A factory role that merely looks
    // OEM-ish must not divert a shop-floor user to a manufacturer portal they
    // are refused from.
    loginResolves({ tenant: "FACTORY_A", role: "OEM_ADMIN", sub: "ops" });
    await signIn();
    await waitFor(() => expect(push).toHaveBeenCalledWith("/dashboard"));
    expect(push).not.toHaveBeenCalledWith("/oem");
  });
});

describe("coming back to a scanned claim code", () => {
  it("returns to the page the QR opened", async () => {
    // Scan on the shop floor, no session, sign in, and the twenty characters
    // off an oily label are still there.
    localStorage.setItem("afterLogin", "/claim/AMP-7K2QM-4XPWD-9TBHF");
    loginResolves({ tenant: "FACTORY_A", role: "Admin", sub: "ops" });
    await signIn();
    await waitFor(() =>
      expect(push).toHaveBeenCalledWith("/claim/AMP-7K2QM-4XPWD-9TBHF"),
    );
    expect(push).not.toHaveBeenCalledWith("/dashboard");
    // Consumed, so a later ordinary sign-in is not hijacked by a stale one.
    expect(localStorage.getItem("afterLogin")).toBeNull();
  });

  it("refuses to bounce to another origin", async () => {
    // An OPEN REDIRECT. Anything that can write localStorage could otherwise
    // land somebody on a foreign page with the visual authority of having just
    // signed in to AMP.
    for (const hostile of [
      "https://evil.example/harvest",
      "//evil.example",
      "javascript:alert(1)",
    ]) {
      // Testing Library unmounts between `it` blocks, not inside one, so
      // without this the second pass finds two sign-in buttons.
      cleanup();
      push.mockReset();
      localStorage.setItem("afterLogin", hostile);
      loginResolves({ tenant: "FACTORY_A", role: "Admin", sub: "ops" });
      await signIn();
      await waitFor(() => expect(push).toHaveBeenCalledWith("/dashboard"));
      expect(push).not.toHaveBeenCalledWith(hostile);
    }
  });
});
