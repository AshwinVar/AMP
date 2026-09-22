"use client";
import type { Proposal } from "../lib/evidence";
import ProposeActionButton from "./ProposeActionButton";

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
// The button itself is ProposeActionButton, shared with the Risk Radar, because
// two surfaces offering a draft must not each carry their own copy of the write.
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
  if (!proposal) return null;
  const tone = PRIORITY_TONE[proposal.priority] || "text-slate-400 border-slate-600";

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

      <ProposeActionButton
        kind={proposal.kind}
        machineId={proposal.machine_id}
        onOpen={onOpen}
      />
    </div>
  );
}
