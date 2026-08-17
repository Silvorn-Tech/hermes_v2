"""Candidate regression models.

V1: `PolynomialRegressionModel(degree)` generalizes linear regression
(degree 1) and degree-2/3 polynomial regression. V2 adds
`AutoregressiveModel(order)` for AR(1)/AR(2) on a return series' own
lags. Fitting always solves via `numpy.linalg.lstsq`, the one place
this module leans on a well-tested numerical routine rather than a
hand-rolled linear solver (see `docs/architecture/model-selection.md`
for why).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


class ModelFitError(Exception):
    """Raised when a candidate cannot be fit at all — e.g. a
    rank-deficient design matrix (too few distinct `x` values for the
    requested degree, or `n <= parameter_count`), or too few
    observations for an autoregressive model's lag order. Caught by
    `ModelSelector`, which reports the candidate as invalid rather than
    letting the whole selection run crash."""


@dataclass(frozen=True)
class FittedModel:
    """A fitted candidate's coefficients, ready to predict with.

    `x_center`/`x_scale` are populated only by `PolynomialRegressionModel`
    (see its docstring) — `None` for `AutoregressiveModel`, which doesn't
    transform its input.
    """

    coefficients: np.ndarray
    parameter_count: int
    x_center: float | None = None
    x_scale: float | None = None


class PolynomialRegressionModel:
    """`y = c0 + c1*x' + c2*x'^2 + ... + c_degree*x'^degree`, where
    `x' = (x - x_center) / x_scale` is `x` centered on its TRAIN mean and
    scaled by its TRAIN standard deviation.

    **Why centering/scaling:** the raw design matrix `[1, x, x^2, x^3]`
    is badly conditioned for large-magnitude `x` (a raw Unix timestamp,
    or a raw price level) — `x^3` for `x ~ 1e6` is `~1e18`, and
    `numpy.linalg.lstsq`'s SVD-based solve loses meaningful precision at
    that scale. Centering and scaling `x` before building the design
    matrix keeps every power's magnitude close to 1, which is standard
    practice for polynomial regression conditioning and doesn't change
    *what* is being fit — `predict()` applies the exact same transform
    (stored on `FittedModel`, computed from TRAIN data only — never
    re-derived from validation data, which would leak validation
    statistics into the fit) before evaluating the polynomial, so
    predictions in `y`-space are unaffected by this transform (up to
    floating-point precision); only the internal coefficients'
    representation is in transformed-`x` units, not raw `x` units.

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

    def _design_matrix(self, x: np.ndarray, center: float, scale: float) -> np.ndarray:
        x = np.asarray(x, dtype=float)
        x_transformed = (x - center) / scale
        return np.vstack([x_transformed**power for power in range(self.degree + 1)]).T

    def fit(self, x: np.ndarray, y: np.ndarray) -> FittedModel:
        x = np.asarray(x, dtype=float)
        y = np.asarray(y, dtype=float)

        center = float(np.mean(x))
        scale = float(np.std(x))
        if scale == 0.0 or not np.isfinite(scale):
            # Degenerate: every x value is identical (or std failed to
            # compute a finite value). Centering alone still makes every
            # transformed x exactly 0, which correctly produces a
            # rank-deficient design matrix below (no information to
            # identify any slope term) rather than a division-by-zero.
            scale = 1.0

        design = self._design_matrix(x, center, scale)
        coefficients, _residuals, rank, _singular_values = np.linalg.lstsq(
            design, y, rcond=None
        )
        if rank < design.shape[1]:
            raise ModelFitError(
                f"{self.name}: design matrix is rank-deficient "
                f"(rank={rank}, expected {design.shape[1]}) — too few distinct "
                f"x values for degree {self.degree}, or not enough observations."
            )
        return FittedModel(
            coefficients=coefficients,
            parameter_count=design.shape[1],
            x_center=center,
            x_scale=scale,
        )

    def predict(self, fitted: FittedModel, x: np.ndarray) -> np.ndarray:
        if fitted.x_center is None or fitted.x_scale is None:
            raise ValueError(
                f"{self.name}: fitted.x_center/x_scale are missing — this FittedModel "
                "wasn't produced by PolynomialRegressionModel.fit()."
            )
        design = self._design_matrix(x, fitted.x_center, fitted.x_scale)
        return design @ fitted.coefficients


class AutoregressiveModel:
    """`y[t] = c0 + c1*y[t-1] + ... + c_order*y[t-order] + error` — a
    candidate fit on a series' own lagged values, reusing the same OLS/
    lstsq machinery as `PolynomialRegressionModel`.

    **Intended for (log-)return series, not raw price** — see
    `docs/architecture/model-selection.md`'s stationarity guidance; a
    raw, non-stationary price series is exactly the kind of input that
    makes AR coefficients spurious and unstable.

    **Calling convention — pass the same series as both `x` and `y`.**
    `ModelSelector`'s orchestration (`selector.py`) always calls
    `model.fit(x, y)` and `model.predict(fitted, x)` uniformly across
    every candidate type. An autoregressive model is self-referential —
    it has no independent feature, only the series' own history — so it
    needs the *same* series again at `predict()` time (called with `x`,
    not `y`). Rather than special-casing `ModelSelector`'s orchestration
    per candidate type, this class requires `x` and `y` to be identical
    and fails loudly (`ModelFitError`) if they aren't, so a caller
    mistake (e.g. accidentally passing an unrelated `x`) is caught
    immediately instead of silently fitting nonsense. Use
    `ModelSelector(candidates=default_autoregressive_candidates())`
    and call `.select(returns, returns)`.

    **Predictions are one-step-ahead using true (not model-generated)
    lagged values**, and only within the single contiguous series
    `predict()` is given — the first `order` points of *that* series
    have no valid lag history within it and are simply not predicted
    (both `fit()` and `predict()` produce `len(series) - order` rows,
    never `len(series)`; `ModelSelector` aligns targets to match, see
    `selector.py`). This deliberately does not carry lag context across
    the train/validation boundary (e.g. seeding validation's first
    prediction from train's last observations) — full multi-step or
    boundary-seeded forecasting is real additional complexity out of
    scope for this small V2 addition; see the architecture doc's
    limitations section.
    """

    def __init__(self, order: int, name: str | None = None) -> None:
        if order < 1:
            raise ValueError(f"order must be >= 1, got {order!r}.")
        self.order = order
        self.name = name or f"ar_{order}"

    @property
    def parameter_count(self) -> int:
        return self.order + 1

    def _require_same_series(self, x: np.ndarray, y: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=float)
        y = np.asarray(y, dtype=float)
        if x.shape != y.shape or not np.array_equal(x, y):
            raise ModelFitError(
                f"{self.name}: x and y must be the identical series — an "
                "autoregressive model predicts a series from its own lags, not "
                "from an independent feature. Call ModelSelector.select(returns, "
                "returns), passing the same series twice."
            )
        return y

    def _lagged_design_matrix(
        self, series: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray]:
        """Returns `(design, target)`, both of length `len(series) - order`:
        row `i` of `design` is `[1, series[order+i-1], ..., series[i]]`
        (most recent lag first) and `target[i] = series[order + i]`."""
        n = len(series)
        if n <= self.order:
            raise ModelFitError(
                f"{self.name}: needs more than order={self.order} observations, "
                f"got {n}."
            )
        rows = n - self.order
        design = np.ones((rows, self.order + 1))
        for lag in range(1, self.order + 1):
            design[:, lag] = series[self.order - lag : n - lag]
        target = series[self.order :]
        return design, target

    def fit(self, x: np.ndarray, y: np.ndarray) -> FittedModel:
        series = self._require_same_series(x, y)
        design, target = self._lagged_design_matrix(series)

        coefficients, _residuals, rank, _singular_values = np.linalg.lstsq(
            design, target, rcond=None
        )
        if rank < design.shape[1]:
            raise ModelFitError(
                f"{self.name}: design matrix is rank-deficient "
                f"(rank={rank}, expected {design.shape[1]})."
            )
        return FittedModel(coefficients=coefficients, parameter_count=design.shape[1])

    def predict(self, fitted: FittedModel, x: np.ndarray) -> np.ndarray:
        # x IS the series here (see the calling-convention note above) —
        # predict() only needs one series, unlike fit()'s symmetry check.
        series = np.asarray(x, dtype=float)
        if len(series) <= self.order:
            return np.empty(0, dtype=float)
        design, _target = self._lagged_design_matrix(series)
        return design @ fitted.coefficients


def default_candidates() -> list[PolynomialRegressionModel]:
    """The regression-on-an-independent-feature candidate set: linear
    regression, degree-2 and degree-3 polynomial regression. Deliberately
    small and explicit (see `docs/architecture/model-selection.md`) — not
    a large auto-generated sweep of degrees. Does NOT include
    `AutoregressiveModel` — see `default_autoregressive_candidates()`,
    kept separate because AR models need the `x=y` calling convention
    documented on `AutoregressiveModel`, which would silently misfire if
    mixed into this list for ordinary `select(x, y)` usage with `x != y`.
    """
    return [
        PolynomialRegressionModel(degree=1),
        PolynomialRegressionModel(degree=2),
        PolynomialRegressionModel(degree=3),
    ]


def default_autoregressive_candidates() -> list[AutoregressiveModel]:
    """AR(1) and AR(2). Use with
    `ModelSelector(candidates=default_autoregressive_candidates())`
    and call `.select(returns, returns)` — same series twice, see
    `AutoregressiveModel`'s docstring."""
    return [AutoregressiveModel(order=1), AutoregressiveModel(order=2)]


__all__ = [
    "AutoregressiveModel",
    "FittedModel",
    "ModelFitError",
    "PolynomialRegressionModel",
    "default_autoregressive_candidates",
    "default_candidates",
]
