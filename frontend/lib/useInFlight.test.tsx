import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { useInFlight } from "./useInFlight";

const deferred = () => {
  let resolve!: () => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<void>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
};

/**
 * Pins the behaviour fixed in #385/#387: double-clicking Save posted twice, and
 * for downtime logs, shift data and escalations - tables with no unique key -
 * that created a duplicate row, double-counting minutes and output into OEE.
 *
 * The subtle part is *which* mechanism guards. A `useState` flag is not enough:
 * the second click lands before React re-renders, so the flag would still read
 * false. The guard has to be a ref. These tests are written to fail if someone
 * "simplifies" it back to state.
 */
describe("useInFlight", () => {
  it("ignores a second submit while the first is still in flight", async () => {
    const { result } = renderHook(() => useInFlight());
    const post = vi.fn();
    const first = deferred();

    // Two clicks with NO await between them - the real double-click, where React
    // has had no chance to re-render and disable anything.
    let firstRun!: Promise<boolean>;
    let secondRun!: Promise<boolean>;
    act(() => {
      firstRun = result.current.run("wo", async () => {
        post();
        await first.promise;
      });
      secondRun = result.current.run("wo", async () => {
        post();
      });
    });

    expect(await secondRun).toBe(false); // rejected as re-entry
    expect(post).toHaveBeenCalledTimes(1);

    await act(async () => {
      first.resolve();
      await firstRun;
    });
    expect(post).toHaveBeenCalledTimes(1);
  });

  it("releases the key afterwards, so the form still works", async () => {
    const { result } = renderHook(() => useInFlight());
    const post = vi.fn();

    await act(async () => {
      await result.current.run("wo", async () => { post(); });
    });
    await act(async () => {
      await result.current.run("wo", async () => { post(); });
    });

    // A guard that latched would be a worse bug than the double-post it replaced.
    expect(post).toHaveBeenCalledTimes(2);
  });

  it("releases the key even when the handler throws", async () => {
    const { result } = renderHook(() => useInFlight());
    const post = vi.fn();

    await act(async () => {
      await result.current
        .run("wo", async () => { throw new Error("network down"); })
        .catch(() => {});
    });

    // Without the finally, one failed save would lock the form for the session.
    await act(async () => {
      await result.current.run("wo", async () => { post(); });
    });
    expect(post).toHaveBeenCalledTimes(1);
  });

  it("keys are independent, so one form does not block another", async () => {
    const { result } = renderHook(() => useInFlight());
    const downtime = vi.fn();
    const shift = vi.fn();
    const held = deferred();

    let downtimeRun!: Promise<boolean>;
    act(() => {
      downtimeRun = result.current.run("downtime-log", async () => {
        downtime();
        await held.promise;
      });
    });

    // Shared keys would make two unrelated forms block each other - the failure
    // mode of the mechanical 18-site rollout in #387.
    await act(async () => {
      await result.current.run("shift", async () => { shift(); });
    });

    expect(shift).toHaveBeenCalledTimes(1);
    await act(async () => {
      held.resolve();
      await downtimeRun;
    });
    expect(downtime).toHaveBeenCalledTimes(1);
  });

  it("reports busy only while in flight, for the disabled/Saving label", async () => {
    const { result } = renderHook(() => useInFlight());
    const held = deferred();

    expect(result.current.busy("wo")).toBe(false);

    let run!: Promise<boolean>;
    await act(async () => {
      run = result.current.run("wo", async () => { await held.promise; });
    });
    expect(result.current.busy("wo")).toBe(true);
    expect(result.current.busy("other")).toBe(false);

    await act(async () => {
      held.resolve();
      await run;
    });
    expect(result.current.busy("wo")).toBe(false);
  });
});
