/**
 * How much of the plant a plant OEE measured (backend `oee_contract.coverage`).
 *
 * A machine that stops reporting leaves the pooled figure, so the metric
 * improves exactly when visibility is lost (OEE contract s4). Every screen that
 * shows a plant OEE says "from N of M machines" when the figure is partial.
 *
 * ONE wording, mirrored from the backend's `oee_contract.coverage_phrase`, so the
 * tiles read exactly as the briefing, the copilot and the weekly report do: ""
 * when every machine reported, or there is no machine to speak of.
 */
export type Coverage = {
  machines_expected: number;
  machines_reporting: number;
  coverage_pct: number | null;
  complete: boolean;
};

export function coveragePhrase(c: Coverage | null | undefined): string {
  if (!c || c.complete || !c.machines_expected) return "";
  const m = c.machines_expected;
  return `from ${c.machines_reporting} of ${m} machine${m !== 1 ? "s" : ""}`;
}
