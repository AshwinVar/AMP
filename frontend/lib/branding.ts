/**
 * A tenant's branding, as the dashboard may apply it.
 *
 * The server refuses anything else on write (backend platform_routes
 * set_branding). These repeat the rule for DISPLAY, because rows written before
 * that rule existed can hold anything, and a value that cannot be applied is
 * shown as the default rather than as something broken.
 */

const BRAND_COLOR = /^#[0-9a-fA-F]{6}$/;

/** The accent colour if it is #rrggbb, else null (the default gradient). */
export function safeBrandColor(value: string | null | undefined): string | null {
  return value && BRAND_COLOR.test(value) ? value : null;
}

/**
 * The logo address if it is https://, else null (the default mark). The page's
 * CSP loads images from https: and data: only, so nothing else could render.
 */
export function safeLogoUrl(value: string | null | undefined): string | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    return url.protocol === "https:" && url.hostname ? url.href : null;
  } catch {
    return null;
  }
}
