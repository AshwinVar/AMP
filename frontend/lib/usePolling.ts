import { useEffect, useRef } from "react";

/**
 * Poll without letting rounds pile up.
 *
 * The dashboard refreshes with `setInterval(fetchAll, 3000)` and `fetchAll`
 * issues ~47 requests a round. `setInterval` does not care whether the previous
 * callback finished, so as soon as a round took longer than the interval — and
 * `/analytics/executive-oee` scans every production record, downtime log and
 * quality inspection with no bound — the next round started on top of it.
 * Browsers cap concurrent connections per origin at around six, so the surplus
 * queues instead of failing, the queue grows for as long as the tab is open,
 * and each extra round slows the backend further, making the next overlap more
 * likely. It compounds exactly when the system is already struggling, once per
 * open tab.
 *
 * So: skip a tick whose predecessor has not finished. As in `useInFlight`
 * (#385), the guard has to be a ref — a state flag would not have been updated
 * yet when the next tick fires.
 */
export function usePolling(
  run: () => Promise<void> | void,
  intervalMs: number,
  enabled = true,
) {
  const inFlight = useRef(false);
  // Kept in a ref so the interval always calls the current callback without
  // having to tear the timer down and rebuild it on every render.
  const latest = useRef(run);
  latest.current = run;

  useEffect(() => {
    if (!enabled) return;

    let cancelled = false;

    const tick = async () => {
      if (inFlight.current) return; // previous round still going — skip this one
      inFlight.current = true;
      try {
        await latest.current();
      } catch {
        // A dropped connection must not stop the dashboard until a reload; the
        // callback reports its own failures (see useLoadError, #383).
      } finally {
        // Releasing even when cancelled keeps the flag honest; a latched guard
        // would silently freeze the dashboard, which is worse than the overlap.
        inFlight.current = false;
      }
    };

    void tick();
    const id = setInterval(() => {
      if (!cancelled) void tick();
    }, intervalMs);

    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [intervalMs, enabled]);
}
