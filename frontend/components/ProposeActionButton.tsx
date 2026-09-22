"use client";
import { useState } from "react";
import { apiPost, errorDetail } from "../lib/api";
import type { ProposableKind } from "../lib/evidence";

/**
 * The one button that raises a drafted action (ADR-0039).
 *
 * THE ONLY PLACE THE FRONTEND WRITES ON THIS PATH. Two surfaces offer a draft
 * now — the Copilot's answer and the Risk Radar — and they must not each carry
 * their own copy of the request. A second implementation is how one of them
 * quietly starts sending a priority the server is re-deriving anyway, or stops
 * showing the refusal, and nothing notices.
 *
 * Only `kind` and `machine_id` are sent. Everything else about the task — the
 * priority, the type, the wording — is re-derived server-side from the machine
 * at that instant, so what any card happens to be displaying can never become
 * what is written (test_copilot_actions.py section 8 pins the other side).
 *
 * Nothing takes effect here. Raising puts the action in the approval queue; a
 * person still has to approve it, and the copy says so both before and after,
 * because a control that reads like a confirmation is how somebody comes to
 * believe a job was booked that was not.
 */
export default function ProposeActionButton({
  kind,
  machineId,
  label = "Propose this action",
  onOpen,
  compact = false,
}: {
  kind: ProposableKind;
  machineId: number;
  label?: string;
  onOpen?: (viewKey: string) => void;
  compact?: boolean;
}) {
  const [raising, setRaising] = useState(false);
  const [raised, setRaised] = useState<{ id: number; summary: string } | null>(null);
  const [err, setErr] = useState("");

  async function raise() {
    if (raising || raised) return;
    setRaising(true);
    setErr("");
    try {
      const res = await apiPost<{ id: number; summary: string }>("/agent-actions/propose", {
        kind,
        machine_id: machineId,
      });
      setRaised({ id: res.id, summary: res.summary });
    } catch (e) {
      // The server's own sentence: a duplicate proposal, a machine that is not
      // this workspace's, a role that may not raise one. A generic "that
      // failed" would hide a refusal the person can act on.
      setErr(errorDetail(e));
    }
    setRaising(false);
  }

  if (raised) {
    return (
      <div className={compact ? "" : "mt-3"}>
        <p role="status" className="text-emerald-300 text-xs">
          Proposed — waiting for approval. Nothing has been carried out yet.
        </p>
        {!compact && <p className="text-slate-500 text-[11px] mt-1">{raised.summary}</p>}
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
    );
  }

  return (
    <div className={compact ? "" : "mt-3"}>
      <div className="flex items-center gap-3 flex-wrap">
        <button
          type="button"
          onClick={raise}
          disabled={raising}
          className={
            compact
              ? "rounded-lg border border-indigo-500/40 text-indigo-300 text-xs px-3 py-1 hover:bg-indigo-500/10 disabled:opacity-50"
              : "rounded-lg bg-indigo-500 text-white text-xs font-semibold px-4 py-2 hover:bg-indigo-400 disabled:opacity-50"
          }
        >
          {raising ? "Proposing…" : label}
        </button>
        {!compact && (
          <span className="text-slate-500 text-[11px]">
            Nothing exists yet. Proposing sends it for approval — it only takes effect once somebody
            approves it.
          </span>
        )}
      </div>
      {err && (
        <p role="alert" className="text-red-400 text-xs mt-2">
          {err}
        </p>
      )}
    </div>
  );
}
