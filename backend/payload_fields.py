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
    except (TypeError, ValueError):
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
