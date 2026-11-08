"""Small, deterministic statistics. No randomness anywhere: the same data always gives the same decision."""
from __future__ import annotations

import math


def norm_sf(z: float) -> float:
    """P(Z > z) for a standard normal."""
    return 0.5 * math.erfc(z / math.sqrt(2))


def two_prop(x1: int, n1: int, x2: int, n2: int) -> dict:
    """Arm 1 vs arm 2. Returns diff (p1-p2), its SE, z, one-sided p for 'arm 1 better' and 'arm 1 worse'."""
    if n1 == 0 or n2 == 0:
        return {"p1": None, "p2": None, "diff": 0.0, "se": float("inf"), "z": 0.0, "p_better": 1.0,
                "p_worse": 1.0, "rel": 0.0}
    p1, p2 = x1 / n1, x2 / n2
    pp = (x1 + x2) / (n1 + n2)
    se0 = math.sqrt(max(pp * (1 - pp) * (1 / n1 + 1 / n2), 1e-12))   # pooled, for the test
    se = math.sqrt(max(p1 * (1 - p1) / n1 + p2 * (1 - p2) / n2, 1e-12))  # unpooled, for the CI
    z = (p1 - p2) / se0
    return {"p1": p1, "p2": p2, "diff": p1 - p2, "se": se, "z": z, "p_better": norm_sf(z), "p_worse": norm_sf(-z),
            "rel": (p1 / p2 - 1) if p2 > 0 else 0.0, "ci95": (p1 - p2 - 1.96 * se, p1 - p2 + 1.96 * se)}


def wilson(x: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = x / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - h) / d, (c + h) / d)


def holm(pvals: dict, alpha: float) -> set:
    """Holm step-down. Returns the keys rejected at family-wise level alpha."""
    out = set()
    items = sorted(pvals.items(), key=lambda kv: kv[1])
    m = len(items)
    for i, (k, p) in enumerate(items):
        if p <= alpha / (m - i):
            out.add(k)
        else:
            break
    return out


def binom_sf(k: int, n: int, p: float) -> float:
    """P(X >= k) for X ~ Binomial(n, p), exact via log-space summation (n is at most a few thousand)."""
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    if p <= 0:
        return 0.0
    if p >= 1:
        return 1.0
    lp, lq = math.log(p), math.log1p(-p)
    logs = []
    for i in range(k, n + 1):
        logs.append(math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1) + i * lp + (n - i) * lq)
    mx = max(logs)
    return min(1.0, math.exp(mx) * sum(math.exp(v - mx) for v in logs))


def n_per_arm(p1: float, p2: float, alpha: float = 0.05, power: float = 0.8) -> int:
    """Two-sided sample size per arm for a two-proportion test (used for ETAs and the SUBMISSION table)."""
    za = _z(1 - alpha / 2)
    zb = _z(power)
    d = abs(p2 - p1)
    if d == 0:
        return 10 ** 9
    return math.ceil((za + zb) ** 2 * (p1 * (1 - p1) + p2 * (1 - p2)) / d ** 2)


def _z(q: float) -> float:
    # inverse normal CDF by bisection: exact enough and dependency-free
    lo, hi = -10.0, 10.0
    for _ in range(100):
        mid = (lo + hi) / 2
        if 1 - norm_sf(mid) < q:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def tv(a: dict, b: dict) -> float:
    keys = set(a) | set(b)
    return 0.5 * sum(abs(a.get(k, 0.0) - b.get(k, 0.0)) for k in keys)


def normalise(d: dict) -> dict:
    s = float(sum(v for v in d.values() if v > 0))
    return {k: v / s for k, v in d.items() if v > 0} if s > 0 else {}
