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

/**
 * Polling continued at full rate in a background tab.
 *
 * The dashboard's round is ~47 requests every three seconds. A shop-floor
 * machine with the dashboard parked behind other tabs, or a manager who leaves
 * it open all day and works elsewhere, kept issuing ~15 requests a second at a
 * screen nobody was looking at - per tab, for as long as the browser was open.
 * Nothing about it is visible to the user either way, which is exactly why it
 * went unnoticed.
 *
 * Pausing while hidden changes nothing anyone can perceive: a hidden tab shows
 * no data, and the moment it comes back the hook refetches immediately rather
 * than making the user wait out the interval on stale numbers.
 */
function setVisibility(state: "visible" | "hidden") {
  Object.defineProperty(document, "visibilityState", {
    value: state,
    configurable: true,
  });
  document.dispatchEvent(new Event("visibilitychange"));
}

describe("usePolling while the tab is hidden", () => {
  afterEach(() => {
    Object.defineProperty(document, "visibilityState", {
      value: "visible",
      configurable: true,
    });
  });

  it("stops polling once the tab is hidden", async () => {
    const run = vi.fn().mockResolvedValue(undefined);
    renderHook(() => usePolling(run, 3000));
    expect(run).toHaveBeenCalledTimes(1);

    await act(async () => setVisibility("hidden"));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000); // ten rounds' worth
    });

    expect(run).toHaveBeenCalledTimes(1);
  });

  it("refetches the moment the tab comes back, without waiting for the interval", async () => {
    const run = vi.fn().mockResolvedValue(undefined);
    renderHook(() => usePolling(run, 3000));

    await act(async () => setVisibility("hidden"));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    expect(run).toHaveBeenCalledTimes(1);

    // Whatever is on screen is now half a minute stale; make it current before
    // the user reads it.
    await act(async () => setVisibility("visible"));
    expect(run).toHaveBeenCalledTimes(2);
  });

  it("resumes the normal cadence after coming back", async () => {
    const run = vi.fn().mockResolvedValue(undefined);
    renderHook(() => usePolling(run, 3000));

    await act(async () => setVisibility("hidden"));
    await act(async () => setVisibility("visible"));
    const afterReturn = run.mock.calls.length;

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });

    // A pause that never lifted would be worse than the waste it replaced.
    expect(run).toHaveBeenCalledTimes(afterReturn + 1);
  });

  it("does not fetch at all when it mounts in a background tab", async () => {
    Object.defineProperty(document, "visibilityState", { value: "hidden", configurable: true });
    const run = vi.fn().mockResolvedValue(undefined);
    renderHook(() => usePolling(run, 3000));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(9000);
    });

    expect(run).toHaveBeenCalledTimes(0);
  });

  it("stops listening for visibility changes after unmount", async () => {
    // The `cancelled` flag already stops a leaked listener from *doing*
    // anything, so behaviour alone cannot tell the two apart. What a leak
    // actually costs is memory: the listener keeps the effect's closure - and
    // through it the caller's callback - alive on `document` for the life of
    // the page. So assert the removal itself.
    const add = vi.spyOn(document, "addEventListener");
    const remove = vi.spyOn(document, "removeEventListener");

    const run = vi.fn().mockResolvedValue(undefined);
    const { unmount } = renderHook(() => usePolling(run, 3000));

    const added = add.mock.calls.filter(([type]) => type === "visibilitychange");
    expect(added).toHaveLength(1);

    unmount();

    const removed = remove.mock.calls.filter(([type]) => type === "visibilitychange");
    expect(removed).toHaveLength(1);
    // Same handler reference, or the removal is a no-op.
    expect(removed[0][1]).toBe(added[0][1]);

    await act(async () => setVisibility("hidden"));
    await act(async () => setVisibility("visible"));
    expect(run).toHaveBeenCalledTimes(1);

    add.mockRestore();
    remove.mockRestore();
  });

  it("still refuses to overlap a round that is in flight when the tab returns", async () => {
    const slow = deferred();
    const run = vi.fn().mockReturnValue(slow.promise);
    renderHook(() => usePolling(run, 3000));
    expect(run).toHaveBeenCalledTimes(1);

    await act(async () => setVisibility("hidden"));
    await act(async () => setVisibility("visible"));

    // The in-flight guard outranks the come-back refetch.
    expect(run).toHaveBeenCalledTimes(1);

    await act(async () => {
      slow.resolve();
      await slow.promise;
    });
  });
});
