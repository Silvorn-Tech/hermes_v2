"""ModelSelector — the engine's single entry point.

Orchestrates: validate input -> temporal split -> fit each candidate on
TRAIN -> RSS/AIC on TRAIN -> RMSE/MAE on VALIDATION -> pick the lowest-
AIC candidate among the valid ones. Nothing here touches Binance, an
order, or a position — see `tests/test_model_selection_isolation.py`
for a static guard that enforces that, not just this docstring.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from hermes_v2.model_selection.metrics import aic as compute_aic
from hermes_v2.model_selection.metrics import mae as compute_mae
from hermes_v2.model_selection.metrics import rmse as compute_rmse
from hermes_v2.model_selection.metrics import rss as compute_rss
from hermes_v2.model_selection.models import (
    ModelFitError,
    PolynomialRegressionModel,
    default_candidates,
)
from hermes_v2.model_selection.split import (
    DEFAULT_TRAIN_RATIO,
    temporal_train_validation_split,
)
from hermes_v2.model_selection.types import ModelCandidateResult, ModelSelectionResult


class ModelSelector:
    """`candidates` defaults to the v1 set (`models.default_candidates()`)
    if not given. `train_ratio` is a plain constructor parameter, not an
    environment variable — this engine has no live/unattended runtime to
    configure via env (see `docs/architecture/model-selection.md`)."""

    def __init__(
        self,
        candidates: Sequence[PolynomialRegressionModel] | None = None,
        train_ratio: float = DEFAULT_TRAIN_RATIO,
    ) -> None:
        self._candidates = (
            list(candidates) if candidates is not None else default_candidates()
        )
        self._train_ratio = train_ratio

    def select(self, x: Sequence[float], y: Sequence[float]) -> ModelSelectionResult:
        x_array, y_array = _validate_input(x, y)

        (x_train, y_train), (x_val, y_val) = temporal_train_validation_split(
            x_array, y_array, train_ratio=self._train_ratio
        )

        results = [
            self._evaluate_candidate(model, x_train, y_train, x_val, y_val)
            for model in self._candidates
        ]

        results.sort(key=_sort_key)
        valid_results = [r for r in results if r.valid]
        selected = valid_results[0] if valid_results else None

        return ModelSelectionResult(
            selected_model=selected,
            candidates=results,
            train_size=len(x_train),
            validation_size=len(x_val),
        )

    def _evaluate_candidate(
        self,
        model: PolynomialRegressionModel,
        x_train: np.ndarray,
        y_train: np.ndarray,
        x_val: np.ndarray,
        y_val: np.ndarray,
    ) -> ModelCandidateResult:
        def invalid(reason: str) -> ModelCandidateResult:
            return ModelCandidateResult(
                model_name=model.name,
                parameter_count=model.parameter_count,
                valid=False,
                invalid_reason=reason,
                aic=None,
                rss=None,
                rmse=None,
                mae=None,
            )

        try:
            fitted = model.fit(x_train, y_train)
        except ModelFitError as exc:
            return invalid(str(exc))

        train_predictions = model.predict(fitted, x_train)
        if not np.all(np.isfinite(train_predictions)):
            return invalid("fitted model produced non-finite predictions on train.")

        train_rss = compute_rss(y_train, train_predictions)
        if not np.isfinite(train_rss):
            return invalid("RSS on train is not finite.")

        try:
            model_aic = compute_aic(len(x_train), train_rss, fitted.parameter_count)
        except ValueError as exc:
            return invalid(str(exc))

        validation_predictions = model.predict(fitted, x_val)
        if not np.all(np.isfinite(validation_predictions)):
            return invalid(
                "fitted model produced non-finite predictions on validation."
            )

        model_rmse = compute_rmse(y_val, validation_predictions)
        model_mae = compute_mae(y_val, validation_predictions)
        if not (np.isfinite(model_rmse) and np.isfinite(model_mae)):
            return invalid("RMSE/MAE on validation is not finite.")

        return ModelCandidateResult(
            model_name=model.name,
            parameter_count=fitted.parameter_count,
            valid=True,
            invalid_reason=None,
            aic=model_aic,
            rss=train_rss,
            rmse=model_rmse,
            mae=model_mae,
        )


def _sort_key(result: ModelCandidateResult) -> tuple[int, float]:
    # Valid candidates (sorted by AIC ascending) always come before
    # invalid ones, which have no AIC to sort by.
    if result.valid and result.aic is not None:
        return (0, result.aic)
    return (1, 0.0)


def _validate_input(
    x: Sequence[float], y: Sequence[float]
) -> tuple[np.ndarray, np.ndarray]:
    x_array = np.asarray(x, dtype=float)
    y_array = np.asarray(y, dtype=float)

    if x_array.ndim != 1 or y_array.ndim != 1:
        raise ValueError("x and y must be one-dimensional sequences.")
    if len(x_array) == 0 or len(y_array) == 0:
        raise ValueError("x and y must not be empty.")
    if len(x_array) != len(y_array):
        raise ValueError(
            f"x and y must have the same length (got {len(x_array)} and "
            f"{len(y_array)})."
        )
    if not np.all(np.isfinite(x_array)):
        raise ValueError("x contains NaN or infinite values.")
    if not np.all(np.isfinite(y_array)):
        raise ValueError("y contains NaN or infinite values.")

    return x_array, y_array


__all__ = ["ModelSelector"]
