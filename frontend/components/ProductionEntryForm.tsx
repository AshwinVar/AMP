"use client";
import { useState } from "react";
import { apiPost, errorDetail } from "../lib/api";

type Machine = { id: number; name: string };

const BLANK = {
  machine_id: "",
  planned_minutes: "480",
  runtime_minutes: "",
  ideal_cycle_time_seconds: "",
  total_count: "",
  good_count: "",
  rejected_count: "",
};

// Record what a machine actually made in a shift.
//
// THIS IS THE ONE INPUT AMP COULD NOT BE GIVEN. `POST /production-records` has
// existed since the beginning and no screen ever called it: every writer was a
// seeder, the simulator or the MQTT ingest. So a factory without a gateway —
// which is most SMEs on day one — had no way to give AMP the rows OEE is
// computed from, and the Command Centre said "no production recorded" forever.
// Manual entry is the floor under every other promise the product makes.
//
// The field help is not decoration. An owner setting AMP up knows their shift
// length and their counts; "ideal cycle time" is the one term that is ours and
// not theirs, so it is explained where it is asked for rather than in a manual.
//
// Nothing is computed here. The two rules below are the server's own
// (machines_routes.create_production_record) and are repeated only so the
// answer is instant; the server still decides, and its refusal is shown
// verbatim rather than replaced with a friendlier lie.
export default function ProductionEntryForm({
  machines,
  onSaved,
}: {
  machines: Machine[];
  onSaved?: () => void;
}) {
  const [form, setForm] = useState<Record<string, string>>({ ...BLANK });
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState("");
  const [saved, setSaved] = useState(false);

  const num = (k: string) => Number(form[k] || 0);
  const set = (k: string, v: string) => {
    setForm((f) => ({ ...f, [k]: v }));
    setSaved(false);
    setErr("");
  };

  // The server's arithmetic rule, checked as you type so the refusal is not a
  // round trip. It is NOT a substitute for the server's check.
  const counted = num("good_count") + num("rejected_count");
  const countsDisagree =
    form.total_count !== "" && form.good_count !== "" && counted !== num("total_count");

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (saving || countsDisagree) return;
    setSaving(true);
    setErr("");
    try {
      await apiPost("/production-records", {
        machine_id: Number(form.machine_id),
        planned_minutes: num("planned_minutes"),
        runtime_minutes: num("runtime_minutes"),
        ideal_cycle_time_seconds: num("ideal_cycle_time_seconds"),
        total_count: num("total_count"),
        good_count: num("good_count"),
        rejected_count: num("rejected_count"),
      });
      setForm({ ...BLANK, machine_id: form.machine_id });
      setSaved(true);
      onSaved?.();
    } catch (e2) {
      // The backend's own sentence — "good_count + rejected_count must equal
      // total_count", "minutes and counts must be non-negative", a 404 for a
      // machine that is not this workspace's. A screen that swallowed these
      // would make a validation refusal look like an outage (#695).
      setErr(errorDetail(e2));
    }
    setSaving(false);
  }

  const field = (
    key: string,
    label: string,
    help: string,
    placeholder: string,
  ) => (
    <label className="block">
      <span className="text-sm text-slate-300">{label}</span>
      <input
        // The accessible name is the LABEL only. The help text below lives
        // inside the same <label>, so without this it would be folded into the
        // name and "Machine" would also match "the machine's best" in the
        // cycle-time help — two fields answering to one query.
        aria-label={label}
        className="mt-1 w-full bg-slate-950 border border-slate-700 rounded-xl px-4 py-2.5"
        type="number"
        min="0"
        inputMode="numeric"
        placeholder={placeholder}
        value={form[key]}
        onChange={(e) => set(key, e.target.value)}
        required
      />
      <span className="text-[11px] text-slate-500">{help}</span>
    </label>
  );

  return (
    <form
      onSubmit={submit}
      data-testid="production-entry-form"
      className="rounded-2xl bg-slate-900 border border-slate-800 p-5"
    >
      <h2 className="text-2xl font-semibold">Record production</h2>
      <p className="text-slate-400 text-sm mt-1 mb-4">
        One row per machine, per shift. This is what AMP measures OEE from — without it there
        is nothing to report.
      </p>

      {machines.length === 0 ? (
        <p role="status" className="text-amber-300/90 text-sm">
          Add a machine first. Production is always recorded against one.
        </p>
      ) : (
        <div className="space-y-4">
          <label className="block">
            <span className="text-sm text-slate-300">Machine</span>
            <select
              aria-label="Machine"
              className="mt-1 w-full bg-slate-950 border border-slate-700 rounded-xl px-4 py-2.5"
              value={form.machine_id}
              onChange={(e) => set("machine_id", e.target.value)}
              required
            >
              <option value="">Choose a machine…</option>
              {machines.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.name}
                </option>
              ))}
            </select>
          </label>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            {field("planned_minutes", "Planned minutes", "How long the machine was meant to run. A shift is usually 480.", "480")}
            {field("runtime_minutes", "Runtime minutes", "How long it actually ran, stoppages excluded.", "456")}
          </div>

          {field(
            "ideal_cycle_time_seconds",
            "Ideal cycle time (seconds)",
            "Seconds to make ONE good part at full speed — the machine's best, not its average. AMP uses it to work out how much the run could have made.",
            "30",
          )}

          <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
            {field("total_count", "Made", "Everything it produced.", "900")}
            {field("good_count", "Good", "Passed inspection.", "891")}
            {field("rejected_count", "Rejected", "Scrap or rework.", "9")}
          </div>

          {countsDisagree && (
            <p role="alert" className="text-amber-300 text-xs">
              Good + rejected is {counted.toLocaleString()}, but you entered{" "}
              {num("total_count").toLocaleString()} made. They have to match.
            </p>
          )}

          <button
            type="submit"
            disabled={saving || countsDisagree}
            className="w-full rounded-xl bg-white text-slate-950 font-semibold px-4 py-3 disabled:opacity-50"
          >
            {saving ? "Saving…" : "Save production record"}
          </button>

          {err && (
            <p role="alert" className="text-red-400 text-sm">
              {err}
            </p>
          )}
          {saved && !err && (
            <p role="status" className="text-emerald-300 text-sm">
              Recorded. OEE, losses and the trends now include it.
            </p>
          )}
        </div>
      )}
    </form>
  );
}
