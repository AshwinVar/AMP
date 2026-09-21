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
from typing import Optional

from fastapi import Response

MAX_PAGE = 2000
TOTAL_HEADER = "X-Total-Count"


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


def page(response: Optional[Response], query, default: int, limit: Optional[int] = None,
         offset: int = 0, max_page: int = MAX_PAGE):
    """One page of `query` (in the caller's own ORDER BY), and the whole count of
    `query` in X-Total-Count. Returns the rows."""
    limit, offset = clamp(limit, default, offset, max_page)
    if response is not None:
        response.headers[TOTAL_HEADER] = str(query.order_by(None).count())
    return query.offset(offset).limit(limit).all()
