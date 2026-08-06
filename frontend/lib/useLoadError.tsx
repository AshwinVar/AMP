"use client";

import { useCallback, useState } from "react";

/**
 * Load state for a list fetch, so a failure never renders as "you have no data".
 *
 * The pattern this replaces was everywhere:
 *
 *     const load = () => apiGet<Remnant[]>("/remnants").then(setRows).catch(() => {});
 *     ...
 *     {rows.length === 0 && <p>No remnants logged yet.</p>}
 *
 * When the request fails, `setRows` is never called, `rows` stays `[]`, and the
 * user is told they have no remnants. A dropped connection, an expired token and
 * a 500 all render as the same reassuring empty state — which is worse than an
 * error in an inventory system, because "no stock on record" is a plausible
 * answer that a stock controller may act on.
 *
 * `track` keeps the same call shape but records the failure, and `LoadError`
 * renders it. Callers should also gate their empty-state text on `!error`, so
 * the two can never be confused.
 *
 * ONE HOOK, SEVERAL LOADERS. The first version of this kept a single error
 * string and cleared it on ANY success, which was fine for the one-list panels
 * it was written for and wrong for the three that share an instance across
 * concurrent fetches — IndustrialConnectivity runs protocols, devices and
 * signals through one hook; GmatsInventory runs the item list and the stock
 * summary. Whichever request settled last decided what the user saw, so a
 * success at 150ms erased a failure from 100ms and the failed panel went back
 * to rendering its reassuring empty state. That is the #383 bug returning
 * through a different door.
 *
 * So failures are keyed by the `what` label the caller already passes. A loader
 * clears only its OWN entry, and the message names every loader currently down.
 * Nothing about the call shape changed.
 */

/** "protocols" / "protocols and signals" / "protocols, devices and signals". */
function listPhrase(items: readonly string[]): string {
  if (items.length <= 1) return items[0] ?? "";
  return `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`;
}

export function useLoadError() {
  // `failed` is an ARRAY, not a Set or an object, because the order the loaders
  // failed in is the order they are named in, and it has to be stable across
  // re-renders. (Object key order is only insertion-ordered for non-integer-like
  // keys, and a `what` label is caller-supplied text — not worth the footgun.)
  const [state, setState] = useState<{ failed: string[]; manual: string | null }>({
    failed: [],
    manual: null,
  });

  const track = useCallback(
    <T,>(promise: Promise<T>, apply: (value: T) => void, what: string) =>
      promise
        .then((value) => {
          apply(value);
          setState((prev) =>
            // Returning `prev` unchanged when this loader was not failing keeps
            // the state identity stable, so a healthy poll round every few
            // seconds does not re-render every panel that uses the hook.
            prev.failed.includes(what)
              ? { ...prev, failed: prev.failed.filter((f) => f !== what) }
              : prev,
          );
        })
        .catch(() =>
          setState((prev) =>
            // Already recorded: keep the original position rather than moving it
            // to the end. usePolling retries these every few seconds, and a
            // message whose word order churns on every failed round is harder to
            // read than one that stays put.
            prev.failed.includes(what)
              ? prev
              : { ...prev, failed: [...prev.failed, what] },
          ),
        ),
    [],
  );

  // Kept for source compatibility — it has no callers in this repo today. A
  // non-null value is an override that wins over the tracked loaders; null is
  // the reset, and it clears the tracked failures too, which is the only
  // reading of `setError(null)` that is not a footgun now that per-loader state
  // exists.
  const setError = useCallback((message: string | null) => {
    setState((prev) => (message === null ? { failed: [], manual: null } : { ...prev, manual: message }));
  }, []);

  const error =
    state.manual ??
    (state.failed.length
      ? `Could not load ${listPhrase(state.failed)}. Check your connection and try again.`
      : null);

  return { error, track, setError };
}

export function LoadError({ message }: { message: string | null }) {
  if (!message) return null;
  return (
    <p role="alert" className="text-red-400 text-sm">
      {message}
    </p>
  );
}
