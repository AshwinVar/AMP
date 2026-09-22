"use client";
import { useState } from "react";
import { apiPost, errorDetail } from "../lib/api";
import type { Proposal } from "../lib/evidence";

const PRIORITY_TONE: Record<string, string> = {
  Critical: "text-red-300 border-red-500/40",
  High: "text-amber-300 border-amber-500/40",
  Medium: "text-sky-300 border-sky-500/40",
};

// An action AMP drafted in a Copilot answer, and the one button that raises it
// (ADR-0039).
//
// NOTHING HAS HAPPENED WHEN THIS RENDERS. The card exists because AMP read the
// plant and wrote down what it would propose; the draft is inert until somebody
// here presses the button, and it still does not take effect then — it goes to
// the approval queue, and a person has to approve it. The copy says both of
// those things out loud, before and after, because a card that looks like a
// confirmation is how a person comes to believe a job was booked that was not.
//
// Only `kind` and `machine_id` are sent. The server re-derives the priority, the
// task type and the wording from the machine itself, so what is drawn here can
// never become what is written (test_copilot_actions.py section 8).
export default function CopilotProposal({
  proposal,
  onOpen,
}: {
  proposal?: Proposal | null;
  onOpen?: (viewKey: string) => void;
}) {
  const [raising, setRaising] = useState(false);
  const [raised, setRaised] = useState<{ id: number; summary: string } | null>(null);
  const [err, setErr] = useState("");

  if (!proposal) return null;
  const tone = PRIORITY_TONE[proposal.priority] || "text-slate-400 border-slate-600";

  async function raise() {
    if (!proposal || raising || raised) return;
    setRaising(true);
    setErr("");
    try {
      const res = await apiPost<{ id: number; summary: string }>("/agent-actions/propose", {
        kind: proposal.kind,
        machine_id: proposal.machine_id,
      });
      setRaised({ id: res.id, summary: res.summary });
    } catch (e) {
      // The server's own sentence: a duplicate proposal, a machine that is not
      // this workspace's, a role that may not raise one. A generic "that failed"
      // would hide a refusal the person can act on.
      setErr(errorDetail(e));
    }
    setRaising(false);
  }

  return (
    <div
      data-testid="copilot-proposal"
      className="mt-3 rounded-xl border border-indigo-500/30 bg-indigo-500/5 p-4"
    >
      <div className="flex items-start justify-between gap-2 flex-wrap">
        <div>
          <p className="text-[10px] uppercase tracking-wide text-indigo-300/80">
            AMP would propose
          </p>
          <p className="text-slate-100 text-sm font-semibold mt-0.5">{proposal.label}</p>
        </div>
        <span className={"text-[10px] uppercase tracking-wide rounded-full px-2 py-0.5 border shrink-0 " + tone}>
          {proposal.priority}
        </span>
      </div>
      <p className="text-slate-400 text-xs mt-2 leading-relaxed">{proposal.reason}</p>

      {!raised && (
        <>
          <div className="flex items-center gap-3 mt-3 flex-wrap">
            <button
              type="button"
              onClick={raise}
              disabled={raising}
              className="rounded-lg bg-indigo-500 text-white text-xs font-semibold px-4 py-2 hover:bg-indigo-400 disabled:opacity-50"
            >
              {raising ? "Proposing…" : "Propose this action"}
            </button>
            <span className="text-slate-500 text-[11px]">
              Nothing exists yet. Proposing sends it for approval — it only takes effect once
              somebody approves it.
            </span>
          </div>
          {err && (
            <p role="alert" className="text-red-400 text-xs mt-2">
              {err}
            </p>
          )}
        </>
      )}

      {raised && (
        <div className="mt-3">
          <p role="status" className="text-emerald-300 text-xs">
            Proposed — waiting for approval. Nothing has been carried out yet.
          </p>
          <p className="text-slate-500 text-[11px] mt-1">{raised.summary}</p>
          {onOpen && (
            <button
              type="button"
              onClick={() => onOpen("inbox")}
              className="mt-2 text-xs text-indigo-300 border border-indigo-500/40 rounded-lg px-3 py-1 hover:bg-indigo-500/10"
            >
              Open Approvals →
            </button>
          )}
        </div>
      )}
    </div>
  );
}
