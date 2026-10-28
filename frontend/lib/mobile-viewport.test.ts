import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

/**
 * Rot guard: the narrow-viewport rules must target classes the markup has.
 *
 * This is the bug that shipped. The stylesheet carried
 * `.phase29-search{display:none}` inside its 1180px block — the clear intent
 * that the 280px search box should not be on a phone — while the markup had
 * long since replaced that class with an inline `style={{width:280,...}}`.
 * The rule matched nothing, so on a 375px viewport the topbar's two flex
 * children were a left group (menu button, greeting, page title) and an
 * actions group that measured 500px. With no shrink constraint the actions
 * won, the left group collapsed to ZERO width, the page title spilled out of
 * it and rendered underneath the search field, and 63px of the user chip sat
 * off-screen — so the account menu could not be tapped at all.
 *
 * Nothing failed. The page did not overflow (the chrome is clipped), every
 * test passed, and the only way to see it was to open a phone-sized browser.
 * So this file pins the two halves together: a selector the narrow rules
 * style must exist in the JSX, and the tap targets must stay thumb-sized.
 *
 * It deliberately does NOT try to measure layout — that needs a browser, and
 * the Playwright suite is where that belongs. It pins the contract that broke.
 */

const APP = join(__dirname, "..", "app");
const SHEET = join(APP, "phase29-enterprise.css");

/** Every .tsx source under app/ and components/, concatenated. */
function markup(): string {
  const out: string[] = [];
  const walk = (dir: string) => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const full = join(dir, entry.name);
      if (entry.isDirectory()) {
        if (entry.name === "node_modules" || entry.name === ".next") continue;
        walk(full);
      } else if (entry.name.endsWith(".tsx")) {
        out.push(readFileSync(full, "utf8"));
      }
    }
  };
  walk(APP);
  walk(join(__dirname, "..", "components"));
  return out.join("\n");
}

const css = readFileSync(SHEET, "utf8");

/** The body of every `@media(max-width: ...)` block, brace-matched. */
function mediaBlocks(source: string): { query: string; body: string }[] {
  const out: { query: string; body: string }[] = [];
  const re = /@media\s*\(([^)]*max-width[^)]*)\)\s*\{/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(source)) !== null) {
    let depth = 1;
    let i = re.lastIndex;
    while (i < source.length && depth > 0) {
      if (source[i] === "{") depth++;
      else if (source[i] === "}") depth--;
      i++;
    }
    out.push({ query: m[1].trim(), body: source.slice(re.lastIndex, i - 1) });
  }
  return out;
}

describe("narrow viewport", () => {
  it("has a breakpoint for phones, not just the 1180px sidebar collapse", () => {
    const widths = mediaBlocks(css).map((b) => b.query);
    expect(widths.some((q) => /7\d\d(\.\d+)?px/.test(q))).toBe(true);
  });

  it("only styles phase29 classes the markup actually carries", () => {
    const jsx = markup();
    const orphans = new Set<string>();
    for (const block of mediaBlocks(css)) {
      for (const [, cls] of block.body.matchAll(/\.(phase29-[a-z0-9-]+)/g)) {
        // The class has to appear in a className somewhere, or the rule is
        // decoration: it will never match an element and the layout it was
        // written to fix stays broken, silently.
        if (!jsx.includes(cls)) orphans.add(cls);
      }
    }
    expect([...orphans]).toEqual([]);
  });

  it("keeps the search box styled by its class, not an inline width", () => {
    // An inline style wins over a stylesheet, so a width in the style attribute
    // cannot be undone by a media query without !important. The box lives in
    // CSS so the narrow block can simply hide it.
    expect(css).toMatch(/\.phase29-search\{[^}]*width:\s*280px/);
    const jsx = markup();
    expect(jsx).toContain("phase29-search");
    expect(jsx).not.toMatch(/style=\{\{width:280/);
  });

  it("gives the drawer's own controls a 44px tap target", () => {
    // WCAG AA, and it must hold wherever the drawer IS the navigation — every
    // width below the 1180px collapse, tablets included. These were 38x38 and
    // 37px tall, which is a miss on a thumb.
    const drawerBlocks = mediaBlocks(css).filter((b) => /1180px/.test(b.query));
    expect(drawerBlocks.length).toBeGreaterThan(0);
    const body = drawerBlocks.map((b) => b.body).join("\n");
    expect(body).toMatch(/\.phase29-menu-btn\{[^}]*width:\s*44px/);
    expect(body).toMatch(/\.phase29-menu-btn\{[^}]*height:\s*44px/);
    expect(body).toMatch(/\.phase29-nav-item\{[^}]*min-height:\s*44px/);
  });

  it("lets the topbar's title shrink instead of crushing it to nothing", () => {
    // The actual failure: .phase29-topbar-actions had no shrink constraint, so
    // it took its content width (500px on a 375px screen) and left the title
    // group with zero. Both halves of the fix are pinned.
    const narrow = mediaBlocks(css)
      .filter((b) => /7\d\d(\.\d+)?px/.test(b.query))
      .map((b) => b.body)
      .join("\n");
    expect(narrow).toMatch(/\.phase29-topbar>:first-child\{[^}]*min-width:\s*0/);
    expect(narrow).toMatch(/\.phase29-topbar-actions\{[^}]*flex:\s*0 0 auto/);
    expect(narrow).toMatch(/\.phase29-topbar h1\{[^}]*text-overflow:\s*ellipsis/);
  });
});
