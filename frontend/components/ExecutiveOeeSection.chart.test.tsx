import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import ExecutiveOeeSection from "./ExecutiveOeeSection";
import type { ExecutiveOee } from "../lib/phase15-types";

/**
 * The "Machine OEE Ranking" chart is a ranking of MEASUREMENTS.
 *
 * A machine that produced nothing in the window has no OEE (the backend now
 * sends `oee: null, measured: false`). It belongs in the table, where it is
 * listed as "No production", but not on a chart titled a ranking — and before
 * the backend fix it sat at the TOP of that chart on an invented 68%.
 *
 * Recharts draws nothing in jsdom, so the ordinary render test cannot see which
 * rows reach the chart; removing the filter survived it (mutation MF4). This file
 * replaces BarChart with a stub that records the `data` it was given, which
 * asserts the behaviour rather than the source text.
 */

// vi.hoisted: vi.mock is lifted above every declaration, so a plain `const` here
// would be read by the mock factory before it exists.
const seen = vi.hoisted(() => [] as { data?: unknown[] }[]);

vi.mock("recharts", async () => {
  const actual = await vi.importActual<typeof import("recharts")>("recharts");
  return {
    ...actual,
    ResponsiveContainer: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
    BarChart: (props: { data?: unknown[]; children?: React.ReactNode }) => {
      seen.push({ data: props.data });
      return <div data-testid="bar-chart" />;
    },
  };
});

// vi.mock is hoisted above the static import, so ExecutiveOeeSection receives the stub.
afterEach(() => {
  cleanup();
  seen.length = 0;
});

function payload(over: Partial<ExecutiveOee> = {}): ExecutiveOee {
  return {
    plant_availability: 83, plant_performance: 75, plant_quality: 95, plant_oee: 59,
    has_data: true, machine_ranking: [], downtime_pareto: [], shift_oee: [],
    quality_trend: [], production_target: 0, production_actual: 0,
    production_achievement: 0, running_machines: 0, breakdown_machines: 0,
    offline_machines: 0, ...over,
  };
}

describe("ExecutiveOeeSection — the OEE ranking chart", () => {
  it("plots measured machines only, never a machine with no production", () => {
    render(<ExecutiveOeeSection data={payload({
      machine_ranking: [
        { machine_id: 1, machine_name: "A-MEASURED", status: "Running", availability: 83,
          performance: 75, quality: 95, oee: 59, measured: true, downtime_minutes: 0,
          total_count: 600, good_count: 570, rejected_count: 30, utilization: 90 },
        { machine_id: 2, machine_name: "B-IDLE", status: "Running", availability: null,
          performance: null, quality: null, oee: null, measured: false, downtime_minutes: 0,
          total_count: 0, good_count: 0, rejected_count: 0, utilization: 80 },
      ],
    })} />);

    // The first BarChart on the section is the machine ranking.
    const ranking = seen[0]?.data as { machine_name: string }[] | undefined;
    expect(ranking?.map((r) => r.machine_name)).toEqual(["A-MEASURED"]);
  });
});
