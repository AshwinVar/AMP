"""A list is a page, and says so.

Every capped list endpoint in AMP -- work orders, purchase orders, notifications,
quality inspections, the audit log, ... -- once returned the newest N rows and
nothing else. A plant with 1,200 stock items saw 500 on the dashboard with no
sign that 700 were missing (found by the restore drill at scale, #671, and
closed for the two inventory lists in #673). The cap is right: an append-only
table must never be hydrated whole on a three-second poll
(test_growing_table_reads.py). What was wrong is that the page could not be
told from a complete list.

`page()` is the one place that rule lives now, and test_lists_are_pages.py
fails the build for a capped GET that does not go through it:

  * the caller's whole count travels in X-Total-Count (the count of the scoped
    query, never the page's), so a screen can say "the newest 200 of 1,234"
    and a KPI computed over a page can be told it is one;
  * ?limit= (at most MAX_PAGE, or the endpoint's own smaller ceiling) and
    ?offset= page through the rest; both are clamped HERE, not only in the
    signatures, so a direct caller (a test, a script) gets the same page a
    request would, and a hostile value is a clamp, not a 500;
  * the endpoint's default page is its old cap, so nothing that reads these
    lists moves.

`page()` returns the rows, never the response body, so a handler may go on to
annotate, join or serialise them exactly as it did before.

Handlers declare `response: Response = None`. FastAPI recognises the parameter
by its annotation and injects the real response on every request (the default
is never used there); a direct caller -- the forty-odd tests that call a list
handler as a function -- may leave it out, and then there is no header to set.

The browser may read the header only because main.py's CORS middleware lists
it in expose_headers; test_inventory_list_paging.py pins that line.
"""
import time
from typing import Optional

from fastapi import Response

MAX_PAGE = 2000
TOTAL_HEADER = "X-Total-Count"

# WHAT THE COUNT COSTS, MEASURED (docs/PERFORMANCE.md, "Re-measured 2026-09-21")
# ---------------------------------------------------------------------------
# The first cut counted on every request. loadtest.py, floor-normalised: the
# four paged lists in its set cost 1.3x-1.9x their previous p50 at every scale
# (/inventory/items 68 -> 252 ms raw at 1,000 machines), the unpaged endpoints
# 1.0x-1.3x -- one SELECT count(*) per request on a three-second poll, per open
# tab. Two things end that without changing what the header says:
#
#   * a page SHORTER than its limit already tells the whole count: the rows end
#     here, so total = offset + len(rows), exactly, with no second statement.
#     Every tenant whose list is smaller than the default page pays nothing;
#   * a FULL page needs the count, and the count is cached per (tenant, query)
#     for COUNT_TTL_S -- the dashboard's poll interval -- so a tab pays one
#     count per list per three seconds, not one per request. The total a
#     notice shows can therefore be up to three seconds old; the rows never
#     are, and the next round corrects it.
#
# An offset past the end returns no rows and says nothing about the count, so
# it counts. Only a total that would otherwise be wrong is ever computed.
COUNT_TTL_S = 3.0
_COUNT_CACHE_MAX = 4096
_count_cache: dict = {}


def _int(value, fallback):
    """A caller-supplied number, or the fallback -- never a 500 for a string
    off the query string of a direct call (requests are typed by FastAPI)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def clamp(limit: Optional[int], default: int, offset: int = 0, max_page: int = MAX_PAGE):
    """(limit, offset) within [1, max_page] and [0, ...): the bounds every page
    obeys whatever the caller asked for."""
    limit = default if limit is None else _int(limit, default)
    return max(1, min(limit, max_page)), max(0, _int(offset, 0))


def _count_key(query):
    """The cache key: the caller's tenant (the ORM hook adds that filter at
    execution, so it is not in the statement) and the statement itself with its
    literal values, so two filters of one table never share a total."""
    import tenancy
    compiled = query.statement.compile(compile_kwargs={"literal_binds": True})
    return (tenancy.current_tenant(), str(compiled))


def cached_count(query, now=None):
    """The whole count of `query`, at most COUNT_TTL_S old for this tenant."""
    now = time.monotonic() if now is None else now
    key = _count_key(query)
    hit = _count_cache.get(key)
    if hit is not None and hit[0] > now:
        return hit[1]
    total = query.order_by(None).count()
    if len(_count_cache) >= _COUNT_CACHE_MAX:
        _count_cache.clear()
    _count_cache[key] = (now + COUNT_TTL_S, total)
    return total


def forget_counts():
    """Drop every cached total (tests, and anything that must not wait)."""
    _count_cache.clear()


def page(response: Optional[Response], query, default: int, limit: Optional[int] = None,
         offset: int = 0, max_page: int = MAX_PAGE):
    """One page of `query` (in the caller's own ORDER BY), and the whole count of
    `query` in X-Total-Count. Returns the rows."""
    limit, offset = clamp(limit, default, offset, max_page)
    rows = query.offset(offset).limit(limit).all()
    if response is not None:
        if len(rows) < limit and (rows or offset == 0):
            total = offset + len(rows)          # the rows ended here: exact, free
        else:
            total = cached_count(query)
        response.headers[TOTAL_HEADER] = str(total)
    return rows
