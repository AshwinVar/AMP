import { describe, expect, it, vi } from "vitest";
import { AGENT_ACTIONS_PAGE, loadAgentActions, type AgentAction } from "./agent-actions";

/**
 * The Approvals list is the only way out for an item an agent proposal holds
 * (ADR-0015 addendum), and the server answers one page of at most 300 rows. A
 * screen that made a single request lost every proposal older than the newest
 * 300 -- measured: a held Draft PO locked, with its proposal nowhere in the
 * inbox. These pin the paging every agent-action screen now shares.
 */

function action(id: number): AgentAction {
  return {
    id, agent: "reorder", action_type: "draft_po", summary: `proposal ${id}`,
    ref_kind: "purchase_order", ref_id: id, severity: "Medium", status: "Proposed",
    related_machine_id: null, created_at: "2026-09-17T08:00:00", decided_by: null,
    decided_at: null, expired: false,
  };
}

const ids = (from: number, count: number) => Array.from({ length: count }, (_, i) => action(from + i));

function server(table: AgentAction[], ignoreOffset = false) {
  return vi.fn(async (path: string) => {
    const query = new URLSearchParams(path.split("?")[1]);
    const limit = Number(query.get("limit"));
    const offset = ignoreOffset ? 0 : Number(query.get("offset"));
    return table.slice(offset, offset + limit);
  });
}

describe("loadAgentActions", () => {
  it("asks for one page with the status, and says nothing more exists after a short page", async () => {
    const get = server(ids(1, 3));
    const page = await loadAgentActions(get, "Proposed");
    expect(get).toHaveBeenCalledTimes(1);
    const query = new URLSearchParams(get.mock.calls[0][0].split("?")[1]);
    expect(get.mock.calls[0][0].startsWith("/agent-actions?")).toBe(true);
    expect([query.get("status"), query.get("limit"), query.get("offset")]).toEqual(["Proposed", "300", "0"]);
    expect(page.rows.map((r) => r.id)).toEqual([1, 2, 3]);
    expect(page.more).toBe(false);
  });

  it("sends no status when asked for every action", async () => {
    const get = server(ids(1, 1));
    await loadAgentActions(get, null);
    expect(new URLSearchParams(get.mock.calls[0][0].split("?")[1]).has("status")).toBe(false);
  });

  it("stops at a full first page when that is all it was asked for, and says more may exist", async () => {
    const get = server(ids(1, AGENT_ACTIONS_PAGE + 1));
    const page = await loadAgentActions(get, "Proposed", AGENT_ACTIONS_PAGE);
    expect(get).toHaveBeenCalledTimes(1);
    expect(page.rows).toHaveLength(AGENT_ACTIONS_PAGE);
    expect(page.more).toBe(true);
  });

  it("walks to the next page for a proposal older than the first 300", async () => {
    const get = server(ids(1, AGENT_ACTIONS_PAGE + 1));
    const page = await loadAgentActions(get, "Proposed", 2 * AGENT_ACTIONS_PAGE);
    expect(get.mock.calls.map(([p]) => new URLSearchParams(p.split("?")[1]).get("offset"))).toEqual(["0", "300"]);
    expect(page.rows).toHaveLength(AGENT_ACTIONS_PAGE + 1);
    expect(page.rows[AGENT_ACTIONS_PAGE].id).toBe(AGENT_ACTIONS_PAGE + 1);
    expect(page.more).toBe(false);
  });

  it("keeps a row once when it shifts across a page boundary while loading", async () => {
    const first = ids(1, AGENT_ACTIONS_PAGE);
    const get = vi.fn(async (path: string) =>
      new URLSearchParams(path.split("?")[1]).get("offset") === "0"
        ? first
        : [first[AGENT_ACTIONS_PAGE - 1], action(999)]);
    const page = await loadAgentActions(get, "Proposed", 2 * AGENT_ACTIONS_PAGE);
    expect(page.rows.map((r) => r.id).filter((id) => id === AGENT_ACTIONS_PAGE)).toHaveLength(1);
    expect(page.rows.at(-1)?.id).toBe(999);
  });

  it("does not loop for ever against a server that ignores the offset", async () => {
    const get = server(ids(1, AGENT_ACTIONS_PAGE), true);
    const page = await loadAgentActions(get, "Proposed", 10 * AGENT_ACTIONS_PAGE);
    expect(get).toHaveBeenCalledTimes(2);
    expect(page.rows).toHaveLength(AGENT_ACTIONS_PAGE);
  });
});
