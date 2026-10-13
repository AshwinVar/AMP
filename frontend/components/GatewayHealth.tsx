"use client";

import { useCallback, useEffect, useState } from "react";

import { apiGet } from "../lib/api";
import {
  LIGHT_LABEL,
  LIGHT_STYLE,
  type GatewayHealth,
  type MachineLink,
  linkFor,
  worthBanner,
} from "../lib/gateway-health";

/**
 * Whether AMP can see the plant, on the two screens people actually watch.
 *
 * A machine with no bars today can be an idle press, a press whose contact is
 * unplugged, or a Raspberry Pi in a panel with no power — and a dashboard that
 * only draws numbers shows all three identically. On an unattended floor that
 * is discovered the day after, when the figures come up short.
 *
 * The verdict is the BACKEND's (ai/gateway_health.py). Nothing here decides what
 * a colour means; it renders what it was told, so the Machines cards and the
 * Plant Board cannot drift into disagreeing.
 */

/**
 * Accept the payload only if it is SHAPED like one.
 *
 * This decorates two screens that must keep working without it, so anything
 * else — an older backend, a proxy's error page, a mock that answers every
 * path — becomes "no health to show" rather than a crash. It was a crash
 * first: the board rendered an empty page because `gateways` was undefined
 * and the banner called `.filter` on it.
 */
function asHealth(raw: unknown): GatewayHealth | null {
  if (!raw || typeof raw !== "object") return null;
  const h = raw as Partial<GatewayHealth>;
  if (!Array.isArray(h.gateways) || !Array.isArray(h.machines)) return null;
  if (typeof h.headline !== "string" || typeof h.light !== "string") return null;
  return h as GatewayHealth;
}

/** One poll, shared by whoever asks. The board and the machine cards both want
 *  it, and two independent timers would make them disagree by up to a minute. */
export function useGatewayHealth(pollMs = 60_000) {
  const [health, setHealth] = useState<GatewayHealth | null>(null);

  const load = useCallback(async () => {
    try {
      setHealth(asHealth(await apiGet<unknown>("/analytics/gateway-health")));
    } catch {
      // A failed health poll must not blank the screen it decorates: the
      // production figures are still true, and the badge simply says nothing.
      setHealth(null);
    }
  }, []);

  useEffect(() => {
    let alive = true;
    const first = setTimeout(() => {
      if (alive) void load();
    }, 0);
    const timer = setInterval(() => {
      if (alive) void load();
    }, pollMs);
    return () => {
      alive = false;
      clearTimeout(first);
      clearInterval(timer);
    };
  }, [load, pollMs]);

  return health;
}

/** The pill on a machine card or a board row. */
export function LinkBadge({ link }: { link: MachineLink | null }) {
  if (!link) return null;
  return (
    <span
      title={link.reason + (link.fix ? " — " + link.fix : "")}
      className={"text-xs px-2 py-0.5 rounded-full border " + LIGHT_STYLE[link.light]}
    >
      {LIGHT_LABEL[link.state]}
      {link.state === "MACHINE SILENT" && link.quiet_minutes != null
        ? " · " + link.quiet_minutes + "m"
        : ""}
    </span>
  );
}

/**
 * The bar across the top. Shown only when there is something to say — a plant
 * where everything reports does not need telling, and a banner that is always
 * there is a banner nobody reads.
 */
export function GatewayBanner({ health }: { health: GatewayHealth | null }) {
  if (!worthBanner(health) || !health) return null;
  const down = health.gateways.filter((g) => g.state === "GATEWAY UNREACHABLE");
  const fix = down[0]?.fix || health.machines.find((m) => m.fix)?.fix || "";
  return (
    <div
      role="status"
      className={"mb-4 rounded-xl border p-4 text-sm " + LIGHT_STYLE[health.light]}
    >
      <p className="font-semibold">{health.headline}</p>
      {fix ? <p className="mt-1 opacity-80">{fix}</p> : null}
      <p className="mt-2 text-xs opacity-70">
        {health.reporting} of {health.total} machines reporting
      </p>
    </div>
  );
}

/** The badge for one machine, looked up by id. */
export function MachineLinkBadge({
  health,
  machineId,
}: {
  health: GatewayHealth | null;
  machineId: number;
}) {
  return <LinkBadge link={linkFor(health, machineId)} />;
}
