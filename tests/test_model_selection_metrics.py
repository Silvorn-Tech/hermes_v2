"""Tests for the RSS/AIC/RMSE/MAE formulas — hand-computed expectations,
plus the RSS=0/near-zero AIC guard and the k-counting convention."""

from __future__ import annotations

import math

import numpy as np
import pytest

from hermes_v2.model_selection.metrics import aic, mae, rmse, rss


def test_rss_hand_computed() -> None:
    y_true = np.array([1.0, 2.0, 3.0])
    y_pred = np.array([1.0, 2.0, 5.0])
    # residuals: 0, 0, -2 -> squares: 0, 0, 4 -> sum 4
    assert rss(y_true, y_pred) == 4.0


def test_rss_zero_for_a_perfect_fit() -> None:
    y = np.array([1.0, 2.0, 3.0])
    assert rss(y, y) == 0.0


def test_aic_hand_computed() -> None:
    n, residual_sum_of_squares, k = 10, 20.0, 3
    expected = n * math.log(residual_sum_of_squares / n) + 2 * k
    assert aic(n, residual_sum_of_squares, k) == pytest.approx(expected)


def test_aic_rejects_rss_zero() -> None:
    # ln(0/n) is undefined -- must fail closed, never return -inf.
    with pytest.raises(ValueError, match="positive RSS"):
        aic(10, 0.0, 3)


def test_aic_rejects_negative_rss() -> None:
    with pytest.raises(ValueError, match="positive RSS"):
        aic(10, -1.0, 3)


def test_aic_rejects_n_less_than_or_equal_to_k() -> None:
    with pytest.raises(ValueError, match="more observations than parameters"):
        aic(3, 5.0, 3)
    with pytest.raises(ValueError, match="more observations than parameters"):
        aic(2, 5.0, 3)


def test_aic_accepts_extremely_small_but_nonzero_rss() -> None:
    # Mathematically valid (very negative but finite AIC) -- no special
    # casing needed beyond correct float arithmetic.
    value = aic(50, 1e-12, 2)
    assert math.isfinite(value)
    assert value < 0


def test_aic_penalizes_more_parameters_for_equal_rss() -> None:
    n, residual_sum_of_squares = 50, 10.0
    simpler = aic(n, residual_sum_of_squares, k=2)
    more_complex = aic(n, residual_sum_of_squares, k=4)
    assert more_complex > simpler


def test_rmse_hand_computed() -> None:
    y_true = np.array([0.0, 0.0, 0.0, 0.0])
    y_pred = np.array([1.0, -1.0, 1.0, -1.0])
    # squared residuals all 1 -> mean 1 -> sqrt 1
    assert rmse(y_true, y_pred) == 1.0


def test_mae_hand_computed() -> None:
    y_true = np.array([0.0, 0.0, 0.0, 0.0])
    y_pred = np.array([2.0, -2.0, 2.0, -2.0])
    assert mae(y_true, y_pred) == 2.0


def test_rmse_and_mae_zero_for_a_perfect_fit() -> None:
    y = np.array([1.0, 5.0, -3.0])
    assert rmse(y, y) == 0.0
    assert mae(y, y) == 0.0
