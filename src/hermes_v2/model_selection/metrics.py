"""RSS/AIC/RMSE/MAE — plain, hand-written formulas, deliberately not
hidden inside a fitting library's `.score()`/`.aic` attribute. Every
formula here is inspectable and independently unit-tested; see
`docs/architecture/model-selection.md` for the exact `k`-counting
convention this module assumes but doesn't itself decide (`k` is
supplied by the caller — `models.py` — as the number of estimated
regression coefficients).
"""

from __future__ import annotations

import math

import numpy as np


def rss(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    """Residual sum of squares: sum((y_true - y_pred) ** 2)."""
    residuals = np.asarray(y_true, dtype=float) - np.asarray(y_pred, dtype=float)
    return float(np.sum(residuals**2))


def aic(n: int, residual_sum_of_squares: float, k: int) -> float:
    """`n * ln(RSS / n) + 2k` — the Gaussian-errors AIC for a fitted
    regression, exactly as specified for this engine. `k` is the number
    of estimated regression coefficients (see `models.py`'s design
    matrix column count) — this function does not itself decide what
    counts as a parameter, it only computes the formula.

    Raises `ValueError` rather than returning `-inf`/`NaN` when the
    formula is undefined or degenerate:
    - `n <= k`: the model has at least as many parameters as
      observations, which is what typically *causes* `RSS == 0` (a
      trivial perfect interpolation, not a meaningful fit) — AIC's
      standard derivation assumes `n > k`.
    - `residual_sum_of_squares <= 0`: `ln(RSS / n)` is undefined at 0
      and `RSS` cannot legitimately be negative for a sum of squares.
    A candidate that hits either case is reported as invalid by
    `ModelSelector`, never silently given an artificially dominant
    (`-inf`) AIC.
    """
    if n <= k:
        raise ValueError(
            f"AIC requires more observations than parameters (n={n}, k={k})."
        )
    if residual_sum_of_squares <= 0:
        raise ValueError(
            f"AIC requires a strictly positive RSS (got {residual_sum_of_squares!r})."
        )
    return n * math.log(residual_sum_of_squares / n) + 2 * k


def rmse(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    """Root mean squared error."""
    residuals = np.asarray(y_true, dtype=float) - np.asarray(y_pred, dtype=float)
    return float(np.sqrt(np.mean(residuals**2)))


def mae(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    """Mean absolute error."""
    residuals = np.asarray(y_true, dtype=float) - np.asarray(y_pred, dtype=float)
    return float(np.mean(np.abs(residuals)))


__all__ = ["aic", "mae", "rmse", "rss"]
