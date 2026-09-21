import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

/**
 * The AI consent card (ADR-0020, ADR-0037).
 *
 * The backend is the boundary (Admin only, never from a preview, audited in the
 * same commit). What this pins is that the screen does not mislead around it:
 * turning learning ON is never one accidental click, a read-only viewer is told
 * why, an Operator is not shown a panel the server would refuse, and a refused
 * write shows the server's own reason instead of looking like success.
 */

const apiGet = vi.fn();
const apiPut = vi.fn();
let role = "Admin";

vi.mock("../lib/api", () => ({
  apiGet: (p: string) => apiGet(p),
  apiPut: (p: string, b: unknown) => apiPut(p, b),
  getUserRole: () => role,
}));

import AILearningConsentCard from "./AILearningConsentCard";
import type { ConsentPage } from "../lib/aiModels";

function page(over: Partial<ConsentPage> = {}, granted = false): ConsentPage {
  return {
    tenant: "ACME",
    can_edit: true,
    read_only_reason: null,
    capabilities: [
      {
        capability: "telemetry_baseline",
        title: "Learn each machine's normal telemetry",
        reads: "Reads this machine's last 14 days of telemetry.",
        stored: "Nothing is stored.",
        used_by: "The anomaly check.",
        without: "The anomaly check refuses.",
        granted,
        granted_by: granted ? "acme-admin" : null,
        granted_at: granted ? "2026-09-17T10:00:00" : null,
        revoked_by: null,
        revoked_at: null,
        updated_at: null,
      },
    ],
    ...over,
  };
}

beforeEach(() => {
  apiGet.mockReset();
  apiPut.mockReset();
  role = "Admin";
});

describe("AILearningConsentCard", () => {
  it("is not shown to an Operator, and does not even ask the server", () => {
    role = "Operator";
    const { container } = render(<AILearningConsentCard />);
    expect(container.innerHTML).toBe("");
    expect(apiGet).not.toHaveBeenCalled();
  });

  it("asks for confirmation, saying what is read and kept, before turning learning ON", async () => {
    apiGet.mockResolvedValue(page());
    apiPut.mockResolvedValue(page({}, true));
    render(<AILearningConsentCard />);
    const toggle = await screen.findByRole("switch", { name: "Learn each machine's normal telemetry" });
    expect(toggle.getAttribute("aria-checked")).toBe("false");
    expect(screen.getByText("Off · never turned on")).toBeTruthy();

    fireEvent.click(toggle);
    expect(apiPut).not.toHaveBeenCalled();
    const dialog = screen.getByRole("dialog");
    expect(dialog.textContent).toContain("Reads this machine's last 14 days of telemetry.");
    expect(dialog.textContent).toContain("Nothing is stored.");

    fireEvent.click(screen.getByRole("button", { name: "Turn on" }));
    await waitFor(() =>
      expect(apiPut).toHaveBeenCalledWith("/ai-consent/telemetry_baseline", { granted: true }),
    );
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(screen.getByRole("switch").getAttribute("aria-checked")).toBe("true");
  });

  it("cancelling the confirmation changes nothing", async () => {
    apiGet.mockResolvedValue(page());
    render(<AILearningConsentCard />);
    fireEvent.click(await screen.findByRole("switch"));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(apiPut).not.toHaveBeenCalled();
  });

  it("turns learning OFF straight away (withdrawing needs no confirmation)", async () => {
    apiGet.mockResolvedValue(page({}, true));
    apiPut.mockResolvedValue(page());
    render(<AILearningConsentCard />);
    fireEvent.click(await screen.findByRole("switch"));
    await waitFor(() =>
      expect(apiPut).toHaveBeenCalledWith("/ai-consent/telemetry_baseline", { granted: false }),
    );
  });

  it("is read-only when the server says so, and shows the server's reason", async () => {
    role = "Supervisor";
    apiGet.mockResolvedValue(
      page({ can_edit: false, read_only_reason: "Only an Admin of this company can change learning consent." }),
    );
    render(<AILearningConsentCard />);
    const toggle = await screen.findByRole("switch");
    expect((toggle as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText("Only an Admin of this company can change learning consent.")).toBeTruthy();
  });

  it("shows why a write was refused instead of pretending it worked", async () => {
    apiGet.mockResolvedValue(page());
    apiPut.mockRejectedValue(new Error(JSON.stringify({ detail: "not from a platform preview" })));
    render(<AILearningConsentCard />);
    fireEvent.click(await screen.findByRole("switch"));
    fireEvent.click(screen.getByRole("button", { name: "Turn on" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("not from a platform preview");
    expect(screen.getByRole("switch").getAttribute("aria-checked")).toBe("false");
  });

  // ADR-0038: a scoped consent names the provider it was given for.
  it("shows a grant given for another provider as not active, and turning it on names the configured one", async () => {
    const scoped = {
      ...page().capabilities[0],
      capability: "external_model",
      title: "Send Copilot questions and evidence to a hosted AI model",
      scoped: true, scope: "anthropic", configured: "gemini", active: false,
      granted: true, granted_by: "acme-admin", granted_at: "2026-09-17T10:00:00",
    };
    apiGet.mockResolvedValue(page({ capabilities: [scoped] }));
    render(<AILearningConsentCard />);
    const toggle = await screen.findByRole("switch", { name: scoped.title });
    expect(screen.getByText(/Not active · given for Anthropic/).textContent).toContain("Google Gemini is configured now");
    // The switch reads ON (the grant exists) but a click reviews and re-grants for the configured provider
    // rather than withdrawing: nothing is sent to Gemini until the Admin says so.
    fireEvent.click(toggle);
    const dialog = await screen.findByRole("dialog");
    expect(dialog.textContent).toContain("for Google Gemini");
    expect(dialog.textContent).toContain("given for Google Gemini only");
    expect(apiPut).not.toHaveBeenCalled();
  });

  it("cannot be turned on when no hosted provider is configured, and says why", async () => {
    const scoped = {
      ...page().capabilities[0],
      capability: "external_model",
      title: "Send Copilot questions and evidence to a hosted AI model",
      scoped: true, scope: null, configured: null, active: false, granted: false,
    };
    apiGet.mockResolvedValue(page({ capabilities: [scoped] }));
    render(<AILearningConsentCard />);
    const toggle = await screen.findByRole("switch", { name: scoped.title });
    expect((toggle as HTMLButtonElement).disabled).toBe(true);
    expect(toggle.getAttribute("title")).toMatch(/nothing to consent to/);
    expect(screen.getByText(/no hosted AI provider is configured, so there is nothing to consent to/)).toBeTruthy();
  });

  it("says the consent could not be loaded rather than showing nothing", async () => {
    apiGet.mockRejectedValue(new Error("Failed request: /ai-consent | 500 | boom"));
    render(<AILearningConsentCard />);
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/Could not load AI consent/);
  });
});
