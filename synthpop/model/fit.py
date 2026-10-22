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
    t = np.maximum(np.asarray(targets, dtype=float), 0)
    s2 = np.maximum(np.zeros_like(t) if se is None else np.asarray(se, dtype=float), min_se) ** 2
    w0 = np.maximum(np.asarray(w0, dtype=float), MIN_WEIGHT)
    hh_like = A.max(axis=0) > 0
    # Start from seed weights scaled to the right overall size, so λ only reshapes.
    scale = t.max() / max((A @ w0).max(), 1e-9)
    w0 = w0 * scale
    # P-MEDM weighs relative entropy over proportions by n/N (n seed units, N people).
    # Over counts that is n/N², which lets ACS totals, not the seed, set the size.
    gamma = len(w0) / max(t.max(), 1.0) ** 2

    lam = np.zeros(len(t))

    def dual(l):
        w = w0 * np.exp(np.clip(A.T @ l / gamma, -40, 40))
        return gamma * w.sum() - t @ l + 0.5 * (s2 * l * l).sum(), w

    f, w = dual(lam)
    converged = False
    it = 0
    for it in range(1, max_iter + 1):
        g = A @ w - t + s2 * lam
        H = (A * w) @ A.T / gamma + np.diag(s2)
        step = np.linalg.solve(H, g)
        alpha = 1.0
        while alpha > 1e-8:
            f_new, w_new = dual(lam - alpha * step)
            if f_new <= f - 1e-4 * alpha * (g @ step):
                break
            alpha /= 2
        lam, f, w = lam - alpha * step, f_new, w_new
        if np.abs(alpha * step).max() < tol * (1 + np.abs(lam).max()):
            converged = True
            break
    w = np.where(hh_like, w, 0.0)
    fitted = A @ w
    err = float(np.max(np.abs(fitted - t) / np.maximum(t, 1.0))) if len(t) else 0.0
    return Fit(w, fitted, it, converged, err)


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
