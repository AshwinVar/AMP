"use client";

import "../phase29-enterprise.css";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";

import { getToken } from "../../lib/api";
import {
  fetchCustomers,
  fetchFleet,
  fetchMachine,
  fetchMachineService,
  fetchOemIdentity,
  fetchServiceQueue,
  fleetSummary,
  OemRequestError,
  shareable,
  type FleetMachine,
  type OemIdentity,
  type ServiceRecommendation,
} from "../../lib/oem";

/**
 * The OEM portal (ADR-0017).
 *
 * A manufacturer is a DIFFERENT PERSONA from a factory user — different login,
 * different branding, different questions — so this is its own route rather than
 * another tab inside the factory dashboard.
 *
 * The screen's one hard rule: NEVER RENDER A PRIVACY SETTING AS AN OPERATIONAL
 * FACT. A customer who declined to share operating hours must not appear here as
 * a machine at 0 h, and a customer who declined to share health must not appear
 * as a machine that has gone offline. Both would send an engineer to a site
 * where nothing is wrong, and both are one careless `?? 0` away.
 *
 * Nothing on this page decides what anybody may see. The server already bound a
 * tenant no factory can hold and applied the sharing policy per field; this only
 * decides how to say what came back.
 */

type Customer = {
  customer: string;
  site: string;
  machines: number;
  active: number;
  shared: string[];
};

type MachineService = Awaited<ReturnType<typeof fetchMachineService>>;

const SEVERITY_TONE: Record<string, string> = {
  high: "text-red-400 border-red-500/40 bg-red-500/10",
  medium: "text-amber-300 border-amber-500/40 bg-amber-500/10",
  low: "text-slate-300 border-slate-600/50 bg-slate-700/20",
};

function Tile({
  label,
  value,
  hint,
}: {
  label: string;
  value: number | string;
  hint?: string;
}) {
  return (
    <div className="rounded-xl border border-slate-800 bg-slate-900/60 p-4">
      <p className="text-slate-500 text-xs uppercase tracking-wide">{label}</p>
      <p className="text-2xl font-semibold text-white mt-1">{value}</p>
      {hint && <p className="text-slate-500 text-[11px] mt-1">{hint}</p>}
    </div>
  );
}

export default function OemPortalPage() {
  const router = useRouter();

  const [identity, setIdentity] = useState<OemIdentity | null>(null);
  const [machines, setMachines] = useState<FleetMachine[]>([]);
  const [customers, setCustomers] = useState<Customer[]>([]);
  const [queue, setQueue] = useState<ServiceRecommendation[]>([]);
  const [customerFilter, setCustomerFilter] = useState("");
  const [selected, setSelected] = useState<FleetMachine | null>(null);
  const [detail, setDetail] = useState<MachineService | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [notOem, setNotOem] = useState(false);

  // Promise callbacks rather than `await`, matching the loaders elsewhere in
  // this app (see lib/useLoadError). Every state update therefore lands in a
  // callback after the requests are in flight, and none runs synchronously as
  // the effect executes — a synchronous setState in an effect body costs a
  // second render before the browser has painted the first.
  const load = useCallback(
    (customer: string) =>
      Promise.all([
        fetchOemIdentity(),
        fetchFleet(customer || undefined),
        fetchCustomers(),
        fetchServiceQueue(),
      ])
        .then(([me, fleet, custs, service]) => {
          setError("");
          setIdentity(me);
          setMachines(fleet.machines);
          setCustomers(custs.customers);
          setQueue(service.recommendations);
        })
        .catch((e: unknown) => {
          // WHETHER THIS IS AN OEM SESSION IS THE SERVER'S ANSWER, NOT THE
          // TOKEN'S. `isOemSession()` reads a claim and is only good enough to
          // pick a link; the backend re-reads the principal from the database on
          // every request, so a suspended manufacturer account still holds a
          // token that SAYS `principal: "oem"`. Deciding here on the 401 means
          // the screen agrees with the authority instead of with a stale claim.
          const denied = e instanceof OemRequestError && e.status === 401;
          if (denied) {
            setNotOem(true);
          } else {
            // A failure must never render as an empty fleet. "You have no
            // machines" is a plausible answer a service desk would act on, and
            // it is the wrong one.
            setError(e instanceof Error ? e.message : "Could not load the fleet");
          }
        })
        .finally(() => setLoading(false)),
    [],
  );

  useEffect(() => {
    if (!getToken()) {
      router.push("/login");
      return;
    }
    load(customerFilter);
  }, [router, load, customerFilter]);

  async function openMachine(machine: FleetMachine) {
    setSelected(machine);
    setDetail(null);
    try {
      const [full, service] = await Promise.all([
        fetchMachine(machine.installation_id),
        fetchMachineService(machine.installation_id),
      ]);
      setSelected(full);
      setDetail(service);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load that machine");
    }
  }

  if (notOem) {
    return (
      <main className="min-h-screen bg-slate-950 text-slate-200 flex items-center justify-center p-8">
        <div className="max-w-md text-center">
          <h1 className="text-2xl font-semibold text-white">
            This is the manufacturer portal
          </h1>
          <p className="text-slate-400 mt-2 text-sm">
            Your session is a factory session. The equipment your suppliers have
            installed — and what they can see about it — is under{" "}
            <strong>Connected Equipment</strong> in your own dashboard.
          </p>
          <button
            type="button"
            onClick={() => router.push("/dashboard")}
            className="mt-6 rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white"
          >
            Go to the dashboard
          </button>
        </div>
      </main>
    );
  }

  const summary = fleetSummary(machines);
  const brand = identity?.branding;

  return (
    <main className="min-h-screen bg-slate-950 text-slate-200 p-6 sm:p-10">
      {/* White label: ONE build, appearance from configuration. A per-OEM fork
          would mean every security fix lands N times, and the Nth is the one
          that gets forgotten. */}
      <header className="flex flex-wrap items-center justify-between gap-4 border-b border-slate-800 pb-6">
        <div className="flex items-center gap-4">
          {brand?.logo_url ? (
            // eslint-disable-next-line @next/next/no-img-element
            <img
              src={brand.logo_url}
              alt=""
              className="h-10 w-10 rounded object-contain"
            />
          ) : (
            <div
              className="h-10 w-10 rounded flex items-center justify-center text-lg font-semibold text-white"
              style={{ background: brand?.color || "#1d4ed8" }}
            >
              {(brand?.name || "?").slice(0, 1)}
            </div>
          )}
          <div>
            <h1 className="text-xl font-semibold text-white">
              {brand?.name || "Manufacturer portal"}
            </h1>
            <p className="text-slate-500 text-xs">
              Connected equipment, powered by AMP
              {identity ? ` · ${identity.username} · ${identity.role}` : ""}
            </p>
          </div>
        </div>
        <div className="text-right text-xs text-slate-500">
          {brand?.support_email && <div>{brand.support_email}</div>}
          {brand?.support_phone && <div>{brand.support_phone}</div>}
        </div>
      </header>

      {error && (
        <p role="alert" className="mt-6 text-sm text-red-400">
          {error}
        </p>
      )}

      {loading && !identity && (
        <p className="mt-6 text-sm text-slate-500">Loading your fleet…</p>
      )}

      {identity && (
        <>
          <section className="mt-6 grid gap-3 grid-cols-2 lg:grid-cols-5">
            <Tile label="Machines" value={summary.total} />
            <Tile label="Active" value={summary.active} />
            <Tile
              label="Reporting"
              value={summary.connected}
              hint="seen in the last 48 h"
            />
            <Tile
              label="Silent"
              value={summary.offline}
              hint="reported, then stopped"
            />
            {/* The important tile. A machine whose owner has not shared health is
                UNKNOWN, not offline — counting it as offline invents a fleet
                problem out of a privacy setting. */}
            <Tile
              label="Not shared"
              value={summary.unknown}
              hint="customer has not shared health"
            />
          </section>

          <section className="mt-8">
            <h2 className="text-lg font-semibold text-white">Service queue</h2>
            <p className="text-slate-500 text-xs mt-1">
              Arithmetic over what each machine reported. Nothing here is a
              prediction, and nothing here uses a customer&apos;s production data.
            </p>
            {queue.length === 0 && !error ? (
              <p className="mt-3 text-sm text-slate-500">
                Nothing needs attention.
              </p>
            ) : (
              <ul className="mt-3 space-y-2">
                {queue.slice(0, 12).map((r, i) => (
                  <li
                    key={`${r.machine}-${r.kind}-${i}`}
                    className="rounded-xl border border-slate-800 bg-slate-900/60 p-4"
                  >
                    <div className="flex flex-wrap items-center gap-2">
                      <span
                        className={`rounded border px-2 py-0.5 text-[11px] ${
                          SEVERITY_TONE[r.severity] ?? SEVERITY_TONE.low
                        }`}
                      >
                        {r.severity}
                      </span>
                      <span className="font-mono text-xs text-slate-400">
                        {r.machine}
                      </span>
                      <span className="text-sm text-slate-200">{r.reason}</span>
                    </div>
                    <p className="mt-1 text-xs text-slate-500">{r.evidence}</p>
                    <p className="mt-1 text-xs text-slate-400">→ {r.action}</p>
                    {/* Confidence is shown only when the backend supplied one.
                        A projection has one; arithmetic does not, and printing
                        "100%" beside a subtraction would be decoration. */}
                    {r.confidence !== null && (
                      <p className="mt-1 text-[11px] text-slate-500">
                        projection confidence {Math.round(r.confidence * 100)}%
                        — a straight line through an hours counter, not a model
                      </p>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section className="mt-8">
            <h2 className="text-lg font-semibold text-white">Customers</h2>
            <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {customers.map((c) => (
                <button
                  type="button"
                  key={`${c.customer}-${c.site}`}
                  onClick={() =>
                    setCustomerFilter(customerFilter === c.customer ? "" : c.customer)
                  }
                  className={`rounded-xl border p-4 text-left ${
                    customerFilter === c.customer
                      ? "border-blue-500/50 bg-blue-500/5"
                      : "border-slate-800 bg-slate-900/60"
                  }`}
                >
                  <p className="text-white font-medium">{c.customer}</p>
                  <p className="text-slate-500 text-xs">{c.site || "—"}</p>
                  <p className="text-slate-400 text-sm mt-2">
                    {c.machines} {c.machines === 1 ? "machine" : "machines"} ·{" "}
                    {c.active} active
                  </p>
                  <p className="text-[11px] text-slate-500 mt-2">
                    {c.shared.length === 0
                      ? "Shares nothing with you yet — ask them to enable it under Connected Equipment"
                      : `Shares ${c.shared.length}: ${c.shared
                          .map((g) => g.replace("SHARE_", "").toLowerCase().replace(/_/g, " "))
                          .join(", ")}`}
                  </p>
                </button>
              ))}
            </div>
          </section>

          <section className="mt-8">
            <div className="flex items-baseline justify-between">
              <h2 className="text-lg font-semibold text-white">
                Installed fleet
                {customerFilter && (
                  <span className="text-slate-500 text-sm"> · {customerFilter}</span>
                )}
              </h2>
              {customerFilter && (
                <button
                  type="button"
                  onClick={() => setCustomerFilter("")}
                  className="text-xs text-slate-400 underline"
                >
                  show all customers
                </button>
              )}
            </div>
            <div className="mt-3 overflow-x-auto rounded-xl border border-slate-800 bg-slate-900/60">
              <table className="w-full text-sm">
                <thead className="text-slate-500 text-xs uppercase tracking-wide">
                  <tr>
                    <th className="text-left p-3">Serial</th>
                    <th className="text-left p-3">Model</th>
                    <th className="text-left p-3">Customer</th>
                    <th className="text-left p-3">Site</th>
                    <th className="text-left p-3">Lifecycle</th>
                    <th className="text-left p-3">Hours</th>
                    <th className="text-left p-3">Status</th>
                  </tr>
                </thead>
                <tbody>
                  {machines.map((m) => {
                    const sharesHours = m.shared.includes("SHARE_OPERATING_HOURS");
                    const sharesHealth = m.shared.includes("SHARE_MACHINE_HEALTH");
                    return (
                      <tr
                        key={m.installation_id}
                        onClick={() => openMachine(m)}
                        className="border-t border-slate-800 cursor-pointer hover:bg-slate-800/40"
                      >
                        <td className="p-3 font-mono text-xs">{m.serial_number}</td>
                        <td className="p-3">{m.model_code ?? "—"}</td>
                        <td className="p-3">{m.customer ?? "unassigned"}</td>
                        <td className="p-3">{m.site || "—"}</td>
                        <td className="p-3">{m.lifecycle_status}</td>
                        <td className="p-3 text-slate-400">
                          {shareable(m.operating_hours, sharesHours, " h")}
                        </td>
                        <td className="p-3 text-slate-400">
                          {shareable(m.machine_status, sharesHealth)}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
              {machines.length === 0 && !error && !loading && (
                <p className="p-6 text-sm text-slate-500">
                  No installations recorded for this manufacturer yet.
                </p>
              )}
            </div>
          </section>
        </>
      )}

      {selected && (
        <div
          className="fixed inset-0 bg-black/60 flex justify-end"
          onClick={() => {
            setSelected(null);
            setDetail(null);
          }}
        >
          <div
            role="dialog"
            aria-label={`Machine ${selected.serial_number}`}
            className="w-full max-w-lg h-full overflow-y-auto bg-slate-900 border-l border-slate-800 p-6"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-start justify-between">
              <div>
                <h2 className="text-lg font-semibold text-white font-mono">
                  {selected.serial_number}
                </h2>
                <p className="text-slate-500 text-xs">
                  {selected.model_name ?? selected.model_code ?? "—"} ·{" "}
                  {selected.customer ?? "unassigned"} · {selected.site || "—"}
                </p>
              </div>
              <button
                type="button"
                onClick={() => {
                  setSelected(null);
                  setDetail(null);
                }}
                className="text-slate-400 text-sm"
              >
                Close
              </button>
            </div>

            {/* What the customer has NOT shared is STATED, not silently omitted.
                An engineer who cannot tell "no data" from "not shared" reads a
                blank as a broken machine and sends somebody to site. */}
            {selected.not_shared && selected.not_shared.length > 0 && (
              <p className="mt-4 rounded-lg border border-slate-800 bg-slate-950/60 p-3 text-xs text-slate-400">
                This customer has not shared:{" "}
                {selected.not_shared
                  .map((g) => g.replace("SHARE_", "").toLowerCase().replace(/_/g, " "))
                  .join(", ")}
                . Those fields are blank because permission was not given, not
                because the machine is faulty.
              </p>
            )}

            {detail ? (
              <div className="mt-4 space-y-4 text-sm">
                <div>
                  <h3 className="text-white font-medium">Service</h3>
                  <p className="text-slate-400">{detail.service.reason}</p>
                </div>
                <div>
                  <h3 className="text-white font-medium">Warranty</h3>
                  <p className="text-slate-400">{detail.warranty.reason}</p>
                </div>
                <div>
                  <h3 className="text-white font-medium">
                    Commissioning{" "}
                    <span className="text-slate-500 text-xs">
                      {detail.commissioning.ready ? "· complete" : "· incomplete"}
                    </span>
                  </h3>
                  <ul className="mt-1 space-y-1">
                    {detail.commissioning.checks.map((c) => (
                      <li key={c.key} className="text-slate-400 text-xs">
                        <span className={c.passed ? "text-green-400" : "text-amber-300"}>
                          {c.passed ? "✓" : "✗"}
                        </span>{" "}
                        {c.description} — <span className="text-slate-500">{c.detail}</span>
                      </li>
                    ))}
                  </ul>
                </div>
                <div>
                  <h3 className="text-white font-medium">Telemetry</h3>
                  {detail.telemetry_profile_error ? (
                    <p className="text-amber-300 text-xs">
                      This model&apos;s telemetry profile could not be read:{" "}
                      {detail.telemetry_profile_error}
                    </p>
                  ) : (
                    <p className="text-slate-400 text-xs">
                      {detail.telemetry_signals.length === 0
                        ? "No telemetry profile defined for this model."
                        : detail.telemetry_signals.join(", ")}
                    </p>
                  )}
                </div>
                {detail.recommendations.length > 0 && (
                  <div>
                    <h3 className="text-white font-medium">Recommendations</h3>
                    <ul className="mt-1 space-y-2">
                      {detail.recommendations.map((r, i) => (
                        <li key={`${r.kind}-${i}`} className="text-xs">
                          <p className="text-slate-200">{r.reason}</p>
                          <p className="text-slate-500">{r.evidence}</p>
                          <p className="text-slate-400">→ {r.action}</p>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
              </div>
            ) : (
              <p className="mt-4 text-sm text-slate-500">Loading…</p>
            )}
          </div>
        </div>
      )}
    </main>
  );
}
