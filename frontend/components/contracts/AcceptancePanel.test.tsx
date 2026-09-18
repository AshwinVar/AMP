import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import AcceptancePanel from "./AcceptancePanel";
import { acceptance, dispute, fakeApi, HASH, statement } from "./testFixtures";

/**
 * Accepting an exact revision (ADR-0021).
 *
 * An acceptance names a hash AND a revision; a later revision cancels it even
 * when the bytes come back the same. The panel must show that per revision, send
 * both values on Accept, block Accept (saying why) while disputes are live, and
 * check consistency over the exact bytes it downloaded.
 */

afterEach(() => {
  vi.unstubAllGlobals();
});

// The backend's canonical bytes for a fixture whose content_hash is HASH.
const CANONICAL = "{\"bucket\":\"FACTORY\",\"reason\":\"Ölpumpe ausgefallen — "
  + "नो मटेरियल\",\"seconds\":3600}";

function api(overrides: Record<string, ReturnType<typeof vi.fn>> = {}) {
  return fakeApi("FACTORY", (name) => overrides[name] ?? vi.fn().mockResolvedValue({}));
}

describe("AcceptancePanel", () => {
  it("sends the hash AND the revision it is showing", async () => {
    const acceptStatement = vi.fn().mockResolvedValue({});
    const onChanged = vi.fn();
    render(<AcceptancePanel api={api({ acceptStatement })} contractId={7}
                            statement={statement()} canSign onChanged={onChanged} />);
    fireEvent.click(screen.getByRole("button", { name: "Accept revision 2" }));
    await waitFor(() => expect(acceptStatement).toHaveBeenCalledWith(7, 5, HASH, 2));
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
  });

  it("is disabled with the reason while a dispute is open", () => {
    render(<AcceptancePanel api={api()} contractId={7} canSign onChanged={vi.fn()}
                            statement={statement({ disputes: [dispute()] })} />);
    expect((screen.getByRole("button", { name: "Accept revision 2" }) as HTMLButtonElement).disabled)
      .toBe(true);
    expect(screen.getByTestId("accept-blockers").textContent)
      .toBe("1 open dispute must be resolved or withdrawn first");
  });

  it("shows acceptances per revision, cancelled ones as cancelled", () => {
    render(<AcceptancePanel api={api()} contractId={7} canSign onChanged={vi.fn()}
                            statement={statement({ revision: 3, acceptances: [
                              acceptance({ party: "OEM", revision: 1, valid: false }),
                              acceptance({ party: "FACTORY", revision: 3, valid: true }),
                            ] })} />);
    expect(screen.getByTestId("acceptances-r1").textContent).toMatch(/OEM accepted .*cancelled/);
    expect(screen.getByTestId("acceptances-r1").textContent).not.toMatch(/valid/);
    expect(screen.getByTestId("acceptances-r3").textContent).toMatch(/\(current\).*FACTORY accepted .*valid/);
    // The factory has accepted revision 3: its own Accept is blocked, saying so.
    expect(screen.getByTestId("accept-blockers").textContent)
      .toBe("You have already accepted revision 3");
  });

  it("offers no Accept to a party that cannot sign", () => {
    render(<AcceptancePanel api={api()} contractId={7} canSign={false} onChanged={vi.fn()}
                            statement={statement()} />);
    expect(screen.queryByRole("button", { name: /Accept revision/ })).toBeNull();
    expect(screen.getByRole("button", { name: "Check consistency" })).toBeTruthy();
  });

  it("shows the server's refusal in its own words", async () => {
    const acceptStatement = vi.fn().mockRejectedValue(new Error(JSON.stringify({ detail: {
      message: "The statement is not the one you reviewed; review the current revision",
      reason: "hash_mismatch" } })));
    render(<AcceptancePanel api={api({ acceptStatement })} contractId={7} canSign
                            onChanged={vi.fn()} statement={statement()} />);
    fireEvent.click(screen.getByRole("button", { name: "Accept revision 2" }));
    await waitFor(() => expect(screen.getByRole("alert").textContent)
      .toBe("The statement is not the one you reviewed; review the current revision"));
  });

  it("checks consistency by hashing the exact downloaded bytes in the browser", async () => {
    const bytes = new TextEncoder().encode(CANONICAL);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true, status: 200, headers: new Headers({ "X-Content-SHA256": HASH }),
      arrayBuffer: async () => bytes.buffer.slice(0),
    }));
    const verify = vi.fn().mockResolvedValue({
      statement_id: 5, revision: 2, stored_hash: HASH, blob_hash: HASH, records_hash: "r",
      consistency: "consistent", acceptances: [], agreed: false,
      live: { hash: null, matches: null, reason: "evidence_expired" },
    });
    render(<AcceptancePanel api={api({ verify })} contractId={7} canSign onChanged={vi.fn()}
                            statement={statement()} />);
    fireEvent.click(screen.getByRole("button", { name: "Check consistency" }));
    await waitFor(() => expect(screen.getByTestId("consistency")).toBeTruthy());
    const text = screen.getByTestId("consistency").textContent ?? "";
    expect(text).toMatch(/Server: Consistent/);
    expect(text).toMatch(/matches revision 2/);
    expect(text).toMatch(/not possible \(evidence_expired\)/);
    expect(text).toMatch(/does not prove/);
  });

  it("says so when the downloaded bytes do not match", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true, status: 200, headers: new Headers(),
      arrayBuffer: async () => new TextEncoder().encode(CANONICAL + " ").buffer,
    }));
    const verify = vi.fn().mockResolvedValue({
      statement_id: 5, revision: 2, stored_hash: HASH, blob_hash: "x", records_hash: "r",
      consistency: "blob_mismatch", acceptances: [], agreed: false,
      live: { hash: HASH, matches: true, reason: null },
    });
    render(<AcceptancePanel api={api({ verify })} contractId={7} canSign onChanged={vi.fn()}
                            statement={statement()} />);
    fireEvent.click(screen.getByRole("button", { name: "Check consistency" }));
    await waitFor(() => expect(screen.getByTestId("consistency")).toBeTruthy());
    const text = screen.getByTestId("consistency").textContent ?? "";
    expect(text).toMatch(/NOT consistent/);
    expect(text).toMatch(/does NOT match/);
  });
});
