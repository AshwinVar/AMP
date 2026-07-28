import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { useLoadError } from "./useLoadError";

/**
 * Pins the behaviour fixed in #383: a failed list fetch used to render as
 * "you have no data", because `.catch(() => {})` left `rows` at `[]` and the
 * component showed its empty state. A dropped connection, an expired token and
 * a 500 all looked like an empty inventory.
 *
 * These were verified by hand in a browser at the time. Encoding them here is
 * the point of adding a test runner.
 */
describe("useLoadError", () => {
  it("records a failure so the caller can render it instead of an empty state", async () => {
    const { result } = renderHook(() => useLoadError());
    const apply = vi.fn();

    await act(async () => {
      await result.current.track(Promise.reject(new Error("boom")), apply, "remnants");
    });

    expect(apply).not.toHaveBeenCalled();
    expect(result.current.error).toContain("remnants");
  });

  it("clears the error on the next success", async () => {
    const { result } = renderHook(() => useLoadError());
    const apply = vi.fn();

    await act(async () => {
      await result.current.track(Promise.reject(new Error("boom")), apply, "remnants");
    });
    expect(result.current.error).not.toBeNull();

    // A stale error left over a successful reload would be its own bug: the user
    // would see "could not load" above rows that had just loaded fine.
    await act(async () => {
      await result.current.track(Promise.resolve([1, 2]), apply, "remnants");
    });

    expect(result.current.error).toBeNull();
    expect(apply).toHaveBeenCalledWith([1, 2]);
  });

  it("names the thing that failed, so one page can distinguish its loaders", async () => {
    const { result } = renderHook(() => useLoadError());

    await act(async () => {
      await result.current.track(Promise.reject(new Error("x")), vi.fn(), "cycle-count history");
    });

    expect(result.current.error).toContain("cycle-count history");
  });

  it("applies the value and stays clean when the request succeeds first time", async () => {
    const { result } = renderHook(() => useLoadError());
    const apply = vi.fn();

    await act(async () => {
      await result.current.track(Promise.resolve(["a"]), apply, "items");
    });

    expect(result.current.error).toBeNull();
    expect(apply).toHaveBeenCalledWith(["a"]);
  });
});
