import { readFileSync } from "node:fs";
import { join } from "node:path";

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import BrandMark from "./BrandMark";

describe("the dashboard sidebar", () => {
  // The dashboard (~2,900 lines) is not unit-rendered, so its wiring is read
  // from source. CRLF normalised: git writes CRLF on Windows.
  const page = readFileSync(join(__dirname, "..", "app", "dashboard", "page.tsx"), "utf8")
    .replace(/\r\n/g, "\n");

  it("renders the tenant's mark from its branding, once", () => {
    const wired = "<BrandMark name={brandName} color={tenantCfg?.brand_color} logoUrl={tenantCfg?.brand_logo_url} />";
    expect(page.split(wired).length - 1).toBe(1);
  });

  it("no longer hard-codes the glyph mark beside it", () => {
    expect(page).not.toMatch(/className="phase29-brand-mark">⌁</);
  });
});

/**
 * The sidebar's mark (item 10 of the sweep queue): the Branding card saved a
 * colour and a logo, and the dashboard applied neither.
 */
describe("BrandMark", () => {
  it("shows the tenant's logo, fetched without a referrer", () => {
    render(<BrandMark name="Acme" color="#e11d2a" logoUrl="https://acme.test/logo.png" />);
    const img = screen.getByRole("img", { name: "Acme logo" }) as HTMLImageElement;
    expect(img.getAttribute("src")).toBe("https://acme.test/logo.png");
    expect(img.getAttribute("referrerpolicy")).toBe("no-referrer");
  });

  it("falls back to the glyph when the logo fails to load", () => {
    render(<BrandMark name="Acme" color="#e11d2a" logoUrl="https://acme.test/missing.png" />);
    fireEvent.error(screen.getByRole("img", { name: "Acme logo" }));
    expect(screen.queryByRole("img")).toBeNull();
    expect(screen.getByTestId("brand-mark").textContent).toBe("⌁");
  });

  it("puts the glyph on the tenant's accent colour when there is no logo", () => {
    render(<BrandMark name="Acme" color="#e11d2a" logoUrl={null} />);
    const mark = screen.getByTestId("brand-mark");
    expect(mark.style.background).toMatch(/rgb\(225, 29, 42\)|#e11d2a/);
  });

  it("keeps the default for a colour or logo it cannot apply", () => {
    // Valid CSS that is not a colour: applied as the background, it would have
    // every viewer's browser fetch a third party's URL.
    render(<BrandMark name="Acme" color="url(https://x.test/a.png)" logoUrl="http://acme.test/logo.png" />);
    const mark = screen.getByTestId("brand-mark");
    expect(screen.queryByRole("img")).toBeNull();
    expect(mark.getAttribute("style")).toBeNull();
  });

  it("CONTROL: that value is one the style engine would accept, so the check means something", () => {
    const probe = document.createElement("div");
    probe.style.background = "url(https://x.test/a.png)";
    expect(probe.getAttribute("style")).not.toBeNull();
  });
});
