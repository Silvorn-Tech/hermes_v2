"""Tests for the candidate models: correct fitting/prediction on known
synthetic data, correct k-counting, rank-deficient rejection, numerical
stability under poorly-scaled x, and the AR(1)/AR(2) candidates."""

from __future__ import annotations

import numpy as np
import pytest

from hermes_v2.model_selection.models import (
    AutoregressiveModel,
    ModelFitError,
    PolynomialRegressionModel,
    default_autoregressive_candidates,
    default_candidates,
)


def test_default_candidates_are_the_v1_set() -> None:
    names = [c.name for c in default_candidates()]
    assert names == [
        "linear_regression",
        "polynomial_regression_2",
        "polynomial_regression_3",
    ]


def test_degree_must_be_at_least_1() -> None:
    with pytest.raises(ValueError, match="degree"):
        PolynomialRegressionModel(degree=0)


@pytest.mark.parametrize("degree,expected_k", [(1, 2), (2, 3), (3, 4)])
def test_parameter_count_is_degree_plus_one(degree: int, expected_k: int) -> None:
    model = PolynomialRegressionModel(degree=degree)
    assert model.parameter_count == expected_k


def test_linear_regression_recovers_the_true_function() -> None:
    # fitted.coefficients are in centered/scaled x-space (see
    # PolynomialRegressionModel's docstring) -- assert on predict()'s
    # output (raw x-space, y-units) rather than the raw coefficient
    # vector, which is the black-box, transform-agnostic way to prove
    # "the true function was recovered."
    x = np.linspace(0, 10, 50)
    y = 3.0 + 2.0 * x  # noiseless -- must be recovered exactly
    model = PolynomialRegressionModel(degree=1)
    fitted = model.fit(x, y)
    assert fitted.parameter_count == 2
    probe_x = np.array([0.0, 5.0, 10.0, -3.0])
    assert model.predict(fitted, probe_x) == pytest.approx(
        3.0 + 2.0 * probe_x, abs=1e-8
    )


def test_polynomial_degree_2_recovers_the_true_function() -> None:
    x = np.linspace(-5, 5, 50)
    y = 1.0 - 2.0 * x + 0.5 * x**2  # noiseless
    model = PolynomialRegressionModel(degree=2)
    fitted = model.fit(x, y)
    probe_x = np.array([-5.0, 0.0, 3.0, 7.0])
    expected = 1.0 - 2.0 * probe_x + 0.5 * probe_x**2
    assert model.predict(fitted, probe_x) == pytest.approx(expected, abs=1e-6)


def test_polynomial_degree_3_recovers_the_true_function() -> None:
    x = np.linspace(-3, 3, 50)
    y = 0.5 + 1.0 * x - 0.3 * x**2 + 0.1 * x**3  # noiseless
    model = PolynomialRegressionModel(degree=3)
    fitted = model.fit(x, y)
    probe_x = np.array([-3.0, 0.0, 2.0, 4.0])
    expected = 0.5 + 1.0 * probe_x - 0.3 * probe_x**2 + 0.1 * probe_x**3
    assert model.predict(fitted, probe_x) == pytest.approx(expected, abs=1e-5)


def test_coefficient_recovery_within_tolerance_under_known_gaussian_noise() -> None:
    # Unlike the noiseless tests above, this proves the solver behaves
    # correctly under realistic noisy conditions, not just that it can
    # solve an exact system. Recovered coefficients must land close to
    # the true generating coefficients -- not exact, but not arbitrary.
    rng = np.random.default_rng(123)
    x = np.linspace(0, 20, 200)
    true_intercept, true_slope = 4.0, -1.5
    y = true_intercept + true_slope * x + rng.normal(0, 0.5, size=len(x))

    model = PolynomialRegressionModel(degree=1)
    fitted = model.fit(x, y)
    probe_x = np.array([0.0, 10.0, 20.0])
    predicted = model.predict(fitted, probe_x)
    true_line = true_intercept + true_slope * probe_x
    assert predicted == pytest.approx(true_line, abs=0.3)


def test_predict_matches_the_fitted_polynomial() -> None:
    x = np.linspace(0, 10, 20)
    y = 3.0 + 2.0 * x
    model = PolynomialRegressionModel(degree=1)
    fitted = model.fit(x, y)
    predictions = model.predict(fitted, np.array([0.0, 5.0, 10.0]))
    assert predictions == pytest.approx([3.0, 13.0, 23.0], abs=1e-8)


def test_rank_deficient_design_matrix_raises_model_fit_error() -> None:
    # Only 2 distinct x values -- can't uniquely determine a degree-3
    # (4-parameter) polynomial from them.
    x = np.array([1.0, 1.0, 1.0, 2.0, 2.0])
    y = np.array([1.0, 1.0, 1.0, 2.0, 2.0])
    model = PolynomialRegressionModel(degree=3)
    with pytest.raises(ModelFitError, match="rank-deficient"):
        model.fit(x, y)


def test_too_few_observations_for_the_degree_raises_model_fit_error() -> None:
    x = np.array([1.0, 2.0])
    y = np.array([1.0, 2.0])
    model = PolynomialRegressionModel(degree=3)  # needs 4 parameters, only 2 points
    with pytest.raises(ModelFitError):
        model.fit(x, y)


# --- numerical stability under poorly-scaled x ---------------------------------


def test_degree_3_fit_is_numerically_stable_for_large_magnitude_x() -> None:
    # Raw Unix-timestamp-scale x (~1e6-1e7) would badly condition an
    # un-centered/un-scaled [1, x, x^2, x^3] design matrix. Centering/
    # scaling (see PolynomialRegressionModel's docstring) must keep the
    # fit accurate regardless.
    x = np.linspace(1_000_000, 1_000_100, 60)
    true_coefficients = [2.0, -0.003, 0.0000005, -0.00000000009]
    y = (
        true_coefficients[0]
        + true_coefficients[1] * x
        + true_coefficients[2] * x**2
        + true_coefficients[3] * x**3
    )

    model = PolynomialRegressionModel(degree=3)
    fitted = model.fit(x, y)
    predictions = model.predict(fitted, x)

    assert np.all(np.isfinite(predictions))
    assert predictions == pytest.approx(y, abs=1e-6)


def test_predict_is_invariant_to_x_scale_up_to_floating_point_precision() -> None:
    # The same linear relationship, fit once on small-scale x and once
    # on the same data shifted to large-scale x -- predictions (in
    # y-space, at the corresponding points) must match, proving the
    # centering/scaling transform doesn't change what's being modeled.
    x_small = np.linspace(0, 10, 40)
    y = 5.0 + 3.0 * x_small
    model = PolynomialRegressionModel(degree=1)
    fitted_small = model.fit(x_small, y)

    offset = 5_000_000.0
    x_large = x_small + offset
    fitted_large = model.fit(x_large, y)

    probe_small = np.array([0.0, 5.0, 10.0])
    probe_large = probe_small + offset
    assert model.predict(fitted_large, probe_large) == pytest.approx(
        model.predict(fitted_small, probe_small), abs=1e-6
    )


# --- direct API misuse (bypassing ModelSelector) --------------------------------


def test_polynomial_predict_raises_on_a_fitted_model_missing_the_scale_transform() -> (
    None
):
    from hermes_v2.model_selection.models import FittedModel

    model = PolynomialRegressionModel(degree=1)
    bare_fitted = FittedModel(coefficients=np.array([1.0, 2.0]), parameter_count=2)
    with pytest.raises(ValueError, match="x_center"):
        model.predict(bare_fitted, np.array([1.0, 2.0]))


# --- AR(1) / AR(2) ---------------------------------------------------------------


def test_default_autoregressive_candidates_are_ar1_and_ar2() -> None:
    names = [c.name for c in default_autoregressive_candidates()]
    assert names == ["ar_1", "ar_2"]


def test_ar_order_must_be_at_least_1() -> None:
    with pytest.raises(ValueError, match="order"):
        AutoregressiveModel(order=0)


@pytest.mark.parametrize("order,expected_k", [(1, 2), (2, 3)])
def test_ar_parameter_count_is_order_plus_one(order: int, expected_k: int) -> None:
    assert AutoregressiveModel(order=order).parameter_count == expected_k


def test_ar1_recovers_a_known_noiseless_autoregressive_process() -> None:
    n = 100
    phi, intercept = 0.4, 0.1
    series = np.zeros(n)
    for t in range(1, n):
        series[t] = intercept + phi * series[t - 1]  # noiseless

    model = AutoregressiveModel(order=1)
    fitted = model.fit(series, series)
    assert fitted.coefficients == pytest.approx([intercept, phi], abs=1e-8)
    assert fitted.parameter_count == 2


def test_ar2_recovers_a_known_noiseless_autoregressive_process() -> None:
    n = 100
    c, phi1, phi2 = 0.05, 0.5, -0.2
    series = np.zeros(n)
    series[1] = 1.0  # seed so the recursion isn't trivially all-zero
    for t in range(2, n):
        series[t] = c + phi1 * series[t - 1] + phi2 * series[t - 2]  # noiseless

    model = AutoregressiveModel(order=2)
    fitted = model.fit(series, series)
    assert fitted.coefficients == pytest.approx([c, phi1, phi2], abs=1e-6)


def test_ar_predict_produces_order_fewer_rows_than_the_input_series() -> None:
    # A perfectly linear series would make AR(2)'s two lag columns
    # exactly collinear (rank-deficient) -- use a curved series instead,
    # this test only cares about output length, not recovery accuracy.
    series = np.sin(np.linspace(0, 6, 30))
    model = AutoregressiveModel(order=2)
    fitted = model.fit(series, series)
    predictions = model.predict(fitted, series)
    assert len(predictions) == len(series) - 2


def test_ar_fit_requires_x_and_y_to_be_the_identical_series() -> None:
    series = np.linspace(0, 1, 20)
    other = np.linspace(0, 1, 20) + 0.001  # not identical
    model = AutoregressiveModel(order=1)
    with pytest.raises(ModelFitError, match="identical series"):
        model.fit(other, series)


def test_ar_fit_requires_more_observations_than_the_order() -> None:
    model = AutoregressiveModel(order=2)
    short_series = np.array([1.0, 2.0])  # order=2 needs > 2 observations
    with pytest.raises(ModelFitError):
        model.fit(short_series, short_series)


def test_ar_predict_returns_empty_for_a_series_too_short_to_lag() -> None:
    series = np.sin(np.linspace(0, 6, 20))
    model = AutoregressiveModel(order=2)
    fitted = model.fit(series, series)
    assert len(model.predict(fitted, np.array([1.0, 2.0]))) == 0
