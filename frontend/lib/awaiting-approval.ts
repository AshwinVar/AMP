/**
 * An agent proposal holds its item until an Admin or Supervisor decides it
 * (backend approvals.py, ADR-0015 addendum).
 *
 * The backend decides whether a row is held and sends the answer as
 * `awaiting_approval` on each maintenance task, escalation and purchase order.
 * The screens read ONLY that field. They never compare status strings: a
 * "Proposed" task or "Draft" PO that a human created has no proposal behind it
 * and stays fully editable, and a tenant whose plan cannot reach Approvals is
 * never held at all. Re-deriving either rule here would be a second copy of it.
 */
export type AwaitingApproval = {
  agent_action_id: number;
  agent: string;
  /** The proposal can no longer be approved, only rejected. */
  expired: boolean;
};

/** Is this row held by an undecided agent proposal? */
export function isHeld(row: { awaiting_approval?: AwaitingApproval | null }): boolean {
  return row.awaiting_approval != null;
}

/** The sentence shown under a held row's controls. */
export function awaitingApprovalCaption(hold: AwaitingApproval): string {
  if (hold.expired) {
    return "This proposal has expired; an Admin or Supervisor must reject it in Approvals before it can be changed.";
  }
  return `Awaiting approval: proposed by the ${hold.agent} agent (action #${hold.agent_action_id}). An Admin or Supervisor approves or rejects it in Approvals.`;
}
