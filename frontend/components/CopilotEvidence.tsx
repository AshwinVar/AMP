"use client";
import { useState } from "react";
import {
  PROVENANCE_MEANING,
  formatFactValue,
  provenanceTone,
  stateNotice,
  type Fact,
  type Grounding,
  type Provenance,
  type Tone,
  type ToolRun,
} from "../lib/evidence";

const TONE_CLASS: Record<Tone, string> = {
  measured: "text-emerald-300 border-emerald-500/40",
  derived: "text-sky-300 border-sky-500/40",
  correlation: "text-amber-300 border-amber-500/40",
  rule: "text-violet-300 border-violet-500/40",
  model: "text-fuchsia-300 border-fuchsia-500/40",
  unknown: "text-slate-400 border-slate-600",
};

// The evidence behind one Copilot answer (ADR-0022): every figure with how AMP
// knows it, the data state when the answer is not complete, the AMP tools that ran
// and, when a model worded the answer, whether its wording passed the check.
//
// The state notice is always visible, because "partial data" or "not configured"
// changes what the answer means. The facts are one tap away, so the answer stays
// the first thing read.
export default function CopilotEvidence({
  facts = [],
  tools = [],
  state,
  grounding = null,
  engine,
}: {
  facts?: Fact[];
  tools?: ToolRun[];
  state?: string;
  grounding?: Grounding;
  engine?: string;
}) {
  const [open, setOpen] = useState(false);
  const notice = stateNotice(state);
  if (!facts.length && !tools.length && !notice) return null;
  return (
    <div className="mt-3">
      {notice && (
        <p role="status" className="text-amber-300/90 text-xs mb-2">
          <span className="font-semibold">{state}</span> · {notice}
        </p>
      )}
      {facts.length > 0 && (
        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          aria-expanded={open}
          className="text-xs text-slate-400 hover:text-white"
        >
          {open ? "Hide" : "Show"} evidence ({facts.length} fact{facts.length === 1 ? "" : "s"})
        </button>
      )}
      {open && (
        <div className="mt-2 rounded-xl border border-slate-800 bg-slate-950/60 p-3 space-y-2">
          <ul className="space-y-1.5">
            {facts.map((f) => (
              <li key={f.id} className="text-xs flex flex-wrap items-baseline gap-x-2 gap-y-1">
                <span className="text-slate-500 tabular-nums">{f.id}</span>
                <span className="text-slate-300">{f.label}</span>
                <span className="text-white font-semibold tabular-nums">{formatFactValue(f)}</span>
                <span
                  className={`text-[10px] uppercase tracking-wide rounded-full px-2 py-0.5 border ${TONE_CLASS[provenanceTone(f.provenance)]}`}
                  title={PROVENANCE_MEANING[f.provenance as Provenance] ?? ""}
                >
                  {f.provenance}
                </span>
                {f.window && <span className="text-slate-500">{f.window}</span>}
                {f.detail && <span className="text-slate-500">· {f.detail}</span>}
              </li>
            ))}
          </ul>
          {tools.length > 0 && (
            <p className="text-[11px] text-slate-500">
              Answered by AMP tools: {tools.map((t) => `${t.tool} (${t.state})`).join(", ")}
            </p>
          )}
          {engine === "llm" && grounding?.passed && (
            <p className="text-[11px] text-emerald-300/80">
              Worded by the model; every figure and name was checked against this evidence.
            </p>
          )}
          {grounding && !grounding.passed && (
            <p className="text-[11px] text-amber-300/80">
              The model&apos;s wording was not shown ({grounding.reasons.join("; ")}). AMP&apos;s own answer is shown instead.
            </p>
          )}
        </div>
      )}
    </div>
  );
}
