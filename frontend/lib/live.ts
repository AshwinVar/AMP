export type LiveEvent = {
  event: string;
  message?: string;

  machine?: {
    id: number;
    name: string;
    status: string;
    utilization: number;
    downtime?: string;
  };

  production?: {
    total_count: number;
    good_count: number;
    rejected_count: number;
  };

  timeline?: {
    old_status?: string;
    new_status: string;
  };

  source?: string;
};

export type LiveConnection = {
  /** Stop for good: cancels any pending retry and closes the socket. */
  close: () => void;
};

// The backend refuses a connection it cannot authenticate, using the
// application close-code range so a refusal cannot be confused with a network
// drop (backend/ws_auth.py). Retrying a refusal is pointless — the same token
// will be refused again — and with the schedule below it would mean a request
// every 30 seconds, forever, from every revoked session.
const REFUSAL_CODES = new Set([
  4400, // the server accepts no client frames
  4401, // no token, malformed, or expired — needs a NEW credential
  4403, // account gone, disabled, or the token's workspace does not match
]);

// Retry schedule. Doubling from a second, capped so a long outage still gets a
// try every half minute rather than drifting to hours away.
const BASE_DELAY_MS = 1000;
const MAX_DELAY_MS = 30_000;
// Every open tab drops at the same instant when the backend redeploys; without
// jitter they would all come back in lockstep.
const JITTER = 0.2;

function socketUrl() {
  const apiBase = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";
  const token = typeof window !== "undefined" ? localStorage.getItem("token") : null;

  // The JWT travels as a query parameter because browsers cannot set auth
  // headers on a WebSocket handshake; the backend reads ?token= (main.py:445).
  // That makes this URL a live credential — never log it.
  return (
    apiBase.replace(/^http/, "ws") +
    "/ws/live" +
    (token ? `?token=${encodeURIComponent(token)}` : "")
  );
}

/**
 * Connect to the live feed, and keep it connected.
 *
 * The previous version reported `disconnected` and gave up, so the feed stayed
 * dead until someone reloaded the page. Railway redeploys the backend on every
 * merge to master and drops every open socket with it, so a screen left up on
 * the shop floor went quiet after the first deploy of the day — invisibly,
 * because the 3s poll keeps the numbers moving either way.
 */
export function connectLiveSocket(
  onEvent: (event: LiveEvent) => void,
  onStatus?: (status: "connected" | "disconnected" | "error" | "refused") => void,
): LiveConnection {
  let socket: WebSocket | null = null;
  let retryTimer: ReturnType<typeof setTimeout> | null = null;
  let attempt = 0;
  let stopped = false;

  const scheduleRetry = () => {
    // One pending retry at a time — a duplicated close event must not fan out
    // into two sockets.
    if (stopped || retryTimer !== null) return;

    const capped = Math.min(BASE_DELAY_MS * 2 ** attempt, MAX_DELAY_MS);
    const delay = Math.round(capped * (1 + (Math.random() * 2 - 1) * JITTER));
    attempt += 1;

    retryTimer = setTimeout(() => {
      retryTimer = null;
      open();
    }, delay);
  };

  const open = () => {
    if (stopped) return;

    const ws = new WebSocket(socketUrl());
    socket = ws;

    ws.onopen = () => {
      // A healthy connection earns a fresh budget; otherwise a link that drops
      // once an hour would climb to the 30s ceiling and stay there.
      attempt = 0;
      onStatus?.("connected");
    };

    ws.onmessage = (message) => {
      try {
        onEvent(JSON.parse(message.data));
      } catch (error) {
        console.error("Invalid WebSocket payload", error);
      }
    };

    ws.onerror = () => {
      onStatus?.("error");
    };

    ws.onclose = (event) => {
      if (stopped) return;
      if (REFUSAL_CODES.has(event.code)) {
        // Refused, not dropped. Stop for good and say so — reporting
        // "disconnected" here would look like a flaky network and hide the
        // real cause (an expired session, or an account that was revoked).
        stopped = true;
        onStatus?.("refused");
        return;
      }
      onStatus?.("disconnected");
      scheduleRetry();
    };
  };

  open();

  return {
    close: () => {
      stopped = true;
      if (retryTimer !== null) {
        clearTimeout(retryTimer);
        retryTimer = null;
      }
      socket?.close();
    },
  };
}
