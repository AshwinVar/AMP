import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

/**
 * The factory's consent panel (ADR-0017).
 *
 * The property under test is not layout. It is that this screen tells a factory
 * administrator the truth about what a machine's manufacturer can read from
 * their shop floor — including, and especially, the NEGATIVE case. A panel that
 * lists three ticked grants and silently omits the other four reads as "we share
 * everything", and the person who would notice is exactly the person reading
 * this screen to check.
 *
 * A second property: a REFUSED write must show the backend's own reason. "That
 * manufacturer has no equipment installed here" and "Unknown sharing grants: X"
 * need different reactions, and a swallowed message makes both look like an
 * outage.
 */

const apiGet = vi.fn();
const apiPut = vi.fn();
let role = "Admin";

vi.mock("../lib/api", () => ({
  apiGet: (p: string) => apiGet(p),
  apiPut: (p: string, b: unknown) => apiPut(p, b),
  getUserRole: () => role,
}));

import ConnectedEquipment, {
  type ConnectedEquipmentResponse,
} from "./ConnectedEquipment";

const GRANTS = [
  { key: "SHARE_MACHINE_HEALTH", label: "Machine health" },
  { key: "SHARE_OPERATING_HOURS", label: "Operating hours" },
  { key: "SHARE_ALARMS", label: "Alarm codes" },
];

function payload(granted: string[] = []): ConnectedEquipmentResponse {
  return {
    equipment: [
      {
        installation_id: 1,
        serial_number: "ALPHA-0001",
        manufacturer: "OEM_ALPHA",
        manufacturer_name: "Alpha Compressors",
        support_email: "service@alpha.example",
        model_code: "X200",
        model_name: "X200",
        site: "Plant 1",
        machine_id: 7,
        lifecycle_status: "Active",
        commissioned_at: null,
        warranty: { state: "active", reason: "expires 2028-01-01", days_remaining: 500 },
        service: { state: "overdue", reason: "2100 h run, never serviced", hours_remaining: -100 },
        shared_with_manufacturer: granted,
      },
    ],
    manufacturers: [
      { oem_code: "OEM_ALPHA", name: "Alpha Compressors", machines: 1, granted },
    ],
    available_grants: GRANTS,
  };
}

beforeEach(() => {
  apiGet.mockReset();
  apiPut.mockReset();
  role = "Admin";
});

describe("what the factory is told", () => {
  it("names every grant it is NOT sharing, not only the ones it is", async () => {
    // The bug this prevents: rendering the granted list alone. Two of three
    // withheld is the common real state, and a screen that shows one tick and
    // says nothing else reads as consent to everything.
    apiGet.mockResolvedValue(payload(["SHARE_OPERATING_HOURS"]));
    render(<ConnectedEquipment />);
    await waitFor(() =>
      expect(screen.getByText(/Not shared: Machine health, Alarm codes\./)).toBeTruthy(),
    );
  });

  it("says plainly when a manufacturer has been given nothing", async () => {
    apiGet.mockResolvedValue(payload([]));
    render(<ConnectedEquipment />);
    await waitFor(() =>
      expect(
        screen.getByText(/Currently shares nothing beyond its own sales record/),
      ).toBeTruthy(),
    );
  });

  it("states the categories no setting can ever reveal", async () => {
    // The commercial promise, on the screen rather than only in a document.
    apiGet.mockResolvedValue(payload([]));
    render(<ConnectedEquipment />);
    await waitFor(() =>
      expect(screen.getByText(/work orders, production quantities/)).toBeTruthy(),
    );
  });

  it("does not colour an UNKNOWN service state as a fault", async () => {
    // "unknown" means AMP was not told. Rendering it red manufactures a fleet
    // problem out of missing configuration.
    const data = payload([]);
    data.equipment[0].service = {
      state: "unknown",
      reason: "this machine has never reported operating hours",
      hours_remaining: null,
    };
    apiGet.mockResolvedValue(data);
    render(<ConnectedEquipment />);
    const badge = await screen.findByText("unknown");
    expect(badge.className).not.toMatch(/red/);
    // CONTROL: a genuinely overdue machine IS red, so the assertion above is
    // about the state and not about the component never using red.
    apiGet.mockResolvedValue(payload([]));
    const second = render(<ConnectedEquipment />);
    const overdue = await second.findByText("overdue");
    expect(overdue.className).toMatch(/red/);
  });
});

describe("changing what a manufacturer sees", () => {
  it("sends the ABSOLUTE grant list, so unticking withdraws", async () => {
    apiGet.mockResolvedValue(payload(["SHARE_OPERATING_HOURS", "SHARE_ALARMS"]));
    apiPut.mockResolvedValue({});
    render(<ConnectedEquipment />);

    const hours = await screen.findByRole("checkbox", { name: /Operating hours/ });
    fireEvent.click(hours);
    fireEvent.click(screen.getByRole("button", { name: /Save sharing/ }));

    await waitFor(() => expect(apiPut).toHaveBeenCalled());
    expect(apiPut.mock.calls[0][0]).toBe("/connected-equipment/sharing");
    // The remaining grant is still sent. A PATCH-style "removed: [...]" would
    // leave the server guessing what the absence of a key means.
    expect(apiPut.mock.calls[0][1]).toEqual({
      oem_code: "OEM_ALPHA",
      grants: ["SHARE_ALARMS"],
    });
  });

  it("withdraws everything as an empty list, not a special verb", async () => {
    apiGet.mockResolvedValue(payload(["SHARE_OPERATING_HOURS"]));
    apiPut.mockResolvedValue({});
    render(<ConnectedEquipment />);

    fireEvent.click(await screen.findByRole("button", { name: /Withdraw everything/ }));
    fireEvent.click(screen.getByRole("button", { name: /Save sharing/ }));

    await waitFor(() => expect(apiPut).toHaveBeenCalled());
    expect(apiPut.mock.calls[0][1]).toEqual({ oem_code: "OEM_ALPHA", grants: [] });
  });

  it("shows the backend's own reason when a change is refused", async () => {
    apiGet.mockResolvedValue(payload([]));
    apiPut.mockRejectedValue(
      new Error(JSON.stringify({ detail: "Unknown sharing grants: SHARE_EVERYTHING" })),
    );
    render(<ConnectedEquipment />);

    fireEvent.click(await screen.findByRole("checkbox", { name: /Alarm codes/ }));
    fireEvent.click(screen.getByRole("button", { name: /Save sharing/ }));

    const shown = await screen.findByText(/Unknown sharing grants: SHARE_EVERYTHING/);
    // And the DETAIL is extracted, not the raw body dumped on screen. Without
    // the parse the regex above still matches — the JSON string contains the
    // phrase — so the assertion that makes this test load-bearing is that the
    // JSON envelope is gone.
    expect(shown.textContent).toBe("Unknown sharing grants: SHARE_EVERYTHING");
  });

  it("gives a non-Admin no controls, and says why", async () => {
    role = "Supervisor";
    apiGet.mockResolvedValue(payload(["SHARE_ALARMS"]));
    render(<ConnectedEquipment />);

    await waitFor(() =>
      expect(screen.getByText(/Only an Admin can change what a manufacturer sees/))
        .toBeTruthy(),
    );
    expect(screen.queryByRole("button", { name: /Save sharing/ })).toBeNull();
    // Still readable, though. A supervisor is entitled to know what is shared.
    expect((screen.getByRole("checkbox", { name: /Alarm codes/ }) as HTMLInputElement)
      .disabled).toBe(true);
  });
});

describe("failure is not emptiness", () => {
  it("reports a failed load instead of 'no connected equipment'", async () => {
    // #383's bug, in a new panel: a dropped request rendering as a reassuring
    // empty state. "No manufacturer has equipment here" is a plausible answer an
    // administrator would accept, and it is the wrong one.
    apiGet.mockRejectedValue(new Error("network"));
    render(<ConnectedEquipment />);
    await waitFor(() => expect(screen.getByRole("alert")).toBeTruthy());
    expect(screen.queryByText(/No manufacturer-connected equipment/)).toBeNull();
  });

  it("shows the empty state only when the load actually succeeded", async () => {
    apiGet.mockResolvedValue({ equipment: [], manufacturers: [], available_grants: GRANTS });
    render(<ConnectedEquipment />);
    await waitFor(() =>
      expect(screen.getByText(/No manufacturer-connected equipment/)).toBeTruthy(),
    );
  });
});
