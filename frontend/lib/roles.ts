/**
 * Who a screen may OFFER a write to.
 *
 * The server decides what a role may do (`require_roles([...])` on every write
 * route) and this file decides nothing — it only keeps a screen from promising
 * what the server refuses. A button that answers 403 is a control that lies:
 * the dashboard's landing view offered every role an "Add Machine" form and a
 * "Delete Machine" button that only an Admin could press, the inventory
 * screens offered an Operator every approval, and Mission Control offered an
 * Operator Approve / Reject on an agent's proposal. Each read as a working
 * control until the banner said otherwise.
 *
 * Each helper mirrors ONE backend rule, named beside it. The lists are the
 * routes', not a policy of their own: widening one here would only re-create a
 * button that 403s.
 */

/** Admin only: POST/DELETE /machines, PATCH /cycle-counts/{id}/approve, POST /inventory/import-csv. */
export function isAdminRole(role: string): boolean {
  return role === "Admin";
}

/**
 * Admin or Supervisor: PATCH /machines/{id}/status, POST /shifts, the remnant /
 * slip / GRN / cycle-count writes in enterprise_inventory_routes, the GMATS
 * stock-in / proforma / invoice / MIN writes, POST /agent-actions/{id}/approve|reject.
 */
export function isAdminOrSupervisorRole(role: string): boolean {
  return role === "Admin" || role === "Supervisor";
}

/** The one sentence shown where a control was withheld by role. */
export function onlyRoles(who: "an Admin" | "an Admin or Supervisor", verb: string): string {
  return `Only ${who} can ${verb}.`;
}
