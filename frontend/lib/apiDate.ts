/**
 * Parsing the two date shapes the API actually sends.
 *
 * The backend stores naive UTC (`Column(DateTime, default=datetime.utcnow)`)
 * and serialises with a bare `.isoformat()` — 119 sites do this. So the wire
 * carries two shapes, neither of which carries a zone:
 *
 *     instant       "2026-07-29T05:00:00.123456"   (datetime.isoformat())
 *     calendar day  "2026-07-29"                   (date.isoformat())
 *
 * JavaScript parses those two in OPPOSITE directions, and `new Date(iso)` was
 * wrong on both (ECMA-262, Date Time String Format):
 *
 *   - a date-TIME with no offset is read as LOCAL time. The instant then lands
 *     off by the viewer's whole UTC offset. In IST an event one minute old
 *     rendered as "5h ago"; west of Greenwich the difference goes negative and
 *     MissionControl's `Math.max(0, …)` pinned every row at "0s ago" forever.
 *
 *   - a DATE-ONLY string is read as UTC midnight. Rendered with the viewer's
 *     zone that is the PREVIOUS day for everyone west of Greenwich: 2026-07-29
 *     is a Wednesday, and the weekday strips labelled it "Tue" in New York.
 *
 * Two call sites had already been patched by hand with `iso + "T00:00:00"`
 * (CoverageSnapshot, PartRunwayDrawer) — the day-shift was found once and fixed
 * locally. This is that fix, generalised and applied everywhere.
 *
 * One function handles both shapes on purpose. MachineTimeline reads
 * `created_at` from the API (naive) AND from live events the dashboard injects
 * itself via `new Date().toISOString()` (Z-suffixed) — the same field, two
 * shapes, so the call site cannot be the thing that decides.
 */

/** "2026-07-29" and nothing more. */
const DATE_ONLY = /^\d{4}-\d{2}-\d{2}$/;

/**
 * A trailing Z, or a ±HH:MM / ±HHMM offset.
 *
 * The `$` is defensive rather than load-bearing, and mutation testing is what
 * established which: removing it changes nothing for any shape the backend
 * emits. The near-miss it looks like it is guarding — the "-07-29" inside the
 * date — cannot match even unanchored, because a zone needs four digits after
 * the sign and the date part never supplies them ("-07" is followed by "-29",
 * "-29" by "T0"). Keep the anchor because a zone genuinely does only occur at
 * the end; do not expect a test to fail if it goes.
 */
const HAS_ZONE = /(?:[Zz]|[+-]\d{2}:?\d{2})$/;

/**
 * Give an API date string the zone the backend meant but did not send.
 *
 * Kept separate from the parsing because it is the part that can be tested
 * without a timezone: on a UTC machine the buggy and the correct Date are
 * indistinguishable, so string-level assertions are what actually guard this.
 *
 *   "2026-07-29T05:00:00" -> "2026-07-29T05:00:00Z"          (UTC instant)
 *   "2026-07-29"          -> "2026-07-29T00:00:00"           (local midnight)
 *   "2026-07-29T05:00:00Z"-> unchanged
 *
 * A calendar day deliberately becomes LOCAL midnight, not UTC: it names a day
 * on a wall calendar, not a moment, and must read as that day for every viewer.
 */
export function normalizeApiDateString(value: string): string {
  const iso = value.trim();
  if (!iso) return iso;
  // A day is a label, not an instant — anchor it to the viewer's own midnight.
  if (DATE_ONLY.test(iso)) return `${iso}T00:00:00`;
  // Already zoned (a real offset, or a live event we stamped ourselves).
  if (HAS_ZONE.test(iso)) return iso;
  // A naive date-time from the backend is UTC, whatever the viewer's clock says.
  return `${iso}Z`;
}

/**
 * Parse a date string from the API. Returns null for null/empty/unparseable so
 * callers can render a dash instead of "Invalid Date".
 */
export function parseApiDate(value: string | null | undefined): Date | null {
  if (!value) return null;
  const d = new Date(normalizeApiDateString(value));
  return Number.isNaN(d.getTime()) ? null : d;
}
