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
 */
export function useLoadError() {
  const [error, setError] = useState<string | null>(null);

  const track = useCallback(
    <T,>(promise: Promise<T>, apply: (value: T) => void, what: string) =>
      promise
        .then((value) => {
          apply(value);
          setError(null);
        })
        .catch(() => setError(`Could not load ${what}. Check your connection and try again.`)),
    [],
  );

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
