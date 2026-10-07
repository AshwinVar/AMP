"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import { apiGet, apiPost, errorDetail } from "../lib/api";
import { CURRENCY } from "../lib/money";
import { todayLocalIso, type PartSpec, type ToolAssignment } from "../lib/plant-board";

/**
 * THE PART MASTER — the figures no controller on the floor knows.
 *
 * A moulding machine counts shots. Everything the plant actually wants is a
 * conversion away from that number, and not one of the conversions exists in any
 * controller: what the part weighs, how many cavities are running, what a good
 * cycle takes, what the part sells for. The customer has all four — on paper and
 * in their head — so this is where they put them in.
 *
 * A DROPDOWN WHERE AMP HAS SEEN THE ANSWER BEFORE, A TEXT BOX WHERE IT HAS NOT.
 * Material is the clearest case: after the first part there is a list worth
 * offering, but a plant that brings in a new grade must not be stuck. So the
 * dropdown always carries an "Other" that reveals a free-text box, rather than
 * AMP deciding in advance what materials exist.
 *
 * THE DERIVED RATE IS SHOWN WHILE TYPING. Cavities and cycle time produce an
 * hourly target that every green bar on the board is then judged against, and a
 * transposed digit there is invisible afterwards. Showing "14,400 parts/hour" as
 * it is typed turns a silent error into an obvious one — a person who knows the
 * machine will recognise a wrong number immediately.
 */

const OTHER = "__other__";

type Draft = {
  part_code: string;
  part_name: string;
  material: string;
  materialOther: string;
  part_weight_g: string;
  cavities: string;
  active_cavities: string;
  ideal_cycle_time_s: string;
  price_per_piece: string;
  effective_from: string;
};

function blank(): Draft {
  return {
    part_code: "",
    part_name: "",
    material: "",
    materialOther: "",
    part_weight_g: "",
    cavities: "",
    active_cavities: "",
    ideal_cycle_time_s: "",
    price_per_piece: "",
    effective_from: todayLocalIso(),
  };
}

/**
 * The hourly target the entered figures imply — the same arithmetic the backend
 * does, shown while typing so a wrong digit is caught before it is saved.
 * Returns null when the inputs cannot produce a rate, rather than 0, because a
 * target of zero is a claim and "not yet" is not.
 */
export function previewRate(cycleSeconds: string, activeCavities: string): number | null {
  const cycle = Number(cycleSeconds);
  const cavities = Number(activeCavities);
  if (!Number.isFinite(cycle) || cycle <= 0) return null;
  if (!Number.isFinite(cavities) || cavities <= 0) return null;
  return Math.round((3600 / cycle) * cavities);
}

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <label className="block text-xs">
      <span className="block text-slate-400">{label}</span>
      {children}
      {hint ? <span className="mt-1 block text-[11px] text-slate-600">{hint}</span> : null}
    </label>
  );
}

const INPUT =
  "mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-white " +
  "placeholder:text-slate-600 focus:border-sky-500 focus:outline-none";

export default function PartMasterCard({
  isAdmin,
  onSaved,
}: {
  isAdmin: boolean;
  onSaved?: () => void;
}) {
  const [specs, setSpecs] = useState<PartSpec[]>([]);
  const [materials, setMaterials] = useState<string[]>([]);
  const [tools, setTools] = useState<ToolAssignment[]>([]);
  const [machines, setMachines] = useState<{ id: number; name: string }[]>([]);
  const [draft, setDraft] = useState<Draft>(blank);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState("");

  const load = useCallback(async () => {
    try {
      const [s, t] = await Promise.all([
        apiGet<{ specs: PartSpec[]; materials: string[] }>("/part-specs"),
        apiGet<{ tools: ToolAssignment[]; machines: { id: number; name: string }[] }>(
          "/tool-assignments"),
      ]);
      setSpecs(s.specs);
      setMaterials(s.materials);
      setTools(t.tools);
      setMachines(t.machines);
    } catch (e) {
      setError(errorDetail(e));
    }
  }, []);

  useEffect(() => {
    // Deferred by a tick, and guarded for unmount, the way RootCauseSection
    // does it: calling load() in the effect body sets state synchronously
    // during the effect, which react-hooks/set-state-in-effect flags and which
    // costs a cascading render on every mount.
    let alive = true;
    const first = setTimeout(() => {
      if (alive) void load();
    }, 0);
    return () => {
      alive = false;
      clearTimeout(first);
    };
  }, [load]);

  const rate = useMemo(
    () => previewRate(draft.ideal_cycle_time_s, draft.active_cavities || draft.cavities),
    [draft.ideal_cycle_time_s, draft.active_cavities, draft.cavities],
  );

  const set = (k: keyof Draft) => (v: string) => setDraft((d) => ({ ...d, [k]: v }));

  async function save() {
    setBusy(true);
    setError("");
    setSaved("");
    const material = draft.material === OTHER ? draft.materialOther : draft.material;
    try {
      await apiPost("/part-specs", {
        part_code: draft.part_code,
        part_name: draft.part_name,
        material,
        part_weight_g: Number(draft.part_weight_g),
        cavities: Number(draft.cavities),
        active_cavities: Number(draft.active_cavities || draft.cavities),
        ideal_cycle_time_s: Number(draft.ideal_cycle_time_s),
        price_per_piece: Number(draft.price_per_piece || 0),
        effective_from: draft.effective_from,
      });
      setSaved("Saved " + draft.part_code + ". Every chart using it has been recalculated.");
      setDraft(blank());
      await load();
      onSaved?.();
    } catch (e) {
      // The backend names the field it refused and why; showing a generic
      // message instead would send somebody guessing which box is wrong.
      setError(errorDetail(e));
    } finally {
      setBusy(false);
    }
  }

  async function assign(toolId: number, body: Record<string, unknown>) {
    setError("");
    try {
      await apiPost("/tool-assignments/" + toolId, body);
      await load();
      onSaved?.();
    } catch (e) {
      setError(errorDetail(e));
    }
  }

  return (
    <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-5">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h3 className="text-sm font-semibold uppercase tracking-wide text-slate-300">
            Part master
          </h3>
          <p className="mt-1 text-xs text-slate-500">
            What a machine cannot know: part weight, cavities, cycle time and
            price. Every kilogram and every {CURRENCY} figure on this page comes
            from here.
          </p>
        </div>
        {isAdmin ? (
          <button
            type="button"
            onClick={() => setOpen((v) => !v)}
            className="rounded-lg border border-sky-500/40 bg-sky-500/10 px-4 py-2 text-sm text-sky-200 hover:bg-sky-500/20"
          >
            {open ? "Close" : "Add a part"}
          </button>
        ) : null}
      </div>

      {error ? (
        <p className="mt-3 rounded-lg border border-red-500/40 bg-red-500/10 p-3 text-sm text-red-200">
          {error}
        </p>
      ) : null}
      {saved ? (
        <p className="mt-3 rounded-lg border border-emerald-500/40 bg-emerald-500/10 p-3 text-sm text-emerald-200">
          {saved}
        </p>
      ) : null}

      {open && isAdmin ? (
        <div className="mt-4 rounded-xl border border-slate-700 bg-slate-950/50 p-4">
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Field label="Part code">
              <input
                className={INPUT}
                value={draft.part_code}
                onChange={(e) => set("part_code")(e.target.value)}
                placeholder="ELE-CLIP"
              />
            </Field>
            <Field label="Part name">
              <input
                className={INPUT}
                value={draft.part_name}
                onChange={(e) => set("part_name")(e.target.value)}
                placeholder="Ele clip"
              />
            </Field>

            {/* The dropdown-or-text-box: known grades offered, anything else typed. */}
            <Field label="Material">
              <select
                className={INPUT}
                value={draft.material}
                onChange={(e) => set("material")(e.target.value)}
              >
                <option value="">Choose…</option>
                {materials.map((m) => (
                  <option key={m} value={m}>
                    {m}
                  </option>
                ))}
                <option value={OTHER}>Other — type it</option>
              </select>
            </Field>
            {draft.material === OTHER ? (
              <Field label="New material" hint="Typed once, then offered in the list above.">
                <input
                  className={INPUT}
                  value={draft.materialOther}
                  onChange={(e) => set("materialOther")(e.target.value)}
                  placeholder="PP H 110"
                />
              </Field>
            ) : null}

            <Field label="Part weight" hint="Grams, as weighed. 0.33 is fine.">
              <input
                className={INPUT}
                type="number"
                step="0.001"
                value={draft.part_weight_g}
                onChange={(e) => set("part_weight_g")(e.target.value)}
                placeholder="0.33"
              />
            </Field>

            <Field label="Cavities in the mould">
              <input
                className={INPUT}
                type="number"
                value={draft.cavities}
                onChange={(e) => set("cavities")(e.target.value)}
                placeholder="56"
              />
            </Field>
            <Field
              label="Cavities actually running"
              hint="Leave blank if all of them. Blocked cavities belong here."
            >
              <input
                className={INPUT}
                type="number"
                value={draft.active_cavities}
                onChange={(e) => set("active_cavities")(e.target.value)}
                placeholder={draft.cavities || "56"}
              />
            </Field>
            <Field label="Cycle time" hint="Seconds for one shot.">
              <input
                className={INPUT}
                type="number"
                step="0.1"
                value={draft.ideal_cycle_time_s}
                onChange={(e) => set("ideal_cycle_time_s")(e.target.value)}
                placeholder="14"
              />
            </Field>
            <Field label={"Price per piece (" + CURRENCY + ")"} hint="Leave blank if not priced yet.">
              <input
                className={INPUT}
                type="number"
                step="0.01"
                value={draft.price_per_piece}
                onChange={(e) => set("price_per_piece")(e.target.value)}
                placeholder="0.09"
              />
            </Field>
            <Field
              label="In force from"
              hint="A price change gets a new date. The old one stays, so last month does not move."
            >
              <input
                className={INPUT}
                type="date"
                value={draft.effective_from}
                onChange={(e) => set("effective_from")(e.target.value)}
              />
            </Field>
          </div>

          <div className="mt-4 flex flex-wrap items-center justify-between gap-3">
            <p className="text-xs text-slate-400">
              {rate != null ? (
                <>
                  This implies{" "}
                  <span className="font-semibold text-sky-300">
                    {rate.toLocaleString()} parts/hour
                  </span>{" "}
                  — every hour on the board is judged against it.
                </>
              ) : (
                "Enter a cycle time and cavity count to see the hourly target."
              )}
            </p>
            <button
              type="button"
              disabled={busy}
              onClick={() => void save()}
              className="rounded-lg bg-sky-600 px-5 py-2 text-sm font-medium text-white hover:bg-sky-500 disabled:opacity-50"
            >
              {busy ? "Saving…" : "Save part"}
            </button>
          </div>
        </div>
      ) : null}

      {/* ── What is on record ──────────────────────────────────── */}
      {specs.length ? (
        <div className="mt-4 overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs uppercase tracking-wide text-slate-500">
                <th className="py-2">Part</th>
                <th className="py-2">Material</th>
                <th className="py-2 text-right">Weight (g)</th>
                <th className="py-2 text-right">Cavities</th>
                <th className="py-2 text-right">Cycle (s)</th>
                <th className="py-2 text-right">Target/hr</th>
                <th className="py-2 text-right">Price</th>
                <th className="py-2">From</th>
              </tr>
            </thead>
            <tbody>
              {specs.map((s) => (
                <tr key={s.id} className="border-t border-slate-800">
                  <td className="py-2 text-slate-100">
                    {s.part_name}
                    <span className="ml-2 text-xs text-slate-500">{s.part_code}</span>
                  </td>
                  <td className="py-2 text-slate-400">{s.material}</td>
                  <td className="py-2 text-right text-slate-300">{s.part_weight_g}</td>
                  <td className="py-2 text-right text-slate-300">
                    {s.active_cavities}
                    {s.active_cavities !== s.cavities ? (
                      <span className="text-slate-600"> of {s.cavities}</span>
                    ) : null}
                  </td>
                  <td className="py-2 text-right text-slate-300">{s.ideal_cycle_time_s}</td>
                  <td className="py-2 text-right text-sky-300">
                    {s.ideal_parts_per_hour.toLocaleString()}
                  </td>
                  <td className="py-2 text-right text-slate-300">
                    {s.price_per_piece > 0 ? CURRENCY + s.price_per_piece : "unpriced"}
                  </td>
                  <td className="py-2 text-slate-500">{s.effective_from ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="mt-4 text-sm text-slate-500">
          No part has a specification yet, so the board can count parts but cannot
          turn them into kilograms or {CURRENCY}.
        </p>
      )}

      {/* ── Mould changes ──────────────────────────────────────── */}
      {tools.length ? (
        <div className="mt-6">
          <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-400">
            Moulds
          </h4>
          <p className="mt-1 text-xs text-slate-600">
            Which mould is on which machine, and what it makes. Set this after a
            mould change — roughly once a fortnight, not once an hour.
          </p>
          <div className="mt-3 overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs uppercase tracking-wide text-slate-500">
                  <th className="py-2">Mould</th>
                  <th className="py-2">On machine</th>
                  <th className="py-2">Making</th>
                  <th className="py-2 text-right">Shots since service</th>
                </tr>
              </thead>
              <tbody>
                {tools.map((t) => (
                  <tr key={t.id} className="border-t border-slate-800">
                    <td className="py-2 text-slate-100">
                      {t.tool_no}
                      <span className="ml-2 text-xs text-slate-500">{t.name}</span>
                    </td>
                    <td className="py-2">
                      {isAdmin ? (
                        <select
                          className="rounded-lg border border-slate-700 bg-slate-950 px-2 py-1 text-sm text-white"
                          value={t.machine_id ?? ""}
                          onChange={(e) =>
                            void assign(t.id, {
                              machine_id: e.target.value ? Number(e.target.value) : null,
                            })
                          }
                        >
                          <option value="">In the tool room</option>
                          {machines.map((m) => (
                            <option key={m.id} value={m.id}>
                              {m.name}
                            </option>
                          ))}
                        </select>
                      ) : (
                        <span className="text-slate-400">{t.machine ?? "In the tool room"}</span>
                      )}
                    </td>
                    <td className="py-2">
                      {isAdmin ? (
                        <select
                          className="rounded-lg border border-slate-700 bg-slate-950 px-2 py-1 text-sm text-white"
                          value={t.part_code ?? ""}
                          onChange={(e) =>
                            void assign(t.id, { part_code: e.target.value || null })
                          }
                        >
                          <option value="">Not stated</option>
                          {Array.from(new Set(specs.map((s) => s.part_code))).map((code) => (
                            <option key={code} value={code}>
                              {code}
                            </option>
                          ))}
                        </select>
                      ) : (
                        <span className="text-slate-400">{t.part_code ?? "Not stated"}</span>
                      )}
                    </td>
                    <td className="py-2 text-right text-slate-300">
                      {t.cycles_since_service.toLocaleString()}
                      {t.service_interval_cycles ? (
                        <span className="text-slate-600">
                          {" "}
                          of {t.service_interval_cycles.toLocaleString()}
                        </span>
                      ) : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : null}

      {!isAdmin ? (
        <p className="mt-4 text-xs text-slate-600">
          Only an administrator can change these. Changing a price restates the
          plant&apos;s revenue and changing a cavity count restates its output.
        </p>
      ) : null}
    </div>
  );
}
