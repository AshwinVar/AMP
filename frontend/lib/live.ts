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
export function connectLiveSocket(
  onEvent: (event: LiveEvent) => void,
  onStatus?: (
    status: "connected" | "disconnected" | "error"
  ) => void
) {
  const apiBase = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";
  const wsUrl = apiBase.replace(/^http/, "ws") + "/ws/live";

  console.log("Connecting WebSocket:", wsUrl);

  const socket = new WebSocket(wsUrl);

  socket.onopen = () => {
    console.log("FlowMES WebSocket connected");
    onStatus?.("connected");
  };

  socket.onmessage = (message) => {
    try {
      const event = JSON.parse(message.data);

      console.log("WS EVENT:", event);

      onEvent(event);
    } catch (error) {
      console.error(
        "Invalid WebSocket payload",
        error
      );
    }
  };

  socket.onerror = (error) => {
    console.error(
      "FlowMES WebSocket error",
      error
    );

    onStatus?.("error");
  };

  socket.onclose = (event) => {
    console.warn(
      "FlowMES WebSocket disconnected",
      event
    );

    onStatus?.("disconnected");
  };

  return socket;
}