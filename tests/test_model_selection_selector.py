"""End-to-end tests for ModelSelector on deterministic synthetic data.

The most important test here is
`test_aic_prefers_the_simpler_model_over_a_lower_rss_complex_one` — it
demonstrates that AIC's complexity penalty is doing real work, not just
re-deriving "pick the lowest RSS" under a different name.
"""

from __future__ import annotations

import numpy as np
import pytest

from hermes_v2.model_selection.models import PolynomialRegressionModel
from hermes_v2.model_selection.selector import ModelSelector


def _linear_dataset(
    n: int = 60, noise_std: float = 1.5, seed: int = 7
) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    x = np.linspace(0, 10, n)
    y = 5.0 + 3.0 * x + rng.normal(0, noise_std, size=n)
    return x, y


def test_aic_prefers_the_simpler_model_over_a_lower_rss_complex_one() -> None:
    """A more complex model (cubic) achieves strictly lower train RSS
    than a simpler one (linear) here -- more free parameters can only
    fit training data at least as well. If ModelSelector just picked
    the lowest RSS, it would select the cubic. AIC's `2k` penalty must
    outweigh that RSS improvement and select the linear model instead —
    proving this engine is actually using Akaike's complexity penalty,
    not RSS in disguise."""
    x, y = _linear_dataset(n=40, noise_std=1.0, seed=0)

    result = ModelSelector().select(x, y)

    by_name = {c.model_name: c for c in result.candidates}
    linear = by_name["linear_regression"]
    cubic = by_name["polynomial_regression_3"]

    assert (
        cubic.rss < linear.rss
    )  # the more complex model really does fit train better...
    assert linear.aic < cubic.aic  # ...but AIC still ranks it worse, once penalized
    assert result.selected_model is not None
    assert result.selected_model.model_name == "linear_regression"


def test_selects_the_lowest_aic_among_valid_candidates() -> None:
    x, y = _linear_dataset()
    result = ModelSelector().select(x, y)

    valid_aics = [c.aic for c in result.candidates if c.valid]
    assert result.selected_model is not None
    assert result.selected_model.aic == min(valid_aics)


def test_candidates_are_sorted_by_aic_ascending() -> None:
    x, y = _linear_dataset()
    result = ModelSelector().select(x, y)
    valid = [c for c in result.candidates if c.valid]
    assert [c.aic for c in valid] == sorted(c.aic for c in valid)


def test_deterministic_output_for_identical_input() -> None:
    x, y = _linear_dataset()
    first = ModelSelector().select(x, y)
    second = ModelSelector().select(x, y)

    assert first.selected_model is not None and second.selected_model is not None
    assert first.selected_model.model_name == second.selected_model.model_name
    assert first.selected_model.aic == second.selected_model.aic
    assert first.selected_model.rss == second.selected_model.rss
    assert first.selected_model.rmse == second.selected_model.rmse
    assert first.selected_model.mae == second.selected_model.mae


def test_perturbing_only_the_validation_target_never_changes_the_selection_or_aic() -> (
    None
):
    """AIC is computed on TRAIN only -- a change confined to the
    VALIDATION partition must never affect which model is selected or
    its AIC, only its (validation-only) RMSE/MAE. This is the temporal-
    leakage guard: if AIC changed here, train and validation data would
    be leaking into each other somewhere in the pipeline."""
    x, y = _linear_dataset(n=50)
    baseline = ModelSelector().select(x, y)

    perturbed_y = y.copy()
    # perturb only the last 20% (== the validation partition at the
    # default 80/20 split)
    perturbed_y[40:] += 1000.0
    perturbed = ModelSelector().select(x, perturbed_y)

    assert perturbed.selected_model is not None and baseline.selected_model is not None
    assert perturbed.selected_model.model_name == baseline.selected_model.model_name
    assert perturbed.selected_model.aic == baseline.selected_model.aic
    assert perturbed.selected_model.rss == baseline.selected_model.rss
    # Validation metrics DO change -- the perturbation is real, just confined.
    assert perturbed.selected_model.rmse != baseline.selected_model.rmse


def test_empty_dataset_is_rejected() -> None:
    with pytest.raises(ValueError, match="empty"):
        ModelSelector().select([], [])


def test_mismatched_lengths_are_rejected() -> None:
    with pytest.raises(ValueError, match="same length"):
        ModelSelector().select([1.0, 2.0, 3.0], [1.0, 2.0])


def test_nan_in_target_is_rejected() -> None:
    x, y = _linear_dataset(n=10)
    y = y.copy()
    y[3] = float("nan")
    with pytest.raises(ValueError, match="NaN or infinite"):
        ModelSelector().select(x, y)


def test_nan_in_features_is_rejected() -> None:
    x, y = _linear_dataset(n=10)
    x = x.copy()
    x[0] = float("nan")
    with pytest.raises(ValueError, match="NaN or infinite"):
        ModelSelector().select(x, y)


def test_infinite_values_are_rejected() -> None:
    x, y = _linear_dataset(n=10)
    y = y.copy()
    y[0] = float("inf")
    with pytest.raises(ValueError, match="NaN or infinite"):
        ModelSelector().select(x, y)


def test_dataset_too_small_for_any_candidate_degrades_gracefully() -> None:
    # 3 observations: the default 80/20 split leaves 2 train rows,
    # which is too few for linear regression's AIC (n=2 <= k=2) and
    # rank-deficient for the polynomial candidates (k=3, k=4). The
    # split itself is still well-formed (a real ValueError case is
    # covered by test_model_selection_split.py), so this must NOT
    # raise -- it degrades to "no valid candidate," reported with a
    # reason per candidate, never a fabricated selection.
    result = ModelSelector().select([1.0, 2.0, 3.0], [1.0, 2.0, 3.0])
    assert result.selected_model is None
    assert all(not c.valid for c in result.candidates)
    assert all(c.invalid_reason is not None for c in result.candidates)


def test_a_candidate_that_cannot_fit_is_excluded_without_crashing_the_run() -> None:
    x, y = _linear_dataset(n=20)
    # degree=15 needs 16 parameters; only 16 train rows (80% of 20) --
    # not enough for a well-posed fit, so this candidate must come back
    # invalid rather than raising out of ModelSelector.select().
    failing_candidate = PolynomialRegressionModel(degree=15)
    working_candidate = PolynomialRegressionModel(degree=1)

    result = ModelSelector(candidates=[failing_candidate, working_candidate]).select(
        x, y
    )

    by_name = {c.model_name: c for c in result.candidates}
    assert by_name["polynomial_regression_15"].valid is False
    assert by_name["polynomial_regression_15"].invalid_reason is not None
    assert by_name["linear_regression"].valid is True
    assert result.selected_model is not None
    assert result.selected_model.model_name == "linear_regression"


def test_all_candidates_invalid_yields_no_selection_but_full_candidate_list() -> None:
    x, y = _linear_dataset(n=10)
    hopeless = [
        PolynomialRegressionModel(degree=20),
        PolynomialRegressionModel(degree=30),
    ]

    result = ModelSelector(candidates=hopeless).select(x, y)

    assert result.selected_model is None
    assert len(result.candidates) == 2
    assert all(not c.valid for c in result.candidates)
    assert all(c.invalid_reason is not None for c in result.candidates)


def test_custom_train_ratio_is_respected() -> None:
    x, y = _linear_dataset(n=100)
    result = ModelSelector(train_ratio=0.6).select(x, y)
    assert result.train_size == 60
    assert result.validation_size == 40
