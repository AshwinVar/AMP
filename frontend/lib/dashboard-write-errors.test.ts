import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

/**
 * Rot-proofing for the write-failure rollout.
 *
 * 69 handlers on the dashboard were changed from swallowing a failed write to
 * reporting it. Nothing stops the next handler being written the old way, and
 * the old way is invisible - it looks fine in review and in the browser, and
 * only shows up when someone's save silently does nothing.
 *
 * Same idea as the backend's csv_safe / payload_fields guards: funnel a class
 * of operation through one place, then assert nothing bypasses it.
 */

const SOURCE = readFileSync(join(__dirname, "..", "app", "dashboard", "page.tsx"), "utf8");

describe("dashboard write failures", () => {
  it("routes every write failure through report(), not the console", () => {
    const swallowed = SOURCE.split("\n")
      .map((line, i) => ({ line: line.trim(), number: i + 1 }))
      .filter(({ line }) => line.includes("console.error"));

    // fetchAll is the exception, and a deliberate one: it is a read, re-run by
    // the 3s poll, so surfacing it here would flap a banner on every blip. List
    // loads have their own path (useLoadError, #383).
    expect(swallowed).toHaveLength(1);
  });

  it("does not tell the user about a failure through alert()", () => {
    // The 27 handlers that did report a failure used a blocking alert() with a
    // fixed string - which threw away the field-level message the backend had
    // just gone to the trouble of returning (#382).
    expect(SOURCE).not.toContain('alert("Failed to');
  });

  it("keeps a label on every report call, so the banner names the action", () => {
    const calls = SOURCE.match(/report\(error,\s*[^)]*\)/g) ?? [];

    expect(calls.length).toBeGreaterThan(60);
    for (const call of calls) {
      // report(error) with no label would render "Could not . Please try again."
      expect(call).toMatch(/report\(error,\s*"[a-z][a-zA-Z ]+"\)/);
    }
  });

  it("renders the banner it reports into", () => {
    // A report with nowhere to render is the same silence in a new costume.
    expect(SOURCE).toContain("<ActionError");
  });
});
