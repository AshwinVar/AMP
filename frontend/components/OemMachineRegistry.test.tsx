import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

/**
 * Recording the cover a manufacturer is giving (ADR-0017).
 *
 * `POST /oem/machines` has accepted `warranty_start` and `warranty_end` since
 * the endpoint was written, and `registerMachine` has carried both in its type.
 * This form never offered them — so there was no way to record a warranty
 * through ANY interface, and every machine in every fleet read "no warranty end
 * date recorded for this installation". The whole chain existed except its first
 * two inches.
 *
 * The dates stay OPTIONAL, and that is the half worth protecting. AMP must not
 * decide a commercial question on a manufacturer's behalf — `warranty_state`
 * refuses to assume a period for exactly that reason — so leaving them blank has
 * to keep meaning "unknown", never an assumed twelve months and never "expired".
 */

const registerMachine = vi.fn();
const createClaim = vi.fn();
const fetchClaims = vi.fn();
const revokeClaim = vi.fn();

vi.mock("../lib/oem", () => ({
  registerMachine: (b: unknown) => registerMachine(b),
  createClaim: (id: number) => createClaim(id),
  fetchClaims: () => fetchClaims(),
  revokeClaim: (id: number) => revokeClaim(id),
}));

import OemMachineRegistry from "./OemMachineRegistry";

const MODELS = [{ id: 7, model_code: "ACX-75", name: "Aeron ACX-75" }];

beforeEach(() => {
  registerMachine.mockReset();
  createClaim.mockReset();
  fetchClaims.mockReset();
  fetchClaims.mockResolvedValue({ claims: [] });
  registerMachine.mockResolvedValue({ installation_id: 3, serial_number: "SN-1" });
  createClaim.mockResolvedValue({
    claim_code: "AMP-AAAAA-BBBBB-CCCCC",
    claim_url: "https://app.marx8.com/claim/AMP-AAAAA-BBBBB-CCCCC",
  });
});

function fill(serial: string) {
  fireEvent.change(screen.getByLabelText(/Serial number/), {
    target: { value: serial },
  });
  fireEvent.change(screen.getByLabelText(/^Model/), { target: { value: "7" } });
}

function submit() {
  fireEvent.click(screen.getByRole("button", { name: /Register and create code/ }));
}

describe("recording a warranty at registration", () => {
  it("sends the dates the manufacturer typed", async () => {
    render(<OemMachineRegistry models={MODELS} />);
    fill("SN-1");
    fireEvent.change(screen.getByLabelText(/Warranty start/), {
      target: { value: "2026-08-16" },
    });
    fireEvent.change(screen.getByLabelText(/Warranty end/), {
      target: { value: "2028-08-16" },
    });
    submit();

    await waitFor(() => expect(registerMachine).toHaveBeenCalled());
    expect(registerMachine.mock.calls[0][0]).toMatchObject({
      serial_number: "SN-1",
      model_id: 7,
      warranty_start: "2026-08-16",
      warranty_end: "2028-08-16",
    });
  });

  it("sends NULL rather than an empty string when they are left blank", async () => {
    // "" is not a date. The endpoint rejects it, so a manufacturer who does not
    // know the cover would get a 422 on a field it never filled in — and the
    // honest "unknown" answer would be unreachable.
    render(<OemMachineRegistry models={MODELS} />);
    fill("SN-2");
    submit();

    await waitFor(() => expect(registerMachine).toHaveBeenCalled());
    const body = registerMachine.mock.calls[0][0];
    expect(body.warranty_start).toBeNull();
    expect(body.warranty_end).toBeNull();
    expect(body.warranty_start).not.toBe("");
  });

  it("keeps the dates for the next machine off the same delivery note", async () => {
    // A batch shares its cover. Clearing the dates on every success means
    // retyping them per unit, which is how the fifth machine gets it wrong —
    // whereas the SERIAL must clear, because reusing one is a duplicate.
    render(<OemMachineRegistry models={MODELS} />);
    fill("SN-3");
    fireEvent.change(screen.getByLabelText(/Warranty end/), {
      target: { value: "2028-01-01" },
    });
    submit();

    await waitFor(() => expect(createClaim).toHaveBeenCalled());
    expect((screen.getByLabelText(/Warranty end/) as HTMLInputElement).value).toBe(
      "2028-01-01",
    );
    expect((screen.getByLabelText(/Serial number/) as HTMLInputElement).value).toBe("");
  });

  it("does not make them required to register a machine", async () => {
    // The submit button gates on serial and model only. A manufacturer that
    // genuinely does not know the cover must still be able to ship the machine.
    render(<OemMachineRegistry models={MODELS} />);
    fill("SN-4");
    expect(
      (screen.getByRole("button", { name: /Register and create code/ }) as HTMLButtonElement)
        .disabled,
    ).toBe(false);
  });
});
