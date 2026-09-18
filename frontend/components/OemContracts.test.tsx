import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { termsTemplate } from "../lib/contracts";
import { OemRequestError, type FleetMachine } from "../lib/oem";
import { detail, periods, statement, version } from "./contracts/testFixtures";

/**
 * Service contracts in the manufacturer's portal (ADR-0021).
 *
 * Two properties. CONSENT: when the factory has withdrawn SHARE_DOWNTIME, the
 * manufacturer sees "sharing withdrawn by factory" and NO numbers — not a zero,
 * not a stale statement, not an error banner. CAPABILITIES: a viewer reads and
 * cannot draft, compute, accept, dispute or terminate (the server refuses those
 * anyway; the screen does not offer them).
 */

const fns = {
  list: vi.fn(), detail: vi.fn(), periods: vi.fn(), history: vi.fn(), statement: vi.fn(),
  propose: vi.fn(), withdraw: vi.fn(), create: vi.fn(), editDraft: vi.fn(), compute: vi.fn(),
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

describe("editing a draft before it is proposed", () => {
  // A reference is never reused (withdrawn contracts keep theirs), so a draft that
  // could not be corrected cost the manufacturer its reference over a typo.
  // Not the new-contract defaults (type AMC, the template's fee), so a form that
  // opened on those instead of on the draft is told apart.
  const DRAFT_TERMS = { ...termsTemplate([{ installation_id: 12, serial_number: "AER-0042" }]),
                        period_fee: "52000.00" };
  const DRAFT = detail({ status: "draft", state: "draft", contract_type: "WARRANTY",
                         versions: [version({ status: "draft", terms_hash: "d".repeat(64),
                                              terms: DRAFT_TERMS })] });
  const machine = (id: number, serial: string): FleetMachine => ({
    installation_id: id, serial_number: serial, model_code: "ACX-75", model_name: null,
    customer: "FACTORY_A", site: "", lifecycle_status: "Active", installed_at: null,
    commissioned_at: null, warranty_start: null, warranty_end: null, operating_hours: null,
    last_seen_at: null, machine_status: null, utilization: null, shared: [],
  });
  const FLEET = [machine(12, "AER-0042"), machine(13, "AER-0043")];
  const box = (serial: string) => screen.getByRole("checkbox", { name: serial }) as HTMLInputElement;

  beforeEach(() => {
    fns.list.mockResolvedValue({ contracts: [DRAFT], truncated: false });
    fns.detail.mockResolvedValue(DRAFT);
    fns.editDraft.mockResolvedValue(DRAFT);
  });

  async function openEditor(fleet: FleetMachine[] = FLEET) {
    render(<OemContracts capabilities={ALL} fleet={fleet} />);
    fireEvent.click(await screen.findByText("AMC-7"));
    fireEvent.click(await screen.findByRole("button", { name: "Edit draft" }));
    return screen.getByRole("textbox", { name: "Contract terms" }) as HTMLTextAreaElement;
  }

  it("opens the draft as it was saved: reference, first month in its own zone, terms, machines", async () => {
    const terms = await openEditor();
    expect(screen.getByDisplayValue("AMC-7")).toBeTruthy();
    // starts_at 2026-04-30T18:30:00Z is May in Asia/Kolkata, not April.
    expect(screen.getByDisplayValue("2026-05")).toBeTruthy();
    const { covered_installations: _machines, ...rest } = DRAFT_TERMS;
    void _machines;
    expect(JSON.parse(terms.value)).toEqual(rest);
    expect((screen.getByRole("combobox", { name: "Type" }) as HTMLSelectElement).value).toBe("WARRANTY");
    expect(box("AER-0042").checked).toBe(true);
    expect(box("AER-0043").checked).toBe(false);
    // Not offered for proposal until the edit is saved or closed.
    expect(screen.queryByRole("button", { name: "Propose to FACTORY_A" })).toBeNull();
  });

  it("saves by replacing the draft, never by creating a second contract", async () => {
    const terms = await openEditor();
    fireEvent.change(terms, { target: { value: terms.value.replace('"52000.00"', '"45000.00"') } });
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(fns.editDraft).toHaveBeenCalledTimes(1));
    const [id, body] = fns.editDraft.mock.calls[0];
    expect(id).toBe(7);
    expect(body).toMatchObject({ contract_ref: "AMC-7", title: "Annual maintenance",
                                 contract_type: "WARRANTY", factory_tenant_code: "FACTORY_A",
                                 start_month: "2026-05" });
    expect(body.terms.period_fee).toBe("45000.00");
    expect(body.terms.covered_installations).toEqual([{ installation_id: 12, serial_number: "AER-0042" }]);
    expect(fns.create).not.toHaveBeenCalled();
    // Saved: the editor closes and the draft can be proposed again.
    expect(await screen.findByRole("button", { name: "Propose to FACTORY_A" })).toBeTruthy();
  });

  it("keeps a covered machine that the loaded fleet page does not list", async () => {
    await openEditor([]);
    expect(box("AER-0042").checked).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(fns.editDraft).toHaveBeenCalledTimes(1));
    expect(fns.editDraft.mock.calls[0][1].terms.covered_installations)
      .toEqual([{ installation_id: 12, serial_number: "AER-0042" }]);
  });

  it("offers no edit once the contract is proposed", async () => {
    fns.list.mockResolvedValue({ contracts: [detail()], truncated: false });
    fns.detail.mockResolvedValue(detail());
    render(<OemContracts capabilities={ALL} fleet={FLEET} />);
    fireEvent.click(await screen.findByText("AMC-7"));
    expect(await screen.findByRole("button", { name: "Withdraw" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Edit draft" })).toBeNull();
  });

  it("lets a signer without manage_contracts propose the draft, but not edit it", async () => {
    render(<OemContracts capabilities={["read_fleet", "read_contracts", "sign_contracts"]} fleet={FLEET} />);
    fireEvent.click(await screen.findByText("AMC-7"));
    expect(await screen.findByRole("button", { name: "Propose to FACTORY_A" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Edit draft" })).toBeNull();
  });
});
