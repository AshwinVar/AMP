"use client";

import React, { useState } from "react";

import { safeBrandColor, safeLogoUrl } from "../lib/branding";

/**
 * The workspace's mark at the top of the sidebar: its logo, or the AMP glyph on
 * its accent colour.
 *
 * The Branding settings card saved a colour and a logo and showed them back,
 * and the dashboard applied neither (only the name). This is where they apply.
 * A logo that fails to load falls back to the glyph rather than a broken image,
 * and it is fetched without a referrer, so the workspace's address is not sent
 * to whoever hosts the logo.
 */
export default function BrandMark({ name, color, logoUrl }: {
  name: string;
  color?: string | null;
  logoUrl?: string | null;
}) {
  const logo = safeLogoUrl(logoUrl);
  const [failed, setFailed] = useState<string | null>(null);
  const accent = safeBrandColor(color);

  if (logo && failed !== logo) {
    return (
      <div className="phase29-brand-mark overflow-hidden" data-testid="brand-mark">
        {/* eslint-disable-next-line @next/next/no-img-element -- a tenant's own https logo, any host */}
        <img src={logo} alt={`${name} logo`} referrerPolicy="no-referrer"
             onError={() => setFailed(logo)} className="h-full w-full object-contain" />
      </div>
    );
  }
  return (
    <div className="phase29-brand-mark" data-testid="brand-mark" aria-hidden="true"
         style={accent ? { background: accent } : undefined}>
      ⌁
    </div>
  );
}
