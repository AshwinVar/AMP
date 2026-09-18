import { awaitingApprovalCaption, type AwaitingApproval } from "../lib/awaiting-approval";

/**
 * The caption under a row an agent proposal is holding. Renders nothing for a
 * row that is not held, so callers can pass `row.awaiting_approval` straight in.
 */
export default function AwaitingApprovalNotice({ hold }: { hold?: AwaitingApproval | null }) {
  if (!hold) return null;
  return (
    <p role="note" className="mt-1 max-w-xs text-xs text-amber-300">
      {awaitingApprovalCaption(hold)}
    </p>
  );
}
