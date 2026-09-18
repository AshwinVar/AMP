import { describe, expect, it } from "vitest";
import { CURRENCY, formatDecimalMoney, lossFigure, money } from "./money";

describe("money", () => {
  it("prefixes the currency symbol", () => {
    // Currency is the one unit rendered as a PREFIX. ScorecardStrip decides
    // prefix-vs-suffix by comparing the backend's `unit` token to CURRENCY, so a
    // suffix here would mean the comparison broke: "49740£" instead of "£49,740".
    expect(money(49740).startsWith(CURRENCY)).toBe(true);
    expect(money(0)).toBe(`${CURRENCY}0`);
  });

  it("groups thousands", () => {
    // Deliberately does NOT pin the separator character. toLocaleString() is
    // locale-dependent (en-US "49,740" vs de-DE "49.740") and CI runners need not
    // match a developer's machine, so asserting a literal comma would be a test
    // that fails for the wrong reason. What matters is that grouping HAPPENS —
    // an unformatted "49740" has no non-digit between the digits.
    const grouped = money(49740).slice(CURRENCY.length);
    expect(grouped).not.toBe("49740");
    expect(grouped.replace(/\D/g, "")).toBe("49740");
  });

  it("keeps negatives readable", () => {
    expect(money(-500)).toContain(CURRENCY);
    expect(money(-500)).toContain("500");
  });

  it("is the single source of truth for the symbol", () => {
    // A one-character constant is easy to inline by accident; this pins that the
    // module still EXPORTS it, which is what backend/test_currency_single.py
    // greps for when it checks the two stacks agree.
    expect(typeof CURRENCY).toBe("string");
    expect(CURRENCY.length).toBeGreaterThan(0);
  });
});

describe("lossFigure", () => {
  // ADR-0010: a loss is money only at the tenant's own unit value. The backend
  // sends cost = null without one, and both null when downtime had no run time to
  // convert. These cards used to money() a fixed £12/min + £25/unit tariff.
  it("shows money when the tenant has a unit value", () => {
    expect(lossFigure(225, 18)).toBe(money(225));
    expect(lossFigure(0, 18)).toBe(money(0)); // a £0 rate is a real £0
  });

  it("shows good units, and never the currency, without one", () => {
    expect(lossFigure(null, 18)).toBe("18 units");
    expect(lossFigure(undefined, 1)).toBe("1 unit");
    expect(lossFigure(null, 18)).not.toContain(CURRENCY);
    expect(lossFigure(null, 0)).toBe("0 units");
  });

  it("says nothing it cannot know", () => {
    expect(lossFigure(null, null)).toBe("—");
  });
});

/**
 * Contract money (ADR-0021) arrives as DECIMAL TEXT ("40000.00") because the
 * backend computes it with exact decimals and never lets it become a float. The
 * display must not undo that on the last inch: Number("1234567.89") is fine,
 * but a 12-digit fee with paise is where binary floating point starts to drift,
 * and a credit two parties signed is the one number that must not.
 */
describe("formatDecimalMoney", () => {
  it("formats rupees with Indian grouping, digit for digit", () => {
    expect(formatDecimalMoney("1234567.89", "INR")).toBe("₹12,34,567.89");
    expect(formatDecimalMoney("40000.00", "INR")).toBe("₹40,000.00");
  });

  it("keeps every digit of the largest amount the terms allow", () => {
    // 12 integer digits and paise: 14 significant digits, past what a float
    // round-trips reliably through arithmetic.
    expect(formatDecimalMoney("999999999999.99", "INR")).toBe("₹9,99,99,99,99,999.99");
    expect(formatDecimalMoney("100000000000.01", "INR")).toBe("₹1,00,00,00,00,000.01");
  });

  it("keeps small amounts and paise exactly", () => {
    expect(formatDecimalMoney("0.05", "INR")).toBe("₹0.05");
    expect(formatDecimalMoney("2000.00", "INR")).toBe("₹2,000.00");
  });

  it("uses the contract's own currency symbol", () => {
    expect(formatDecimalMoney("1234.50", "GBP")).toBe("£1,234.50");
  });

  it("says nothing rather than a number when there is no amount", () => {
    // A credit that cannot be computed yet (disputes pending, not evaluable) is
    // null, and must never render as a zero credit.
    expect(formatDecimalMoney(null, "INR")).toBe("—");
  });

  it("refuses text that is not two-place decimal money", () => {
    for (const bad of ["1e3", "12.5", "12", "-5.00", "1,000.00", " 1.00", "0x10.00"]) {
      expect(() => formatDecimalMoney(bad, "INR"), bad).toThrow(RangeError);
    }
  });
});
