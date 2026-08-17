"""Temporal train/validation split — never a random shuffle.

`x`/`y` must already be in chronological order (oldest first); this
module trusts that order and slices a single contiguous boundary,
exactly the way a time series must be split to avoid letting the model
"see the future" during fitting.
"""

from __future__ import annotations

import math

import numpy as np

DEFAULT_TRAIN_RATIO = 0.8


def temporal_train_validation_split(
    x: np.ndarray, y: np.ndarray, train_ratio: float = DEFAULT_TRAIN_RATIO
) -> tuple[tuple[np.ndarray, np.ndarray], tuple[np.ndarray, np.ndarray]]:
    """Split `(x, y)` at `floor(n * train_ratio)` — everything before the
    boundary is TRAIN, everything from the boundary onward is
    VALIDATION. Returns `((x_train, y_train), (x_validation, y_validation))`.

    Raises `ValueError` if `train_ratio` isn't strictly between 0 and 1,
    if `x`/`y` have mismatched lengths, or if the resulting split would
    leave either partition empty — never silently proceeds with a 0-row
    train or validation set. This function validates its own
    preconditions rather than trusting a caller (e.g. `ModelSelector`)
    to have already checked them, since it's a public, independently
    callable function in its own right.
    """
    if not (0 < train_ratio < 1):
        raise ValueError(f"train_ratio must be in (0, 1), got {train_ratio!r}.")
    if len(x) != len(y):
        raise ValueError(
            f"x and y must have the same length (got {len(x)} and {len(y)})."
        )

    n = len(x)
    split_index = math.floor(n * train_ratio)

    if split_index < 1 or split_index >= n:
        raise ValueError(
            f"train_ratio={train_ratio!r} leaves an empty train or validation "
            f"partition for n={n} observations."
        )

    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    return (x[:split_index], y[:split_index]), (x[split_index:], y[split_index:])


__all__ = ["DEFAULT_TRAIN_RATIO", "temporal_train_validation_split"]
