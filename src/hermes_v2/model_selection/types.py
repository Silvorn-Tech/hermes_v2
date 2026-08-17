"""Result types for the Model Selection Engine.

Deliberately plain, frozen dataclasses — not a dict/JSON blob — so a
caller gets real attribute access (`result.selected_model.aic`) and a
type checker catches a typo'd field name. `ModelSelectionResult` is the
engine's only output; nothing here references Binance, orders, or
positions, because nothing in this module needs to.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelCandidateResult:
    """One candidate model's fit and validation outcome.

    `aic`/`rss` are computed on the TRAIN partition only; `rmse`/`mae`
    on the VALIDATION partition only — never mixed (see
    `docs/architecture/model-selection.md`). All four are `None` when
    `valid` is `False`: an invalid candidate has no trustworthy metrics
    to report, not a fabricated zero or a partial result.
    """

    model_name: str
    parameter_count: int
    valid: bool
    invalid_reason: str | None
    aic: float | None
    rss: float | None
    rmse: float | None
    mae: float | None


@dataclass(frozen=True)
class ModelSelectionResult:
    """`ModelSelector.select()`'s output. `selected_model` is `None` when
    every candidate was invalid — never a fallback guess. `candidates`
    always lists every candidate that was attempted, sorted by AIC
    ascending (invalid candidates last, since they have no AIC to sort
    by), so a caller can see *why* a candidate was rejected even when
    none were selected.
    """

    selected_model: ModelCandidateResult | None
    candidates: list[ModelCandidateResult]
    train_size: int
    validation_size: int


__all__ = ["ModelCandidateResult", "ModelSelectionResult"]
