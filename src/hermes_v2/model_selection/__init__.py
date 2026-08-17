"""Hermes's Model Selection Engine — a research library, not a trading
component. Compares candidate regression models on historical data
using AIC (which penalizes complexity, unlike raw RSS) and returns a
structured result. See `docs/architecture/model-selection.md`.

This package is deliberately isolated from the execution path: it
never imports `hermes_v2.integrations.binance`,
`hermes_v2.trading.order_service`, or `hermes_v2.trading.risk_engine`,
and never reads the trading kill-switch flag — enforced by
`tests/test_model_selection_isolation.py`, not just this docstring.
"""

from hermes_v2.model_selection.metrics import (
    aic,
    aic_corrected,
    aic_from_log_likelihood,
    gaussian_ols_log_likelihood,
    mae,
    rmse,
    rss,
)
from hermes_v2.model_selection.models import (
    AutoregressiveModel,
    FittedModel,
    ModelFitError,
    PolynomialRegressionModel,
    default_autoregressive_candidates,
    default_candidates,
)
from hermes_v2.model_selection.selector import CandidateModel, ModelSelector
from hermes_v2.model_selection.split import (
    DEFAULT_TRAIN_RATIO,
    temporal_train_validation_split,
)
from hermes_v2.model_selection.types import ModelCandidateResult, ModelSelectionResult

__all__ = [
    "DEFAULT_TRAIN_RATIO",
    "AutoregressiveModel",
    "CandidateModel",
    "FittedModel",
    "ModelCandidateResult",
    "ModelFitError",
    "ModelSelectionResult",
    "ModelSelector",
    "PolynomialRegressionModel",
    "aic",
    "aic_corrected",
    "aic_from_log_likelihood",
    "default_autoregressive_candidates",
    "default_candidates",
    "gaussian_ols_log_likelihood",
    "mae",
    "rmse",
    "rss",
    "temporal_train_validation_split",
]
