import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Unit tests for the shared hooks in lib/. Deliberately narrow: this is not a
// component-rendering harness, it is a place to pin the behaviour of logic that
// was previously only provable by driving a browser by hand (see #383, #385).
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    include: ["lib/**/*.test.{ts,tsx}"],
    globals: true,

    coverage: {
      provider: "v8",
      reporter: ["text", "text-summary", "html"],

      // SCOPED TO lib/, AND THE REASON MATTERS — read docs/TESTING.md before
      // quoting this number anywhere.
      //
      // lib/ is what this suite tests, so lib/ is what it can honestly gate.
      // Point the same run at components/ and app/ and it reads 4.5% branch
      // coverage, because 95 components and a 2,900-line dashboard have no
      // unit tests at all — they are exercised by the Playwright suite in e2e/,
      // which drives a real browser and produces no vitest coverage data.
      //
      // So this figure is "how well is the shared logic tested", NOT "how well
      // is the frontend tested". Widening `include` to make the number look
      // comprehensive would make it dishonest instead: it would still be
      // measuring only what vitest ran.
      include: ["lib/**"],
      exclude: ["lib/**/*.test.{ts,tsx}"],

      // NOTE: no `all: true`. That option was REMOVED in Vitest 4 — reporting
      // every file matched by `include`, not merely the ones a test imported,
      // is now the default. Setting it here is a type error, not a no-op.
      //
      // The behaviour still matters and is what surfaced this work: the first
      // run reported lib/utils.ts at 0%/0%/0%/0% rather than omitting it, which
      // is how a module that no test had ever imported became visible.
      //
      // Branch coverage is the number the gate uses. Statement coverage marks
      // `if (!res.ok)` covered the moment the happy path walks past it; branch
      // coverage does not, and the error paths are the whole reason these
      // modules have tests (#383 empty-state-on-error, #393 session expiry).
      //
      // Derived from a measurement, with the same ~3 points of slack the
      // backend floor uses — a floor at the current number turns the next merge
      // red for a reason unrelated to the change in it.
      //
      //   measured   92.59 branches / 96.10 statements / 98.91 functions / 97.98 lines
      //   floor      89       / 93         / 96         / 95
      //
      // The first measurement here was 74.89% branches, and the floor was going
      // to be 72. Then the modules that were actually uncovered got tests —
      // lib/utils.ts had never been imported by a test at all — so the floor
      // was raised to match rather than left where the ratchet started. That is
      // the intended ritual. Never lower it to make a build pass.
      thresholds: {
        branches: 89,
        statements: 93,
        functions: 96,
        lines: 95,
      },
    },
  },
});
