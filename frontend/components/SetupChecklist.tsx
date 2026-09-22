"use client";

export type SetupState = {
  machines: number;
  production: number;
  shifts: number;
  workOrders: number;
  inventoryItems: number;
  unitValueSet: boolean;
};

type Step = {
  key: string;
  title: string;
  done: boolean;
  detail: string;
  /** The nav view that owns it, when a screen exists. */
  view?: string;
};

function steps(s: SetupState): Step[] {
  return [
    {
      key: "machines",
      title: "Add your machines",
      done: s.machines > 0,
      view: "machines",
      detail: s.machines
        ? `${s.machines} registered.`
        : "Everything else hangs off a machine. Add them by hand or import a CSV.",
    },
    {
      key: "production",
      title: "Record what they made",
      done: s.production > 0,
      view: "machines",
      detail: s.production
        ? `${s.production} machine${s.production === 1 ? "" : "s"} reporting output.`
        : "This is what OEE is measured from. Until a machine reports, AMP has nothing to report.",
    },
    {
      key: "shifts",
      title: "Enter a shift's target and actual",
      done: s.shifts > 0,
      view: "shifts",
      detail: s.shifts ? `${s.shifts} recorded.` : "So attainment has something to compare against.",
    },
    {
      key: "workorders",
      title: "Raise a work order",
      done: s.workOrders > 0,
      view: "workorders",
      detail: s.workOrders
        ? `${s.workOrders} open or completed.`
        : "What the plant is making, and for whom. Production plans attach to one.",
    },
    {
      key: "inventory",
      title: "Add the materials you hold",
      done: s.inventoryItems > 0,
      view: "inventory",
      detail: s.inventoryItems
        ? `${s.inventoryItems} item${s.inventoryItems === 1 ? "" : "s"}.`
        : "With a reorder level on each, AMP can tell you what is about to run out.",
    },
    {
      key: "unitvalue",
      title: "Set what a good unit is worth",
      done: s.unitValueSet,
      view: "costing",
      detail: s.unitValueSet
        ? "Losses are reported in money."
        : "Optional. Without it AMP reports losses in units and says so — it will not invent a price.",
    },
  ];
}

// Where a new workspace is in getting AMP useful (ADR-0039 era: build-first).
//
// There was no first-run experience of any kind — no wizard, no guided setup,
// not one call to action. The empty states were honest ("No machines yet",
// "Fleet health not measured") and every one of them was a dead end: the moment
// a new Admin most needed direction was the moment they got none.
//
// This is not a wizard and does not pretend to be one. It reads what the
// workspace already has and says what is still missing, in the order the data
// depends on itself — machines before production, production before OEE. Each
// row links to the screen that owns it.
//
// It lists only steps AMP ACTUALLY supports. "Create a site" is deliberately
// absent: `Machine.site` exists in the schema but is reachable from no form and
// no route, so a checklist row for it would be an instruction nobody can follow.
export default function SetupChecklist({
  state,
  onOpen,
}: {
  state: SetupState;
  onOpen?: (viewKey: string) => void;
}) {
  const rows = steps(state);
  const done = rows.filter((r) => r.done).length;
  if (done === rows.length) return null;

  return (
    <section
      data-testid="setup-checklist"
      className="mb-8 rounded-2xl border border-indigo-500/30 bg-indigo-500/5 p-5"
    >
      <div className="flex items-baseline justify-between gap-3 flex-wrap">
        <h2 className="text-xl font-semibold">Getting AMP useful</h2>
        <span className="text-xs text-slate-400 tabular-nums">
          {done} of {rows.length} done
        </span>
      </div>
      <p className="text-slate-400 text-sm mt-1">
        AMP reports what your plant records. These are the things it still has nothing to read.
      </p>

      <ul className="mt-4 space-y-2">
        {rows.map((r) => (
          <li key={r.key} className="flex items-start gap-3">
            <span
              aria-hidden="true"
              className={
                "mt-0.5 shrink-0 w-5 h-5 rounded-full border grid place-items-center text-[11px] " +
                (r.done
                  ? "border-emerald-500/50 text-emerald-300"
                  : "border-slate-600 text-slate-600")
              }
            >
              {r.done ? "✓" : ""}
            </span>
            <div className="min-w-0">
              <p className={"text-sm " + (r.done ? "text-slate-400" : "text-slate-100 font-medium")}>
                {r.title}
                <span className="sr-only">{r.done ? " — done" : " — not done yet"}</span>
              </p>
              <p className="text-slate-500 text-xs">{r.detail}</p>
              {!r.done && r.view && onOpen && (
                <button
                  type="button"
                  onClick={() => onOpen(r.view!)}
                  className="mt-1 text-xs text-indigo-300 hover:text-indigo-200 underline underline-offset-2"
                >
                  Go to {r.title.toLowerCase()} →
                </button>
              )}
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}
