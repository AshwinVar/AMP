import vocab from "./status-vocab.json";

/**
 * The status / priority / type vocabularies a `<select>` may offer, stated ONCE.
 *
 * THE DEFECT THIS EXISTS FOR
 * --------------------------
 * A controlled `<select value={row.status}>` whose `<option>` list does not
 * contain `row.status` renders with NOTHING selected — an empty box. React does
 * not warn, the row still exists, and the operator simply cannot read the
 * status. Six sections hardcoded their option lists, and those lists had drifted
 * away from what the backend actually writes. Seeding a real factory and
 * counting: **42 of 123 rows (34%) rendered a blank dropdown**, including 12 of
 * 12 quality inspections, because nothing in AMP ever writes the four statuses
 * that screen offered.
 *
 * Worse than cosmetic on the agent path: a maintenance task or escalation the
 * AI proposed carries status "Proposed", which no list offered. An operator who
 * clicked the blank box to see what it was would overwrite it — and
 * `ai/agents.py` only transitions `if item.status == "Proposed"`, so approving
 * OR rejecting that task afterwards silently does nothing, forever.
 *
 * THE RULE
 * --------
 * `options` are what a HUMAN may choose. `systemOnly` are values only an agent
 * or a seeder writes ("Proposed", a reorder agent's "Draft") — a human must not
 * be able to pick them, but the box must still SHOW them when a row holds one.
 *
 * `statusOptions()` returns `options`, plus the row's CURRENT value when that
 * value is not already in the list. That is the complement shape, not a
 * whitelist: a value nobody enumerated is DISPLAYED rather than blanked. A
 * vocabulary can always drift again; a dropdown built this way degrades to
 * "shows the truth" instead of to "shows nothing".
 *
 * The vocabulary itself lives in status-vocab.json rather than here because
 * backend/test_status_vocabulary_parity.py reads the SAME file and fails if the
 * backend learns to write a value this list cannot display. One rule, one file,
 * two readers — the alternative is the two-implementations drift that caused
 * the defect in the first place.
 */
type Vocab = Record<string, Record<string, { options: string[]; systemOnly: string[] }>>;

const VOCAB = vocab as Vocab;

/** Every value the backend may hold for this field — what a guard checks against. */
export function knownValues(model: string, field: string): string[] {
  const entry = VOCAB[model]?.[field];
  if (!entry) return [];
  return [...entry.options, ...entry.systemOnly];
}

/**
 * The `<option>` list for a controlled select bound to `current`.
 *
 * Never returns a list that cannot render `current`: an unrecognised value is
 * appended rather than dropped, so the box shows what the row actually holds.
 */
export function statusOptions(model: string, field: string, current?: string | null): string[] {
  const entry = VOCAB[model]?.[field];
  const options = entry ? [...entry.options] : [];
  const value = typeof current === "string" ? current.trim() : "";
  if (value && !options.includes(value)) options.push(value);
  return options;
}
