"""Tests for the v1 candidate models: correct fitting/prediction on
known synthetic data, correct k-counting, and rank-deficient rejection."""

from __future__ import annotations

import numpy as np
import pytest

from hermes_v2.model_selection.models import (
    ModelFitError,
    PolynomialRegressionModel,
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


def test_linear_regression_recovers_known_coefficients() -> None:
    x = np.linspace(0, 10, 50)
    y = 3.0 + 2.0 * x  # noiseless -- coefficients must be recovered exactly
    model = PolynomialRegressionModel(degree=1)
    fitted = model.fit(x, y)
    assert fitted.coefficients == pytest.approx([3.0, 2.0], abs=1e-8)
    assert fitted.parameter_count == 2


def test_polynomial_degree_2_recovers_known_coefficients() -> None:
    x = np.linspace(-5, 5, 50)
    y = 1.0 - 2.0 * x + 0.5 * x**2  # noiseless
    model = PolynomialRegressionModel(degree=2)
    fitted = model.fit(x, y)
    assert fitted.coefficients == pytest.approx([1.0, -2.0, 0.5], abs=1e-8)


def test_polynomial_degree_3_recovers_known_coefficients() -> None:
    x = np.linspace(-3, 3, 50)
    y = 0.5 + 1.0 * x - 0.3 * x**2 + 0.1 * x**3  # noiseless
    model = PolynomialRegressionModel(degree=3)
    fitted = model.fit(x, y)
    assert fitted.coefficients == pytest.approx([0.5, 1.0, -0.3, 0.1], abs=1e-6)


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
