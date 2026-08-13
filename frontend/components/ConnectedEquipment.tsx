"use client";

import React, { useCallback, useEffect, useState } from "react";

import { apiGet, apiPost, apiPut, getUserRole } from "../lib/api";
import { LoadError, useLoadError } from "../lib/useLoadError";
import { useInFlight } from "../lib/useInFlight";
import AddConnectedEquipment from "./AddConnectedEquipment";

/**
 * The FACTORY's side of the OEM edge (ADR-0017).
 *
 * The manufacturer gets a portal showing its fleet. This is the other half, and
 * the half that makes the arrangement legitimate: what the customer can see
 * about the manufacturers on their shop floor, and how they take it back.
 *
 * A consent control nobody can find is not consent. So this panel refuses to be
 * coy about the negative case — what is NOT shared is rendered as plainly as
 * what is, because a screen that lists four green ticks and silently omits the
 * other three reads as "we share everything".
 *
 * Every write goes to PUT /connected-equipment/sharing, which is Admin-only and
 * audited server-side with a before/after. Nothing here is the security
 * boundary; a non-Admin's controls are disabled because showing somebody a
 * switch that will be refused is a worse experience, not because the disabling
 * protects anything.
 */

type ServiceState = {
  state: string;
  reason: string;
  hours_remaining?: number | null;
};

type WarrantyState = {
  state: string;
  reason: string;
  days_remaining: number | null;
};

export type Equipment = {
  installation_id: number;
  serial_number: string;
  manufacturer: string;
  manufacturer_name: string;
  support_email: string | null;
  model_code: string | null;
  model_name: string | null;
  site: string;
  machine_id: number | null;
  lifecycle_status: string;
  commissioned_at: string | null;
  warranty: WarrantyState;
  service: ServiceState;
  shared_with_manufacturer: string[];
};

export type Manufacturer = {
  oem_code: string;
  name: string;
  machines: number;
  granted: string[];
};

export type ConnectedEquipmentResponse = {
  equipment: Equipment[];
  manufacturers: Manufacturer[];
  available_grants: Array<{ key: string; label: string }>;
};

const STATE_TONE: Record<string, string> = {
  overdue: "text-red-400 border-red-500/40 bg-red-500/10",
  due: "text-amber-300 border-amber-500/40 bg-amber-500/10",
  due_soon: "text-amber-300 border-amber-500/40 bg-amber-500/10",
  expired: "text-red-400 border-red-500/40 bg-red-500/10",
  ok: "text-green-400 border-green-500/40 bg-green-500/10",
  active: "text-green-400 border-green-500/40 bg-green-500/10",
  // "unknown" and "not_configured" are deliberately GREY, not red. The machine
  // is not unhealthy — AMP has not been told, and colouring an absence of
  // information as a fault is how a fleet acquires imaginary problems.
  unknown: "text-slate-400 border-slate-600/50 bg-slate-700/20",
  not_configured: "text-slate-400 border-slate-600/50 bg-slate-700/20",
  not_started: "text-slate-400 border-slate-600/50 bg-slate-700/20",
};

function tone(state: string) {
  return STATE_TONE[state] ?? "text-slate-400 border-slate-600/50 bg-slate-700/20";
}

function label(state: string) {
  return state.replace(/_/g, " ");
}

/** Just enough of a shop-floor machine to offer it in the link list. */
type FloorMachine = { id: number; name: string; site?: string | null };

export default function ConnectedEquipment() {
  const [data, setData] = useState<ConnectedEquipmentResponse | null>(null);
  const [roster, setRoster] = useState<FloorMachine[]>([]);
  const [draft, setDraft] = useState<Record<string, string[]>>({});
  const [saved, setSaved] = useState<Record<string, string>>({});
  const [failed, setFailed] = useState<Record<string, string>>({});
  const [linkFailed, setLinkFailed] = useState<Record<number, string>>({});

  const isAdmin = getUserRole() === "Admin";
  const { error, track } = useLoadError();
  const { run, busy } = useInFlight();

  const load = useCallback(() => {
    track(
      Promise.all([
        apiGet<ConnectedEquipmentResponse>("/connected-equipment"),
        apiGet<FloorMachine[]>("/machines"),
      ]),
      ([response, machines]) => {
        setData(response);
        setRoster(machines);
        // The draft is re-seeded from the server on every load, so a grant an
        // administrator on another screen withdrew cannot linger here as a
        // ticked box that quietly re-grants it on the next save.
        setDraft(
          Object.fromEntries(
            response.manufacturers.map((m) => [m.oem_code, [...m.granted]]),
          ),
        );
      },
      "connected equipment",
    );
  }, [track]);

  useEffect(() => {
    load();
  }, [load]);

  function toggle(oemCode: string, grant: string) {
    setDraft((prev) => {
      const current = prev[oemCode] ?? [];
      return {
        ...prev,
        [oemCode]: current.includes(grant)
          ? current.filter((g) => g !== grant)
          : [...current, grant],
      };
    });
    setSaved((prev) => ({ ...prev, [oemCode]: "" }));
    setFailed((prev) => ({ ...prev, [oemCode]: "" }));
  }

  async function save(oemCode: string) {
    await run(`sharing:${oemCode}`, async () => {
      setSaved((prev) => ({ ...prev, [oemCode]: "" }));
      setFailed((prev) => ({ ...prev, [oemCode]: "" }));
      try {
        // The list is ABSOLUTE, not additive: it replaces what was granted, so
        // withdrawal is an empty list and needs no separate verb.
        await apiPut("/connected-equipment/sharing", {
          oem_code: oemCode,
          grants: draft[oemCode] ?? [],
        });
        setSaved((prev) => ({ ...prev, [oemCode]: "Saved. This takes effect immediately." }));
        load();
      } catch (e) {
        // The backend's own reason, not a generic one. "That manufacturer has no
        // equipment installed here" and "Unknown sharing grants: X" need
        // different reactions, and a swallowed message makes both look like an
        // outage.
        const raw = e instanceof Error ? e.message : String(e);
        let detail = raw;
        try {
          const parsed = JSON.parse(raw);
          if (parsed?.detail) detail = String(parsed.detail);
        } catch {
          /* a non-JSON error body is still an error; keep the text */
        }
        setFailed((prev) => ({ ...prev, [oemCode]: detail }));
      }
    });
  }

  /**
   * Say which machine on this floor a supplied serial is (ADR-0019).
   *
   * The manufacturer cannot do this — it does not know, and letting it choose
   * would let it point at a machine it never built. So the choice is here, on
   * the factory's own screen, and it is the step that lets its manufacturer
   * commission the machine.
   */
  async function link(installationId: number, machineId: number | null) {
    await run(`link:${installationId}`, async () => {
      setLinkFailed((prev) => ({ ...prev, [installationId]: "" }));
      try {
        await apiPost(`/connected-equipment/${installationId}/link`, {
          machine_id: machineId,
        });
        load();
      } catch (e) {
        // "PRESS-1 is already linked to serial SN-002" is the whole point of
        // the message — a generic failure would leave somebody re-picking the
        // same machine forever.
        const raw = e instanceof Error ? e.message : String(e);
        let detail = raw;
        try {
          const parsed = JSON.parse(raw);
          if (parsed?.detail) detail = String(parsed.detail);
        } catch {
          /* a non-JSON error body is still an error; keep the text */
        }
        setLinkFailed((prev) => ({ ...prev, [installationId]: detail }));
      }
    });
  }

  const grants = data?.available_grants ?? [];

  return (
    <section className="mt-8 space-y-6">
      <div>
        <h2 className="text-2xl font-semibold text-white">Connected Equipment</h2>
        <p className="text-slate-400 text-sm mt-1 max-w-3xl">
          Machines on your shop floor that came from an equipment manufacturer, and
          exactly what each manufacturer can see about them. Nothing is shared
          until you share it, and withdrawing takes effect on their next request.
        </p>
      </div>

      <LoadError message={error} />

      {/* THE WAY EQUIPMENT GETS HERE (ADR-0019). A manufacturer cannot attach a
          machine to this workspace; it can only send a code. This is where that
          code becomes an installation — Admin-only, because it establishes a
          commercial relationship and a data-sharing agreement in one press. */}
      {isAdmin && <AddConnectedEquipment onAdded={load} />}

      {!error && data && data.equipment.length === 0 && (
        <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-10 text-center">
          <div className="text-3xl mb-3">◈</div>
          <h3 className="text-lg font-semibold text-white">
            No manufacturer-connected equipment
          </h3>
          <p className="text-slate-400 mt-1 text-sm">
            When a supplier registers a machine they sold you, it appears here
            along with the controls for what they may see.
          </p>
        </div>
      )}

      {data?.manufacturers.map((m) => {
        const chosen = draft[m.oem_code] ?? [];
        const withheld = grants.filter((g) => !chosen.includes(g.key));
        const dirty =
          [...chosen].sort().join(",") !== [...m.granted].sort().join(",");
        return (
          <div
            key={m.oem_code}
            className="rounded-2xl border border-slate-800 bg-slate-900/60 p-6"
          >
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <div>
                <h3 className="text-lg font-semibold text-white">{m.name}</h3>
                <p className="text-slate-500 text-xs mt-0.5">
                  {m.oem_code} · {m.machines}{" "}
                  {m.machines === 1 ? "machine" : "machines"} here
                </p>
              </div>
              <p className="text-slate-400 text-sm">
                {m.granted.length === 0
                  ? "Currently shares nothing beyond its own sales record"
                  : `Currently shares ${m.granted.length} of ${grants.length}`}
              </p>
            </div>

            <div className="mt-4 grid gap-2 sm:grid-cols-2">
              {grants.map((g) => (
                <label
                  key={g.key}
                  className={`flex items-start gap-3 rounded-lg border p-3 text-sm ${
                    chosen.includes(g.key)
                      ? "border-blue-500/40 bg-blue-500/5 text-slate-200"
                      : "border-slate-800 bg-slate-900 text-slate-400"
                  } ${isAdmin ? "cursor-pointer" : "cursor-not-allowed opacity-70"}`}
                >
                  <input
                    type="checkbox"
                    className="mt-0.5"
                    checked={chosen.includes(g.key)}
                    disabled={!isAdmin}
                    onChange={() => toggle(m.oem_code, g.key)}
                  />
                  <span>
                    {g.label}
                    <span className="block text-[11px] text-slate-500">{g.key}</span>
                  </span>
                </label>
              ))}
            </div>

            {/* The negative case, stated. A list of what IS shared, with the rest
                silently absent, reads as "everything". */}
            <p className="mt-3 text-xs text-slate-500">
              {withheld.length === 0
                ? "Nothing is withheld from this manufacturer."
                : `Not shared: ${withheld.map((g) => g.label).join(", ")}.`}
            </p>
            <p className="mt-1 text-xs text-slate-500">
              No setting here can reveal work orders, production quantities,
              recipes, BOMs, inventory, costs, customers, operators or quality
              detail.
            </p>

            {isAdmin ? (
              <div className="mt-4 flex flex-wrap items-center gap-3">
                <button
                  type="button"
                  onClick={() => save(m.oem_code)}
                  disabled={!dirty || busy(`sharing:${m.oem_code}`)}
                  className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white disabled:opacity-40"
                >
                  {busy(`sharing:${m.oem_code}`) ? "Saving…" : "Save sharing"}
                </button>
                {chosen.length > 0 && (
                  <button
                    type="button"
                    onClick={() =>
                      setDraft((prev) => ({ ...prev, [m.oem_code]: [] }))
                    }
                    className="rounded-lg border border-slate-700 px-4 py-2 text-sm text-slate-300"
                  >
                    Withdraw everything
                  </button>
                )}
                {saved[m.oem_code] && (
                  <span className="text-sm text-green-400">{saved[m.oem_code]}</span>
                )}
                {failed[m.oem_code] && (
                  <span className="text-sm text-red-400">{failed[m.oem_code]}</span>
                )}
              </div>
            ) : (
              <p className="mt-4 text-xs text-slate-500">
                Only an Admin can change what a manufacturer sees.
              </p>
            )}
          </div>
        );
      })}

      {data && data.equipment.length > 0 && (
        <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-6 overflow-x-auto">
          <h3 className="text-lg font-semibold text-white mb-4">
            Equipment on site
          </h3>
          <table className="w-full text-sm">
            <thead className="text-slate-500 text-xs uppercase tracking-wide">
              <tr>
                <th className="text-left pb-2">Serial</th>
                <th className="text-left pb-2">Manufacturer</th>
                <th className="text-left pb-2">Model</th>
                <th className="text-left pb-2">Site</th>
                <th className="text-left pb-2">Machine on floor</th>
                <th className="text-left pb-2">Lifecycle</th>
                <th className="text-left pb-2">Warranty</th>
                <th className="text-left pb-2">Service</th>
              </tr>
            </thead>
            <tbody className="text-slate-300">
              {data.equipment.map((e) => (
                <tr key={e.installation_id} className="border-t border-slate-800">
                  <td className="py-2 font-mono text-xs">{e.serial_number}</td>
                  <td className="py-2">
                    {e.manufacturer_name}
                    {e.support_email && (
                      <span className="block text-[11px] text-slate-500">
                        {e.support_email}
                      </span>
                    )}
                  </td>
                  <td className="py-2">{e.model_code ?? "—"}</td>
                  <td className="py-2">{e.site || "—"}</td>
                  <td className="py-2">
                    <select
                      aria-label={`Machine linked to ${e.serial_number}`}
                      value={e.machine_id ?? ""}
                      disabled={!isAdmin || busy(`link:${e.installation_id}`)}
                      onChange={(ev) =>
                        link(
                          e.installation_id,
                          ev.target.value === "" ? null : Number(ev.target.value),
                        )
                      }
                      className="rounded border border-slate-700 bg-slate-950 px-2 py-1 text-xs text-slate-200 disabled:opacity-50"
                    >
                      <option value="">Not linked</option>
                      {roster.map((m) => (
                        <option key={m.id} value={m.id}>
                          {m.name}
                        </option>
                      ))}
                    </select>
                    {linkFailed[e.installation_id] && (
                      <span
                        role="alert"
                        className="block text-[11px] text-red-400 mt-1 max-w-[16rem]"
                      >
                        {linkFailed[e.installation_id]}
                      </span>
                    )}
                    {!e.machine_id && !linkFailed[e.installation_id] && (
                      <span className="block text-[11px] text-slate-500 mt-1">
                        needed before its maker can commission it
                      </span>
                    )}
                  </td>
                  <td className="py-2">{e.lifecycle_status}</td>
                  <td className="py-2">
                    <span
                      className={`inline-block rounded border px-2 py-0.5 text-xs ${tone(
                        e.warranty.state,
                      )}`}
                      title={e.warranty.reason}
                    >
                      {label(e.warranty.state)}
                    </span>
                  </td>
                  <td className="py-2">
                    <span
                      className={`inline-block rounded border px-2 py-0.5 text-xs ${tone(
                        e.service.state,
                      )}`}
                      title={e.service.reason}
                    >
                      {label(e.service.state)}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
