import { readFileSync, readdirSync } from "node:fs";
import { join, relative } from "node:path";

import { fireEvent, render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { LiveInput } from "./useLiveField";

/**
 * These tests are written against two reproduced failures, not against the fix.
 *
 * With the old `defaultValue={row.x}` the first two below produce:
 *
 *   after poll -> input.value = "500" | value attribute = "420" | chip = "Low Stock"
 *   blur       -> committed [500]
 *
 * i.e. the box froze at its mount value while the rest of the row moved on, and
 * clicking into it and away — typing nothing — wrote the frozen number back over
 * the server's real one. React sets the DOM dirty-value flag when it assigns
 * `element.value` at mount, after which it only updates the `value` attribute,
 * which the browser ignores for a dirty input.
 *
 * The interesting cases are the last four: the naive fix (make it controlled)
 * breaks a user who is part-way through typing when a poll lands.
 */

function Row({
  stock,
  onCommit,
}: {
  stock: number;
  onCommit: (raw: string) => void;
}) {
  return <LiveInput data-testid="box" type="number" value={stock} onCommit={onCommit} />;
}

const box = (r: { getByTestId: (id: string) => HTMLElement }) =>
  r.getByTestId("box") as HTMLInputElement;

describe("a field in a 3s-polled table", () => {
  it("shows the value the poll just delivered", () => {
    // THE DISPLAY BUG. Stock drops 500 -> 420 when the line draws material.
    const r = render(<Row stock={500} onCommit={vi.fn()} />);
    expect(box(r).value).toBe("500");

    r.rerender(<Row stock={420} onCommit={vi.fn()} />);
    expect(box(r).value).toBe("420");
  });

  it("does not write anything back when you click in and out without typing", () => {
    // THE DATA-LOSS BUG. Reading a cell must never be a write.
    const commit = vi.fn();
    const r = render(<Row stock={500} onCommit={commit} />);

    fireEvent.focus(box(r));
    fireEvent.blur(box(r));

    expect(commit).not.toHaveBeenCalled();
  });

  it("commits what you typed", () => {
    const commit = vi.fn();
    const r = render(<Row stock={500} onCommit={commit} />);

    fireEvent.focus(box(r));
    fireEvent.change(box(r), { target: { value: "460" } });
    fireEvent.blur(box(r));

    expect(commit).toHaveBeenCalledExactlyOnceWith("460");
  });

  it("does not commit when you type the value it already had", () => {
    // A no-op PATCH is not harmless here: it is an unaudited write of a value
    // that may have moved on the server since this row was rendered.
    //
    // Note the detour via "46". Changing straight to "500" fires no onChange at
    // all — React suppresses it when the value did not actually change — so the
    // field never becomes edited and the test would pass through the untouched
    // branch instead, leaving this guard unexercised. Mutation testing caught
    // exactly that.
    const commit = vi.fn();
    const r = render(<Row stock={500} onCommit={commit} />);

    fireEvent.focus(box(r));
    fireEvent.change(box(r), { target: { value: "46" } });
    fireEvent.change(box(r), { target: { value: "500" } });
    fireEvent.blur(box(r));

    expect(commit).not.toHaveBeenCalled();
  });
});

describe("a poll landing while someone is typing", () => {
  it("leaves the half-typed value alone", () => {
    // The whole reason this cannot just be `value={stock}`: at 3s intervals a
    // controlled input would overwrite the operator mid-keystroke.
    const r = render(<Row stock={500} onCommit={vi.fn()} />);

    fireEvent.focus(box(r));
    fireEvent.change(box(r), { target: { value: "46" } });
    r.rerender(<Row stock={420} onCommit={vi.fn()} />);

    expect(box(r).value).toBe("46");
  });

  it("still commits the typed value, not the value the poll brought", () => {
    const commit = vi.fn();
    const r = render(<Row stock={500} onCommit={commit} />);

    fireEvent.focus(box(r));
    fireEvent.change(box(r), { target: { value: "460" } });
    r.rerender(<Row stock={420} onCommit={commit} />);
    fireEvent.blur(box(r));

    expect(commit).toHaveBeenCalledExactlyOnceWith("460");
  });

  it("adopts the withheld value on blur if you never actually typed", () => {
    // Focus alone suppresses the resync, so the box would otherwise sit on the
    // stale number indefinitely after the user tabs away.
    const commit = vi.fn();
    const r = render(<Row stock={500} onCommit={commit} />);

    fireEvent.focus(box(r));
    r.rerender(<Row stock={420} onCommit={commit} />);
    expect(box(r).value).toBe("500"); // withheld on purpose while focused

    fireEvent.blur(box(r));
    expect(box(r).value).toBe("420");
    expect(commit).not.toHaveBeenCalled();
  });

  it("resyncs again after an edit is committed", () => {
    // edited-ness must reset, or the second poll after a commit is ignored.
    const commit = vi.fn();
    const r = render(<Row stock={500} onCommit={commit} />);

    fireEvent.focus(box(r));
    fireEvent.change(box(r), { target: { value: "460" } });
    fireEvent.blur(box(r));
    expect(commit).toHaveBeenCalledExactlyOnceWith("460");

    r.rerender(<Row stock={415} onCommit={commit} />);
    expect(box(r).value).toBe("415");
  });

  it("goes back to writing nothing once an edit is behind you", () => {
    // Edited-ness has to RESET, not just be set. A field the operator corrected
    // earlier in the shift must still be safe to click into and read later.
    // Every other test starts from a pristine field, so none of them notices if
    // the flag latches on — mutation testing found this gap, not review.
    const commit = vi.fn();
    const r = render(<Row stock={500} onCommit={commit} />);

    fireEvent.focus(box(r));
    fireEvent.change(box(r), { target: { value: "460" } });
    fireEvent.blur(box(r));
    expect(commit).toHaveBeenCalledExactlyOnceWith("460");

    // ...later, the operator clicks into that same cell while the line is
    // running, reads it, and tabs away. A poll lands in between, so the draft
    // and the server have diverged — which is what makes this the real test:
    // if edited-ness had latched on, the untouched-blur branch is skipped and
    // the stale 460 gets written over the server's 415.
    fireEvent.focus(box(r));
    r.rerender(<Row stock={415} onCommit={commit} />);
    fireEvent.blur(box(r));

    expect(commit).toHaveBeenCalledExactlyOnceWith("460"); // still just the one
    expect(box(r).value).toBe("415");
  });
});

describe("text fields, not just numbers", () => {
  function Notes({ notes, onCommit }: { notes: string; onCommit: (raw: string) => void }) {
    return <LiveInput data-testid="box" value={notes} onCommit={onCommit} />;
  }

  it("refreshes resolution notes another user edited", () => {
    // EscalationSection's notes box. This one mounts EMPTY on a fresh
    // escalation, and an empty mount happens to escape the dirty-value flag —
    // so it looked exempt. It is not: any escalation somebody has actually
    // worked on mounts with text, and froze exactly like the numeric cells.
    const r = render(<Notes notes="Awaiting vendor" onCommit={vi.fn()} />);
    r.rerender(<Notes notes="Vendor called back" onCommit={vi.fn()} />);
    expect(box(r).value).toBe("Vendor called back");
  });

  it("commits an edited note", () => {
    const commit = vi.fn();
    const r = render(<Notes notes="Awaiting vendor" onCommit={commit} />);

    fireEvent.focus(box(r));
    fireEvent.change(box(r), { target: { value: "Chased twice" } });
    fireEvent.blur(box(r));

    expect(commit).toHaveBeenCalledExactlyOnceWith("Chased twice");
  });
});

describe("rot guard: no data-bound defaultValue in the app", () => {
  // The failure mode is silent in the worst way: `defaultValue={row.x}` type-checks,
  // renders a correct-looking number, and only diverges once a poll lands — which
  // never happens in a test that renders once. Fifteen sites accumulated exactly
  // that way. `defaultValue=""` (a literal, not bound to server data) is a real
  // form default and does not match the pattern, so the allowlist stays empty.
  const ALLOWED: string[] = [];

  function sources(dir: string, out: string[] = []): string[] {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const path = join(dir, entry.name);
      if (entry.isDirectory()) sources(path, out);
      else if (/\.tsx?$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name)) out.push(path);
    }
    return out;
  }

  it("finds none", () => {
    const root = join(__dirname, "..");
    const hits: string[] = [];
    for (const dir of ["app", "components"]) {
      for (const file of sources(join(root, dir))) {
        const rel = relative(root, file).replace(/\\/g, "/");
        if (ALLOWED.includes(rel)) continue;
        readFileSync(file, "utf8")
          .split("\n")
          .forEach((text, i) => {
            if (/defaultValue=\{/.test(text)) hits.push(`  ${rel}:${i + 1}  ${text.trim().slice(0, 90)}`);
          });
      }
    }
    expect(
      hits,
      hits.length
        ? `An uncontrolled input bound to server data freezes at its mount value once ` +
          `the 3s poll re-renders the row, and blurring it writes that stale value back. ` +
          `Use LiveInput from lib/useLiveField:\n${hits.join("\n")}`
        : "",
    ).toEqual([]);
  });

  it("detects the pattern it is looking for", () => {
    // Guards the guard: if this regex stopped matching, the scan would be silent.
    expect(/defaultValue=\{/.test("<input defaultValue={row.amount} />")).toBe(true);
    expect(/defaultValue=\{/.test('<input defaultValue="" />')).toBe(false);
  });
});

describe("the input still behaves like an input", () => {
  it("forwards the props the call sites pass", () => {
    const r = render(
      <LiveInput data-testid="box" type="number" className="w-24" step="5" value={7} onCommit={vi.fn()} />,
    );
    expect(box(r).type).toBe("number");
    expect(box(r).className).toBe("w-24");
    expect(box(r).getAttribute("step")).toBe("5");
  });
});
