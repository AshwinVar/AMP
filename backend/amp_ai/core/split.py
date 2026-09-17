"""Leakage-proof train / validation / test splits.

Two ways a model looks better on its own evaluation than it will on a real plant:

1. ENTITY leakage. The same machine (or the same question family) appears in
   training and in test, so the model is graded on something it memorised.
   Entities (groups) are therefore partitioned FIRST, and every row follows its
   entity.
2. TIME / LABEL leakage. A training row's label looks ``horizon`` into the
   future; if that window reaches into the period the model is tested on, the
   training data has seen the test period. Each split therefore owns a time
   window, and a row is kept only if its whole label window lies inside it.

FRACTIONS
---------
``test_frac`` and ``val_frac`` are fractions of ALL entities (groups), each
rounded half-up: 40 entities with test_frac=0.25, val_frac=0.15 gives 10 test,
6 validation, 24 training entities. (For "15% of the entities that are not in
test", pass ``val_frac = 0.15 * (1 - test_frac)``.) Every split must end up
with at least one entity and at least one row, or ``SplitError`` is raised.

TIME WINDOWS (``entity_time_split``)
------------------------------------
With ``t`` the row's time and labels looking ahead over ``(t, t + horizon]``:
    train entities keep rows with   t + horizon < val_start   and   t <= train_end
    val entities keep rows with     val_start <= t   and   t + horizon < test_start
    test entities keep rows with    t >= test_start
and the configuration must satisfy ``test_start >= train_end + horizon + gap``.
``train_end`` is the LAST feature time any model that is scored on the test
split may train on - including a final refit on train + validation, whose rows
come from ``refit_rows`` (which drops validation rows after ``train_end``).

``t``, ``horizon`` and ``gap`` can be numbers (day indices) or datetimes with
timedeltas; they only need ``+`` and comparisons.

Keys (``entity_key``, ``time_key``, ``group_key``) are either a callable applied
to each item or a key looked up with ``item[key]``.
"""
import math

from .rng import make_rng

__all__ = ["SplitError", "entity_time_split", "verify_entity_time_split", "refit_rows", "group_split"]


class SplitError(ValueError):
    """The split would leak, or would leave a split empty."""


def _accessor(key):
    if callable(key):
        return key
    return lambda item: item[key]


def _sort_key(value):
    return (type(value).__name__, value)


def _half_up(x):
    return int(math.floor(round(x, 9) + 0.5))


def _check_fraction(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.0 < float(value) < 1.0:
        raise ValueError(f"{label} must be strictly between 0 and 1, got {value!r}")


def _partition(keys, test_frac, val_frac, seed, stream):
    _check_fraction(test_frac, "test_frac")
    _check_fraction(val_frac, "val_frac")
    if test_frac + val_frac >= 1.0:
        raise ValueError("test_frac + val_frac must be < 1")
    order = sorted(set(keys), key=_sort_key)
    make_rng(seed, stream).shuffle(order)
    total = len(order)
    n_test = _half_up(test_frac * total)
    n_val = _half_up(val_frac * total)
    n_train = total - n_test - n_val
    if n_test < 1 or n_val < 1 or n_train < 1:
        raise SplitError(
            f"{total} groups cannot fill train/val/test (would be {n_train}/{n_val}/{n_test}); "
            "every split needs at least one group")
    test = set(order[:n_test])
    val = set(order[n_test:n_test + n_val])
    train = set(order[n_test + n_val:])
    return train, val, test


def _check_windows(train_end, val_start, test_start, horizon, gap):
    if not val_start < test_start:
        raise SplitError(f"val_start ({val_start!r}) must be before test_start ({test_start!r})")
    if test_start < train_end + horizon + gap:
        raise SplitError(
            f"test_start ({test_start!r}) is closer to train_end ({train_end!r}) than horizon + gap "
            f"({horizon!r} + {gap!r}): training labels would overlap the test period")


def _in_train(t, *, train_end, val_start, horizon):
    return t + horizon < val_start and t <= train_end


def _in_val(t, *, val_start, test_start, horizon):
    return val_start <= t and t + horizon < test_start


def _in_test(t, *, test_start):
    return t >= test_start


def verify_entity_time_split(train, val, test, entity_key, time_key, *,
                             train_end, val_start, test_start, horizon, gap) -> None:
    """Raise SplitError unless (train, val, test) is a leak-free split under the rules above.

    ``entity_time_split`` calls this on its own output; call it yourself on any
    split you assemble or modify by hand.
    """
    _check_windows(train_end, val_start, test_start, horizon, gap)
    ek, tk = _accessor(entity_key), _accessor(time_key)
    for name, part in (("train", train), ("val", val), ("test", test)):
        if not part:
            raise SplitError(f"the {name} split is empty")
    entities = {name: {ek(r) for r in part} for name, part in (("train", train), ("val", val), ("test", test))}
    for a, b in (("train", "val"), ("train", "test"), ("val", "test")):
        shared = entities[a] & entities[b]
        if shared:
            example = sorted(shared, key=_sort_key)[0]
            raise SplitError(f"entity overlap between {a} and {b}: {len(shared)} shared, e.g. {example!r}")
    for r in train:
        t = tk(r)
        if not _in_train(t, train_end=train_end, val_start=val_start, horizon=horizon):
            raise SplitError(f"train row at t={t!r}: label window reaches val_start or t is after train_end")
    for r in val:
        t = tk(r)
        if not _in_val(t, val_start=val_start, test_start=test_start, horizon=horizon):
            raise SplitError(f"val row at t={t!r}: outside [val_start, test_start - horizon)")
    for r in test:
        t = tk(r)
        if not _in_test(t, test_start=test_start):
            raise SplitError(f"test row at t={t!r}: before test_start")


def entity_time_split(samples, entity_key, time_key, *, test_frac, val_frac, seed,
                      train_end, val_start, test_start, horizon, gap) -> tuple[list, list, list]:
    """(train, val, test) lists of the original samples, in input order. See the module docstring."""
    _check_windows(train_end, val_start, test_start, horizon, gap)
    samples = list(samples)
    if not samples:
        raise SplitError("no samples")
    ek, tk = _accessor(entity_key), _accessor(time_key)
    train_e, val_e, test_e = _partition([ek(s) for s in samples], test_frac, val_frac, seed, "entity_time_split")
    train, val, test = [], [], []
    for s in samples:
        e, t = ek(s), tk(s)
        if e in test_e:
            if _in_test(t, test_start=test_start):
                test.append(s)
        elif e in val_e:
            if _in_val(t, val_start=val_start, test_start=test_start, horizon=horizon):
                val.append(s)
        elif e in train_e:
            if _in_train(t, train_end=train_end, val_start=val_start, horizon=horizon):
                train.append(s)
    verify_entity_time_split(train, val, test, ek, tk, train_end=train_end, val_start=val_start,
                             test_start=test_start, horizon=horizon, gap=gap)
    return train, val, test


def refit_rows(train, val, time_key, *, train_end) -> list:
    """Rows for a final refit on train + validation: every train row, plus validation rows with t <= train_end.

    Validation windows run up to ``test_start - horizon``, which is later than
    ``train_end``; a refit that took them all would put training labels inside
    the ``gap`` before the test period.
    """
    tk = _accessor(time_key)
    rows = list(train) + [r for r in val if tk(r) <= train_end]
    late = [tk(r) for r in rows if not tk(r) <= train_end]
    if late:
        raise SplitError(f"{len(late)} refit rows are after train_end (e.g. t={late[0]!r})")
    return rows


def group_split(items, group_key, *, test_frac, val_frac, seed) -> tuple[list, list, list]:
    """(train, val, test) with every group wholly inside one split (e.g. question families)."""
    items = list(items)
    if not items:
        raise SplitError("no items")
    gk = _accessor(group_key)
    train_g, val_g, test_g = _partition([gk(i) for i in items], test_frac, val_frac, seed, "group_split")
    train = [i for i in items if gk(i) in train_g]
    val = [i for i in items if gk(i) in val_g]
    test = [i for i in items if gk(i) in test_g]
    if not (train and val and test):
        raise SplitError("a split is empty")
    return train, val, test
