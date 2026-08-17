"""Tests for the RSS/AIC/AICc/general-log-likelihood-AIC/RMSE/MAE
formulas — hand-computed expectations, the RSS=0/near-zero AIC guard,
the k-counting convention, and direct-API-misuse (mismatched lengths)."""

from __future__ import annotations

import math

import numpy as np
import pytest

from hermes_v2.model_selection.metrics import (
    aic,
    aic_corrected,
    aic_from_log_likelihood,
    gaussian_ols_log_likelihood,
    mae,
    rmse,
    rss,
)


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


# --- direct API misuse: mismatched lengths ---------------------------------------


def test_rss_rejects_mismatched_lengths() -> None:
    with pytest.raises(ValueError, match="same shape"):
        rss(np.array([1.0, 2.0, 3.0]), np.array([1.0, 2.0]))


def test_rmse_rejects_mismatched_lengths() -> None:
    with pytest.raises(ValueError, match="same shape"):
        rmse(np.array([1.0, 2.0, 3.0]), np.array([1.0, 2.0]))


def test_mae_rejects_mismatched_lengths() -> None:
    with pytest.raises(ValueError, match="same shape"):
        mae(np.array([1.0, 2.0, 3.0]), np.array([1.0, 2.0]))


# --- gaussian_ols_log_likelihood / aic_from_log_likelihood -----------------------


def test_gaussian_ols_log_likelihood_hand_computed() -> None:
    n, residual_sum_of_squares = 10, 20.0
    expected = (
        -n / 2 * math.log(2 * math.pi)
        - n / 2 * math.log(residual_sum_of_squares / n)
        - n / 2
    )
    assert gaussian_ols_log_likelihood(n, residual_sum_of_squares) == pytest.approx(
        expected
    )


def test_gaussian_ols_log_likelihood_rejects_rss_zero() -> None:
    with pytest.raises(ValueError, match="positive RSS"):
        gaussian_ols_log_likelihood(10, 0.0)


def test_aic_from_log_likelihood_hand_computed() -> None:
    log_likelihood, k = -42.5, 3
    assert aic_from_log_likelihood(log_likelihood, k) == pytest.approx(
        2 * k - 2 * log_likelihood
    )


def test_aic_from_log_likelihood_rejects_non_finite_input() -> None:
    with pytest.raises(ValueError, match="finite"):
        aic_from_log_likelihood(float("-inf"), 3)
    with pytest.raises(ValueError, match="finite"):
        aic_from_log_likelihood(float("nan"), 3)


def test_aic_is_the_documented_special_case_of_the_general_form() -> None:
    """Proves the algebraic relationship claimed in aic()'s docstring:
    aic_from_log_likelihood(ll, k+1) - aic(n, rss, k) is exactly the
    n-dependent constant the simplified formula drops (n*ln(2*pi) + n +
    2) -- not approximately, exactly, for every (n, rss, k)."""
    for n, residual_sum_of_squares, k in [(10, 20.0, 3), (200, 0.001, 4), (50, 1e6, 2)]:
        simplified = aic(n, residual_sum_of_squares, k)
        log_likelihood = gaussian_ols_log_likelihood(n, residual_sum_of_squares)
        general_full_k = aic_from_log_likelihood(log_likelihood, k + 1)
        expected_gap = n * math.log(2 * math.pi) + n + 2
        assert general_full_k - simplified == pytest.approx(expected_gap)


# --- AICc -------------------------------------------------------------------------


def test_aic_corrected_hand_computed() -> None:
    n, residual_sum_of_squares, k = 20, 15.0, 3
    expected = aic(n, residual_sum_of_squares, k) + (2 * k * (k + 1)) / (n - k - 1)
    assert aic_corrected(n, residual_sum_of_squares, k) == pytest.approx(expected)


def test_aic_corrected_exceeds_plain_aic() -> None:
    # The correction term 2k(k+1)/(n-k-1) is always > 0 for a valid
    # (n, k), so AICc must always be strictly greater than plain AIC.
    n, residual_sum_of_squares, k = 30, 12.0, 4
    assert aic_corrected(n, residual_sum_of_squares, k) > aic(
        n, residual_sum_of_squares, k
    )


def test_aic_corrected_converges_toward_aic_as_n_grows() -> None:
    residual_sum_of_squares, k = 12.0, 4
    gap_small_n = aic_corrected(20, residual_sum_of_squares, k) - aic(
        20, residual_sum_of_squares, k
    )
    gap_large_n = aic_corrected(2000, residual_sum_of_squares, k) - aic(
        2000, residual_sum_of_squares, k
    )
    assert gap_large_n < gap_small_n


def test_aic_corrected_rejects_n_less_than_or_equal_to_k_plus_1() -> None:
    # n=5, k=4 -> n-k-1=0, the correction term's denominator.
    with pytest.raises(ValueError, match="n > k \\+ 1"):
        aic_corrected(5, 10.0, 4)


def test_aic_corrected_rejects_the_same_degenerate_rss_as_aic() -> None:
    with pytest.raises(ValueError, match="positive RSS"):
        aic_corrected(20, 0.0, 3)
