// The one place the frontend writes down the platform's currency symbol.
//
// This mirrors backend/currency.py, and backend/test_currency_single.py fails the
// build if the two drift apart. They cannot simply be independent constants: the
// scorecard KPI payload carries the symbol as its `unit` token and ScorecardStrip
// branches on it to decide prefix-vs-suffix formatting, so a mismatch renders
// "49740£" instead of "£49,740" — wrong-looking rather than merely inconsistent.
//
// Before this file the dashboard printed one plant's money in two currencies: the
// cost-of-losses cards were "$" while the unit-value cards were "£", and modules.ts
// used "£" as the Costing nav icon directly above an all-"$" card. ADR-0010 (accepted)
// makes a per-tenant £/good-unit rate the single money basis — the column is
// `unit_value_gbp` — so GBP is canonical and "$" was the defect.
export const CURRENCY = "£";

/** money(49740) -> "£49,740" */
export function money(n: number): string {
  return `${CURRENCY}${n.toLocaleString()}`;
}

/**
 * A loss as a card shows it (ADR-0010): money when the tenant has set its unit
 * value (the backend then sends a number for `cost`), good units when it has not
 * (`cost` is null), and "—" when the downtime could not be converted into units
 * at all (no run time in the window, so both are null).
 *
 * The cost-of-losses cards used to call money() on figures priced at a fixed £12 a
 * minute and £25 a unit for every tenant. With no rate there is no £ to show, and
 * money(null) would crash; this is the one place that decides what to show instead.
 */
export function lossFigure(cost: number | null | undefined, units: number | null | undefined): string {
  if (cost != null) return money(cost);
  if (units != null) return `${units.toLocaleString()} unit${units === 1 ? "" : "s"}`;
  return "—";
}
