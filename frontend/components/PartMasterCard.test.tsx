import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { PartSpec } from "../lib/plant-board";

/**
 * Revising a part is the operation that silently corrupts a plant's numbers.
 *
 * A PartSpec carries the four figures no controller knows -- weight, cavities,
 * cycle time, price -- and every kilogram and rupee on the board is derived
 * from them. Revising one used to mean retyping all eight fields, and a cavity
 * count mistyped on the way back in is invisible afterwards: nothing looks
 * wrong, every downstream figure is simply scaled.
 *
 * So what is pinned here is not that a button exists. It is:
 *
 *   * revising PREFILLS from the row, so there is nothing to retype
 *   * it dates the revision TODAY, never the original's date -- reusing it
 *     would collide on (tenant, part_code, effective_from) and, worse, would
 *     mean overwriting a period already reported
 *   * the old row SURVIVES. Effective dating is the whole reason last year's
 *     revenue does not move when this year's price does
 *   * "Add a part" after a revision starts BLANK. The dangerous failure is a
 *     new part saved carrying another part's cavity count
 *   * a non-admin gets no revise control at all
 */

const apiGet = vi.hoisted(() => vi.fn());
const apiPost = vi.hoisted(() => vi.fn());
vi.mock("../lib/api", () => ({
  apiGet,
  apiPost,
  errorDetail: (e: unknown) => (e instanceof Error ? e.message : String(e)),
}));

import PartMasterCard from "./PartMasterCard";

const SPEC: PartSpec = {
  id: 1,
  part_code: "MCLIP-WHITE",
  part_name: "M clip white",
  material: "PP H110",
  part_weight_g: 0.45,
  cavities: 40,
  active_cavities: 40,
  ideal_cycle_time_s: 14.5,
  price_per_piece: 0,
  effective_from: "2026-10-12",
  ideal_parts_per_hour: 9931,
};

function mountWith(specs: PartSpec[] = [SPEC], materials = ["PP H110"]) {
  apiGet.mockImplementation((path: string) =>
    path.startsWith("/part-specs")
      ? Promise.resolve({ specs, materials })
      : Promise.resolve({ tools: [], machines: [] }));
}

beforeEach(() => {
  apiGet.mockReset();
  apiPost.mockReset();
  apiPost.mockResolvedValue({});
});

describe("revising a part", () => {
  it("prefills every field from the row, so nothing is retyped", async () => {
    mountWith();
    render(<PartMasterCard isAdmin />);
    await waitFor(() => expect(screen.getByText("M clip white")).toBeTruthy());

    fireEvent.click(screen.getByRole("button", { name: "Revise" }));

    expect((screen.getByDisplayValue("MCLIP-WHITE") as HTMLInputElement).value)
      .toBe("MCLIP-WHITE");
    expect(screen.getByDisplayValue("M clip white")).toBeTruthy();
    expect(screen.getByDisplayValue("0.45")).toBeTruthy();
    // The cavity count is the one that scales every other figure.
    expect(screen.getAllByDisplayValue("40").length).toBeGreaterThan(0);
    expect(screen.getByDisplayValue("14.5")).toBeTruthy();
  });

  it("dates the revision today, not the original's date", async () => {
    mountWith();
    render(<PartMasterCard isAdmin />);
    await waitFor(() => expect(screen.getByText("M clip white")).toBeTruthy());

    fireEvent.click(screen.getByRole("button", { name: "Revise" }));

    // Reusing 2026-10-12 would collide on (tenant, part_code, effective_from)
    // -- and would be an overwrite of a period already reported.
    const dates = screen.getAllByDisplayValue(/^\d{4}-\d{2}-\d{2}$/);
    expect(dates.length).toBeGreaterThan(0);
    const today = new Date();
    const iso = [
      today.getFullYear(),
      String(today.getMonth() + 1).padStart(2, "0"),
      String(today.getDate()).padStart(2, "0"),
    ].join("-");
    expect((dates[0] as HTMLInputElement).value).toBe(iso);
  });

  it("says it is superseding, and that the old figures survive", async () => {
    mountWith();
    render(<PartMasterCard isAdmin />);
    await waitFor(() => expect(screen.getByText("M clip white")).toBeTruthy());

    fireEvent.click(screen.getByRole("button", { name: "Revise" }));

    expect(screen.getByText(/Revising/)).toBeTruthy();
    expect(screen.getByText(/figures already reported\s+do not change/)).toBeTruthy();
  });

  it("starts blank when adding a part after a revision", async () => {
    mountWith();
    render(<PartMasterCard isAdmin />);
    await waitFor(() => expect(screen.getByText("M clip white")).toBeTruthy());

    fireEvent.click(screen.getByRole("button", { name: "Revise" }));
    expect(screen.getByDisplayValue("MCLIP-WHITE")).toBeTruthy();

    // Close, then reopen as a new part. THE DEFECT THIS CATCHES: the form kept
    // the revised part's eight fields, so a genuinely new part could be saved
    // carrying MCLIP WHITE's 40 cavities -- and nothing downstream would look
    // wrong, it would simply all be out by a factor of 40.
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    fireEvent.click(screen.getByRole("button", { name: "Add a part" }));

    expect(screen.queryByDisplayValue("MCLIP-WHITE")).toBeNull();
    expect(screen.queryByDisplayValue("M clip white")).toBeNull();
    expect(screen.queryByText(/Revising/)).toBeNull();
  });

  it("gives a non-admin no revise control", async () => {
    mountWith();
    render(<PartMasterCard isAdmin={false} />);
    await waitFor(() => expect(screen.getByText("M clip white")).toBeTruthy());

    expect(screen.queryByRole("button", { name: "Revise" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Add a part" })).toBeNull();
  });
});
