# ADR-0036: A list is a page, and says so

**Date:** 2026-09-21
**Status:** Accepted
**Builds on:** ADR-0002 (tenant scoping), ADR-0010 (no row cap on `/machines`), `test_growing_table_reads.py` (no polled read hydrates a whole growing table)

## Context

Every list endpoint in AMP that reads a table which grows with time takes a
`.limit(N)`: work orders (200), purchase orders and suppliers (500),
notifications (500), quality inspections (300), the audit log (200), and
thirty more. That cap is right, and a static guard keeps it: a table that is
append-only in normal operation must never be hydrated whole on a three-second
poll (`test_growing_table_reads.py`, #531).

What the cap did was quietly turn every list into **the newest N rows, and
nothing else**. Nothing in the response said so. The restore drill at scale
(#671) restored 1,200 inventory items and its verify phase counted 500 of
them: the dashboard showed a complete-looking table with 700 items missing
and no sign of it. The notification screen counted the unread rows in the
newest 500 and displayed that as *the* unread count. A KPI computed over a
page that cannot be told from the whole list is a confident wrong number,
the same failure ADR-0010 refused a row cap on `/machines` for.

#673 closed the defect for the two inventory lists with a private helper. The
survey that followed found thirty-three more capped list endpoints with the
same shape, in seventeen route modules, and no shared rule.

## Decision

1. **One helper, `paging.page(response, query, default, limit, offset,
   max_page)`**, is the only way a capped list is served. It clamps `limit`
   into `[1, max_page]` and `offset` into `[0, …)`, sets the caller's whole
   count of the scoped query in the **`X-Total-Count`** response header
   (`query.order_by(None).count()`, a `SELECT count(*)` in SQL, never the
   page's length), and returns the rows so the handler may go on to annotate,
   join or serialise them as before.

2. **Every list endpoint keeps its old cap as its default page.** Nothing
   that reads these lists moves. What a caller gains is `?limit=` (at most
   `paging.MAX_PAGE`, 2,000, or the endpoint's own smaller ceiling) and
   `?offset=` to page through the rest.

3. **Handlers declare `response: Response = None`.** FastAPI recognises the
   parameter by its annotation and injects the real response on every request;
   a direct caller (the forty-odd tests that call a handler as a function) may
   leave it out, and then there is no header to set.

4. **The browser may read the header** because `main.py`'s CORS middleware
   lists it in `expose_headers`. Without that line the count is sent and never
   seen; `test_inventory_list_paging.py` pins it.

5. **A capped GET that does not page fails the build.** `test_lists_are_pages.py`
   walks the AST of every `*_routes.py`: a GET handler that hydrates a
   `db.query(...).limit(...).all()` slice must call `paging.page`, or be listed
   in `ALLOWED` with the reason the cap is a window (rows that feed a figure or
   a chart — page two of a KPI's inputs is not a thing), a body envelope with
   its own total (`/oem/fleet`, `/oem/claims`, `/oem/notifications`), or an
   on-demand download. An allowlist entry that no longer names a real capped
   read fails the build too.

6. **`/notifications?unread=true`** filters to every notification not
   explicitly `Read`, NULL included (the convention `read-all` and the
   generator's dedup already apply). With `limit=1` the header IS the unread
   count over every notification the tenant has.

## Consequences

- Thirty-five GET handlers page through the helper; ten capped GETs are
  windows, envelopes or downloads and are allowed by name and reason.
- The enterprise inventory lists (remnants, issue slips, GRNs, cycle counts)
  keep their own ceiling of 200 through `max_page`; the agent-actions log keeps
  its cap as both default and ceiling.
- The frontend can now say "Showing the newest 200 of 1,234 work orders" and
  count unread notifications over the whole tenant. `apiGetWithTotal` in
  `frontend/lib/api.ts` reads the header (`null` when absent, never a guess);
  the inventory screen uses it since #673, the other sections follow.
- `/machines` stays uncapped (ADR-0010) until its KPIs are computed on the
  server; that is the acceptable future change ADR-0010 names, and the header
  is what makes it possible.
- Three body-envelope conventions remain (`total` in the body, `truncated`
  flags). They are honest and untouched; new lists use the header.

## Verification

`test_lists_are_pages.py` (the guard, its self-test, and live checks on real
handlers: the default page is the old cap, the total is the whole scoped
count and the other tenant's is its own, an annotating handler still
annotates, the module ceiling survives, the unread count is over every
notification). `mutate_lists_are_pages.py`: the cap, the offset, the clamp, a
limit of 0, the module ceiling, the header, the header carrying the page's
count, both inventory defaults, the CORS line, a handler that drops the
helper, the unread filter — every mutation caught.
