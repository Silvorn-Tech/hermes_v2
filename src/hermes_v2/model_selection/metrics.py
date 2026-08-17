"""RSS/AIC/AICc/RMSE/MAE — plain, hand-written formulas, deliberately not
hidden inside a fitting library's `.score()`/`.aic` attribute. Every
formula here is inspectable and independently unit-tested; see
`docs/architecture/model-selection.md` for the exact `k`-counting
convention this module assumes but doesn't itself decide (`k` is
supplied by the caller — `models.py` — as the number of estimated
regression coefficients).

Two AIC functions exist, deliberately:

- `aic_from_log_likelihood(log_likelihood, k)` — the GENERAL formula,
  `2k - 2*log_likelihood`. `k` here must be *every* estimated parameter
  of whatever produced `log_likelihood` — this is what a future non-OLS
  candidate (e.g. GARCH, fit by its own maximum-likelihood procedure)
  must use, since its likelihood doesn't reduce to the RSS shortcut
  below.
- `aic(n, rss, k)` — the RSS-based shortcut this engine has used since
  v1, a documented SPECIAL CASE of the general form for Gaussian-OLS
  regression specifically (see its own docstring for the exact
  derivation and why it's numerically different from, but rank-
  equivalent to, calling `aic_from_log_likelihood` naively).
"""

from __future__ import annotations

import math

import numpy as np


def _validate_matching_lengths(
    y_true: np.ndarray, y_pred: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    if y_true.shape != y_pred.shape:
        raise ValueError(
            f"y_true and y_pred must have the same shape "
            f"(got {y_true.shape} and {y_pred.shape})."
        )
    return y_true, y_pred


def rss(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    """Residual sum of squares: sum((y_true - y_pred) ** 2)."""
    y_true, y_pred = _validate_matching_lengths(y_true, y_pred)
    residuals = y_true - y_pred
    return float(np.sum(residuals**2))


def gaussian_ols_log_likelihood(n: int, residual_sum_of_squares: float) -> float:
    """The (concentrated/profile) log-likelihood of an OLS fit under i.i.d.
    Gaussian errors, evaluated at the MLE variance estimate
    `sigma_hat^2 = RSS / n` — i.e. `sigma^2` has already been maximized
    out of the likelihood, not held fixed at some assumed value:

        ln L = -n/2 * ln(2*pi) - n/2 * ln(RSS/n) - n/2

    This is the quantity `aic()` below is a documented shortcut for (see
    its docstring). Requires `RSS > 0` — `ln(RSS/n)` is undefined at
    `RSS = 0`, and this is a genuine singularity of the Gaussian
    likelihood at zero variance (infinite density), not a limitation of
    this implementation: it applies equally whether `RSS = 0` came from
    a degenerate `n <= k` interpolation or from a genuinely noiseless
    perfect fit with `n > k`. Raises `ValueError` rather than returning
    `+inf`.
    """
    if residual_sum_of_squares <= 0:
        raise ValueError(
            f"log-likelihood requires a strictly positive RSS "
            f"(got {residual_sum_of_squares!r})."
        )
    return (
        -n / 2 * math.log(2 * math.pi)
        - n / 2 * math.log(residual_sum_of_squares / n)
        - n / 2
    )


def aic_from_log_likelihood(log_likelihood: float, k: int) -> float:
    """The general Akaike Information Criterion: `2k - 2*log_likelihood`.

    `k` must be *every* estimated parameter of the model that produced
    `log_likelihood` — for a Gaussian OLS regression that includes the
    noise variance itself (`k = regression coefficients + 1`), not just
    the regression coefficients. This is the form any future candidate
    whose likelihood doesn't reduce to the RSS shortcut (e.g. GARCH, fit
    by its own maximum-likelihood procedure) must use — comparing its
    result directly against `aic()`'s output below is only valid once
    both sides count parameters on this same, complete convention (see
    `docs/architecture/model-selection.md`'s "AIC vs GARCH" section).

    Raises `ValueError` if `log_likelihood` is not finite, rather than
    silently propagating a NaN/Infinity into a selection decision.
    """
    if not math.isfinite(log_likelihood):
        raise ValueError(f"log_likelihood must be finite (got {log_likelihood!r}).")
    return 2 * k - 2 * log_likelihood


def aic(n: int, residual_sum_of_squares: float, k: int) -> float:
    """`n * ln(RSS / n) + 2k` — this engine's Gaussian-OLS AIC, exactly
    as specified. `k` is the number of estimated regression coefficients
    (see `models.py`'s design matrix column count) — this function does
    not itself decide what counts as a parameter, it only computes the
    formula.

    **This is a documented special case of `aic_from_log_likelihood()`,
    not an independent formula.** Substituting the Gaussian-OLS profile
    log-likelihood (`gaussian_ols_log_likelihood`, above) into the
    general AIC and expanding:

        aic_from_log_likelihood(ll, k+1)
            = 2(k+1) - 2*ll
            = n*ln(RSS/n) + 2k + [n*ln(2*pi) + n + 2]

    The bracketed term is a constant for any fixed `n` — identical
    across every candidate compared here, since they're all fit on the
    same `n`. Dropping it (and using `k` alone, not `k+1`, since the
    dropped constant already absorbed the "+1" parameter's own `+2`
    contribution) gives exactly this function's formula. **This
    simplification is only valid when comparing models that are all
    Gaussian-OLS-with-freely-estimated-variance, fit on the same `n`** —
    which is true for every candidate in this engine today, but will
    stop being true the moment a different likelihood family (e.g.
    GARCH) enters the same comparison; use `aic_from_log_likelihood`
    with the full `k+1`-style parameter count there instead. See
    `docs/architecture/model-selection.md` for the full derivation and
    a numeric example of the gap between the two.

    Raises `ValueError` rather than returning `-inf`/`NaN` when the
    formula is undefined or degenerate:
    - `n <= k`: the model has at least as many parameters as
      observations, which is what typically *causes* `RSS == 0` (a
      trivial perfect interpolation, not a meaningful fit) — AIC's
      standard derivation assumes `n > k`.
    - `residual_sum_of_squares <= 0`: undefined at 0 (see
      `gaussian_ols_log_likelihood`'s docstring — this is a genuine
      likelihood singularity, not specific to the `n <= k` case above);
      `RSS` cannot legitimately be negative for a sum of squares either.
    A candidate that hits either case is reported as invalid by
    `ModelSelector`, never silently given an artificially dominant
    (`-inf`) AIC.
    """
    if n <= k:
        raise ValueError(
            f"AIC requires more observations than parameters (n={n}, k={k})."
        )
    # Only called for its RSS<=0/finiteness validation here — see the
    # derivation above for why aic()'s own formula is a direct
    # (numerically different, rank-equivalent) shortcut rather than a
    # literal call through aic_from_log_likelihood().
    gaussian_ols_log_likelihood(n, residual_sum_of_squares)
    return n * math.log(residual_sum_of_squares / n) + 2 * k


def aic_corrected(n: int, residual_sum_of_squares: float, k: int) -> float:
    """AICc — the finite-sample-corrected AIC (Hurvich & Tsai, 1989):

        AICc = AIC + 2k(k+1) / (n - k - 1)

    Plain AIC is a large-sample (asymptotic) approximation that is known
    to under-penalize model complexity when `n` is small relative to
    `k`. AICc corrects for this and converges to plain AIC as `n` grows
    large relative to `k`. **Use AICc instead of (or alongside) `aic()`
    whenever `n` is small relative to `k`** — there is no universally
    agreed exact cutoff in the literature, but the correction term
    itself is the honest signal: the larger `2k(k+1)/(n-k-1)` is
    relative to the AIC value itself, the more plain AIC should be
    distrusted. See `docs/architecture/model-selection.md` for guidance
    on when Hermes should prefer AICc.

    Requires `n > k + 1` — stricter than `aic()`'s own `n > k` — since
    the correction term's denominator (`n - k - 1`) must be strictly
    positive. Raises `ValueError` (never returns NaN/Infinity silently)
    when that doesn't hold, or when the underlying `aic()` call itself
    would be undefined (non-positive RSS, `n <= k`) — this function
    reuses `aic()` directly rather than re-deriving its guards, so both
    stay consistent by construction.
    """
    base_aic = aic(n, residual_sum_of_squares, k)
    denominator = n - k - 1
    if denominator <= 0:
        raise ValueError(
            f"AICc requires n > k + 1 (n={n}, k={k}); the correction term's "
            f"denominator is undefined otherwise."
        )
    correction = (2 * k * (k + 1)) / denominator
    result = base_aic + correction
    if not math.isfinite(result):
        raise ValueError(f"AICc did not evaluate to a finite value (got {result!r}).")
    return result


def rmse(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    """Root mean squared error."""
    y_true, y_pred = _validate_matching_lengths(y_true, y_pred)
    residuals = y_true - y_pred
    return float(np.sqrt(np.mean(residuals**2)))


def mae(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    """Mean absolute error."""
    y_true, y_pred = _validate_matching_lengths(y_true, y_pred)
    residuals = y_true - y_pred
    return float(np.mean(np.abs(residuals)))


__all__ = [
    "aic",
    "aic_corrected",
    "aic_from_log_likelihood",
    "gaussian_ols_log_likelihood",
    "mae",
    "rmse",
    "rss",
]
