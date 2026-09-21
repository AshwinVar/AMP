/**
 * A list is a page, and says so (ADR-0036).
 *
 * Every list the dashboard shows is the newest page of a table that grows with
 * time (work orders 200, purchase orders 500, notifications 500, ...). The
 * backend says how many rows the tenant has in all (X-Total-Count, read by
 * apiGetWithTotal). When that is more than the rows on screen, the table must
 * say so: a table that looks complete and is not misleads, and a KPI counted
 * over it is a confident wrong number. When the page IS the whole list, or
 * the count is not known (an older backend, a failed call), nothing is shown:
 * a notice that cries wolf on a complete list misleads just as much.
 */
export default function PageNotice({
  shown,
  total,
  noun,
  exportName,
  testId = "page-notice",
  className = "",
}: {
  /** rows on screen */
  shown: number;
  /** rows the tenant has in all; null/undefined when not known */
  total: number | null | undefined;
  /** "work orders", "notifications", ... */
  noun: string;
  /** the Reports CSV export that carries the complete list, when there is one */
  exportName?: string;
  testId?: string;
  className?: string;
}) {
  if (typeof total !== "number" || total <= shown) return null;
  return (
    <p className={`text-amber-300/90 text-sm ${className}`.trim()} data-testid={testId}>
      Showing the newest {shown.toLocaleString()} of {total.toLocaleString()} {noun}
      {exportName ? ` — the complete list is in the ${exportName} CSV export (Reports).` : "."}
    </p>
  );
}
