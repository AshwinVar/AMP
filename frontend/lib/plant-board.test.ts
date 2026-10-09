import { describe, expect, it } from "vitest";

import {
  HOUR_FILL,
  HOUR_LABEL,
  hasTarget,
  hourFromClick,
  hourLabel,
  dayOnlyMachines,
  missingLinks,
  plantHourly,
  powerMode,
  shiftLabel,
  type MachineProduction,
} from "./plant-board";

/**
 * The board's job is to turn a shot count into something a plant manager can
 * act on, WITHOUT inventing anything. These tests pin the places where the
 * tempting shortcut is wrong.
 */

function machine(over: Partial<MachineProduction> = {}): MachineProduction {
  return {
    machine_id: 1,
    machine: "IMM-01",
    part: "Ele clip",
    part_code: "ELE-CLIP",
    tool: "MLD-ELE",
    ideal_per_hour: 14400,
    average_per_hour: 600,
    total: 14400,
    hours_known: true,
    hourly_total: 14400,
    points: Array.from({ length: 24 }, (_, h) => ({
      hour: h,
      parts: h === 9 ? 14400 : 0,
      ideal: 14400,
      status: h === 9 ? "ok" : "low",
    })),
    ...over,
  };
}

describe("hour labels", () => {
  it("pads the hour so the axis does not jump width at 10:00", () => {
    expect(hourLabel(0)).toBe("00:00");
    expect(hourLabel(9)).toBe("09:00");
    expect(hourLabel(23)).toBe("23:00");
  });

  it("names a shift by the block it averages over", () => {
    expect(shiftLabel({ from_hour: 8, to_hour: 16 })).toBe("08:00–16:00");
  });

  it("wraps the last block's end to midnight rather than printing 24:00", () => {
    expect(shiftLabel({ from_hour: 16, to_hour: 24 })).toBe("16:00–00:00");
  });
});

describe("the three hour states stay distinguishable", () => {
  it("gives an unrated hour its own colour, never the passing one", () => {
    expect(HOUR_FILL.unrated).not.toBe(HOUR_FILL.ok);
    expect(HOUR_FILL.unrated).not.toBe(HOUR_FILL.low);
  });

  it("explains each state in words, for a tooltip and a legend", () => {
    expect(HOUR_LABEL.unrated).toMatch(/no target/i);
    expect(HOUR_LABEL.ok).toMatch(/target/i);
    expect(HOUR_LABEL.low).toMatch(/below/i);
  });
});

describe("a target has to exist before anything is compared to it", () => {
  it("is present for the customer's own part", () => {
    expect(hasTarget(machine())).toBe(true);
  });

  it("is absent with no declared cycle time, so no target line is drawn", () => {
    // Drawing a target of zero would paint every hour, including an idle one,
    // as having cleared its target.
    expect(hasTarget(machine({ ideal_per_hour: 0 }))).toBe(false);
  });
});

describe("what a machine is missing, in the order somebody would fix it", () => {
  it("says nothing when the chain is complete", () => {
    expect(missingLinks(machine(), true)).toEqual([]);
  });

  it("names the mould first, and does not also complain about the part", () => {
    const gaps = missingLinks(machine({ tool: null, part: null, ideal_per_hour: 0 }), false);
    expect(gaps).toHaveLength(1);
    expect(gaps[0]).toMatch(/no mould assigned/i);
  });

  it("names the part when a mould is fitted but says nothing about what it makes", () => {
    const gaps = missingLinks(machine({ part: null, ideal_per_hour: 0 }), false);
    expect(gaps).toEqual(["the fitted mould does not say which part it makes"]);
  });

  it("asks for a cycle time when the part exists but carries no target", () => {
    const gaps = missingLinks(machine({ ideal_per_hour: 0 }), true);
    expect(gaps).toEqual(["the part has no cycle time, so there is no hourly target"]);
  });

  it("reports an unpriced part separately, because counting still works", () => {
    const gaps = missingLinks(machine(), false);
    expect(gaps).toEqual(["the part has no price, so no rupee figure is shown"]);
  });
});

describe("the plant-wide hourly bar", () => {
  const a = machine({ machine_id: 1, machine: "IMM-01" });
  const b = machine({ machine_id: 2, machine: "IMM-02" });

  it("adds the machines together rather than averaging them", () => {
    const hours = plantHourly([a, b]);
    expect(hours[9].parts).toBe(28800);
    expect(hours[9].ideal).toBe(28800);
  });

  it("keeps all 24 hours, so an idle night is visible as idle", () => {
    expect(plantHourly([a, b])).toHaveLength(24);
    expect(plantHourly([a, b])[3].parts).toBe(0);
  });

  it("leaves an unspecified machine out of the plant TARGET but counts its parts", () => {
    // IMM-02 makes parts nobody has specified. Its output is real and is
    // counted; adding a target it does not have would measure the plant against
    // a number nobody set, and dropping its parts would lose real production.
    const unspecified = machine({
      machine_id: 2,
      machine: "IMM-02",
      ideal_per_hour: 0,
      points: Array.from({ length: 24 }, (_, h) => ({
        hour: h,
        parts: h === 9 ? 5000 : 0,
        ideal: 0,
        status: "unrated" as const,
      })),
    });
    const hours = plantHourly([a, unspecified]);
    expect(hours[9].parts).toBe(19400);
    expect(hours[9].ideal).toBe(14400);
  });

  it("returns nothing at all when there are no machines, rather than 24 zeros", () => {
    expect(plantHourly([])).toEqual([]);
  });
});

describe("which hour a chart click means", () => {
  it("reads the hour off the clicked datum", () => {
    expect(hourFromClick({ hour: 12, parts: 7920 })).toBe(12);
  });

  it("...including when recharts wraps it in a payload", () => {
    expect(hourFromClick({ payload: { hour: 17, parts: 100 } })).toBe(17);
  });

  it("keeps midnight, which is a real hour", () => {
    expect(hourFromClick({ hour: 0, parts: 0 })).toBe(0);
  });

  it("returns null for a click that resolved to nothing", () => {
    // The original bug: these all went through Number(), and Number(null) is 0,
    // so a missed click drilled into midnight and showed a plant that made
    // nothing. Null has to mean null.
    expect(hourFromClick(null)).toBeNull();
    expect(hourFromClick(undefined)).toBeNull();
    expect(hourFromClick({})).toBeNull();
    expect(hourFromClick({ hour: null })).toBeNull();
    expect(hourFromClick({ hour: "12" })).toBeNull();
  });

  it("refuses an hour outside the day", () => {
    expect(hourFromClick({ hour: 24 })).toBeNull();
    expect(hourFromClick({ hour: -1 })).toBeNull();
    expect(hourFromClick({ hour: 9.5 })).toBeNull();
  });
});

describe("powerMode", () => {
  // THE DEFECT: two states where there are three. A meter that reports a day's
  // total and no hours is not "no meter" and is not "24 bars" — rendered as the
  // first it denies a measurement AMP is holding; rendered as the second it
  // stacks a whole day onto midnight.
  const unavailable = {
    available: false as const,
    reason: "no meter",
    fix: "fit one",
    points: [] as [],
  };
  const hourly = {
    available: true as const,
    unit: "kWh",
    total: 48.9,
    points: [{ hour: 0, kwh: 3.4 }],
    hours_known: true,
  };

  it("draws nothing when nothing measured", () => {
    expect(powerMode(unavailable)).toBe("unavailable");
  });

  it("draws the hours when the hours are known", () => {
    expect(powerMode(hourly)).toBe("hourly");
  });

  it("draws the total, not the hours, when only the day was measured", () => {
    expect(powerMode({ ...hourly, hours_known: false })).toBe("day-only");
    expect(powerMode({ ...hourly, points: null })).toBe("day-only");
  });

  it("never calls a measured day unavailable", () => {
    // The direction that matters: a moulder told his machine has no meter
    // while AMP holds the kilowatt-hours that machine measured.
    expect(powerMode({ ...hourly, hours_known: false })).not.toBe("unavailable");
  });
});

describe("dayOnlyMachines", () => {
  const press = (over: Partial<MachineProduction>): MachineProduction => ({
    machine_id: 1,
    machine: "IMM-01",
    part: null,
    part_code: null,
    tool: null,
    ideal_per_hour: 0,
    average_per_hour: 0,
    total: 0,
    hours_known: true,
    hourly_total: 0,
    points: [],
    ...over,
  });

  it("names a machine whose day is real but whose hours are not", () => {
    expect(
      dayOnlyMachines([press({ machine: "IMM-02", total: 5050, hours_known: false })]),
    ).toEqual(["IMM-02"]);
  });

  it("says nothing about a machine that reports hours", () => {
    expect(
      dayOnlyMachines([press({ machine: "IMM-03", total: 2228, hourly_total: 2228 })]),
    ).toEqual([]);
  });

  it("says nothing about a machine that made nothing", () => {
    // A press that stood idle has no hours either, but naming it would read as
    // "data is missing" when the truth is simply that it did not run.
    expect(dayOnlyMachines([press({ total: 0, hours_known: false })])).toEqual([]);
  });

  it("names every such machine, in the order they are given", () => {
    expect(
      dayOnlyMachines([
        press({ machine: "IMM-01", total: 2456, hours_known: false }),
        press({ machine: "IMM-03", total: 2228 }),
        press({ machine: "IMM-06", total: 2131, hours_known: false }),
      ]),
    ).toEqual(["IMM-01", "IMM-06"]);
  });
});
