"""End-to-end tests for ModelSelector on deterministic synthetic data.

The most important test here is
`test_aic_prefers_the_simpler_model_over_a_lower_rss_complex_one` — it
demonstrates that AIC's complexity penalty is doing real work, not just
re-deriving "pick the lowest RSS" under a different name.
"""

from __future__ import annotations

import numpy as np
import pytest

from hermes_v2.model_selection.models import (
    AutoregressiveModel,
    PolynomialRegressionModel,
    default_autoregressive_candidates,
)
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


def test_empty_candidates_list_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="candidates must not be empty"):
        ModelSelector(candidates=[])


def test_none_candidates_still_uses_the_default_set() -> None:
    # candidates=None (the default) is NOT the same as candidates=[] --
    # None means "use the built-in set," [] means "nothing to compare,"
    # which is almost certainly a caller mistake (see the test above).
    result = ModelSelector(candidates=None).select(*_linear_dataset())
    names = {c.model_name for c in result.candidates}
    assert names == {
        "linear_regression",
        "polynomial_regression_2",
        "polynomial_regression_3",
    }


def test_explicit_aic_tie_breaks_toward_the_earlier_listed_candidate() -> None:
    # Two identically-configured candidates (different names) fit on the
    # same data produce byte-identical AIC -- documents the tie-break
    # rule in selector.py's _sort_key: whichever is listed first wins.
    x, y = _linear_dataset()
    first = PolynomialRegressionModel(degree=1, name="first")
    second = PolynomialRegressionModel(degree=1, name="second")

    result = ModelSelector(candidates=[first, second]).select(x, y)

    by_name = {c.model_name: c for c in result.candidates}
    assert by_name["first"].aic == by_name["second"].aic
    assert result.selected_model is not None
    assert result.selected_model.model_name == "first"

    # Listing them in the opposite order flips the tie-break, proving
    # it's genuinely list-order-driven, not name-alphabetical or
    # otherwise hidden.
    reversed_result = ModelSelector(candidates=[second, first]).select(x, y)
    assert reversed_result.selected_model is not None
    assert reversed_result.selected_model.model_name == "second"


def test_aic_selection_and_validation_ranking_can_disagree_and_both_are_visible() -> (
    None
):
    """Characterizes today's documented behavior (see the audit): the
    engine does not reject or re-rank a candidate for having worse
    validation error than a competitor -- it only ranks by AIC. This
    test proves that disagreement is visible (both metrics are always
    present) even though nothing here acts on it."""
    x, y = _linear_dataset(n=60, noise_std=1.0, seed=0)
    result = ModelSelector().select(x, y)

    valid = [c for c in result.candidates if c.valid]
    aic_order = [c.model_name for c in sorted(valid, key=lambda c: c.aic)]
    rmse_order = [c.model_name for c in sorted(valid, key=lambda c: c.rmse)]

    # Not asserting they disagree on every run (that depends on the
    # noise draw) -- asserting that IF they disagree, the AIC-selected
    # model is still exactly aic_order[0], and every candidate's own
    # rmse remains visible in the result regardless of its AIC rank.
    assert result.selected_model is not None
    assert result.selected_model.model_name == aic_order[0]
    assert all(c.rmse is not None for c in valid)
    if aic_order[0] != rmse_order[0]:
        # A genuine disagreement occurred in this run -- confirm it's
        # not hidden: the AIC-best model's own rmse is still reported,
        # even if it isn't the best validation performer.
        aic_best = next(c for c in valid if c.model_name == aic_order[0])
        assert aic_best.rmse is not None


def test_valid_candidates_expose_aicc_alongside_aic() -> None:
    x, y = _linear_dataset(n=60)
    result = ModelSelector().select(x, y)
    valid = [c for c in result.candidates if c.valid]
    assert valid  # sanity: the dataset is large enough that something fits
    for candidate in valid:
        assert candidate.aicc is not None
        assert candidate.aicc > candidate.aic  # correction term is always positive


def test_aicc_is_none_when_n_is_too_small_relative_to_k_even_though_aic_is_valid() -> (
    None
):
    # n_train=6 with degree=3 (k=4): aic() needs n>k (6>4, fine), but
    # aic_corrected() needs n>k+1 (6>5, fine too) -- use exactly the
    # boundary the other direction: n_train=5, k=4 -> aic ok (5>4),
    # aicc's denominator (n-k-1=0) is not.
    x = np.linspace(0, 10, 7)  # 80% split -> 5 train rows (floor(7*0.8)=5)
    y = 3.0 + 2.0 * x + 0.1 * x**2 + 0.01 * x**3
    result = ModelSelector(candidates=[PolynomialRegressionModel(degree=3)]).select(
        x, y
    )
    candidate = result.candidates[0]
    assert candidate.valid is True
    assert candidate.aic is not None
    assert candidate.aicc is None


# --- AR(1)/AR(2) end-to-end -------------------------------------------------------


def _ar1_returns(n: int = 80, phi: float = 0.3, seed: int = 3) -> np.ndarray:
    rng = np.random.default_rng(seed)
    series = np.zeros(n)
    for t in range(1, n):
        series[t] = 0.0005 + phi * series[t - 1] + rng.normal(0, 0.01)
    return series


def test_ar_candidates_selected_end_to_end_through_model_selector() -> None:
    returns = _ar1_returns()
    result = ModelSelector(candidates=default_autoregressive_candidates()).select(
        returns, returns
    )

    assert result.selected_model is not None
    assert result.selected_model.model_name in {"ar_1", "ar_2"}
    names = {c.model_name for c in result.candidates}
    assert names == {"ar_1", "ar_2"}


def test_ar_candidate_via_model_selector_requires_x_equals_y() -> None:
    returns = _ar1_returns()
    unrelated_x = np.linspace(0, 1, len(returns))
    result = ModelSelector(candidates=[AutoregressiveModel(order=1)]).select(
        unrelated_x, returns
    )

    # Never crashes the whole run -- reported as an invalid candidate,
    # same as any other fit failure.
    assert result.selected_model is None
    assert result.candidates[0].valid is False
    assert "identical series" in (result.candidates[0].invalid_reason or "")
