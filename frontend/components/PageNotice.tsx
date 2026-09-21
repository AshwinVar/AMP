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
 *
 * When the screen can ask for a deeper page (lib/paged-list), the notice also
 * offers it: "Show the next 200". The button is only there when `more` says
 * how many rows the next batch adds -- never past the tenant's total or the
 * backend's ceiling -- so a list that is everything, or one already at the
 * ceiling, offers nothing.
 */
export default function PageNotice({
  shown,
  total,
  noun,
  exportName,
  more = null,
  onMore,
  order = "newest",
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
  /** rows the next batch would add (lib/paged-list moreFor); null when none */
  more?: number | null;
  /** asks the screen for the next batch; the button needs both this and `more` */
  onMore?: () => void;
  /** which end of the list the page holds: the dashboard's lists are newest
   *  first; the OEM fleet is listed in the order machines were registered */
  order?: "newest" | "first";
  testId?: string;
  className?: string;
}) {
  if (typeof total !== "number" || total <= shown) return null;
  const offer = onMore && typeof more === "number" && more > 0;
  return (
    <p className={`text-amber-300/90 text-sm ${className}`.trim()} data-testid={testId}>
      Showing the {order === "first" ? "first" : "newest"} {shown.toLocaleString()} of {total.toLocaleString()} {noun}
      {exportName ? ` — the complete list is in the ${exportName} CSV export (Reports).` : "."}
      {offer && (
        <>
          {" "}
          <button
            type="button"
            onClick={onMore}
            className="ml-1 rounded-lg border border-amber-400/40 px-2 py-0.5 text-xs text-amber-200 hover:bg-amber-400/10"
          >
            Show the next {more.toLocaleString()}
          </button>
        </>
      )}
    </p>
  );
}
