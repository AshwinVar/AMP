"""Deterministic randomness and hashing for AMP-native AI builds.

WHY NOT ``random.seed(n)`` AND ``hash()``
-----------------------------------------
A build uses randomness in several independent places (the synthetic fleet, the
split, the bootstrap). One shared ``random`` stream couples them: adding one
draw to the generator silently reshuffles the split and changes every metric.
``make_rng(seed, "split")`` gives each consumer its own stream derived from the
build seed, so they cannot disturb each other.

The built-in ``hash()`` of a string is salted per process (PYTHONHASHSEED). A
feature-hashing model trained in one process and served in another would look
up different weights for the same word and nothing would crash. ``stable_hash``
uses blake2b, which is the same everywhere.
"""
import hashlib
import random

__all__ = ["make_rng", "stable_hash"]


def make_rng(seed: int, *stream: str) -> random.Random:
    """Independent ``random.Random`` for (seed, stream...).

    The generator is seeded with ``int(sha256(f"{seed}|{'|'.join(stream)}"))``.
    Stream names may not contain ``|``: ``("a|b",)`` and ``("a", "b")`` would
    otherwise alias to the same stream.

    Integer seeding of ``random.Random`` does not depend on PYTHONHASHSEED. The
    draws are stable for a given CPython version; committed artifacts are
    verified by pinned hash, and rebuilt metrics compared within a tolerance.
    """
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise TypeError(f"seed must be an int, got {type(seed).__name__}")
    for name in stream:
        if not isinstance(name, str):
            raise TypeError(f"stream names must be str, got {type(name).__name__}")
        if "|" in name:
            raise ValueError(f"stream name {name!r} contains '|', which would alias two streams")
    material = f"{seed}|{'|'.join(stream)}".encode("utf-8")
    return random.Random(int.from_bytes(hashlib.sha256(material).digest(), "big"))


def stable_hash(text: str, dim: int) -> tuple[int, int]:
    """(index, sign) for feature hashing. FROZEN SPEC - shipped models depend on it.

    value = int.from_bytes(blake2b(text.encode("utf-8"), digest_size=8), "big")
    sign  = +1 if value is odd else -1        (lowest bit)
    index = (value >> 1) % dim                 (remaining bits)

    Changing any of this invalidates every artifact trained with it.
    """
    if not isinstance(text, str):
        raise TypeError(f"text must be str, got {type(text).__name__}")
    if isinstance(dim, bool) or not isinstance(dim, int) or dim < 1:
        raise ValueError(f"dim must be a positive int, got {dim!r}")
    value = int.from_bytes(hashlib.blake2b(text.encode("utf-8"), digest_size=8).digest(), "big")
    sign = 1 if value & 1 else -1
    return (value >> 1) % dim, sign
