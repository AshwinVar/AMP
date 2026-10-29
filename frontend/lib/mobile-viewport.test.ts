import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";

import postcss from "postcss";
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

/**
 * The sheet with comments stripped.
 *
 * This guards the ORPHAN test, which pulls `.phase29-*` out of raw media-block
 * text: a comment that merely NAMES a class -- the obvious thing to write when
 * explaining why a rule was dropped -- is otherwise read as a rule that matches
 * nothing, and the suite fails pointing at a comment. Verified by inserting
 * one: with the strip it passes, without it the orphan test reports
 * `phase29-legacy-toolbar`.
 *
 * It does NOT protect the padding assertion below, though this sheet's own
 * comment quotes `padding-right:124px`. rulesFor() matches a selector exactly,
 * and a comment has no braces, so it is absorbed into the next rule's SELECTOR
 * and can never be read as a declaration.
 */
const raw = readFileSync(SHEET, "utf8");
const css = raw.replace(/\/\*[\s\S]*?\*\//g, "");

/** The declaration body of every rule whose selector list names `sel` exactly. */
function rulesFor(sel: string, source: string = css): string[] {
  const out: string[] = [];
  const re = /([^{}]+)\{([^{}]*)\}/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(source)) !== null) {
    if (m[1].split(",").map((x) => x.trim()).includes(sel)) out.push(m[2]);
  }
  return out;
}

/** The sheet minus every @media block: the rules that hold at EVERY width. */
function baseRules(source: string): string {
  const re = /@media[^{]*\{/g;
  let out = "";
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(source)) !== null) {
    out += source.slice(last, m.index);
    let depth = 1;
    let i = re.lastIndex;
    while (i < source.length && depth > 0) {
      if (source[i] === "{") depth++;
      else if (source[i] === "}") depth--;
      i++;
    }
    last = i;
    re.lastIndex = i;
  }
  return out + source.slice(last);
}

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
  it("is a stylesheet the compiler can actually parse", () => {
    // Everything else here strips comments and never parses, so when a comment
    // was once mis-edited and orphaned four lines of prose into the sheet, all
    // of it still passed -- while the dev server answered 500 for the entire
    // dashboard ("Unknown word Between"). A guard that cannot see a broken
    // stylesheet is not guarding the stylesheet. This runs PostCSS, the parser
    // the build itself uses, over the raw file.
    expect(() => postcss.parse(raw, { from: SHEET })).not.toThrow();
  });

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
    // group with zero. The fix is a BASE rule -- the same crush happened at
    // 1280, where the actions measured 892px and left the title 36px -- so it
    // is asserted outside any media query, where it holds at every width.
    const base = baseRules(css);
    expect(base).toMatch(/\.phase29-topbar>:first-child\{[^}]*flex:\s*1 1 auto/);
    expect(base).toMatch(/\.phase29-topbar>:first-child\{[^}]*min-width:\s*0/);
    expect(base).toMatch(/\.phase29-topbar-actions\{[^}]*min-width:\s*0/);
  });

  it("reserves no topbar space for an element that is not there", () => {
    // `padding-right:124px` (98px below 1180) came in with the scaffold and
    // nothing had measured the row since. Measured at 1280 the last control
    // ended at 1126 against a bar edge of 1280 -- 154px of the row sat empty
    // while the title was crushed to 36px, because the actions were the only
    // child that could not give way. The bar's own 30px gutter is the margin;
    // a padding here is a reserve, and a reserve has to be justified by
    // something fixed sitting over it. Nothing is.
    const actions = rulesFor(".phase29-topbar-actions");
    expect(actions.length).toBeGreaterThan(0);
    for (const body of actions) {
      expect(body).not.toMatch(/padding-right\s*:\s*[1-9]/);
      // And it must stay able to shrink: flex-shrink:0 re-creates the crush
      // from the other side -- title ellipsised away rather than overlapped.
      expect(body).not.toMatch(/flex\s*:\s*0\s+0/);
      expect(body).not.toMatch(/flex-shrink\s*:\s*0/);
    }
  });

  it("keeps the topbar one row, because the bar cannot grow a second one", () => {
    // .phase29-topbar is a fixed 74px, so a title that wraps does not make the
    // bar taller -- it spills out of it. Measured at 1280 before the fix: 90px
    // of title group inside a 74px bar, because "Mission Control" wants 175px
    // on one line and had 174. Width was not the real fault; a long enough
    // machine or user name spills at 1440 too, which is why this is asserted
    // at BASE and not inside a phone query.
    const base = baseRules(css);
    for (const sel of [".phase29-topbar h1", ".phase29-eyebrow"]) {
      const bodies = rulesFor(sel, base);
      expect(bodies.length, `${sel} has no base rule`).toBeGreaterThan(0);
      const all = bodies.join(";");
      expect(all, `${sel} may wrap`).toMatch(/white-space\s*:\s*nowrap/);
      expect(all, `${sel} has no ellipsis`).toMatch(/text-overflow\s*:\s*ellipsis/);
    }
    // The phone query may still resize the title, but truncation is no longer
    // its job -- a redundant duplicate is how a mutation survived this suite.
    const narrow = mediaBlocks(css)
      .filter((b) => /7\d\d(\.\d+)?px/.test(b.query))
      .map((b) => b.body)
      .join("\n");
    for (const sel of [".phase29-topbar h1", ".phase29-eyebrow"]) {
      for (const body of rulesFor(sel, narrow)) {
        expect(body, `${sel} re-declares truncation on phones`)
          .not.toMatch(/text-overflow/);
      }
    }
    // (.phase29-user keeps its own ellipsis there -- that chip is not the title.)
  });

  it("gives the open drawer a way out that is not navigating away", () => {
    // The topbar's "X" is painted UNDER the open drawer -- sidebar z-index 100,
    // topbar 90, and the drawer spans 0-274px over a button at x 14-58 -- so a
    // hit test on the button's centre returned .phase29-brand-mark. With no
    // scrim and no Escape handler, the only dismissal was tapping a nav item,
    // which also navigates: you could not open the menu, look, and back out.
    const base = baseRules(css);
    const zOf = (sel: string) => {
      const m = /z-index\s*:\s*(\d+)/.exec(rulesFor(sel, base).join(";"));
      expect(m, `no z-index on ${sel}`).not.toBeNull();
      return Number(m![1]);
    };
    const topbar = zOf(".phase29-topbar");
    const scrim = zOf(".phase29-nav-backdrop");
    const drawer = zOf(".phase29-sidebar");
    // Over the topbar, so a tap on the covered "X" still lands on the scrim
    // and closes; under the drawer, so the drawer itself stays usable.
    expect(scrim, "scrim must sit above the topbar").toBeGreaterThan(topbar);
    expect(scrim, "scrim must sit below the drawer").toBeLessThan(drawer);

    // The hamburger exists only below 1180, so a full-screen layer must not be
    // live on desktop, where it would silently eat every click on the page.
    const scrimBase = rulesFor(".phase29-nav-backdrop", base).join(";");
    expect(scrimBase, "scrim is live at desktop widths").toMatch(/display\s*:\s*none/);
    const drawerWidths = mediaBlocks(css)
      .filter((b) => /1180px/.test(b.query))
      .map((b) => b.body)
      .join("\n");
    expect(rulesFor(".phase29-nav-backdrop", drawerWidths).join(";"))
      .toMatch(/display\s*:\s*block/);

    // Both dismissal paths must actually close it, not merely exist.
    const page = readFileSync(join(APP, "dashboard", "page.tsx"), "utf8");
    expect(page, "the backdrop does not close the drawer")
      .toMatch(/phase29-nav-backdrop[\s\S]{0,160}?setMobileNavOpen\(false\)/);
    expect(page, "Escape does not close the drawer")
      .toMatch(/"Escape"[\s\S]{0,80}?setMobileNavOpen\(false\)/);
    // ...and the handler has to be REGISTERED. Deleting only the
    // addEventListener leaves the Escape branch sitting there, inert, and a
    // guard that looks for the branch alone passes. A mutation did exactly
    // that and survived, so the wiring is pinned too.
    expect(page, "the Escape handler is never registered")
      .toMatch(/"Escape"[\s\S]{0,200}?addEventListener\("keydown"/);
  });

  it("lets the tenant switcher give way instead of the search box", () => {
    // .phase29-pill is a <select>, so its width is its widest <option> -- the
    // tenant list. At min-width:auto a name like "Shrinidhi Plastics Private
    // Limited" pins it at 245px and the search box eats the whole shortfall:
    // measured 129px at 1181. min-width:0 moves it to 167 and the search holds
    // 190. (A floor on the search and text-overflow on the select were both
    // tried here and measured out -- neither ever changed a pixel.)
    const bodies = rulesFor(".phase29-pill", baseRules(css));
    expect(bodies.length, "no base rule for .phase29-pill").toBeGreaterThan(0);
    expect(bodies.join(";")).toMatch(/min-width\s*:\s*0/);
  });

  it("makes the chrome yield before the page title does", () => {
    // Both can shrink, so flexbox splits a shortfall between them in
    // proportion to base size times shrink factor -- and the actions are much
    // the wider, so an equal factor makes the TITLE pay. At 1181px that clipped
    // "Mission Control" beside a search box still holding 71px of slack.
    const base = baseRules(css);
    const m = /\.phase29-topbar-actions\{[^}]*flex\s*:\s*(\d+)\s+(\d+)/.exec(base);
    expect(m, "no base flex on .phase29-topbar-actions").not.toBeNull();
    expect(Number(m![2]), "chrome must yield faster than the title").toBeGreaterThan(1);
  });
});
