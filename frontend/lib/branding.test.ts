import { describe, expect, it } from "vitest";

import { safeBrandColor, safeLogoUrl } from "./branding";

/**
 * The display half of the branding rule (the server's is platform_routes
 * set_branding). Rows written before that rule existed can hold anything; the
 * sidebar shows the default for a value it cannot apply, never a broken one.
 */
describe("safeBrandColor", () => {
  it("applies a #rrggbb colour as given", () => {
    expect(safeBrandColor("#e11d2a")).toBe("#e11d2a");
    expect(safeBrandColor("#1D4ED8")).toBe("#1D4ED8");
  });

  it("falls back for anything else", () => {
    for (const bad of ["red", "#123", "#1234567", "1d4ed8", "#gggggg", "url(x)", "", null, undefined]) {
      expect(safeBrandColor(bad), String(bad)).toBeNull();
    }
  });
});

describe("safeLogoUrl", () => {
  it("applies an https:// logo", () => {
    expect(safeLogoUrl("https://acme.test/logo.png")).toBe("https://acme.test/logo.png");
  });

  it("falls back for anything the page could not or should not load", () => {
    for (const bad of ["http://acme.test/logo.png", "javascript:alert(1)", "data:image/png;base64,AAAA",
                       "//acme.test/logo.png", "acme.test/logo.png", "https://", "", null, undefined]) {
      expect(safeLogoUrl(bad), String(bad)).toBeNull();
    }
  });
});
