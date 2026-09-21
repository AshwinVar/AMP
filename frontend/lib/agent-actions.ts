/**
 * The agent action log and approval queue, page by page (backend
 * agent_routes.list_agent_actions, ADR-0015 addendum).
 *
 * An undecided agent proposal holds its task, purchase order or escalation until
 * an Admin or Supervisor decides it, so the Approvals list is the only way out
 * for a held item. The server answers at most one page (300 rows, newest first);
 * before paging, a held proposal older than 300 newer ones never appeared at
 * all. Screens therefore load page after page until they hold the rows they
 * asked for, and offer "load older" while the last page came back full.
 */

/** Mirrors agent_routes.AGENT_ACTIONS_PAGE: the most rows one request returns. */
export const AGENT_ACTIONS_PAGE = 300;

/** Mirrors the backend AgentAction (agent_routes._agent_action_dict). */
export type AgentAction = {
  id: number;
  agent: string;
  action_type: string;
  summary: string;
  ref_kind: string;
  ref_id: number | null;
  severity: string;
  status: string;
  related_machine_id: number | null;
  created_at: string;
  decided_by: string | null;
  decided_at: string | null;
  /** Undecided and past its expiry: it can only be rejected, which releases its item. */
  expired?: boolean;
};

export type AgentActionPages = {
  rows: AgentAction[];
  /** The last page came back full, so older rows may exist. */
  more: boolean;
};

/**
 * Load agent actions (optionally of one status), newest first, until at least
 * `atLeast` rows are held or the server has no more. A row that shifts between
 * two pages while they load is kept once; one that shifts past a page boundary
 * reappears on the next refresh.
 */
export async function loadAgentActions(
  get: (path: string) => Promise<AgentAction[]>,
  status: string | null,
  atLeast: number = AGENT_ACTIONS_PAGE,
): Promise<AgentActionPages> {
  const rows: AgentAction[] = [];
  const seen = new Set<number>();
  let offset = 0;
  for (;;) {
    const query = new URLSearchParams({ limit: String(AGENT_ACTIONS_PAGE), offset: String(offset) });
    if (status) query.set("status", status);
    const page = await get(`/agent-actions?${query.toString()}`);
    let added = 0;
    for (const row of page) {
      if (seen.has(row.id)) continue;
      seen.add(row.id);
      rows.push(row);
      added += 1;
    }
    offset += page.length;
    const more = page.length >= AGENT_ACTIONS_PAGE;
    // `added === 0` stops a server that ignores `offset` from looping for ever.
    if (!more || added === 0 || rows.length >= atLeast) return { rows, more };
  }
}

/** The sentence shown on a proposal that can no longer be approved. */
export const EXPIRED_PROPOSAL_NOTE =
  "This proposal has expired and can no longer be approved. It can only be rejected, which releases the item it holds.";

/**
 * Who may press Approve / Reject: POST /agent-actions/{id}/approve|reject is
 * `require_roles(["Admin", "Supervisor"])` (agent_routes). Mission Control and
 * the machine cockpit are Operator-visible screens, and both offered the two
 * buttons to an Operator, whose click came back 403.
 */
export function canDecideProposals(role: string): boolean {
  return role === "Admin" || role === "Supervisor";
}

/** Shown in place of Approve / Reject to a role the server would refuse. */
export const DECISION_ROLE_NOTE =
  "Approving or rejecting an agent's proposal is a Supervisor's or Admin's decision.";
