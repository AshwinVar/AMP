import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * The Industrial Connectivity screen must not present an invented number as a
 * measurement.
 *
 * AMP opens no connection to a PLC in any protocol — no driver library is a
 * dependency, `ProtocolAdapter.read` raises NotImplementedError, and
 * `get_adapter()` returns the simulator for every device. The backend now
 * refuses to simulate anything but its own seeded demo fleet
 * (test_no_invented_plc_readings.py), and this screen has to say which is which.
 *
 * What it used to do, for a device an engineer registered for a real compressor
 * PLC at a real IP: post `status: "Online"`, print "signals will start flowing",
 * paint a green Online badge, show "Awaiting first poll…", and then render
 * `random.randint()` values in the same green as everything else. The one
 * disclaimer sat under the protocol grid, nowhere near the number.
 */
const apiGet = vi.fn();
const apiPost = vi.fn();

vi.mock("../lib/api", () => ({
  apiGet: (p: string) => apiGet(p),
  apiPost: (p: string, b: unknown) => apiPost(p, b),
}));

import IndustrialConnectivity from "./IndustrialConnectivity";

const PROTOCOLS = [
  { key: "modbus", name: "Modbus TCP", port: 502, library: "pymodbus",
    transport: "TCP", desc: "Register-based fieldbus." },
];

function device(over: Record<string, unknown> = {}) {
  return {
    id: 1, device_code: "COMP-01", device_name: "Customer compressor",
    device_type: "PLC", protocol: "Modbus TCP", ip_address: "192.168.10.22:502",
    status: "Registered", linked_machine_id: null, simulated: false,
    ...over,
  };
}

function signal(over: Record<string, unknown> = {}) {
  return {
    id: 1, device_id: 1, signal_name: "pressure", signal_value: "7",
    numeric_value: 7, unit: "bar", quality: "Good", source_protocol: "Modbus TCP",
    created_at: "2026-09-12T10:00:00Z", ...over,
  };
}

function mount(devices: unknown[], signals: unknown[] = []) {
  apiGet.mockImplementation((path: string) => {
    if (path === "/industrial/protocols") return Promise.resolve(PROTOCOLS);
    if (path === "/industrial/devices") return Promise.resolve(devices);
    if (path === "/industrial/signals") return Promise.resolve(signals);
    return Promise.resolve([]);
  });
  return render(<IndustrialConnectivity />);
}

beforeEach(() => {
  apiGet.mockReset();
  apiPost.mockReset();
});
afterEach(cleanup);

describe("IndustrialConnectivity", () => {
  it("does not claim a registered device is Online", async () => {
    mount([device()]);
    const badge = await screen.findByText("Registered");
    // Green is a claim about a connection AMP has never made. Red would claim
    // the PLC is down, which is equally unfounded.
    expect(badge.className).not.toContain("green");
    expect(badge.className).not.toContain("red");
  });

  it("says no data has arrived, rather than that a poll is pending", async () => {
    mount([device()], []);
    await screen.findByText(/No data yet/);
    expect(screen.queryByText(/Awaiting first poll/)).toBeNull();
  });

  it("labels a simulated demo device, and does not label a registered one", async () => {
    mount([device({ id: 2, device_code: "PLC-MODBUS-01", simulated: true })]);
    await screen.findByText("Simulated demo device");

    cleanup();
    mount([device()]);
    await screen.findByText("Registered");
    expect(screen.queryByText("Simulated demo device")).toBeNull();
  });

  it("renders a simulated reading in a different colour from a real one", async () => {
    mount([device({ id: 3, device_code: "PLC-MODBUS-01", simulated: true })],
          [signal({ device_id: 3 })]);
    const simulated = (await screen.findByText("7")).className;

    cleanup();
    mount([device()], [signal({ device_id: 1 })]);
    const real = (await screen.findByText("7")).className;

    expect(simulated).not.toBe(real);
    expect(simulated).toContain("amber");
    expect(real).toContain("green");
  });

  it("does not post a status when registering a device", async () => {
    apiPost.mockResolvedValue({});
    mount([]);
    await screen.findByText(/No devices yet/);

    const form = document.querySelector("form")!;
    const [code, name] = Array.from(form.querySelectorAll("input"));
    const set = (el: HTMLInputElement, v: string) => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!
        .set!.call(el, v);
      el.dispatchEvent(new Event("input", { bubbles: true }));
    };
    set(code as HTMLInputElement, "COMP-01");
    set(name as HTMLInputElement, "Customer compressor");
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));

    await waitFor(() => expect(apiPost).toHaveBeenCalled());
    const body = apiPost.mock.calls[0][1] as Record<string, unknown>;
    // Asserting the absence of a key, because sending "Online" is what made the
    // backend simulate the device in the first place.
    expect(body).not.toHaveProperty("status");
    expect(body.device_code).toBe("COMP-01");
  });

  it("does not promise signals will start flowing", async () => {
    apiPost.mockResolvedValue({});
    mount([]);
    await screen.findByText(/No devices yet/);
    const form = document.querySelector("form")!;
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));

    const message = await screen.findByText(/Device registered/);
    expect(message.textContent).toMatch(/does not poll PLCs/);
    expect(message.textContent).not.toMatch(/start flowing/);
  });

  it("describes the protocol grid as a catalogue, not a driver list", async () => {
    mount([]);
    await screen.findByText("Modbus TCP", { selector: "span" });
    expect(screen.getByText(/A catalogue, not a driver list/)).toBeTruthy();
  });
});
