import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import ExecutiveOeeSection from "./ExecutiveOeeSection";
import type { ExecutiveOee } from "../lib/phase15-types";

/**
 * A week the plant never ran is not a week it scored 0%.
 *
 * #556 and #558 closed this on the backend. `oee_contract.is_measurable` is the
 * one rule for "was there anything to measure", all four call sites use it, and
 * `/analytics/executive-oee` publishes the answer as `has_data` with the reason
 * written beside it in analytics_routes.py:963:
 *
 *     # See /analytics/summary: 0% and "did not run" are different answers.
 *
 * The screen never asked. `ExecutiveOeeSection` rendered
 * `${data?.plant_oee ?? 0}%`, so a shutdown week, a bank holiday, or a tenant on
 * their first morning read **Plant OEE 0%** — in red, because `oeeStyle`
 * colours it as a catastrophe. The API could tell the two apart; the dashboard
 * could not, and 0% is not a neutral placeholder. It is a specific, alarming
 * claim about a week that did not happen, on the card an owner shows their
 * management team.
 *
 * `has_data` was not even declared on the `ExecutiveOee` type, so no consumer
 * could have used it: the last mile of the fix was missing rather than wrong.
 *
 * Only the four POOLED plant figures are governed by this flag. Target, Actual,
 * Breakdowns and the per-machine rows answer different questions with their own
 * data presence, and are deliberately untouched — Achievement in particular is
 * actual-over-target, so its 0% can be perfectly true and alarming on purpose.
 *
 * "Not loaded" and "not run" are also different answers, so a null payload (the
 * first poll, and every failed one) renders "—" rather than either.
 */

function payload(over: Partial<ExecutiveOee> = {}): ExecutiveOee {
  return {
    plant_availability: 0,
    plant_performance: 0,
    plant_quality: 0,
    plant_oee: 0,
    has_data: true,
    machine_ranking: [],
    downtime_pareto: [],
    shift_oee: [],
    quality_trend: [],
    production_target: 0,
    production_actual: 0,
    production_achievement: 0,
    running_machines: 0,
    breakdown_machines: 0,
    offline_machines: 0,
    ...over,
  };
}

describe("ExecutiveOeeSection — an unrun week", () => {
  it("does not report 0% OEE for a week with nothing to measure", () => {
    render(<ExecutiveOeeSection data={payload({ has_data: false })} />);
    // The four pooled figures say so in words rather than claiming a number.
    expect(screen.getAllByText("Not run").length).toBe(4);
    // Achievement is actual-over-target and keeps its own answer: it is NOT
    // governed by the OEE flag, and blanking it would be a second dishonesty.
    expect(screen.getAllByText("0%").length).toBe(1);
  });

  it("does not colour an unmeasured week as a catastrophe", () => {
    // Text alone is not the whole claim. `oeeStyle` paints anything under 65
    // red, so "Not run" inside a red card still shouts that something went
    // badly wrong in a week that did not happen. Mutation testing caught this:
    // reverting `highlight` to `?? 0` passed every text assertion above.
    render(<ExecutiveOeeSection data={payload({ has_data: false })} />);
    const card = screen.getByText("Plant OEE").closest("div");
    expect(card?.className).not.toContain("red");
    expect(card?.className).toContain("border-slate-800");
  });

  it("...but a real, measured 0% IS a catastrophe and stays red", () => {
    // The control for the test above. Dropping the colour unconditionally would
    // pass it while destroying the signal that matters most.
    render(<ExecutiveOeeSection data={payload({ has_data: true, plant_oee: 0 })} />);
    const card = screen.getByText("Plant OEE").closest("div");
    expect(card?.className).toContain("red");
  });

  it("still reports a genuine 0% when the plant DID run and scored nothing", () => {
    // The other half, and the reason this cannot just hide the card: a plant
    // that ran and produced nothing usable has a real, terrible OEE, and that
    // is exactly when the number matters most.
    render(<ExecutiveOeeSection data={payload({ has_data: true })} />);
    // All four pooled figures plus Achievement.
    expect(screen.getAllByText("0%").length).toBe(5);
    expect(screen.queryAllByText("Not run").length).toBe(0);
  });

  it("renders real figures unchanged", () => {
    render(
      <ExecutiveOeeSection
        data={payload({
          has_data: true,
          plant_oee: 58,
          plant_availability: 71,
          plant_performance: 90,
          plant_quality: 91,
        })}
      />,
    );
    expect(screen.getByText("58%")).toBeTruthy();
    expect(screen.getByText("71%")).toBeTruthy();
    expect(screen.queryAllByText("Not run").length).toBe(0);
  });

  it("leaves counts that are not OEE alone when there is no data", () => {
    // Target/Actual/Breakdowns are counts, not pooled ratios. Zero of them is a
    // fact about the week, not an unmeasured quantity, so blanking them would
    // trade one dishonesty for another.
    render(
      <ExecutiveOeeSection
        data={payload({ has_data: false, production_target: 0, breakdown_machines: 3 })}
      />,
    );
    expect(screen.getByText("3")).toBeTruthy();
    expect(screen.getAllByText("Not run").length).toBe(4);
  });

  it("treats a missing payload as nothing to measure, not as zero", () => {
    // The first poll, and every failed one. `data` is null until it resolves,
    // and `?? 0` printed 0% for that window too.
    render(<ExecutiveOeeSection data={null} />);
    // Not "Not run" either — nothing has been asked yet, let alone answered.
    expect(screen.getAllByText("—").length).toBe(4);
    expect(screen.queryAllByText("Not run").length).toBe(0);
  });
});
