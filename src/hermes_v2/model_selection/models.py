"""Candidate regression models for v1.

A single `PolynomialRegressionModel(degree)` generalizes all three v1
candidates — "linear regression" *is* a degree-1 polynomial. Fitting
builds an explicit design matrix (`[1, x, x^2, ..., x^degree]`, plainly
visible here, not hidden inside a library's `.fit()`) and solves it via
`numpy.linalg.lstsq`, the one place this module leans on a well-tested
numerical routine rather than a hand-rolled linear solver (see
`docs/architecture/model-selection.md` for why).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


class ModelFitError(Exception):
    """Raised when a candidate cannot be fit at all — e.g. a
    rank-deficient design matrix (too few distinct `x` values for the
    requested degree, or `n <= parameter_count`). Caught by
    `ModelSelector`, which reports the candidate as invalid rather than
    letting the whole selection run crash."""


@dataclass(frozen=True)
class FittedModel:
    """A fitted candidate's coefficients, ready to predict with."""

    coefficients: np.ndarray
    parameter_count: int


class PolynomialRegressionModel:
    """`y = c0 + c1*x + c2*x^2 + ... + c_degree*x^degree`.

    `parameter_count` (`k`, for AIC) is always `degree + 1` — one
    coefficient per polynomial term, including the intercept. This is
    the number of columns in the design matrix, computed directly from
    it (`design_matrix.shape[1]`), not a separately-maintained number
    that could drift out of sync with what's actually being fit.
    """

    def __init__(self, degree: int, name: str | None = None) -> None:
        if degree < 1:
            raise ValueError(f"degree must be >= 1, got {degree!r}.")
        self.degree = degree
        self.name = name or (
            "linear_regression" if degree == 1 else f"polynomial_regression_{degree}"
        )

    @property
    def parameter_count(self) -> int:
        return self.degree + 1

    def _design_matrix(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=float)
        return np.vstack([x**power for power in range(self.degree + 1)]).T

    def fit(self, x: np.ndarray, y: np.ndarray) -> FittedModel:
        design = self._design_matrix(x)
        y = np.asarray(y, dtype=float)

        coefficients, _residuals, rank, _singular_values = np.linalg.lstsq(
            design, y, rcond=None
        )
        if rank < design.shape[1]:
            raise ModelFitError(
                f"{self.name}: design matrix is rank-deficient "
                f"(rank={rank}, expected {design.shape[1]}) — too few distinct "
                f"x values for degree {self.degree}, or not enough observations."
            )
        return FittedModel(coefficients=coefficients, parameter_count=design.shape[1])

    def predict(self, fitted: FittedModel, x: np.ndarray) -> np.ndarray:
        design = self._design_matrix(x)
        return design @ fitted.coefficients


def default_candidates() -> list[PolynomialRegressionModel]:
    """The v1 candidate set: linear regression, degree-2 and degree-3
    polynomial regression. Deliberately small and explicit (see
    `docs/architecture/model-selection.md`) — not a large auto-generated
    sweep of degrees."""
    return [
        PolynomialRegressionModel(degree=1),
        PolynomialRegressionModel(degree=2),
        PolynomialRegressionModel(degree=3),
    ]


__all__ = [
    "FittedModel",
    "ModelFitError",
    "PolynomialRegressionModel",
    "default_candidates",
]
