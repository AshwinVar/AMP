import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

/**
 * The digital twin's loss heat map and the "OEE in money" downtime tile, with and
 * without the tenant's unit value (ADR-0010).
 *
 * The twin printed "Cost: $…" (a currency the product does not use) from a figure
 * priced at a fixed £12 a minute and £25 a unit. The money tile read
 * `gbp(estimated_loss_value ?? 0)` whenever a rate was set, so downtime with no run
 * time to convert showed a confident £0; and without a rate the backend used to
 * send £8 a minute there instead of nothing.
 */

const apiGet = vi.fn();

vi.mock("../lib/api", () => ({
  apiGet: (p: string) => apiGet(p),
  apiPatch: vi.fn(),
}));

import DigitalTwinSection from "./DigitalTwinSection";
import MoneyStorySnapshot from "./MoneyStorySnapshot";
import { CURRENCY } from "../lib/money";

const noop = () => {};

function twin(metrics: unknown[]) {
  apiGet.mockImplementation((p: string) =>
    Promise.resolve(p === "/twin-overlay" ? { machines: metrics } : {}));
  render(
    <DigitalTwinSection
      machines={[
        { id: 1, name: "PRESS-01", status: "Running", utilization: 80, downtime: "0 min" },
        { id: 2, name: "CNC-02", status: "Running", utilization: 80, downtime: "0 min" },
      ]}
      nodes={[
        { id: 10, machine_id: 1, node_name: "PRESS-01", node_type: "machine", x_position: 10, y_position: 10, width: 120, height: 80, zone: "SMT" },
        { id: 11, machine_id: 2, node_name: "CNC-02", node_type: "machine", x_position: 200, y_position: 10, width: 120, height: 80, zone: "SMT" },
      ]}
      commandCenter={null}
      form={{ machine_id: "", node_name: "", node_type: "machine", x_position: 0, y_position: 0, width: 120, height: 80, zone: "" }}
      setForm={noop}
      createNode={noop}
      updateNode={noop}
      autoGenerateLayout={noop}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "Losses" }));
}

beforeEach(() => {
  apiGet.mockReset();
});

describe("DigitalTwinSection loss overlay", () => {
  it("labels a machine's loss in good units, never a currency, without a unit value", async () => {
    twin([{ machine_id: 1, oee: 70, cost: null, lost_units: 18 }]);
    expect(await screen.findByText("Lost: 18 units")).toBeTruthy();
    // CNC-02 has no loss row this week: nothing measured, not a zero
    expect(screen.getByText("Lost: —")).toBeTruthy();
    expect(document.body.textContent).not.toContain("$");
    expect(document.body.textContent).not.toContain(CURRENCY);
  });

  it("labels it in the platform currency once priced", async () => {
    twin([{ machine_id: 1, oee: 70, cost: 225, lost_units: 18 }]);
    expect(await screen.findByText(`Lost: ${CURRENCY}225`)).toBeTruthy();
    expect(document.body.textContent).not.toContain("$");
  });
});

const RECOVERY = {
  has_data: true, oee: 70, world_class: 85, gap_points: 15, unit_value_gbp: null,
  recoverable_units_per_year: 1000, recoverable_value_per_year: null, oee_trend: "new",
  oee_points_delta: null, biggest_lever: null, lever_label: null,
  lever_recoverable_value_per_year: null, lever_recoverable_units_per_year: 0,
};

function moneyStory(rate: number | null, mgmt: Record<string, unknown>) {
  apiGet.mockImplementation((p: string) =>
    // With a rate the recovery read-model prices its upside too (1000 units x rate).
    Promise.resolve(p === "/recovery-summary"
      ? { ...RECOVERY, unit_value_gbp: rate, recoverable_value_per_year: rate == null ? null : 1000 * rate }
      : mgmt));
  return render(<MoneyStorySnapshot />);
}

describe("MoneyStorySnapshot downtime loss", () => {
  it("shows good units without a unit value", async () => {
    moneyStory(null, { total_downtime_minutes: 40, estimated_loss_units: 8, estimated_loss_value: null, unit_value_gbp: null });
    expect(await screen.findByText("8 units")).toBeTruthy();
  });

  it("shows unknown, not £0, when the downtime could not be converted", async () => {
    moneyStory(4.5, { total_downtime_minutes: 480, estimated_loss_units: null, estimated_loss_value: null, unit_value_gbp: 4.5 });
    expect(await screen.findByText("480 min of downtime, with no run time to convert into units")).toBeTruthy();
    expect(screen.queryByText(`${CURRENCY}0`)).toBeNull();
    expect(screen.getByText("—")).toBeTruthy();
  });

  it("shows the backend's £ when priced", async () => {
    moneyStory(4.5, { total_downtime_minutes: 40, estimated_loss_units: 8, estimated_loss_value: 36, unit_value_gbp: 4.5 });
    expect(await screen.findByText(`${CURRENCY}36`)).toBeTruthy();
  });
});
