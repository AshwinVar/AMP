import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

/**
 * Adding a machine a supplier sent you (ADR-0019).
 *
 * The property under test is the ORDER OF EVENTS. Looking up a code must not
 * claim the machine — a QR scanned by somebody walking past a crate, or a link
 * forwarded to the wrong person, must not attach equipment to a workspace. So
 * the lookup is a read and the commit is a separate, deliberate press, and the
 * screen in between has to say what the person is agreeing to before they agree.
 */

const apiGet = vi.fn();
const apiPost = vi.fn();
const onAdded = vi.fn();

vi.mock("../lib/api", () => ({
  apiGet: (p: string) => apiGet(p),
  apiPost: (p: string, b: unknown) => apiPost(p, b),
}));

import AddConnectedEquipment from "./AddConnectedEquipment";

const PREVIEW = {
  claim_id: 1,
  manufacturer: "OEM_ALPHA",
  manufacturer_name: "Alpha Compressors",
  support_email: "service@alpha.example",
  serial_number: "SN-001",
  model_code: "X200",
  model_name: "X200 Compressor",
  family: "Compressor",
  firmware_version: "1.2.0",
  warranty: { state: "active", reason: "expires 2028-01-01", days_remaining: 500 },
  expires_at: "2026-09-10T00:00:00",
  available_grants: [
    { key: "SHARE_MACHINE_HEALTH", label: "Machine health" },
    { key: "SHARE_OPERATING_HOURS", label: "Operating hours" },
  ],
  already_granted: [] as string[],
};

async function openAndLookUp(code = "AMP-7K2QM-4XPWD-9TBHF") {
  render(<AddConnectedEquipment onAdded={onAdded} />);
  fireEvent.click(screen.getByRole("button", { name: /Add equipment/ }));
  fireEvent.change(screen.getByLabelText(/Claim code/), { target: { value: code } });
  fireEvent.click(screen.getByRole("button", { name: /Look up machine/ }));
}

beforeEach(() => {
  apiGet.mockReset();
  apiPost.mockReset();
  onAdded.mockReset();
});

describe("looking up a code", () => {
  it("READS the claim and does not accept it", async () => {
    apiGet.mockResolvedValue(PREVIEW);
    await openAndLookUp();
    await waitFor(() => expect(apiGet).toHaveBeenCalled());
    // The whole point: a lookup is a GET, and nothing is committed.
    expect(apiGet.mock.calls[0][0]).toContain("/connected-equipment/claim/");
    expect(apiPost).not.toHaveBeenCalled();
    expect(onAdded).not.toHaveBeenCalled();
  });

  it("shows what the machine actually is before anything is committed", async () => {
    apiGet.mockResolvedValue(PREVIEW);
    await openAndLookUp();
    await waitFor(() => expect(screen.getByText("SN-001")).toBeTruthy());
    // The manufacturer is named TWICE on purpose — once as "this is who made
    // it" and once as "this is who you would be sharing with". Somebody
    // skim-reading the consent section should not have to scroll up to learn
    // whose name is on the agreement.
    expect(screen.getAllByText(/Alpha Compressors/).length).toBe(2);
    expect(screen.getByText(/X200 Compressor/)).toBeTruthy();
    expect(screen.getByText(/expires 2028-01-01/)).toBeTruthy();
  });

  it("shows the backend's own refusal, which is the same for every failure", async () => {
    const refusal =
      "That claim code is not valid. It may have been mistyped, expired, " +
      "already used, or withdrawn by the manufacturer — ask them to issue a new one.";
    apiGet.mockRejectedValue(new Error(JSON.stringify({ detail: refusal })));
    await openAndLookUp("AMP-XXXXX-XXXXX-XXXXX");
    await waitFor(() => expect(screen.getByRole("alert")).toBeTruthy());
    expect(screen.getByRole("alert").textContent).toBe(refusal);
    // A failed lookup must not leave a half-built confirmation on screen.
    expect(screen.queryByRole("button", { name: /Confirm and add/ })).toBeNull();
  });
});

describe("confirming", () => {
  it("sends the chosen grants, and only on the explicit press", async () => {
    apiGet.mockResolvedValue(PREVIEW);
    apiPost.mockResolvedValue({ serial_number: "SN-001", message: "ok" });
    await openAndLookUp();
    await waitFor(() => expect(screen.getByText("SN-001")).toBeTruthy());

    fireEvent.click(screen.getByRole("checkbox", { name: /Operating hours/ }));
    expect(apiPost).not.toHaveBeenCalled(); // ticking a box commits nothing

    fireEvent.click(screen.getByRole("button", { name: /Confirm and add machine/ }));
    await waitFor(() => expect(apiPost).toHaveBeenCalled());
    expect(apiPost.mock.calls[0][1]).toEqual({ grants: ["SHARE_OPERATING_HOURS"] });
    await waitFor(() => expect(onAdded).toHaveBeenCalled());
  });

  it("adds the machine with NO sharing when nothing is ticked", async () => {
    apiGet.mockResolvedValue(PREVIEW);
    apiPost.mockResolvedValue({ serial_number: "SN-001", message: "ok" });
    await openAndLookUp();
    await waitFor(() => expect(screen.getByText("SN-001")).toBeTruthy());
    expect(screen.getByText(/You are sharing nothing/)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: /Confirm and add machine/ }));
    await waitFor(() => expect(apiPost).toHaveBeenCalled());
    expect(apiPost.mock.calls[0][1]).toEqual({ grants: [] });
  });

  it("starts from what is ALREADY agreed, so adding a machine never reads as turning sharing off", async () => {
    // Accepting can only widen consent. Empty boxes beside an existing
    // agreement would imply it was about to be switched off — which is exactly
    // the defect the backend union fixes, surfaced here so the screen agrees.
    apiGet.mockResolvedValue({
      ...PREVIEW,
      already_granted: ["SHARE_MACHINE_HEALTH"],
    });
    await openAndLookUp();
    await waitFor(() => expect(screen.getByText("SN-001")).toBeTruthy());
    expect(
      (screen.getByRole("checkbox", { name: /Machine health/ }) as HTMLInputElement)
        .checked,
    ).toBe(true);
    expect(screen.getByText(/already shared with this manufacturer/)).toBeTruthy();
  });

  it("names the categories no setting can ever reveal", async () => {
    apiGet.mockResolvedValue(PREVIEW);
    await openAndLookUp();
    await waitFor(() =>
      expect(screen.getByText(/work orders, production quantities/)).toBeTruthy(),
    );
  });

  it("surfaces a refused confirmation instead of pretending it worked", async () => {
    apiGet.mockResolvedValue(PREVIEW);
    apiPost.mockRejectedValue(
      new Error(JSON.stringify({ detail: "That claim code is not valid." })),
    );
    await openAndLookUp();
    await waitFor(() => expect(screen.getByText("SN-001")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: /Confirm and add machine/ }));
    await waitFor(() =>
      expect(screen.getByRole("alert").textContent).toBe("That claim code is not valid."),
    );
    expect(onAdded).not.toHaveBeenCalled();
  });
});
