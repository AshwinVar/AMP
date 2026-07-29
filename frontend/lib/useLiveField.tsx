"use client";

import { useEffect, useRef, useState } from "react";

/**
 * An input that lives in a polled table.
 *
 * The dashboard re-fetches every 3s (app/dashboard/page.tsx, usePolling), so the
 * rows under these boxes are constantly replaced with fresher server data. The
 * boxes were `defaultValue={row.x}`, i.e. UNCONTROLLED, and that combination is
 * quietly destructive.
 *
 * React assigns `element.value` once at mount (initInput). That sets the DOM's
 * "dirty value flag", and from then on React only writes the `value` ATTRIBUTE
 * on updates (setDefaultValue) — which the browser ignores for a dirty input. So:
 *
 *   t=0   stock 500 -> box "500",  chip "Healthy"
 *   t=3s  stock 420 -> box "500",  chip "Low Stock"   <- the row contradicts itself
 *   blur             -> PATCH {current_stock: 500}    <- 80 units of real issue erased
 *
 * The second line is a display bug. The third is data loss: `onBlur` fired
 * unconditionally, so merely clicking into a box to read it and clicking away
 * wrote the stale number back over the server's real one, with no transaction
 * row and no audit trail (inventory_routes.py setattrs it directly).
 *
 * The fix has to satisfy two opposing requirements at once:
 *
 *   1. a poll MUST refresh a box the user is not editing, and
 *   2. a poll MUST NOT touch a box the user is part-way through typing into.
 *
 * so the field keeps a draft, resyncs only while unfocused, and — the part that
 * stops the data loss — commits only if the user actually edited. Blurring a box
 * you never typed in adopts the server's value instead of writing yours back.
 */

type LiveField = {
  value: string;
  onChange: (e: React.ChangeEvent<HTMLInputElement>) => void;
  onFocus: () => void;
  onBlur: () => void;
};

export function useLiveField(external: string | number, commit: (raw: string) => void): LiveField {
  const externalText = String(external);
  const [draft, setDraft] = useState(externalText);

  // Refs, not state: focus and edited-ness are read inside an effect and inside
  // the blur handler, both of which can run before a state update has committed.
  // (They are deliberately never read during render — that trips
  // react-hooks/refs, and the eslint baseline is exact.)
  const focused = useRef(false);
  const edited = useRef(false);

  useEffect(() => {
    // Requirement 2: a poll landing mid-edit must not move the cursor or the text.
    // No need to compare against the previous value first — the dependency array
    // is already that comparison. (Mutation testing: an inner guard here survived
    // every test, because it can never be false when the effect runs.)
    if (!focused.current) setDraft(externalText);
  }, [externalText]);

  return {
    value: draft,
    onChange: (e) => {
      edited.current = true;
      setDraft(e.target.value);
    },
    onFocus: () => {
      focused.current = true;
    },
    onBlur: () => {
      focused.current = false;
      if (!edited.current) {
        // Never typed. Anything the poll withheld while we had focus applies now,
        // and crucially we do NOT write our stale value back to the server.
        // `externalText` is the latest: this handler is rebuilt every render, so
        // it closes over the value the most recent poll delivered.
        setDraft(externalText);
        return;
      }
      edited.current = false;
      if (draft === externalText) return; // typed, but landed back on the server's value
      commit(draft);
    },
  };
}

type LiveInputProps = Omit<
  React.InputHTMLAttributes<HTMLInputElement>,
  "value" | "defaultValue" | "onChange" | "onFocus" | "onBlur"
> & {
  /** The server's current value for this cell. */
  value: string | number;
  /** Called on blur, with the raw string, only when the user actually changed it. */
  onCommit: (raw: string) => void;
};

/**
 * Hooks cannot be called inside a `.map()` callback, and every one of these
 * inputs is rendered per row — so the hook is wrapped in a component.
 */
export function LiveInput({ value, onCommit, ...rest }: LiveInputProps) {
  const field = useLiveField(value, onCommit);
  return <input {...rest} {...field} />;
}
