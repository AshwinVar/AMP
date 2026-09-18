import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { detail, periods, statement, version } from "./contracts/testFixtures";

/**
 * The factory's service-contract screen (ADR-0020).
 *
 * The property that matters most: accepting a contract GRANTS the manufacturer
 * SHARE_DOWNTIME, so the grant must be an explicit, informed choice. The screen
 * says what is shared, keeps Accept disabled until the box is ticked, and sends
 * `grant_downtime_sharing: true` with the terms hash the factory reviewed. A
 * Supervisor reads; only an Admin decides.
 */

let role = "Admin";
const fns = {
  list: vi.fn(), detail: vi.fn(), periods: vi.fn(), history: vi.fn(), statement: vi.fn(),
  accept: vi.fn(), reject: vi.fn(), reasonVocabulary: vi.fn(),
};

vi.mock("../lib/api", () => ({
  API_URL: "http://test",
  getAuthHeaders: () => ({}),
  getUserRole: () => role,
  apiGet: vi.fn(),
  apiPost: vi.fn(),
}));

vi.mock("../lib/serviceContracts", () => ({
  serviceContractsApi: new Proxy({ side: "FACTORY" } as Record<string, unknown>, {
    get: (target, name: string) =>
      name in target ? target[name]
        : name === "downloadPath" ? () => "/download"
          : (...args: unknown[]) =>
            (((fns as Record<string, unknown>)[name] ?? vi.fn().mockResolvedValue({})) as
              (...a: unknown[]) => unknown)(...args),
  }),
}));

import ServiceContracts from "./ServiceContracts";

beforeEach(() => {
  role = "Admin";
  for (const f of Object.values(fns)) f.mockReset();
  fns.list.mockResolvedValue({ contracts: [detail()], truncated: false });
  fns.detail.mockResolvedValue(detail());
  fns.periods.mockResolvedValue(periods({ periods: [] }));
  fns.history.mockResolvedValue({ history: [], truncated: false });
  fns.accept.mockResolvedValue(detail({ status: "accepted", state: "active" }));
  fns.reasonVocabulary.mockResolvedValue({
    terms_version: 1, since: "2026-03-01T00:00:00Z", until: "2026-05-30T00:00:00Z",
    reasons: [
      { reason_key: "no material", count: 4, status: "mapped", bucket: "FACTORY" },
      { reason_key: "breakdown", count: 9, status: "generic", bucket: null },
      { reason_key: "hydraulic leak", count: 2, status: "unmapped", bucket: "DISPUTED" },
    ],
  });
});

async function openProposal() {
  render(<ServiceContracts />);
  fireEvent.click(await screen.findByText("AMC-7"));
  return screen.findByTestId("contract-review");
}

describe("reviewing a proposed contract", () => {
  it("shows which of the factory's own reasons the terms map, and which would be Disputed", async () => {
    await openProposal();
    await waitFor(() => expect(screen.getByTestId("reason-vocabulary")).toBeTruthy());
    const text = screen.getByTestId("reason-vocabulary").textContent ?? "";
    expect(text).toMatch(/no material.*attributed to Factory/);
    expect(text).toMatch(/breakdown.*treated as no reason/);
    expect(text).toMatch(/hydraulic leak.*would be Disputed/);
    expect(fns.reasonVocabulary).toHaveBeenCalledWith(7);
  });

  it("says exactly what accepting shares, including that it covers the relationship", async () => {
    await openProposal();
    const disclosure = screen.getByTestId("consent-disclosure").textContent ?? "";
    expect(disclosure).toMatch(/status history/);
    expect(disclosure).toMatch(/downtime reason text/);
    expect(disclosure).toMatch(/Never production counts/);
    expect(disclosure).toMatch(/whole relationship with OEM_ALPHA/);
  });

  it("keeps Accept disabled until consent is ticked, then sends the hash and the grant", async () => {
    await openProposal();
    const accept = screen.getByRole("button", { name: "Accept contract" }) as HTMLButtonElement;
    expect(accept.disabled).toBe(true);
    fireEvent.click(accept);
    expect(fns.accept).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("checkbox"));
    expect(accept.disabled).toBe(false);
    fireEvent.click(accept);
    await waitFor(() => expect(fns.accept).toHaveBeenCalledWith(7, version().terms_hash, true));
  });

  it("shows the server's refusal when acceptance is refused", async () => {
    fns.accept.mockRejectedValue(new Error(JSON.stringify({ detail: {
      message: "Coverage cannot be accepted", problems: ["AER-0042 is not linked to a machine"] } })));
    await openProposal();
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: "Accept contract" }));
    await waitFor(() => expect(screen.getByRole("alert").textContent)
      .toBe("Coverage cannot be accepted: AER-0042 is not linked to a machine"));
  });

  it("gives a Supervisor the review but no decision", async () => {
    role = "Supervisor";
    await openProposal();
    expect(screen.queryByRole("checkbox")).toBeNull();
    expect(screen.queryByRole("button", { name: "Accept contract" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Reject" })).toBeNull();
    expect(screen.getByText(/Only an Admin can accept or reject/)).toBeTruthy();
  });
});

describe("an accepted contract", () => {
  it("opens a statement from its period", async () => {
    fns.list.mockResolvedValue({ contracts: [detail({ status: "accepted", state: "active" })],
                                 truncated: false });
    fns.detail.mockResolvedValue(detail({ status: "accepted", state: "active",
                                          versions: [version({ status: "accepted" })] }));
    fns.periods.mockResolvedValue(periods());
    fns.statement.mockResolvedValue(statement());
    render(<ServiceContracts />);
    fireEvent.click(await screen.findByText("AMC-7"));
    fireEvent.click(await screen.findByRole("button", { name: "Open" }));
    await waitFor(() => expect(screen.getByTestId("statement-view")).toBeTruthy());
    expect(fns.statement).toHaveBeenCalledWith(7, 5);
    expect(screen.queryByTestId("contract-review")).toBeNull();
    expect(screen.getByTestId("trust-note").textContent).toMatch(/not authenticated/);
  });
});
