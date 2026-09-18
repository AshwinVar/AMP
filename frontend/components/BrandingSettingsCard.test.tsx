import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as api from "../lib/api";

vi.mock("../lib/api", async (importOriginal) => {
  const real = await importOriginal<typeof import("../lib/api")>();
  return { ...real, apiGet: vi.fn(), apiPatch: vi.fn() };
});

import BrandingSettingsCard from "./BrandingSettingsCard";

/**
 * The Branding settings card. It saved a colour and a logo and showed them
 * back while the dashboard applied neither; now the sidebar does (BrandMark),
 * the preview is that same mark, and a refused save says why.
 */
beforeEach(() => {
  vi.mocked(api.apiGet).mockReset().mockResolvedValue({
    brand_name: "Acme", brand_color: "#e11d2a", brand_logo_url: null,
  });
  vi.mocked(api.apiPatch).mockReset().mockResolvedValue({});
});

describe("BrandingSettingsCard", () => {
  it("previews the sidebar's own mark, with the logo as typed", async () => {
    render(<BrandingSettingsCard />);
    const preview = await screen.findByTestId("branding-preview");
    expect(preview.textContent).toContain("Acme");
    fireEvent.change(screen.getByPlaceholderText("https://…/logo.png"),
                     { target: { value: "https://acme.test/logo.png" } });
    expect((screen.getByRole("img", { name: "Acme logo" }) as HTMLImageElement).getAttribute("src"))
      .toBe("https://acme.test/logo.png");
  });

  it("shows the server's reason when a save is refused", async () => {
    vi.mocked(api.apiPatch).mockRejectedValue(new Error(
      'Failed request: /tenant-config | 400 | {"detail":"brand_logo_url must be an https:// address (or empty, to remove the logo)"}'));
    render(<BrandingSettingsCard />);
    fireEvent.click(await screen.findByRole("button", { name: "Save branding" }));
    await waitFor(() => expect(screen.getByText(/Could not save branding/).textContent)
      .toBe("Could not save branding: brand_logo_url must be an https:// address (or empty, to remove the logo)"));
    expect(screen.queryByText(/check your permissions/)).toBeNull();
  });

  it("sends the logo as null when it is cleared", async () => {
    render(<BrandingSettingsCard />);
    fireEvent.click(await screen.findByRole("button", { name: "Save branding" }));
    await waitFor(() => expect(api.apiPatch).toHaveBeenCalledWith("/tenant-config", {
      brand_name: "Acme", brand_color: "#e11d2a", brand_logo_url: null,
    }));
  });
});
