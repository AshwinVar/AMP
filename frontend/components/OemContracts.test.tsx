import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { OemRequestError } from "../lib/oem";
import { detail, periods, statement, version } from "./contracts/testFixtures";

/**
 * Service contracts in the manufacturer's portal (ADR-0020).
 *
 * Two properties. CONSENT: when the factory has withdrawn SHARE_DOWNTIME, the
 * manufacturer sees "sharing withdrawn by factory" and NO numbers — not a zero,
 * not a stale statement, not an error banner. CAPABILITIES: a viewer reads and
 * cannot draft, compute, accept, dispute or terminate (the server refuses those
 * anyway; the screen does not offer them).
 */

const fns = {
  list: vi.fn(), detail: vi.fn(), periods: vi.fn(), history: vi.fn(), statement: vi.fn(),
  propose: vi.fn(), withdraw: vi.fn(), create: vi.fn(), compute: vi.fn(),
};

vi.mock("../lib/api", () => ({ API_URL: "http://test", getAuthHeaders: () => ({}) }));

vi.mock("../lib/oemContracts", () => ({
  oemContractsApi: new Proxy({ side: "OEM" } as Record<string, unknown>, {
    get: (target, name: string) =>
      name in target ? target[name]
        : name === "downloadPath" ? () => "/download"
          : (...args: unknown[]) =>
            (((fns as Record<string, unknown>)[name] ?? vi.fn().mockResolvedValue({})) as
              (...a: unknown[]) => unknown)(...args),
  }),
}));

import OemContracts from "./OemContracts";

const ALL = ["read_fleet", "read_contracts", "manage_contracts", "sign_contracts"];
const ACCEPTED = detail({ status: "accepted", state: "active",
                          versions: [version({ status: "accepted" })] });

beforeEach(() => {
  for (const f of Object.values(fns)) f.mockReset();
  fns.list.mockResolvedValue({ contracts: [ACCEPTED], truncated: false });
  fns.detail.mockResolvedValue(ACCEPTED);
  fns.periods.mockResolvedValue(periods());
  fns.history.mockResolvedValue({ history: [
    { at: "2026-06-01T00:00:00Z", actor: "oem:OEM_ALPHA:alpha_admin",
      action: "contract_statement_computed", entity_type: "contract_statement", entity_id: 5,
      details: null, details_withheld: true },
  ], truncated: false });
  fns.statement.mockResolvedValue(statement());
});

describe("who sees service contracts", () => {
  it("renders nothing, and asks for nothing, without read_contracts", () => {
    const { container } = render(<OemContracts capabilities={["read_fleet"]} fleet={[]} />);
    expect(container.textContent).toBe("");
    expect(fns.list).not.toHaveBeenCalled();
  });

  it("gives a viewer the contract and its statement, and no action on either", async () => {
    render(<OemContracts capabilities={["read_fleet", "read_contracts"]} fleet={[]} />);
    fireEvent.click(await screen.findByText("AMC-7"));
    fireEvent.click(await screen.findByRole("button", { name: "Open" }));
    await waitFor(() => expect(screen.getByTestId("statement-view")).toBeTruthy());
    for (const name of [/Draft a service contract/, /Compute/, /Recompute/, /Accept revision/,
                        /Raise dispute/, /Terminate/, /Draft an amendment/]) {
      expect(screen.queryByRole("button", { name }), String(name)).toBeNull();
    }
  });

  it("gives an administrator the actions", async () => {
    render(<OemContracts capabilities={ALL} fleet={[]} />);
    expect(screen.getByRole("button", { name: /Draft a service contract/ })).toBeTruthy();
    fireEvent.click(await screen.findByText("AMC-7"));
    expect(await screen.findByRole("button", { name: "Recompute" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Terminate" })).toBeTruthy();
  });
});

describe("when the factory has withdrawn downtime sharing", () => {
  const WITHHELD = new OemRequestError(403, "withheld until it does", {
    withheld: true, reason: "sharing withdrawn by factory",
    message: "This factory does not currently share downtime with you (SHARE_DOWNTIME).",
  });

  it("says sharing was withdrawn, shows no numbers and offers no compute", async () => {
    fns.detail.mockResolvedValue({ ...ACCEPTED, downtime_shared: false });
    fns.statement.mockRejectedValue(WITHHELD);
    render(<OemContracts capabilities={ALL} fleet={[]} />);
    fireEvent.click(await screen.findByText("AMC-7"));
    await waitFor(() => expect(screen.getAllByTestId("withheld").length).toBeGreaterThan(0));
    expect(screen.queryByRole("button", { name: /Compute|Recompute/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Preview/ })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Open" }));
    await waitFor(() => expect(screen.getAllByTestId("withheld").length).toBe(2));
    expect(screen.queryByTestId("statement-view")).toBeNull();
    const detailText = screen.getByTestId("contract-detail").textContent ?? "";
    expect(detailText).toMatch(/Sharing withdrawn by factory/);
    // The TERMS stay visible (their fee included); nothing of a statement does.
    expect(screen.queryByTestId("credit")).toBeNull();
    expect(screen.queryByTestId("sla-state")).toBeNull();
    expect(detailText).not.toMatch(/No data:|Available:|SLA (met|breached)|Revision \d/);
    // Not an error: nothing went wrong, the factory decided.
    expect(screen.queryByRole("alert")).toBeNull();
    // The history still lists that a statement was computed, without its details.
    expect(detailText).toMatch(/withheld: the factory has withdrawn downtime sharing/);
  });
});

describe("offering a draft", () => {
  it("proposes the exact terms hash of version 1", async () => {
    const draft = detail({ status: "draft", state: "draft",
                           versions: [version({ status: "draft", terms_hash: "d".repeat(64) })] });
    fns.list.mockResolvedValue({ contracts: [draft], truncated: false });
    fns.detail.mockResolvedValue(draft);
    fns.propose.mockResolvedValue({ ...draft, status: "proposed" });
    render(<OemContracts capabilities={ALL} fleet={[]} />);
    fireEvent.click(await screen.findByText("AMC-7"));
    fireEvent.click(await screen.findByRole("button", { name: "Propose to FACTORY_A" }));
    await waitFor(() => expect(fns.propose).toHaveBeenCalledWith(7, "d".repeat(64)));
  });
});
