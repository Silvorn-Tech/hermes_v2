# Model Selection Engine v1 (`feature/model-selection-v1`)

## Purpose

Before any future Strategy Engine acts on a mathematical model, Hermes
needs a principled way to pick which model to use — comparing candidate
regressions by AIC (Akaike Information Criterion), which penalizes
complexity, rather than by raw fit error (RSS) alone. This is that
comparison engine, **and nothing more**: a pure research library with no
market-data integration, no bot wiring, and no involvement in order
execution.

```text
Market Data
   |  (caller's responsibility — this module never talks to Binance)
   v
Feature Preparation
   |  (caller's responsibility — produces x, y below)
   v
ModelSelector.select(x, y)
   |
   |-- Candidate Models        (models.py: PolynomialRegressionModel, degree 1/2/3)
   |-- Temporal train/validation split   (split.py)
   |-- Model Fitting (TRAIN only)         (models.py, via numpy.linalg.lstsq)
   |-- Residuals -> RSS (TRAIN)            (metrics.py)
   |-- AIC (TRAIN)                          (metrics.py)
   |-- Validation: RMSE/MAE (VALIDATION)     (metrics.py)
   v
ModelSelectionResult                          (types.py)
```

A later phase will connect `ModelSelectionResult` to a Strategy Engine →
`RiskEngine` → `OrderService` pipeline. **That pipeline does not exist
yet and this module does not build it.** See "Isolation from execution"
below for how that's enforced, not just stated.

## Module layout

`src/hermes_v2/model_selection/` — a new top-level package, sibling to
`trading/`/`auth/`/`integrations/`, not nested inside `trading/`:

| File | Contents |
|---|---|
| `types.py` | `ModelCandidateResult`, `ModelSelectionResult` — frozen dataclasses |
| `metrics.py` | `rss()`, `aic()`, `rmse()`, `mae()` — plain, hand-written formulas |
| `split.py` | `temporal_train_validation_split()` |
| `models.py` | `PolynomialRegressionModel`, `default_candidates()` |
| `selector.py` | `ModelSelector` — orchestrates the pipeline above |

## Data contract (v1 scope)

`ModelSelector.select(x, y)` takes two equal-length, chronologically-
ordered sequences: `x` is a single already-prepared feature (e.g. a time
index or one derived market signal), `y` is the target. Turning raw
market data into this pair ("Feature Preparation") is the caller's job —
this module has no Binance integration at all.

**Multivariate regression (multiple simultaneous features) is out of
scope for v1.** All three v1 candidates are naturally single-variable
models; multivariate support is a natural, additive future extension
(a new candidate model class), not a redesign of this contract.

## Candidate models (v1)

One class, `PolynomialRegressionModel(degree)`, covers all three —
linear regression *is* degree 1:

1. `linear_regression` — `degree=1`
2. `polynomial_regression_2` — `degree=2`
3. `polynomial_regression_3` — `degree=3`

Fitting builds an explicit design matrix `[1, x, x², ..., x^degree]` and
solves it via `numpy.linalg.lstsq` — the design matrix is visible code,
not hidden inside a library's `.fit()`. `numpy` was the only new
dependency added for this phase (see "Dependency" below); the RSS/AIC/k
computation itself stays in hand-written Python, independent of numpy's
solver.

A fit is rejected (the candidate is marked invalid, not silently
accepted) if the design matrix is rank-deficient — too few distinct `x`
values for the requested degree, or `n <= parameter_count`.

## Dependency: `numpy`

The only new dependency (`pyproject.toml`). Not `scikit-learn` or
`statsmodels` — those are full ML/stats frameworks that would compute
RSS/AIC/parameter-counting inside an opaque `.fit()` call, which this
engine deliberately avoids so `k`'s definition (below) stays inspectable
and testable. `numpy` is the numerical-array primitive layer beneath
those frameworks — used *only* for `numpy.linalg.lstsq`'s
rank-aware OLS solve, never to hide a metric computation.

## RSS

```
RSS = sum((y_true - y_pred) ** 2)
```

## AIC

```
AIC = n * ln(RSS / n) + 2k
```

exactly as specified: `n` = number of observations (TRAIN only), `RSS` =
residual sum of squares (TRAIN only), `k` = number of parameters.

### How `k` is counted (stated explicitly, not hidden)

**`k` = the number of estimated regression coefficients, including the
intercept** — `degree + 1` for a degree-`d` polynomial:

| Model | degree | k |
|---|---|---|
| `linear_regression` | 1 | 2 |
| `polynomial_regression_2` | 2 | 3 |
| `polynomial_regression_3` | 3 | 4 |

This is the literal reading of "number of estimated parameters" applied
to the model's mean function, and it's computed directly as
`design_matrix.shape[1]` — not a separately-maintained constant that
could drift out of sync with what's actually fit.

**A note on convention:** some AIC treatments add `+1` to `k` to also
count the separately-estimated noise variance (`σ²`) in a Gaussian
model. This implementation does **not** — it matches the exact formula
given (`n·ln(RSS/n) + 2k`), which is the standard "concentrated/profile
likelihood" form where `σ²` has already been profiled out of the
likelihood and `k` refers only to the mean-model's parameters. If a
future phase needs the `+1` convention, it changes in exactly one place
(`models.py`'s `parameter_count` property) and every downstream number
updates consistently.

### Degenerate-RSS guard

`ln(RSS/n)` is undefined at `RSS = 0`. A candidate reaching `RSS = 0`
(or `n <= k`, which is what typically *causes* a degenerate perfect fit
— a model with as many parameters as data points trivially interpolates
them) is marked **invalid**, never allowed to produce `AIC = -inf` and
trivially "win" the comparison. A very small but nonzero RSS is
mathematically fine (a very negative but finite AIC) and needs no
special-casing.

## Train / validation split — always temporal, never random

```python
temporal_train_validation_split(x, y, train_ratio=0.8)
```

A single contiguous boundary at `floor(n * train_ratio)` — everything
before it is TRAIN, everything from it onward is VALIDATION. **Never** a
random shuffle: shuffling a time series before splitting would let the
model "see the future" during fitting, which this engine treats as a
correctness bug, not a stylistic preference.

`train_ratio` defaults to `0.8` (80/20) and is a **plain constructor
parameter** on `ModelSelector`, not an environment variable — this
engine has no live, unattended runtime to configure via env (unlike
`RiskLimits`, which every request re-reads). It's a per-call research
parameter.

Rejected with `ValueError` (never silently proceeds): `train_ratio`
outside the open interval `(0, 1)`, or a split that would leave either
partition empty.

## Validation metrics: RMSE / MAE

Computed **only** on the VALIDATION partition, using each candidate's
TRAIN-fitted coefficients — never refit on validation data (that would
leak validation into the model itself). AIC never touches validation
data; RMSE/MAE never touch train data. Enforced structurally in
`selector.py` (train-only values never leave the fitting step;
validation-only values never enter the AIC step) and covered by a
dedicated no-leakage test (`tests/test_model_selection_selector.py`):
perturbing only the validation target changes RMSE/MAE but never
changes the selected model or its AIC.

## Selection rule

1. Fit every candidate on TRAIN. A candidate that fails to fit
   (rank-deficient design matrix) or whose RSS/AIC/predictions contain
   `NaN`/`Infinity` anywhere (train or validation) is marked
   **invalid** with a human-readable reason and excluded from
   selection — it never crashes the whole run.
2. Among the **valid** candidates, the one with the lowest AIC (TRAIN)
   is selected.
3. **No RMSE/MAE threshold is invented to reject an otherwise-valid,
   merely-mediocre-on-validation candidate.** There is no approved
   statistical policy today for "how bad is too bad" on out-of-sample
   error — see Limitations. Every valid candidate is returned, sorted
   by AIC ascending, each carrying its own RMSE/MAE, so a human (or a
   future explicit policy) can judge.
4. If every candidate is invalid, `selected_model` is `None` and
   `candidates` still lists every attempt with its reason — this is a
   normal, structured outcome (not an exception), so a caller always
   gets full diagnostic detail rather than a bare error.

## Result shape

```python
ModelCandidateResult(
    model_name: str, parameter_count: int, valid: bool,
    invalid_reason: str | None,
    aic: float | None,   # TRAIN
    rss: float | None,   # TRAIN
    rmse: float | None,  # VALIDATION
    mae: float | None,   # VALIDATION
)

ModelSelectionResult(
    selected_model: ModelCandidateResult | None,  # the full object — .aic/.rss/.rmse/.mae/.parameter_count all live here directly
    candidates: list[ModelCandidateResult],          # every attempted candidate, sorted by AIC ascending (invalid last)
    train_size: int,
    validation_size: int,
)
```

### Example

```python
from hermes_v2.model_selection import ModelSelector

result = ModelSelector().select(x, y)
result.selected_model.model_name   # "polynomial_regression_2"
result.selected_model.aic          # 220.42...
result.selected_model.rss          # 1167.10...  (train)
result.selected_model.rmse         # 3.39...     (validation)
result.selected_model.mae          # 2.91...     (validation)
result.selected_model.parameter_count  # 3
```

## Edge cases

| Case | Handling |
|---|---|
| Empty dataset | `ValueError` at `select()` entry |
| `x`/`y` length mismatch | `ValueError` |
| `x`/`y` contain `NaN`/`Infinity` | `ValueError` at entry — never silently dropped/imputed |
| Invalid `train_ratio` / resulting empty partition | `ValueError` (`split.py`) |
| A candidate's design matrix is rank-deficient | that candidate marked invalid; others proceed |
| Dataset too small for a candidate (`n <= k` after the split) | that candidate marked invalid via the AIC guard; others proceed |
| `RSS = 0` | that candidate marked invalid (AIC undefined there) |
| `RSS` extremely small but nonzero | valid — AIC is very negative but finite |
| All candidates invalid | `selected_model=None`, full candidate list with reasons still returned — not an exception |

## Isolation from the execution path

Enforced by `tests/test_model_selection_isolation.py`, which statically
parses (via Python's `ast` module — not just a runtime import check, so
an unused-but-present import is still caught) every file in
`model_selection/` and asserts:

- none import `hermes_v2.integrations.binance`,
  `hermes_v2.trading.order_service`, or `hermes_v2.trading.risk_engine`;
- none reference the trading kill-switch environment variable anywhere.

This module cannot create or cancel an order, cannot read account
balances, and does not know whether trading is enabled — it is
structurally incapable of any of that, not merely unused for it today.

## Limitations

- **AIC alone does not guarantee out-of-sample predictive power.** It
  compares candidate models fit on the same dataset/target and
  penalizes complexity, but a low-AIC model can still generalize
  poorly; that's exactly why this engine also reports RMSE/MAE on a
  held-out validation partition alongside AIC, rather than selecting on
  AIC in isolation.
- **No RMSE/MAE-based rejection policy exists yet.** A model can be
  "selected" by AIC while having conspicuously bad validation error;
  this is surfaced (every candidate's RMSE/MAE is always in the result)
  rather than hidden, but nothing here decides "too bad to use" — that
  requires a statistical/business policy this phase deliberately does
  not invent.
- **Single-feature regression only.** No multivariate models in v1 (see
  "Data contract" above).
- **No real market-data integration.** The engine consumes an
  already-prepared `(x, y)` dataset; nothing here fetches historical
  prices from Binance or anywhere else.
- **Not connected to Bots.** `Bot.strategy_model`/`strategy_config`
  are not written to by this engine. Persisting a selection result
  against a Bot, and versioning selection results over time, is future
  work once that design is defined.
