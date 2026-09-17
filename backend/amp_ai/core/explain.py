"""Why did a linear model score this row the way it did?

For a linear model the logit is exactly ``intercept + sum_j w_j * z_j`` over the
standardised inputs ``z``, so ``w_j * z_j`` IS feature j's contribution - an
exact decomposition, not an approximation. The contributions of every feature
plus the intercept add back up to the logit (pinned by a test).

``direction`` says which way the feature pushed the score ("increases",
"decreases", or "none" for an exact zero). It says nothing about causation.
"""
import math

__all__ = ["linear_contributions"]


def linear_contributions(weights, z, names, top=3) -> list[dict]:
    """[{name, value, contribution, direction}] ranked by |contribution| (ties by name).

    ``value`` is the standardised input z_j. ``top=None`` returns every feature.
    """
    weights, z, names = list(weights), list(z), list(names)
    if not (len(weights) == len(z) == len(names)):
        raise ValueError(f"weights ({len(weights)}), z ({len(z)}) and names ({len(names)}) differ in length")
    if top is not None and (isinstance(top, bool) or not isinstance(top, int) or top < 0):
        raise ValueError("top must be a non-negative int or None")
    items = []
    for w, v, name in zip(weights, z, names):
        w, v = float(w), float(v)
        if not (math.isfinite(w) and math.isfinite(v)):
            raise ValueError(f"feature {name!r}: non-finite weight or value")
        contribution = w * v
        direction = "increases" if contribution > 0 else "decreases" if contribution < 0 else "none"
        items.append({"name": name, "value": v, "contribution": contribution, "direction": direction})
    items.sort(key=lambda d: (-abs(d["contribution"]), d["name"]))
    return items if top is None else items[:top]
