"use client";

import { useCallback, useRef, useState } from "react";

/**
 * Runs a submit handler at most once at a time, so a double-click cannot post twice.
 *
 * The forms this guards write to tables with no unique key — downtime logs, shift
 * data, escalations — so a second POST does not bounce off a constraint, it
 * silently creates a second identical row. Duplicated downtime minutes are then
 * double-counted into OEE and the cost-of-losses figures, and a duplicated shift
 * double-counts output: numbers a plant manager acts on, quietly wrong, with
 * nothing in the UI to suggest anything happened twice.
 *
 * Note which half does the work. The `useRef` set is the guard: a second click
 * lands before React has re-rendered, so a state flag would still be `false` and
 * would let the second POST through. The `active` state exists only so the button
 * can show a disabled/"Saving…" state — it is feedback, not protection.
 */
export function useInFlight() {
  const keys = useRef<Set<string>>(new Set());
  const [active, setActive] = useState<string[]>([]);

  const run = useCallback(async (key: string, fn: () => Promise<void>) => {
    if (keys.current.has(key)) return false; // already in flight — ignore the re-entry
    keys.current.add(key);
    setActive(Array.from(keys.current));
    try {
      await fn();
      return true;
    } finally {
      keys.current.delete(key);
      setActive(Array.from(keys.current));
    }
  }, []);

  const busy = useCallback((key: string) => active.includes(key), [active]);

  return { run, busy };
}
