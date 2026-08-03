/**
 * The height/width percentage a proportional bar should render at.
 *
 * Bars across the dashboard are drawn as `value / peak` of a track, floored to a
 * few percent so a tiny-but-nonzero value still leaves a visible sliver. The
 * floor is a RENDERING aid for real data — it must never leak onto a ZERO value.
 * A zero bucket (a day with no losses, a loss component that cost nothing) has to
 * render at 0: painting a floored sliver reads as activity/magnitude that did not
 * happen — the display-honesty rule the daily-series audit (#464) applied to the
 * count-based sparklines (`value === 0 ? 0 : Math.max(floor, ...)`).
 *
 * One definition so a card can't re-derive it and drift: zero (and any
 * non-positive / non-finite value, or a non-positive peak) collapses to 0, a
 * positive value keeps its floor, and the result is capped at 100.
 */
export function barPct(value: number, peak: number, floor = 0): number {
  // Not `value <= 0`: also folds NaN (a bad reading) to 0 rather than NaN%.
  if (!(value > 0) || !(peak > 0)) return 0;
  return Math.min(100, Math.max(floor, Math.round((value / peak) * 100)));
}
