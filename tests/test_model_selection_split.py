"""Tests for the temporal train/validation split — sizes, ordering (no
shuffle), and invalid-ratio/empty-partition rejection."""

from __future__ import annotations

import numpy as np
import pytest

from hermes_v2.model_selection.split import temporal_train_validation_split


def test_split_sizes_match_the_ratio() -> None:
    x = np.arange(100, dtype=float)
    y = np.arange(100, dtype=float)
    (x_train, y_train), (x_val, y_val) = temporal_train_validation_split(
        x, y, train_ratio=0.8
    )
    assert len(x_train) == len(y_train) == 80
    assert len(x_val) == len(y_val) == 20


def test_split_is_a_contiguous_boundary_never_a_shuffle() -> None:
    x = np.arange(10, dtype=float)
    y = np.arange(10, dtype=float) * 10
    (x_train, y_train), (x_val, y_val) = temporal_train_validation_split(
        x, y, train_ratio=0.7
    )

    # Train is exactly the first 7 in original order; validation is
    # exactly the last 3, also in original order.
    assert list(x_train) == [0, 1, 2, 3, 4, 5, 6]
    assert list(x_val) == [7, 8, 9]
    assert list(y_train) == [0, 10, 20, 30, 40, 50, 60]
    assert list(y_val) == [70, 80, 90]


@pytest.mark.parametrize("train_ratio", [0.0, 1.0, -0.1, 1.1])
def test_split_rejects_a_ratio_outside_the_open_interval(train_ratio: float) -> None:
    x = np.arange(10, dtype=float)
    y = np.arange(10, dtype=float)
    with pytest.raises(ValueError, match="train_ratio"):
        temporal_train_validation_split(x, y, train_ratio=train_ratio)


def test_a_ratio_close_to_1_still_leaves_at_least_one_validation_row() -> None:
    # train_ratio is constrained to the OPEN interval (0, 1), so
    # floor(n * ratio) can never reach n -- validation can never be
    # emptied out by a high ratio alone, only train by a low one (see
    # the test below). This documents/locks in that invariant.
    x = np.arange(5, dtype=float)
    y = np.arange(5, dtype=float)
    (x_train, _), (x_val, _) = temporal_train_validation_split(x, y, train_ratio=0.999)
    assert len(x_val) >= 1


def test_split_rejects_a_ratio_that_leaves_train_empty() -> None:
    x = np.arange(5, dtype=float)
    y = np.arange(5, dtype=float)
    with pytest.raises(ValueError, match="empty"):
        temporal_train_validation_split(x, y, train_ratio=0.001)


def test_split_default_ratio_is_80_20() -> None:
    x = np.arange(10, dtype=float)
    y = np.arange(10, dtype=float)
    (x_train, _), (x_val, _) = temporal_train_validation_split(x, y)
    assert len(x_train) == 8
    assert len(x_val) == 2
