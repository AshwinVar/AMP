"""The copilot intent model's label set: the keyword router's allowlist plus "no opinion".

WHY HARD-CODED
--------------
``ai.assistant.route_names()`` is the allowlist, but importing ``ai`` drags in
about forty modules and the database layer, and the classifier must run on the
standard library alone. So the names are written out here and
``test_amp_ai_intent_labels.py`` asserts they equal ``route_names()``: a pillar
added to or removed from the router fails CI until this list (and a retrained
model) follow.

``__none__`` is the model saying "none of these" (small talk, off-topic,
out-of-scope requests). It is never a route: ``to_route`` maps it to None, and
None means "let the keyword router decide".

The order of ``LABELS`` is the order of the model's weight columns; a shipped
artifact must list exactly this order.
"""

__all__ = ["NONE_LABEL", "ROUTE_LABELS", "LABELS", "to_route"]

NONE_LABEL = "__none__"

ROUTE_LABELS = (
    "briefing", "compliance", "cost", "delivery", "downtime", "flow", "help", "inventory",
    "machines", "maintenance", "oee", "production", "quality", "shift", "trend",
)

LABELS = ROUTE_LABELS + (NONE_LABEL,)

_ROUTES = frozenset(ROUTE_LABELS)


def to_route(label):
    """The route name for a model label: the name itself, or None for ``__none__``.

    Anything that is not a label raises ValueError: a caller holding an unknown
    label has a model that does not match this allowlist, and guessing would be
    worse than failing.
    """
    if isinstance(label, str):
        if label in _ROUTES:
            return label
        if label == NONE_LABEL:
            return None
    raise ValueError(f"{label!r} is not a copilot intent label")
