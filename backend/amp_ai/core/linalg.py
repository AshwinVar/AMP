"""Dense linear algebra for small systems (k <= ~64), standard library only.

Used for the Newton/IRLS Hessian solve in ``logistic`` and the covariance
inverse in ``robust``. One elimination routine (Gauss-Jordan with partial
pivoting) serves both ``solve`` and ``invert``. Inputs are never mutated.

Cost is O(k^3) Python operations: fine for a 20-feature risk model or a
6-signal covariance, not meant for anything bigger.
"""
import math

__all__ = ["SingularMatrix", "solve", "invert", "dot"]

# A pivot smaller than this fraction of the largest entry is treated as zero.
_RELATIVE_PIVOT_TOL = 1e-12


class SingularMatrix(ValueError):
    """The matrix has no usable inverse at working precision."""


def _square(a):
    if not isinstance(a, (list, tuple)) or not a:
        raise ValueError("matrix must be a non-empty list of rows")
    n = len(a)
    rows = []
    for row in a:
        if not isinstance(row, (list, tuple)) or len(row) != n:
            raise ValueError(f"matrix must be square ({n}x{n})")
        values = [float(x) for x in row]
        if not all(math.isfinite(x) for x in values):
            raise ValueError("matrix contains a non-finite value")
        rows.append(values)
    return rows


def _gauss_jordan(a, rhs_columns):
    """Reduce [a | rhs] to [I | x]; returns the rhs block as rows."""
    n = len(a)
    width = len(rhs_columns[0])
    m = [a[i] + list(rhs_columns[i]) for i in range(n)]
    scale = max(abs(x) for row in a for x in row)
    if scale == 0.0:
        raise SingularMatrix("matrix is all zeros")
    tol = _RELATIVE_PIVOT_TOL * scale
    total = n + width
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))  # first max wins: deterministic
        if abs(m[pivot][col]) <= tol:
            raise SingularMatrix(f"no usable pivot in column {col}")
        if pivot != col:
            m[col], m[pivot] = m[pivot], m[col]
        inv_pivot = 1.0 / m[col][col]
        prow = [x * inv_pivot for x in m[col]]
        m[col] = prow
        for r in range(n):
            if r == col:
                continue
            factor = m[r][col]
            if factor != 0.0:
                row = m[r]
                for c in range(col, total):
                    row[c] -= factor * prow[c]
    return [row[n:] for row in m]


def solve(a, b) -> list:
    """x such that a x = b. Raises SingularMatrix."""
    rows = _square(a)
    if not isinstance(b, (list, tuple)) or len(b) != len(rows):
        raise ValueError("b must have one entry per row of a")
    rhs = [[float(v)] for v in b]
    if not all(math.isfinite(v[0]) for v in rhs):
        raise ValueError("b contains a non-finite value")
    return [r[0] for r in _gauss_jordan(rows, rhs)]


def invert(a) -> list:
    """Inverse of a square matrix. Raises SingularMatrix."""
    rows = _square(a)
    n = len(rows)
    identity = [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]
    return _gauss_jordan(rows, identity)


def dot(u, v) -> float:
    if len(u) != len(v):
        raise ValueError(f"dot of vectors with lengths {len(u)} and {len(v)}")
    total = 0.0
    for x, y in zip(u, v):
        total += x * y
    return total
