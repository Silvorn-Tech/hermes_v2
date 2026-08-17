"""ModelSelector — the engine's single entry point.

Orchestrates: validate input -> temporal split -> fit each candidate on
TRAIN -> RSS/AIC on TRAIN -> RMSE/MAE on VALIDATION -> pick the lowest-
AIC candidate among the valid ones. Nothing here touches Binance, an
order, or a position — see `tests/test_model_selection_isolation.py`
for a static guard that enforces that, not just this docstring.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

import numpy as np

from hermes_v2.model_selection.metrics import aic as compute_aic
from hermes_v2.model_selection.metrics import aic_corrected as compute_aic_corrected
from hermes_v2.model_selection.metrics import mae as compute_mae
from hermes_v2.model_selection.metrics import rmse as compute_rmse
from hermes_v2.model_selection.metrics import rss as compute_rss
from hermes_v2.model_selection.models import (
    FittedModel,
    ModelFitError,
    default_candidates,
)
from hermes_v2.model_selection.split import (
    DEFAULT_TRAIN_RATIO,
    temporal_train_validation_split,
)
from hermes_v2.model_selection.types import ModelCandidateResult, ModelSelectionResult


class CandidateModel(Protocol):
    """The minimal interface `ModelSelector` needs from a candidate —
    satisfied by both `PolynomialRegressionModel` and
    `AutoregressiveModel` today. `predict()` may return fewer rows than
    it was given `x` (an autoregressive model can't predict its own
    first `order` points) — `ModelSelector` aligns targets to whatever
    length comes back rather than assuming it always matches the input.
    """

    name: str
    parameter_count: int

    def fit(self, x: np.ndarray, y: np.ndarray) -> FittedModel: ...
    def predict(self, fitted: FittedModel, x: np.ndarray) -> np.ndarray: ...


class ModelSelector:
    """`candidates` defaults to the v1 set (`models.default_candidates()`)
    if not given — must be non-empty; an explicitly empty list is almost
    certainly a caller mistake, not an intentional "no candidates"
    request, and is rejected at construction rather than silently
    producing a `ModelSelectionResult` with nothing in it. `train_ratio`
    is a plain constructor parameter, not an environment variable — this
    engine has no live/unattended runtime to configure via env (see
    `docs/architecture/model-selection.md`)."""

    def __init__(
        self,
        candidates: Sequence[CandidateModel] | None = None,
        train_ratio: float = DEFAULT_TRAIN_RATIO,
    ) -> None:
        resolved = list(candidates) if candidates is not None else default_candidates()
        if not resolved:
            raise ValueError("candidates must not be empty.")
        self._candidates = resolved
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
        model: CandidateModel,
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
                aicc=None,
            )

        try:
            fitted = model.fit(x_train, y_train)
        except ModelFitError as exc:
            return invalid(str(exc))
        except np.linalg.LinAlgError as exc:
            # A candidate's numerical solve can fail in ways its own
            # fit() didn't anticipate (e.g. an unexpected singular
            # matrix past the rank check) -- one candidate's surprise
            # must not crash the whole selection run.
            return invalid(f"{model.name}: numerical error while fitting ({exc}).")

        # A candidate (e.g. AutoregressiveModel) may predict fewer rows
        # than it was given x for -- align the target to the *last*
        # len(predictions) rows rather than assuming a 1:1 match, so
        # "n" for AIC always reflects the observations actually used.
        train_predictions = model.predict(fitted, x_train)
        if len(train_predictions) == 0:
            return invalid("fitted model produced no predictions on train.")
        if not np.all(np.isfinite(train_predictions)):
            return invalid("fitted model produced non-finite predictions on train.")
        aligned_y_train = y_train[-len(train_predictions) :]

        train_rss = compute_rss(aligned_y_train, train_predictions)
        if not np.isfinite(train_rss):
            return invalid("RSS on train is not finite.")

        n_train_effective = len(train_predictions)
        try:
            model_aic = compute_aic(
                n_train_effective, train_rss, fitted.parameter_count
            )
        except ValueError as exc:
            return invalid(str(exc))

        # AICc is best-effort, additional information -- never gates
        # validity or selection (that stays AIC-only, per the audit).
        try:
            model_aicc = compute_aic_corrected(
                n_train_effective, train_rss, fitted.parameter_count
            )
        except ValueError:
            model_aicc = None

        validation_predictions = model.predict(fitted, x_val)
        if len(validation_predictions) == 0:
            return invalid("fitted model produced no predictions on validation.")
        if not np.all(np.isfinite(validation_predictions)):
            return invalid(
                "fitted model produced non-finite predictions on validation."
            )
        aligned_y_val = y_val[-len(validation_predictions) :]

        model_rmse = compute_rmse(aligned_y_val, validation_predictions)
        model_mae = compute_mae(aligned_y_val, validation_predictions)
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
            aicc=model_aicc,
        )


def _sort_key(result: ModelCandidateResult) -> tuple[int, float]:
    # Valid candidates (sorted by AIC ascending) always come before
    # invalid ones, which have no AIC to sort by.
    #
    # Tie-break rule (explicit, not an accident of implementation):
    # Python's list.sort() is stable, and every invalid candidate (and
    # every exact-AIC tie among valid ones) shares an identical sort
    # key here -- so on a tie, whichever candidate appears earlier in
    # the `candidates` list passed to ModelSelector wins. The v1/v2
    # default candidate lists (default_candidates(),
    # default_autoregressive_candidates()) are both ordered
    # simplest-first, so this rule favors parsimony on a tie by
    # construction -- but it is a consequence of list order, not a
    # separate rule this function applies. A caller passing a custom
    # `candidates` list controls tie-breaking via that list's order.
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
