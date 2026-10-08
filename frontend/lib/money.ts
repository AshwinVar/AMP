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
export const CURRENCY = "₹";

// ── Grouping, which is part of the currency and not a detail ──────────
//
// `n.toLocaleString()` with no locale follows whatever locale the BROWSER is
// set to, so the same figure read "₹14,00,000" on the customer's machine and
// "₹1,400,000" on the developer's. A money format that depends on who is
// looking is not a format.
//
// en-IN, pinned, for the reason stated for contract money below: the platform
// prints rupees, and an Indian plant's own invoices and ledgers group in lakhs.
// It also puts this in step with backend/currency.py, which groups the same way
// — the two had quietly disagreed above six digits, which the symbol check
// could not see because it only ever compared ₹49,740, identical either way.
const GROUP = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 });

/** money(49740) -> "₹49,740"; money(1400000) -> "₹14,00,000" */
export function money(n: number): string {
  return `${CURRENCY}${GROUP.format(n)}`;
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

// ── Contract money (ADR-0021) ────────────────────────────────────────
//
// A service-contract statement carries money as DECIMAL TEXT with exactly two
// places ("40000.00"), in the contract's own currency, computed server-side
// with exact decimals. It is displayed without ever becoming a float: the
// integer part is grouped as a BigInt and the paise are appended as the two
// characters the server sent. `money()` above is the platform's whole-rupee
// analytics figure and is a different thing -- it takes a number, so it would
// round the paise away; do not route contract amounts through it.
//
// Indian digit grouping (12,34,567.89) is right because contracts are INR-only
// (backend contract_terms.CURRENCIES). Admitting a second currency means the
// grouping must follow it: a GBP fee would otherwise read "£12,34,567.89".
const DECIMAL_MONEY = /^([0-9]{1,12})\.([0-9]{2})$/;

/** formatDecimalMoney("1234567.89", "INR") -> "₹12,34,567.89" (en-IN grouping). */
export function formatDecimalMoney(amount: string | null, currency: string): string {
  if (amount === null) return "—";
  const match = DECIMAL_MONEY.exec(amount);
  if (!match) {
    throw new RangeError("contract money must be decimal text with two places");
  }
  const grouped = new Intl.NumberFormat("en-IN").format(BigInt(match[1]));
  const symbol =
    new Intl.NumberFormat("en-IN", { style: "currency", currency })
      .formatToParts(0)
      .find((part) => part.type === "currency")?.value ?? currency;
  return symbol + grouped + "." + match[2];
}
