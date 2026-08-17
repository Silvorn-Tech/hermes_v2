# Model Selection Engine (`feature/model-selection-v1`)

**v1** shipped `PolynomialRegressionModel` (linear regression, degree-2,
degree-3) and the AIC/RSS/temporal-split machinery. **v2** (this
revision) followed an independent statistical/architectural audit of
v1 (verdict: correct as a research baseline, several changes needed
before real financial models — see the audit summary at the end of this
document) and added: numerical stability for large-magnitude `x`,
`AutoregressiveModel` (AR(1)/AR(2)), AICc, a general log-likelihood-based
AIC abstraction, and the mechanical robustness fixes the audit found.
No GARCH, ARIMA's MA component, Monte Carlo, regime-switching, or
Strategy Engine connection — still exactly the same research-only scope
as v1.

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
| `metrics.py` | `rss()`, `aic()`, `aic_corrected()`, `aic_from_log_likelihood()`, `gaussian_ols_log_likelihood()`, `rmse()`, `mae()` |
| `split.py` | `temporal_train_validation_split()` |
| `models.py` | `PolynomialRegressionModel`, `AutoregressiveModel`, `default_candidates()`, `default_autoregressive_candidates()` |
| `selector.py` | `ModelSelector`, `CandidateModel` (the minimal protocol a candidate implements) — orchestrates the pipeline above |

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

## Why (log-)returns, not price, as the target

Financial prices are close to a unit-root/random-walk process —
non-stationary. Fitting any of this engine's regression candidates to
raw price as `y` is the textbook spurious-regression setup: the model
fits the *shape* of historical price with no principled reason to
extrapolate, since prices don't follow a deterministic polynomial or
autoregressive trend. **Every candidate in this engine — polynomial
regression and AR(1)/AR(2) alike — is intended to be fed (log-)returns
as `y`, never raw price.** Log-returns are preferred over simple returns
for their nicer additive/compounding properties and closer-to-symmetric
distribution. This module doesn't enforce the transform (it has no
opinion on what `y` represents, by design — "Feature Preparation" is the
caller's job), so this is a usage discipline documented here explicitly
rather than a runtime check.

Crypto in particular should be treated with extra caution even after
this transform: crypto returns are typically fatter-tailed and more
volatility-clustered than equities, meaning the Gaussian-error
assumption behind every AIC computation in this engine is a rougher
approximation for crypto than for equities. Nothing here corrects for
that yet — it's a known, stated limitation (see Limitations).

## Why GARCH isn't included yet

GARCH models the **conditional variance** of returns, not returns (or
price) itself, and is fit by its own maximum-likelihood procedure — its
likelihood does **not** reduce to this engine's RSS-based shortcut.
Before GARCH can be added as a candidate here:

- its AIC must go through `aic_from_log_likelihood()` (the general
  form), with `k` counting *every* GARCH parameter (ARCH terms + GARCH
  terms + constant, plus any mean-equation and error-distribution
  shape parameters) — not `aic()`'s coefficients-only shortcut;
- its out-of-sample evaluation needs different metrics than `rmse()`/
  `mae()` (which score point-forecast accuracy) — variance-forecast
  models are typically evaluated with something like QLIKE loss against
  a realized-volatility proxy, or VaR backtesting;
- comparing a GARCH candidate's AIC against a regression candidate's
  `aic()` output is only valid once both sides count nuisance
  parameters on the same, complete convention (see "The general form"
  above) — this engine's `k`-only shortcut and GARCH's full likelihood
  would otherwise be silently miscalibrated relative to each other.

None of this is implemented. `aic_from_log_likelihood()` exists now
specifically so this integration point is ready when GARCH work starts,
without requiring `aic()`'s existing behavior (or any current
candidate's numbers) to change.

## Why Monte Carlo is not a candidate model

Monte Carlo has no "residuals against a target" in the sense
`fit → RSS → AIC` requires — it isn't a thing to select by AIC. It's a
**simulator**, naturally positioned as a downstream consumer of a
`ModelSelectionResult` (e.g. "here's the fitted volatility model, now
generate 10,000 future paths from it") or a risk/valuation tool closer
to a future `RiskEngine`-adjacent module than to this package. It will
never appear in `default_candidates()` or
`default_autoregressive_candidates()`.

## Why walk-forward validation is the next phase, not this one

A single fixed temporal split (this engine's current and only
validation strategy) gives one read of out-of-sample performance on
what's likely a non-stationary series — a single split can land in an
unusually easy or hard regime purely by chance. Walk-forward (or
rolling-window) validation — repeatedly fit on an expanding/rolling
window, validate on the next chunk, step forward, aggregate — is the
standard fix for time-series model validation specifically, and is
recommended as Hermes's next validation-methodology phase. Not
implemented here because it needs its own aggregation policy (majority-
vote model across folds? average AIC? average validation error?) — a
genuine future decision, not something to default silently while adding
unrelated v2 changes.

## Candidate models

### v1: `PolynomialRegressionModel(degree)`

One class covers all three — linear regression *is* degree 1:

1. `linear_regression` — `degree=1`
2. `polynomial_regression_2` — `degree=2`
3. `polynomial_regression_3` — `degree=3`

Fitting builds an explicit design matrix `[1, x', x'², ..., x'^degree]`
(`x'` is `x` centered/scaled, see "Numerical stability" below) and
solves it via `numpy.linalg.lstsq` — the design matrix is visible code,
not hidden inside a library's `.fit()`. `numpy` was the only new
dependency added for v1 (see "Dependency" below); the RSS/AIC/k
computation itself stays in hand-written Python, independent of numpy's
solver.

A fit is rejected (the candidate is marked invalid, not silently
accepted) if the design matrix is rank-deficient — too few distinct `x`
values for the requested degree, or `n <= parameter_count`.

### v2: `AutoregressiveModel(order)` — AR(1) and AR(2)

`y[t] = c0 + c1*y[t-1] + ... + c_order*y[t-order] + error` — fit on a
series' own lagged values, reusing the same `numpy.linalg.lstsq`
machinery as `PolynomialRegressionModel` (a design matrix of
`[1, lag_1, ..., lag_order]`, same rank-deficiency guard). Not a full
ARIMA — no differencing, no moving-average term (see "Why not ARIMA's MA
component" below).

**Calling convention: pass the same series as both `x` and `y`** —
`ModelSelector(candidates=default_autoregressive_candidates()).select(returns, returns)`.
An autoregressive model is self-referential (no independent feature),
but `ModelSelector`'s orchestration always calls `predict(fitted, x)`
uniformly across every candidate type; rather than special-casing that
orchestration per candidate, `AutoregressiveModel` requires `x == y` and
raises `ModelFitError` immediately if they differ, so a caller mistake
is caught loudly instead of silently fitting nonsense.

**Predictions are one-step-ahead, using true (not model-generated)
lagged values, and only within the single contiguous series `predict()`
is given.** The first `order` points of that series have no valid lag
history within it and simply aren't predicted — both `fit()` and
`predict()` return `len(series) - order` rows, never `len(series)`.
`ModelSelector` aligns the target array to match (see "How `n` is
determined per candidate" below) rather than assuming every candidate
predicts 1:1 with its input. This deliberately does **not** carry lag
context across the train/validation boundary (e.g. seeding validation's
first prediction from train's last observations) — that's real
additional complexity (recursive/multi-step forecasting) intentionally
out of scope for this small v2 addition; see Limitations.

`default_autoregressive_candidates()` is a **separate** function from
`default_candidates()`, not merged into it — mixing AR models into the
plain `default_candidates()` list would silently misfire the moment
someone calls `ModelSelector().select(x, y)` with `x != y` (the normal
case for polynomial regression), since every AR candidate would
immediately fail its `x == y` check.

**Intended for (log-)return series, not raw price** — see "Why returns,
not price" below; a non-stationary price series is exactly the kind of
input that makes AR coefficients spurious and unstable.

## Numerical stability: centering and scaling

`PolynomialRegressionModel` centers `x` on its TRAIN mean and scales it
by its TRAIN standard deviation before building the design matrix:
`x' = (x - x_center) / x_scale`. Both are computed from TRAIN data only
and stored on `FittedModel`, then reused verbatim at `predict()` time —
never re-derived from validation data (which would leak validation
statistics into the transform).

**Why:** the raw design matrix `[1, x, x², x³]` is badly conditioned for
large-magnitude `x` — a raw Unix timestamp or price level around `1e6`
makes `x³ ~ 1e18`, and `numpy.linalg.lstsq`'s SVD-based solve loses
meaningful precision at that scale (verified in
`tests/test_model_selection_models.py`'s large-magnitude-`x` test).
Centering/scaling doesn't change *what* is being fit — predictions in
`y`-space are unaffected (up to floating-point precision, also directly
tested); only the internal coefficients' representation moves from raw
`x` units to transformed-`x` units, which is why
`fitted.coefficients` is no longer directly comparable to a naive
by-hand OLS fit's coefficients — read predictions via `predict()`, not
`fitted.coefficients` directly, if raw-`x`-space values are needed.

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
special-casing. Both causes of `RSS = 0` (degenerate `n ≤ k`
interpolation, and a genuinely noiseless perfect fit with `n > k`) are
rejected identically — this isn't extra caution, it's because the
Gaussian likelihood has a genuine singularity at zero variance
regardless of which caused it.

## The general form: `aic_from_log_likelihood()`

**`aic(n, rss, k)` is a documented special case, not the only AIC this
engine can compute.** `metrics.py` also exposes:

```python
gaussian_ols_log_likelihood(n, rss)  # -n/2*ln(2*pi) - n/2*ln(rss/n) - n/2
aic_from_log_likelihood(log_likelihood, k)  # 2k - 2*log_likelihood
```

Substituting the Gaussian-OLS profile log-likelihood into the general
form and expanding, for `k_full = k + 1` (the regression coefficients
**plus** the noise variance — every parameter, the complete convention):

```
aic_from_log_likelihood(ll, k+1) = n*ln(RSS/n) + 2k + [n*ln(2*pi) + n + 2]
```

The bracketed term is a constant for any fixed `n`, identical across
every candidate compared here (all fit on the same `n`) — dropping it,
and using `k` alone rather than `k+1` (the dropped constant already
absorbs the `+1` parameter's own `+2` contribution), gives exactly
`aic()`'s formula. `tests/test_model_selection_metrics.py`'s
`test_aic_is_the_documented_special_case_of_the_general_form` proves this
relationship holds exactly (not approximately) for arbitrary `(n, rss,
k)`.

**Why this matters, not just as a curiosity:** the simplification is
only valid when every compared candidate is Gaussian-OLS-with-freely-
estimated-variance, fit on the same `n` — true for every candidate in
this engine today (regression and AR alike both use this path). It
stops being true the moment a fundamentally different likelihood family
enters the same comparison — see "AIC vs GARCH" below. `k` for
`aic_from_log_likelihood` must then be the *complete* parameter count
for whatever produced the log-likelihood, not the coefficients-only
convention `aic()` uses.

## AICc — the finite-sample-corrected AIC

```
AICc = AIC + 2k(k+1) / (n - k - 1)
```

Plain AIC is a large-sample (asymptotic) approximation, well-established
(Hurvich & Tsai, 1989) to under-penalize complexity when `n` is small
relative to `k`. AICc corrects for this and converges to plain AIC as
`n` grows large relative to `k`.

**When to prefer AICc over AIC in Hermes:** there's no single agreed
exact cutoff in the statistics literature, so this engine doesn't invent
one — but the correction term itself is the honest signal: the larger
`2k(k+1)/(n-k-1)` is relative to the AIC value, the more plain AIC
should be distrusted. As a rough, non-binding guide, some texts suggest
treating AIC cautiously once `n/k` is small (well under ~40); Hermes's
candidates (`k` up to 4 for `polynomial_regression_3`, up to 3 for
AR(2)) make this a real concern for short lookback windows, not a
hypothetical one.

**AIC stays the sole selection criterion** — `ModelSelector` never ranks
or selects by `aicc`, only by `aic` (per the audit's explicit "no
reemplaces AIC" instruction). Every valid `ModelCandidateResult` carries
both: `aic` (always present when valid) and `aicc` (present only when
`n > k + 1`, `None` otherwise — a stricter requirement than `aic()`'s own
`n > k`, so a candidate can be `valid=True` with a real `aic` while
`aicc` is unavailable). A human (or a future policy) can use `aicc` as
an additional, informational cross-check.

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

## How `n` is determined per candidate

Not every candidate predicts one row per input row —
`AutoregressiveModel` predicts `len(series) - order` rows, never
`len(series)` (it has no valid lag history for the first `order`
points). `selector.py` never assumes a 1:1 match: it aligns the target
array to the *last* `len(predictions)` rows before computing RSS, and
uses `len(predictions)` — the number of observations actually used —
as `n` for both `aic()` and `aic_corrected()`. For
`PolynomialRegressionModel`, `len(predictions) == len(x)` always, so
this is a no-op; it only changes behavior for candidates like
`AutoregressiveModel` that legitimately produce fewer predictions than
they were given input rows.

## Selection rule

1. Fit every candidate on TRAIN. A candidate that fails to fit
   (rank-deficient design matrix, too few observations for its lag
   order, or an unexpected numerical error) or whose RSS/AIC/predictions
   contain `NaN`/`Infinity`, or produces zero predictions, anywhere
   (train or validation) is marked **invalid** with a human-readable
   reason and excluded from selection — it never crashes the whole run.
2. Among the **valid** candidates, the one with the lowest AIC (TRAIN)
   is selected.
3. **No RMSE/MAE threshold is invented to reject an otherwise-valid,
   merely-mediocre-on-validation candidate.** There is no approved
   statistical policy today for "how bad is too bad" on out-of-sample
   error — see Limitations. Every valid candidate is returned, sorted
   by AIC ascending, each carrying its own RMSE/MAE (and `aicc` when
   computable), so a human (or a future explicit policy) can judge.
4. If every candidate is invalid, `selected_model` is `None` and
   `candidates` still lists every attempt with its reason — this is a
   normal, structured outcome (not an exception), so a caller always
   gets full diagnostic detail rather than a bare error.
5. **Tie-break rule, stated explicitly:** on an exact AIC tie, whichever
   candidate appears earlier in the `candidates` list passed to
   `ModelSelector` wins (Python's stable sort preserves insertion order
   among equal keys). `default_candidates()`/`default_autoregressive_candidates()`
   both list simplest-first, so this favors parsimony on a tie by
   construction — but it is a consequence of list order, not a rule
   `ModelSelector` applies independently. A caller passing a custom
   `candidates` list controls tie-breaking via that list's order.
6. `ModelSelector(candidates=[])` (an explicitly empty list, distinct
   from the default `None`) raises `ValueError` at construction —
   almost certainly a caller mistake, not an intentional "nothing to
   compare" request.

## Result shape

```python
ModelCandidateResult(
    model_name: str, parameter_count: int, valid: bool,
    invalid_reason: str | None,
    aic: float | None,   # TRAIN
    rss: float | None,   # TRAIN
    rmse: float | None,  # VALIDATION
    mae: float | None,   # VALIDATION
    aicc: float | None,  # TRAIN; None if n <= k+1 even when aic is valid; never used for selection
)

ModelSelectionResult(
    selected_model: ModelCandidateResult | None,  # the full object — .aic/.rss/.rmse/.mae/.aicc/.parameter_count all live here directly
    candidates: list[ModelCandidateResult],          # every attempted candidate, sorted by AIC ascending (invalid last)
    train_size: int,
    validation_size: int,
)
```

### Example

```python
from hermes_v2.model_selection import ModelSelector

result = ModelSelector().select(x, y)
result.selected_model.model_name  # "polynomial_regression_2"
result.selected_model.aic  # 220.42...
result.selected_model.aicc  # 220.55...  (or None if n <= k+1)
result.selected_model.rss  # 1167.10...  (train)
result.selected_model.rmse  # 3.39...     (validation)
result.selected_model.mae  # 2.91...     (validation)
result.selected_model.parameter_count  # 3
```

### AR(1)/AR(2) example

```python
from hermes_v2.model_selection import ModelSelector, default_autoregressive_candidates

result = ModelSelector(candidates=default_autoregressive_candidates()).select(
    returns, returns
)
result.selected_model.model_name  # "ar_1" or "ar_2"
```

## Edge cases

| Case | Handling |
|---|---|
| Empty dataset | `ValueError` at `select()` entry |
| `x`/`y` length mismatch | `ValueError` — checked independently in `ModelSelector._validate_input`, `temporal_train_validation_split()`, and `rss()`/`rmse()`/`mae()`, so a direct call to any of these (not just via `ModelSelector`) still fails closed |
| `x`/`y` contain `NaN`/`Infinity` | `ValueError` at entry — never silently dropped/imputed |
| Invalid `train_ratio` / resulting empty partition | `ValueError` (`split.py`) |
| `ModelSelector(candidates=[])` (empty, not `None`) | `ValueError` at construction |
| A candidate's design matrix is rank-deficient | that candidate marked invalid; others proceed |
| A candidate's fit raises an unanticipated numerical error (`numpy.linalg.LinAlgError`) | that candidate marked invalid; others proceed |
| A candidate produces zero predictions (e.g. a series too short for its AR order) | that candidate marked invalid |
| Dataset too small for a candidate (`n <= k` after the split) | that candidate marked invalid via the AIC guard; others proceed |
| `RSS = 0` | that candidate marked invalid (AIC undefined there) |
| `RSS` extremely small but nonzero | valid — AIC is very negative but finite |
| Large-magnitude `x` (e.g. raw timestamps, `~1e6`+) for `PolynomialRegressionModel` | handled via centering/scaling — stays numerically stable, tested directly |
| `AutoregressiveModel` fit with `x != y` | `ModelFitError` immediately — never silently fits against the wrong series |
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
- **Single fixed 80/20 temporal split, not walk-forward.** See "Why
  walk-forward validation is the next phase, not this one" above — a
  single split gives one noisy read of out-of-sample performance for a
  likely non-stationary series.
- **`AutoregressiveModel` doesn't carry lag context across the train/
  validation boundary.** Its first `order` validation predictions
  simply don't exist (see the AR section above) rather than being seeded
  from train's tail — real multi-step/boundary-seeded forecasting is
  out of scope for this small v2 addition.
- **No AR-specific centering/scaling.** Only `PolynomialRegressionModel`
  centers/scales its input; returns are typically already small-
  magnitude and well-scaled, so this wasn't extended to
  `AutoregressiveModel` for v2 — worth revisiting if that assumption
  stops holding for some future feature.
- **The Gaussian-error assumption is a rougher approximation for crypto
  than equities**, given crypto's fatter tails and more pronounced
  volatility clustering — nothing here corrects for this yet (see "Why
  (log-)returns, not price" above).

## Independent audit (v1 → v2)

Before this v2 revision, an independent statistical/architectural audit
reviewed v1's AIC/RSS/train-validation/selection implementation line by
line. Verdict: **B — correct as a v1 research baseline, specific changes
needed before real financial models.** No CRITICAL findings (nothing
computed an incorrect number for what it claimed to do; isolation from
execution was already proven, not just asserted). The audit's HIGH/
MEDIUM findings are what this v2 revision addresses — the AICc addition,
the general log-likelihood AIC abstraction, centering/scaling, and the
mechanical robustness fixes (length validation, empty-candidates
rejection, broader exception handling, the explicit tie-break/RSS=0
documentation) all trace directly back to specific audit findings, not
independently invented afterward. GARCH, ARIMA's MA component, Monte
Carlo, regime-switching, walk-forward validation, and any RMSE/MAE
rejection threshold were all identified by the audit as *not* v2 work,
and remain unimplemented here for exactly that reason.
