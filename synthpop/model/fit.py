"""Fitting seed units to block-group totals, and turning weights into whole units.

`fit_weights` is penalized maximum-entropy calibration, the idea behind P-MEDM
(Nagle et al.; used by UrbanPop): find weights as close as possible to the seed
weights (in relative entropy) such that every ACS total is met to within roughly
its own standard error. Noisy, slightly inconsistent ACS cells therefore pull
less than precise ones instead of forcing an impossible exact fit.

It is solved in the dual with Newton's method:

    w(λ) = w0 · exp(Aᵀλ / γ)
    minimize  γ Σ w(λ) − tᵀλ + ½ Σ s² λ²,   γ = n / N²

At the optimum the miss on each cell is A·w − t = −s²·λ. A[c, u] is how much
unit u contributes to cell c (1 for a household cell, the number of matching
people for a person cell), so household and person totals are fitted together.

`fit_tract` solves the same problem for all block groups of a tract at once, with
the tract's own ACS totals as extra cells on the block groups' sum (P-MEDM's
multilevel form). `fit_weights` is its one-block-group case.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

MIN_SE = 1.0          # every cell may miss by about one unit; keeps the problem well conditioned
MIN_WEIGHT = 1e-3     # replicate weights can be zero or negative; keep every seed unit usable


@dataclass
class Fit:
    weights: np.ndarray
    fitted: np.ndarray        # A @ weights, per cell
    iterations: int
    converged: bool
    max_rel_error: float      # largest |fitted − target| / max(target, 1)


def fit_weights(A: np.ndarray, w0: np.ndarray, targets: np.ndarray, se: np.ndarray | None = None, min_se: float = MIN_SE,
                max_iter: int = 200, tol: float = 1e-8) -> Fit:
    """One block group on its own: `fit_tract` with a single block group and no tract cells."""
    t = np.asarray(targets, dtype=float)
    return fit_tract(A, w0, t[None], None if se is None else np.asarray(se, dtype=float)[None],
                     min_se=min_se, max_iter=max_iter, tol=tol)[0]


def fit_tract(A: np.ndarray, w0: np.ndarray, targets: np.ndarray, se: np.ndarray | None = None,
              tract_targets: np.ndarray | None = None, tract_se: np.ndarray | None = None,
              bg_rows: np.ndarray | None = None,
              min_se: float = MIN_SE, max_iter: int = 200, tol: float = 1e-8) -> list[Fit]:
    """The block groups of one tract fitted together: each to its own cells and, when
    `tract_targets` is given, their sum to the tract's cells. Returns one Fit per block group.

    A has one row per cell. `bg_rows` marks the rows block groups are fitted to (default
    all); `targets` and `se` are block groups x those rows. Tract cells are every row of
    A, so a row outside `bg_rows` (a table published only for tracts) is fitted on the
    block groups' sum alone.

    The objective is the sum of the block groups' own objectives plus the tract cells'
    misses, so without tract cells every block group gets exactly its separate fit. Tract
    estimates are far more precise than block-group ones, so they keep the block groups'
    sum on the tract's totals and let each block group's noisy cells pull less.
    """
    bg = np.ones(len(A), dtype=bool) if bg_rows is None else np.asarray(bg_rows, dtype=bool)
    Ab = A[bg]
    T = np.maximum(np.atleast_2d(np.asarray(targets, dtype=float)), 0)
    k, C = T.shape
    S2 = np.maximum(np.zeros_like(T) if se is None else np.atleast_2d(np.asarray(se, dtype=float)), min_se) ** 2
    base = np.maximum(np.asarray(w0, dtype=float), MIN_WEIGHT)
    hh_like = A.max(axis=0) > 0
    seed = Ab @ base
    # Each block group starts from seed weights scaled to its size, so λ only reshapes.
    # P-MEDM weighs relative entropy over proportions by n/N (n seed units, N people).
    # Over counts that is n/N², which lets ACS totals, not the seed, set the size.
    W0 = np.stack([base * (T[b].max() / max(seed.max(), 1e-9)) for b in range(k)])
    gamma = np.array([len(base) / max(T[b].max(), 1.0) ** 2 for b in range(k)])
    joint = tract_targets is not None
    if joint:
        tt = np.maximum(np.asarray(tract_targets, dtype=float), 0)
        ts2 = np.maximum(np.zeros_like(tt) if tract_se is None else np.asarray(tract_se, dtype=float), min_se) ** 2
        t = np.concatenate([T.ravel(), tt])
        s2 = np.concatenate([S2.ravel(), ts2])
    else:
        t, s2 = T.ravel(), S2.ravel()
    lam = np.zeros(len(t))

    def weights(l):
        E = l[:k * C].reshape(k, C) @ Ab
        if joint:
            E = E + l[k * C:] @ A
        return W0 * np.exp(np.clip(E / gamma[:, None], -40, 40))

    def fitted(W):
        F = (W @ Ab.T).ravel()
        return np.concatenate([F, (W.sum(axis=0) @ A.T)]) if joint else F

    def dual(l):
        W = weights(l)
        return float((gamma[:, None] * W).sum() - t @ l + 0.5 * (s2 * l * l).sum()), W

    f, W = dual(lam)
    converged = False
    it = 0
    n = len(t)
    for it in range(1, max_iter + 1):
        g = fitted(W) - t + s2 * lam
        # The Hessian is A·diag(w/γ)·Aᵀ per block group (M below). A tract cell is the
        # sum over block groups, so its blocks are sums of the block groups' blocks.
        H = np.zeros((n, n))
        total = 0
        for b in range(k):
            M = (A * (W[b] / gamma[b])) @ A.T if joint else None
            rows = slice(b * C, (b + 1) * C)
            if joint:
                H[rows, rows] = M[np.ix_(bg, bg)]
                H[rows, k * C:] = M[bg]
                H[k * C:, rows] = M[bg].T
                total = total + M
            else:
                H[rows, rows] = (Ab * (W[b] / gamma[b])) @ Ab.T
        if joint:
            H[k * C:, k * C:] = total
        H[np.diag_indices(n)] += s2
        step = np.linalg.solve(H, g)
        alpha = 1.0
        while alpha > 1e-8:
            f_new, W_new = dual(lam - alpha * step)
            if f_new <= f - 1e-4 * alpha * (g @ step):
                break
            alpha /= 2
        lam, f, W = lam - alpha * step, f_new, W_new
        if np.abs(alpha * step).max() < tol * (1 + np.abs(lam).max()):
            converged = True
            break
    out = []
    for b in range(k):
        w = np.where(hh_like, W[b], 0.0)
        fit = Ab @ w
        err = float(np.max(np.abs(fit - T[b]) / np.maximum(T[b], 1.0))) if C else 0.0
        out.append(Fit(w, fit, it, converged, err))
    return out


def integerize(weights: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """Truncate, then sample the fractional remainder without replacement, so the
    integer total equals the rounded weight total."""
    w = np.maximum(weights, 0)
    counts = np.floor(w).astype(int)
    rem = w - counts
    k = int(round(rem.sum()))
    if k > 0:
        pick = rng.choice(len(w), size=min(k, int((rem > 0).sum())), replace=False, p=rem / rem.sum())
        counts[pick] += 1
    return counts
