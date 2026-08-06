import { act, fireEvent, render, renderHook, screen } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { LoadError, useLoadError } from "./useLoadError";

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

/**
 * ONE HOOK, SEVERAL LOADERS — the case #383 did not cover.
 *
 * Three shipped panels share a single useLoadError() across concurrent fetches:
 * IndustrialConnectivity runs three (protocols, devices, signals) and
 * GmatsInventory runs two (item list, stock summary). The hook kept ONE error
 * slot and every success cleared it unconditionally, so the last request to
 * settle decided what the user saw.
 *
 * Both directions are wrong, and both are reachable on a normal page load:
 *
 *   fail then succeed — the failure is erased, the failed list stays [], and the
 *     panel renders its empty state with nothing wrong showing. That is exactly
 *     the #383 symptom (a failure reading as "you have no data") coming back
 *     through the shared slot rather than through `.catch(() => {})`.
 *
 *   succeed then fail — an error is painted over rows that loaded perfectly
 *     well, which teaches people to ignore the error bar.
 *
 * These use manually-settled promises rather than timers because the ORDER of
 * settlement is the whole variable; a sleep would only make it probabilistic.
 */
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  // Attach a no-op catch so a rejection settled before the hook's own handler
  // runs cannot surface as an unhandled rejection and fail the suite for the
  // wrong reason.
  promise.catch(() => {});
  return { promise, resolve, reject };
}

describe("one hook shared by several loaders", () => {
  it("keeps a failure when a DIFFERENT loader succeeds after it", async () => {
    // IndustrialConnectivity, exactly: /industrial/protocols 500s, then
    // /industrial/devices comes back fine 50ms later. The user must still be
    // told protocols failed — otherwise the protocols table renders empty and
    // reads as "this plant has no protocols configured".
    const { result } = renderHook(() => useLoadError());
    const applyProtocols = vi.fn();
    const applyDevices = vi.fn();

    const protocols = deferred<string[]>();
    const devices = deferred<string[]>();

    let tracked: Promise<unknown>;
    await act(async () => {
      tracked = Promise.all([
        result.current.track(protocols.promise, applyProtocols, "protocols"),
        result.current.track(devices.promise, applyDevices, "devices"),
      ]);
    });

    await act(async () => {
      protocols.reject(new Error("500"));
      await Promise.resolve();
    });
    expect(result.current.error).toContain("protocols");

    await act(async () => {
      devices.resolve(["plc-1"]);
      await tracked;
    });

    // The good loader applied its rows — the fix must not cost us that.
    expect(applyDevices).toHaveBeenCalledWith(["plc-1"]);
    expect(applyProtocols).not.toHaveBeenCalled();
    // ...and the failure it knows nothing about is still on screen.
    expect(result.current.error).toContain("protocols");
  });

  it("does not paint an error over a loader that succeeded, when a different one fails later", async () => {
    // The converse, and the reason this cannot be fixed by simply never
    // clearing: a slow failure must name ITS OWN loader, not condemn the whole
    // panel. Without this control, "keep every error forever" would pass the
    // test above.
    const { result } = renderHook(() => useLoadError());
    const applyItems = vi.fn();

    const items = deferred<string[]>();
    const summary = deferred<string[]>();

    let tracked: Promise<unknown>;
    await act(async () => {
      tracked = Promise.all([
        result.current.track(items.promise, applyItems, "the item list"),
        result.current.track(summary.promise, vi.fn(), "the stock summary"),
      ]);
    });

    await act(async () => {
      items.resolve(["A-100"]);
      await Promise.resolve();
    });
    expect(result.current.error).toBeNull();

    await act(async () => {
      summary.reject(new Error("500"));
      await tracked;
    });

    expect(applyItems).toHaveBeenCalledWith(["A-100"]);
    expect(result.current.error).toContain("the stock summary");
    // The one that worked is not blamed.
    expect(result.current.error).not.toContain("the item list");
  });

  it("names every loader that is currently failing, not just the newest", async () => {
    // Two of the three panels on IndustrialConnectivity can fail together — a
    // dropped connection takes all of them. Reporting only the last one to land
    // sends the operator chasing a single endpoint when the network is down.
    const { result } = renderHook(() => useLoadError());

    await act(async () => {
      await Promise.all([
        result.current.track(Promise.reject(new Error("x")), vi.fn(), "protocols"),
        result.current.track(Promise.reject(new Error("x")), vi.fn(), "signals"),
      ]);
    });

    expect(result.current.error).toContain("protocols");
    expect(result.current.error).toContain("signals");
  });

  it("recovers cleanly once every failing loader has succeeded again", async () => {
    // CONTROL. Without it, "remember every failure that ever happened" would
    // satisfy all three tests above and leave a permanent error bar on a healthy
    // page — the mirror image of the bug, and just as misleading.
    const { result } = renderHook(() => useLoadError());

    await act(async () => {
      await Promise.all([
        result.current.track(Promise.reject(new Error("x")), vi.fn(), "protocols"),
        result.current.track(Promise.reject(new Error("x")), vi.fn(), "signals"),
      ]);
    });
    expect(result.current.error).not.toBeNull();

    await act(async () => {
      await result.current.track(Promise.resolve([]), vi.fn(), "protocols");
    });
    // One recovered, one still down: still an error, and it names the right one.
    expect(result.current.error).toContain("signals");
    expect(result.current.error).not.toContain("protocols");

    await act(async () => {
      await result.current.track(Promise.resolve([]), vi.fn(), "signals");
    });
    expect(result.current.error).toBeNull();
  });

  it("does not accumulate a loader that keeps failing", async () => {
    // usePolling re-runs these loaders every few seconds. If each failed round
    // appended, the message would grow without bound: "Could not load protocols,
    // protocols, protocols and protocols."
    const { result } = renderHook(() => useLoadError());

    for (let i = 0; i < 3; i++) {
      await act(async () => {
        await result.current.track(Promise.reject(new Error("x")), vi.fn(), "protocols");
      });
    }

    expect(result.current.error).toBe(
      "Could not load protocols. Check your connection and try again.",
    );
  });

  it("reads naturally however many loaders are down", async () => {
    // The message is assembled rather than concatenated, because
    // "Could not load protocols, devices, signals." is the kind of sentence
    // that gets ignored. Three is the real case — IndustrialConnectivity.
    const { result } = renderHook(() => useLoadError());

    await act(async () => {
      await Promise.all(
        ["protocols", "devices", "signals"].map((what) =>
          result.current.track(Promise.reject(new Error("x")), vi.fn(), what),
        ),
      );
    });

    expect(result.current.error).toBe(
      "Could not load protocols, devices and signals. Check your connection and try again.",
    );
  });
});

describe("useLoadError's manual setError", () => {
  // It has no callers in this repo today, but it is exported, so its behaviour
  // is a contract someone can rely on — and after the per-loader change there
  // are two plausible readings of it. These pin the one that shipped.

  it("lets a caller override what the tracked loaders are saying", async () => {
    const { result } = renderHook(() => useLoadError());

    await act(async () => {
      await result.current.track(Promise.reject(new Error("x")), vi.fn(), "protocols");
    });
    expect(result.current.error).toContain("protocols");

    act(() => result.current.setError("Your session expired. Sign in again."));

    // A caller who knows something more specific than "check your connection"
    // wins — otherwise the generic message would bury the actionable one.
    expect(result.current.error).toBe("Your session expired. Sign in again.");
  });

  it("resets the tracked loaders too, not just the override", async () => {
    // The footgun this closes: if setError(null) cleared only the override, a
    // caller trying to dismiss the error bar would watch it immediately repaint
    // itself from state they cannot see.
    const { result } = renderHook(() => useLoadError());

    await act(async () => {
      await Promise.all([
        result.current.track(Promise.reject(new Error("x")), vi.fn(), "protocols"),
        result.current.track(Promise.reject(new Error("x")), vi.fn(), "signals"),
      ]);
    });
    act(() => result.current.setError("something else"));

    act(() => result.current.setError(null));

    expect(result.current.error).toBeNull();
  });
});

/**
 * `LoadError` is the half of #383 the user actually sees, and until now nothing
 * exercised it. The hook can record a failure perfectly and the fix still ships
 * broken if the component decides not to render it — the reassuring empty state
 * is then the only thing on the page again, which is the exact bug #383 closed.
 */
describe("LoadError", () => {
  it("announces the failure rather than leaving it as text to scroll past", () => {
    render(<LoadError message="Could not load remnants. Check your connection and try again." />);

    // role="alert" is the load-bearing part: it is what makes a stock controller
    // who is already looking at the table hear about a failure that happened
    // above the fold, instead of reading a stale row count as fact.
    const alert = screen.getByRole("alert");
    expect(alert.textContent).toBe("Could not load remnants. Check your connection and try again.");
  });

  it("renders nothing at all when there is no error", () => {
    // Control for the case above: that test proves the alert DOES render for a
    // real message, so "no alert" here means suppressed, not "this component
    // never renders anything".
    //
    // And it has to be nothing, not an empty alert. Every caller drops
    // <LoadError message={error} /> straight into a stacked column
    // (GmatsInventory, EnterpriseInventory, IndustrialConnectivity), so an
    // always-rendered <p> would open a gap under the heading of every healthy
    // page and park a permanently-live alert region in the DOM.
    const { container } = render(<LoadError message={null} />);

    expect(container.firstChild).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("treats an empty message as no error, not as an error with nothing to say", () => {
    // `setError` is exported, so a caller can push back "" from a response body
    // that had no detail in it. A `message === null` guard would let that
    // through and paint an empty red bar the user can neither read nor act on.
    const { container } = render(<LoadError message="" />);

    expect(container.firstChild).toBeNull();
  });
});

/**
 * A list that legitimately renders empty is where #383 actually bit, so drive
 * the hook and the component together the way a real panel wires them.
 */
function RemnantPanel({ load }: { load: () => Promise<number[]> }) {
  const [rows, setRows] = useState<number[]>([]);
  const { error, track } = useLoadError();

  return (
    <div>
      <button onClick={() => void track(load(), setRows, "remnants")}>Reload</button>
      <LoadError message={error} />
      {/* The gating the hook's doc comment asks callers for. */}
      {!error && rows.length === 0 && <p>No remnants logged yet.</p>}
      <ul>
        {rows.map((r) => (
          <li key={r}>Remnant {r}</li>
        ))}
      </ul>
    </div>
  );
}

describe("useLoadError driving LoadError", () => {
  it("puts the failure in place of the empty state, and takes it back down on a good reload", async () => {
    const load = vi
      .fn()
      .mockRejectedValueOnce(new Error("network down"))
      .mockResolvedValueOnce([7]);

    render(<RemnantPanel load={load} />);
    // The empty state is genuinely reachable — otherwise the assertion below
    // that it disappeared would pass against a panel that never showed it.
    expect(screen.getByText("No remnants logged yet.")).toBeTruthy();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Reload" }));
    });

    // "No remnants logged yet." after a dropped request is the sentence a stock
    // controller may act on. It must be gone, and the reason must be on screen.
    expect(screen.getByRole("alert").textContent).toContain("remnants");
    expect(screen.queryByText("No remnants logged yet.")).toBeNull();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Reload" }));
    });

    // A banner left standing over rows that just loaded fine is how operators
    // learn to ignore the banner, so the retry has to clear it.
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByText("Remnant 7")).toBeTruthy();
  });
});
