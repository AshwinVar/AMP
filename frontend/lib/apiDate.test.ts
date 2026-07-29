import { describe, expect, it } from "vitest";

import { normalizeApiDateString, parseApiDate } from "./apiDate";

/**
 * A note on what actually guards this file.
 *
 * On a machine whose clock is UTC, the bug and the fix are INDISTINGUISHABLE:
 * "local time" and "UTC" are the same thing, so every Date-level assertion below
 * passes just as happily against plain `new Date(iso)`. CI runners are UTC. That
 * is the same blind spot that let #412 through, and it means the Date-level
 * tests here are worth little on their own.
 *
 * So the load-bearing assertions are the string-level ones on
 * `normalizeApiDateString`: "does a naive timestamp come back with a Z on it"
 * is a question about a string, and its answer does not move with the clock.
 * Those fail on any runner, in any zone, the moment the normalisation is
 * dropped. The Date-level tests are kept as documentation of the consequence,
 * and they do discriminate when run off-UTC (they were mutation-verified in
 * BST, +1).
 */

describe("what the platform does, and why this module exists", () => {
  it("parses the API's two shapes in opposite directions", () => {
    // ECMA-262 Date Time String Format: a date-only form is UTC, a date-time
    // form without an offset is local. The backend sends both, zoneless.
    expect(new Date("2026-07-29").toISOString()).toBe("2026-07-29T00:00:00.000Z");

    // ...whereas the date-time form lands at local midnight, which is the UTC
    // instant shifted by exactly the viewer's offset. On a UTC box the shift is
    // zero and this reads as an identity - that is the point being made.
    const asLocal = new Date("2026-07-29T00:00:00");
    expect(asLocal.getTime() - Date.UTC(2026, 6, 29)).toBe(asLocal.getTimezoneOffset() * 60_000);
  });
});

describe("normalizeApiDateString", () => {
  it("marks a naive backend timestamp as UTC", () => {
    // THE BUG, in one line. Without the Z the viewer's clock decides what
    // instant this is, and every viewer outside UTC decides wrong.
    expect(normalizeApiDateString("2026-07-29T05:00:00")).toBe("2026-07-29T05:00:00Z");
  });

  it("handles the microseconds Python actually emits", () => {
    // datetime.utcnow().isoformat() -> '2026-07-29T01:15:03.532941'
    expect(normalizeApiDateString("2026-07-29T01:15:03.532941")).toBe(
      "2026-07-29T01:15:03.532941Z",
    );
  });

  it("anchors a calendar day to the viewer's own midnight, not UTC's", () => {
    // A date.isoformat() names a day on a wall calendar. Left alone it parses as
    // UTC midnight and renders as the day BEFORE for everyone west of Greenwich.
    expect(normalizeApiDateString("2026-07-29")).toBe("2026-07-29T00:00:00");
  });

  it("leaves a Z-suffixed string alone", () => {
    // MachineTimeline reads created_at from the API (naive) and from live events
    // the dashboard stamps itself with new Date().toISOString() (Z-suffixed).
    // Same field, two shapes - appending a second Z would break the live half.
    expect(normalizeApiDateString("2026-07-29T05:00:00.000Z")).toBe("2026-07-29T05:00:00.000Z");
  });

  it("leaves a real offset alone, in either notation", () => {
    expect(normalizeApiDateString("2026-07-29T05:00:00+05:30")).toBe("2026-07-29T05:00:00+05:30");
    expect(normalizeApiDateString("2026-07-29T05:00:00-05:00")).toBe("2026-07-29T05:00:00-05:00");
    expect(normalizeApiDateString("2026-07-29T05:00:00+0530")).toBe("2026-07-29T05:00:00+0530");
  });

  it("does not mistake the date's own hyphens for an offset", () => {
    // "-07-29" reads like an offset at a glance. It is not one, and it turns out
    // it cannot be mistaken for one even by an unanchored pattern: a zone needs
    // four digits after the sign, and "-07" is followed by "-29", "-29" by "T0".
    // Mutation testing established that - un-anchoring HAS_ZONE breaks nothing -
    // so this pins the behaviour without pretending to guard the anchor.
    expect(normalizeApiDateString("2026-07-29T05:00:00")).toMatch(/Z$/);
    expect(normalizeApiDateString("2026-11-30T23:59:59")).toBe("2026-11-30T23:59:59Z");
  });

  it("does not mistake a seconds field for an offset", () => {
    // ":00:00" is the other near-miss - it needs a leading + or - to be a zone.
    expect(normalizeApiDateString("2026-07-29T00:00")).toBe("2026-07-29T00:00Z");
  });

  it("tolerates whitespace and leaves an empty string empty", () => {
    expect(normalizeApiDateString("  2026-07-29T05:00:00  ")).toBe("2026-07-29T05:00:00Z");
    expect(normalizeApiDateString("")).toBe("");
    expect(normalizeApiDateString("   ")).toBe("");
  });
});

describe("parseApiDate", () => {
  it("recovers the exact instant the backend recorded", () => {
    // Round-trip through the wire format: take a known UTC instant, serialise it
    // the way the backend does (isoformat, no zone), and parse it back.
    const instant = Date.UTC(2026, 6, 29, 5, 0, 0);
    const wire = new Date(instant).toISOString().replace("Z", "");
    expect(parseApiDate(wire)?.getTime()).toBe(instant);
  });

  it("keeps a calendar day on its own date in the viewer's zone", () => {
    // 2026-07-29 must read as the 29th for everyone. getFullYear/Month/Date are
    // the viewer's own calendar fields, so this asserts what a user sees.
    const d = parseApiDate("2026-07-29");
    expect(d?.getFullYear()).toBe(2026);
    expect(d?.getMonth()).toBe(6); // July
    expect(d?.getDate()).toBe(29);
  });

  it("reports a fresh event as fresh", () => {
    // MissionControl's timeAgo did (Date.now() - parsed). Misparsed, a one-minute
    // -old event read as the UTC offset old - "5h ago" in IST - and west of
    // Greenwich went negative, where Math.max(0, ...) pinned it at "0s ago".
    const oneMinuteAgo = new Date(Date.now() - 60_000).toISOString().replace("Z", "");
    const elapsedMs = Date.now() - (parseApiDate(oneMinuteAgo)?.getTime() ?? 0);
    expect(elapsedMs).toBeGreaterThanOrEqual(59_000);
    expect(elapsedMs).toBeLessThan(120_000);
  });

  it("returns null for nothing rather than an Invalid Date", () => {
    // Callers render "—" on null. An Invalid Date would print "Invalid Date".
    expect(parseApiDate(null)).toBeNull();
    expect(parseApiDate(undefined)).toBeNull();
    expect(parseApiDate("")).toBeNull();
  });

  it("returns null for a string that is not a date", () => {
    expect(parseApiDate("not a date")).toBeNull();
    expect(parseApiDate("2026-13-45T99:99:99")).toBeNull();
  });
});
