"""Coercing fields out of a raw request body, without turning a typo into a 500.

A number of handlers take the body as an untyped ``payload: dict`` rather than a
Pydantic schema, so nothing validates it before the handler runs. Written the
obvious way that becomes::

    item_id=int(payload["item_id"])
    qty = int(payload["qty"])

and then a missing key, an empty string from a half-filled form, a JSON ``null``
(what ``JSON.stringify`` emits for ``undefined``/``NaN``) or a stray "abc" raises
KeyError / ValueError / TypeError straight out of the handler. FastAPI has no
choice but to answer HTTP 500 "Internal Server Error", which tells the user
nothing about which of their fields was wrong — and tells the operator it was a
server fault when it was a form typo.

These helpers turn each of those into a 400 that names the field. They are
deliberately small and total: every path either returns a value or raises
HTTPException, so nothing else can escape to the 500 handler.

`test_payload_validation.py` asserts no route module goes back to calling
``int(payload[...])`` directly.
"""
from fastapi import HTTPException

# Beyond this a quantity is a typo or a float that overflowed, not a real
# figure. A UI that sends Number(input) uncapped turns a long digit string into
# 1e+23, which int() widens happily and the database then rejects mid-write.
MAX_QTY = 1_000_000_000


def _label(key: str, where: str) -> str:
    """"Line 2: 'qty'" when the caller knows the row, else "'qty'"."""
    return f"{where}: '{key}'" if where else f"'{key}'"


def _missing(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def int_field(payload: dict, key: str, where: str = "", *, required: bool = True,
              default: int = 0, minimum: int = 0, maximum: int = MAX_QTY) -> int:
    """A whole number from the body, or a 400 saying which field is wrong."""
    raw = payload.get(key)
    if _missing(raw):
        if required:
            raise HTTPException(status_code=400, detail=f"{_label(key, where)} is required")
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError, OverflowError):
        # OverflowError is caught alongside TypeError/ValueError: JSON permits an
        # out-of-range exponent, and Python's json.loads decodes "1e999" straight
        # to float('inf') (a UI that sends Number(input) uncapped, or a fat-finger,
        # produces exactly this). int(float('inf')) raises OverflowError — NOT one of
        # (TypeError, ValueError) — so an infinite quantity escaped this guard and
        # threw out of the handler as an unhandled 500, defeating the whole point of
        # this module (turn a bad field into a 400 naming it). A NaN ("NaN" in JSON)
        # already raised ValueError and was caught; +-inf raise OverflowError, so
        # catching it here is the precise fix, the same OverflowError guard
        # mqtt_service._non_negative_int already applies to int() on a raw reading.
        raise HTTPException(
            status_code=400,
            detail=f"{_label(key, where)} must be a whole number (got {raw!r})")
    if minimum is not None and value < minimum:
        raise HTTPException(
            status_code=400,
            detail=f"{_label(key, where)} cannot be less than {minimum}")
    if maximum is not None and value > maximum:
        raise HTTPException(
            status_code=400,
            detail=f"{_label(key, where)} is unrealistically large ({value})")
    return value


def int_cell(value, label: str = "value", *, default: int = 0, minimum: int = 0,
             maximum: int = MAX_QTY) -> int:
    """A bounded whole number from a CSV cell — decimal-tolerant, like int_field
    with a [0, MAX_QTY] bound, but for the import (row) side rather than a JSON body.

    The inventory CSV importers (gmats / enterprise) parse a quantity cell with a
    bare ``int(float(cell))``: decimal-tolerant on purpose — Excel/Tally write an
    integer quantity as "5", "5.0" or "5.00" — but UNBOUNDED, so a "-5" cell was
    stored as a negative stock and an out-of-range value ("1e20") was widened by
    ``int()`` and stored silently. Both then flowed straight into DISPLAYED numbers
    — ``available_stock`` (physical - reserved) and the /gmats/summary totals — so a
    single bad row dragged the plant figures below or above the truth. Every JSON
    write of these same columns already refuses exactly those via ``int_field``'s
    [0, MAX_QTY] bound (and the proforma/MIN line guards reject a negative qty); this
    is that same bound for the CSV side, so the two ingest routes agree.

    A missing / blank cell coalesces to ``default`` — a blank quantity cell means 0,
    matching the ``or "0"`` the call sites already applied. Anything present but not
    a number, negative, or past ``maximum`` raises ``ValueError`` (not HTTPException):
    the importer wraps each row in its own ``except`` and turns that into a reported,
    SKIPPED row, so one bad quantity neither silently poisons inventory nor loses the
    whole import (#440) — the per-row CSV analogue of ``int_field``'s whole-request
    400. OverflowError is caught alongside TypeError/ValueError for the same reason
    int_field catches it: ``int(float("1e999"))`` is ``int(inf)`` -> OverflowError.
    """
    if _missing(value):
        return default
    try:
        n = int(float(value))
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{label} must be a whole number (got {value!r})")
    if minimum is not None and n < minimum:
        raise ValueError(f"{label} cannot be less than {minimum} (got {n})")
    if maximum is not None and n > maximum:
        raise ValueError(f"{label} is unrealistically large ({n})")
    return n


def str_field(payload: dict, key: str, where: str = "", *, required: bool = True,
              default: str = "") -> str:
    """Trimmed text from the body, or a 400. A whitespace-only value counts as
    missing — otherwise a form of spaces creates a nameless record."""
    raw = payload.get(key)
    if _missing(raw):
        if required:
            raise HTTPException(status_code=400, detail=f"{_label(key, where)} is required")
        return default
    return str(raw).strip()
