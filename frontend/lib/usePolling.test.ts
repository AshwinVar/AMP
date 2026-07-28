import { renderHook, act } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { usePolling } from "./usePolling";

/**
 * The dashboard refreshes itself with `setInterval(fetchAll, 3000)`, and
 * `fetchAll` issues ~47 requests per round (three in a `Promise.all`, the rest
 * in a `Promise.allSettled`).
 *
 * There was no guard against a round still being in flight. `setInterval` does
 * not care whether the previous callback finished, so the moment a round took
 * longer than three seconds - and `/analytics/executive-oee` scans every
 * production record, downtime log and quality inspection with no bound - the
 * next round started on top of it. Browsers cap concurrent connections per
 * origin at around six, so the extra requests queue rather than fail, the queue
 * grows for as long as the tab is open, and every extra round makes the backend
 * slower, which makes the next overlap more likely. It compounds exactly when
 * the system is already struggling, and it does it per open tab.
 *
 * These tests drive the clock directly rather than waiting on real time.
 */

const deferred = () => {
  let resolve!: () => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<void>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
};

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("usePolling", () => {
  it("runs once immediately, without waiting for the first interval", async () => {
    const run = vi.fn().mockResolvedValue(undefined);
    renderHook(() => usePolling(run, 3000));

    // The dashboard must not be blank for three seconds on load.
    expect(run).toHaveBeenCalledTimes(1);
  });

  it("runs again on each tick once the previous round has finished", async () => {
    const run = vi.fn().mockResolvedValue(undefined);
    renderHook(() => usePolling(run, 3000));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    expect(run).toHaveBeenCalledTimes(2);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    expect(run).toHaveBeenCalledTimes(3);
  });

  it("skips ticks while a round is still in flight", async () => {
    // The headline case. A slow round used to have a second, third and fourth
    // round piled on top of it.
    const slow = deferred();
    const run = vi.fn().mockReturnValue(slow.promise);
    renderHook(() => usePolling(run, 3000));

    expect(run).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(9000); // three ticks pass
    });

    expect(run).toHaveBeenCalledTimes(1);

    await act(async () => {
      slow.resolve();
      await slow.promise;
    });
  });

  it("resumes on the next tick after a slow round finishes", async () => {
    const slow = deferred();
    const run = vi.fn().mockReturnValueOnce(slow.promise).mockResolvedValue(undefined);
    renderHook(() => usePolling(run, 3000));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(9000);
      slow.resolve();
      await slow.promise;
    });

    // A guard that latched would be worse than the overlap it replaced: the
    // dashboard would silently stop updating.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    expect(run).toHaveBeenCalledTimes(2);
  });

  it("keeps polling after a round rejects", async () => {
    const failing = vi.fn().mockRejectedValueOnce(new Error("network blip"));
    failing.mockResolvedValue(undefined);
    renderHook(() => usePolling(failing, 3000));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });

    // One dropped connection must not stop the dashboard until a reload.
    expect(failing).toHaveBeenCalledTimes(2);
  });

  it("stops when the component unmounts", async () => {
    const run = vi.fn().mockResolvedValue(undefined);
    const { unmount } = renderHook(() => usePolling(run, 3000));

    unmount();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(9000);
    });

    expect(run).toHaveBeenCalledTimes(1);
  });

  it("does not start at all when disabled", async () => {
    const run = vi.fn().mockResolvedValue(undefined);
    renderHook(() => usePolling(run, 3000, false));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(9000);
    });

    // The dashboard skips polling entirely when there is no token.
    expect(run).toHaveBeenCalledTimes(0);
  });

  it("always calls the latest callback, not the one captured on mount", async () => {
    const first = vi.fn().mockResolvedValue(undefined);
    const second = vi.fn().mockResolvedValue(undefined);
    const { rerender } = renderHook(({ fn }) => usePolling(fn, 3000), {
      initialProps: { fn: first },
    });

    rerender({ fn: second });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });

    expect(second).toHaveBeenCalled();
  });
});
