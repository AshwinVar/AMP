import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { connectLiveSocket } from "./live";

/**
 * The live socket carries machine status onto the dashboard. It had no
 * reconnect at all: `onclose` reported "disconnected" and stopped there, so the
 * feed stayed dead until someone reloaded the page.
 *
 * That is not a rare event. Railway redeploys the backend on every merge to
 * master, which happens several times a day, and every redeploy drops every
 * open socket. A shop-floor screen left up all day would go quiet after the
 * first one and never come back - while still looking fine, because the 3s
 * poll keeps the numbers moving.
 *
 * The connect URL was also logged in full, and the URL carries the JWT as a
 * query parameter (browsers cannot set WebSocket auth headers, so the backend
 * reads `?token=` - see main.py:445). That put a working credential in the
 * browser console for anything with console access to pick up.
 */

class FakeSocket {
  static instances: FakeSocket[] = [];

  url: string;
  readyState = 0;
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onerror: ((e: unknown) => void) | null = null;
  onclose: ((e: unknown) => void) | null = null;
  closedByCaller = false;

  constructor(url: string) {
    this.url = url;
    FakeSocket.instances.push(this);
  }

  close() {
    this.closedByCaller = true;
    this.readyState = 3;
  }

  /** Server accepted the connection. */
  open() {
    this.readyState = 1;
    this.onopen?.();
  }

  /** Connection dropped from the other end (redeploy, wifi, idle timeout). */
  drop() {
    this.readyState = 3;
    this.onclose?.({ code: 1006 });
  }
}

const latest = () => FakeSocket.instances[FakeSocket.instances.length - 1];

beforeEach(() => {
  vi.useFakeTimers();
  FakeSocket.instances = [];
  vi.stubGlobal("WebSocket", FakeSocket);
  localStorage.setItem("token", "header.payload.signature");
  vi.spyOn(console, "log").mockImplementation(() => {});
  vi.spyOn(console, "warn").mockImplementation(() => {});
  vi.spyOn(console, "error").mockImplementation(() => {});
  // Midpoint jitter keeps the expected delays exact.
  vi.spyOn(Math, "random").mockReturnValue(0.5);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  localStorage.clear();
});

describe("connectLiveSocket", () => {
  it("opens one socket and reports the connection", () => {
    const onStatus = vi.fn();
    connectLiveSocket(vi.fn(), onStatus);

    expect(FakeSocket.instances).toHaveLength(1);
    latest().open();
    expect(onStatus).toHaveBeenCalledWith("connected");
  });

  it("delivers parsed events to the caller", () => {
    const onEvent = vi.fn();
    connectLiveSocket(onEvent, vi.fn());
    latest().open();

    latest().onmessage?.({ data: JSON.stringify({ event: "heartbeat" }) });

    expect(onEvent).toHaveBeenCalledWith({ event: "heartbeat" });
  });

  it("survives a malformed payload without tearing the socket down", () => {
    const onEvent = vi.fn();
    connectLiveSocket(onEvent, vi.fn());
    latest().open();

    latest().onmessage?.({ data: "not json{" });

    expect(onEvent).not.toHaveBeenCalled();
    expect(FakeSocket.instances).toHaveLength(1);
  });

  it("reconnects after the connection drops", async () => {
    const onStatus = vi.fn();
    connectLiveSocket(vi.fn(), onStatus);
    latest().open();

    latest().drop();
    expect(onStatus).toHaveBeenCalledWith("disconnected");
    expect(FakeSocket.instances).toHaveLength(1); // not immediately - that would hammer

    await vi.advanceTimersByTimeAsync(1000);
    expect(FakeSocket.instances).toHaveLength(2);
  });

  it("backs off exponentially while the server stays down", async () => {
    connectLiveSocket(vi.fn(), vi.fn());
    latest().open();

    latest().drop();
    await vi.advanceTimersByTimeAsync(1000);
    expect(FakeSocket.instances).toHaveLength(2);

    latest().drop(); // second attempt fails too
    await vi.advanceTimersByTimeAsync(1000);
    expect(FakeSocket.instances).toHaveLength(2); // 1s is no longer enough
    await vi.advanceTimersByTimeAsync(1000);
    expect(FakeSocket.instances).toHaveLength(3); // ~2s

    latest().drop();
    await vi.advanceTimersByTimeAsync(3000);
    expect(FakeSocket.instances).toHaveLength(3); // ~4s
    await vi.advanceTimersByTimeAsync(1000);
    expect(FakeSocket.instances).toHaveLength(4);
  });

  it("caps the backoff so a long outage still retries regularly", async () => {
    connectLiveSocket(vi.fn(), vi.fn());
    latest().open();

    for (let i = 0; i < 12; i += 1) {
      latest().drop();
      await vi.advanceTimersByTimeAsync(30_000);
    }

    const before = FakeSocket.instances.length;
    latest().drop();
    await vi.advanceTimersByTimeAsync(30_000);

    // Unbounded doubling would put the next retry hours away - the screen would
    // never come back on its own.
    expect(FakeSocket.instances.length).toBe(before + 1);
  });

  it("resets the backoff once a connection succeeds", async () => {
    connectLiveSocket(vi.fn(), vi.fn());
    latest().open();

    latest().drop();
    await vi.advanceTimersByTimeAsync(1000);
    latest().drop();
    await vi.advanceTimersByTimeAsync(2000);
    latest().open(); // back up

    const count = FakeSocket.instances.length;
    latest().drop();
    await vi.advanceTimersByTimeAsync(1000);

    // A backoff that kept climbing across a healthy period would leave a flaky
    // link retrying every 30s forever.
    expect(FakeSocket.instances).toHaveLength(count + 1);
  });

  it("stops reconnecting once the caller closes it", async () => {
    const onStatus = vi.fn();
    const connection = connectLiveSocket(vi.fn(), onStatus);
    latest().open();

    connection.close();
    onStatus.mockClear();
    latest().drop(); // the close itself fires onclose

    await vi.advanceTimersByTimeAsync(60_000);

    // Leaving the dashboard must not leave a socket respawning behind it.
    expect(FakeSocket.instances).toHaveLength(1);
    // Nor report a disconnect at a caller that has already unmounted - that
    // lands as a setState on a dead component.
    expect(onStatus).not.toHaveBeenCalled();
  });

  it("never runs two sockets at once", async () => {
    connectLiveSocket(vi.fn(), vi.fn());
    latest().open();

    // A drop that somehow fires twice must not schedule two reconnects.
    latest().drop();
    latest().drop();
    await vi.advanceTimersByTimeAsync(5000);

    expect(FakeSocket.instances).toHaveLength(2);
  });

  it("keeps the token out of the console", () => {
    const log = vi.spyOn(console, "log");
    connectLiveSocket(vi.fn(), vi.fn());
    latest().open();
    latest().drop();

    const printed = log.mock.calls.flat().map(String).join(" ");
    expect(printed).not.toContain("header.payload.signature");
  });

  it("still sends the token, it just does not print it", () => {
    connectLiveSocket(vi.fn(), vi.fn());
    expect(latest().url).toContain("token=header.payload.signature");
  });

  it("reports an error, and leaves the reconnect to the close that follows", async () => {
    const onStatus = vi.fn();
    connectLiveSocket(vi.fn(), onStatus);
    latest().open();
    onStatus.mockClear();

    // A rejected handshake (expired JWT, backend mid-redeploy) surfaces as
    // onerror. Without this the status dot stays on its last value and the
    // dashboard looks healthy while the feed is dead.
    latest().onerror?.({});
    expect(onStatus).toHaveBeenCalledWith("error");

    // A socket can report an error and stay up. Reconnecting off onerror would
    // throw away a working connection - and when the error IS fatal, onclose
    // fires straight after, so we would have scheduled the retry twice.
    await vi.advanceTimersByTimeAsync(5000);
    expect(FakeSocket.instances).toHaveLength(1);

    // Control for the assertion above: the retry path is alive, it is just
    // driven by close rather than error.
    latest().drop();
    await vi.advanceTimersByTimeAsync(1000);
    expect(FakeSocket.instances).toHaveLength(2);
  });

  it("reconnects even when the caller wants events and no status", async () => {
    const onEvent = vi.fn();
    connectLiveSocket(onEvent); // onStatus is optional in the signature

    latest().open();
    latest().onerror?.({});
    latest().drop();
    await vi.advanceTimersByTimeAsync(1000);

    // If any of those handlers called onStatus unguarded, the TypeError would
    // escape before scheduleRetry and this caller would silently lose its feed
    // for the rest of the session.
    expect(FakeSocket.instances).toHaveLength(2);
    latest().open();
    latest().onmessage?.({ data: JSON.stringify({ event: "machine_status" }) });
    expect(onEvent).toHaveBeenCalledWith({ event: "machine_status" });
  });

  it("cancels a reconnect that is already pending when the caller closes", async () => {
    const connection = connectLiveSocket(vi.fn(), vi.fn());
    latest().open();
    // Counted as a delta, not an absolute: earlier cases in this file leave
    // their own timers parked on the clock.
    const idle = vi.getTimerCount();

    latest().drop();
    // Control: without a genuinely armed timer here, "it cancelled the retry"
    // would pass just as well against a client that never retried at all.
    expect(vi.getTimerCount()).toBe(idle + 1);

    connection.close();

    // close() is documented as stopping for good. Leaving the timer armed means
    // every mount of the dashboard hands the tab a wake-up that outlives it -
    // navigate back and forth a few times and they stack up.
    expect(vi.getTimerCount()).toBe(idle);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(FakeSocket.instances).toHaveLength(1);
  });

  it("omits the token entirely when there is none to send", () => {
    localStorage.removeItem("token");

    connectLiveSocket(vi.fn(), vi.fn());

    // Before login (or after a logout) there is no JWT. Sending `?token=null`
    // would hand the backend a string it has to reject rather than an honest
    // anonymous handshake.
    expect(latest().url).not.toContain("token=");
    expect(latest().url).toContain("/ws/live");
  });
});
