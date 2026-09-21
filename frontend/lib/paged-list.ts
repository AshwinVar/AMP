/**
 * A list is a page, and the page can grow (ADR-0036).
 *
 * Every capped list the dashboard shows is the newest page of a table that
 * grows with time; the backend says how many rows the tenant has in all
 * (X-Total-Count, read by apiGetWithTotal). This module is the client side of
 * that contract, kept out of the dashboard component so it can be tested on
 * its own. One ListState per screen remembers, per list path:
 *
 *   totals[path]   the tenant's whole count (null until known)
 *   pages[path]    the size of the endpoint's default page, learned once --
 *                  the size of the batch "Show more" adds
 *   depth[path]    how many rows the user asked to see; carried by every later
 *                  poll round, so the list does not snap back to page one three
 *                  seconds later. Capped at the backend's own ceiling
 *                  (paging.MAX_PAGE) and at the tenant's total.
 */

/** Mirrors backend paging.MAX_PAGE: the most rows one request returns. */
export const MAX_PAGE = 2000;

export type ListState = {
  totals: Record<string, number | null>;
  pages: Record<string, number>;
  depth: Record<string, number>;
};

export function emptyListState(): ListState {
  return { totals: {}, pages: {}, depth: {} };
}

/** `path` asking for `limit` rows (the endpoint's default page when unset). */
export function pagedPath(path: string, limit?: number): string {
  if (typeof limit !== "number" || !(limit > 0)) return path;
  const bounded = Math.min(MAX_PAGE, Math.floor(limit));
  return `${path}${path.includes("?") ? "&" : "?"}limit=${bounded}`;
}

/**
 * Fetch one list at the depth the user asked for (the default page until they
 * ask). The state learns the tenant's whole count, and -- from the default
 * page, the first time -- the endpoint's page size. Returns the rows.
 */
export async function pagedList<T>(
  get: (path: string) => Promise<{ data: T[]; total: number | null }>,
  state: ListState,
  path: string,
): Promise<T[]> {
  const depth = state.depth[path];
  const { data, total } = await get(pagedPath(path, depth));
  const rows = Array.isArray(data) ? data : [];
  state.totals[path] = total;
  if (typeof depth !== "number" && !(path in state.pages)) {
    state.pages[path] = rows.length;
  }
  return rows;
}

/**
 * The depth to ask for after "Show more": one more default page on top of what
 * is shown, never past the tenant's total or the backend ceiling. `null` when
 * there is nothing more to show (the page is everything, the count is unknown,
 * or the ceiling is reached).
 */
export function nextDepth(shown: number, total: number | null | undefined, page: number): number | null {
  if (typeof total !== "number" || total <= shown || shown >= MAX_PAGE) return null;
  const step = page > 0 ? page : shown;
  return Math.min(MAX_PAGE, total, shown + step);
}

/** How many more rows "Show more" would add for `path`, or null when none. */
export function moreFor(state: Pick<ListState, "totals" | "pages">, path: string, shown: number): number | null {
  const depth = nextDepth(shown, state.totals[path], state.pages[path] ?? 0);
  return depth === null ? null : depth - shown;
}

/**
 * Record the user's wish to see more of `path`. Returns the new depth, or null
 * when there was nothing more to ask for (and nothing was changed).
 */
export function askForMore(state: ListState, path: string, shown: number): number | null {
  const depth = nextDepth(shown, state.totals[path], state.pages[path] ?? 0);
  if (depth !== null) state.depth[path] = depth;
  return depth;
}
