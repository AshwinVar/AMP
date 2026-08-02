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
