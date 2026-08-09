# Changelog — Predictive System Rework (2026-07 / 2026-08)

---

## Next Phase: Bucket 4 — composite weight refitting infrastructure

This pass addresses the first item from the previous "still open" list:
hand-picked M/Q/T/S composite weights that had never been fit to tracked
outcomes.

Three concrete gaps were closed:

### 1. Fixed factor-score logging gap (`app/services/prediction_tracker_service.py`)

`auto_log_scan_predictions` was inserting predictions with no record of
the M/Q/T/S scores that produced them.  Without the scores there is nothing
to regress against, so no weight fitting is possible regardless of how many
predictions accumulate.

- Scored rows from the warmer are now passed as full dicts (not stripped to
  plain symbol strings — that was a pre-existing bug fixed here too).
- `full_payload` now includes `factor_scores: {momentum, quality, trend,
  stability, exit_risk}` and `algo_weights_at_log` for every auto_scan row.
- `market` and `max_new` parameters added (the warmer was already passing
  them but the old signature didn't accept them → silent kwarg errors).
- Plain symbol strings are still accepted for backward compatibility:
  they are resolved via the snapshot store before the score extraction step.

### 2. Added missing `evaluate_expired_predictions` (`prediction_tracker_service.py`)

The `/api/predictions/evaluate` route called `tracker.evaluate_expired_predictions`
but the function did not exist — an AttributeError at runtime for every caller.

The new implementation:
- Pulls all `status='open'` rows whose `expires_at` has passed.
- Looks up the current price from the snapshot/quote cache (no network calls).
- Marks `status='correct'` if price moved in the predicted direction (bull →
  price > anchor; bear → price < anchor), `status='incorrect'` otherwise.
- Rows for which no cached price is available are left `open` and retried on
  the next call.
- `neutral` predictions (no directional signal to grade) are marked `expired`.

This is intentionally minimal: the real signal is directional, not magnitude.

### 3. Built weight optimizer (`app/services/weight_optimizer_service.py`)

A new service that fits the blending weights

    final_score = w_m·momentum + w_q·quality + w_t·trend + w_s·stability

against the historical record of closed `auto_scan` predictions using
walk-forward cross-validated logistic regression.

**Math:**  We model `p(correct | x) = σ((x·w − T) / T)` where `x` is the
four factor scores, `w` is on the probability simplex (wᵢ ≥ 0, Σwᵢ = 1),
and `T = 50` brings the 0–100 score scale into a ±2 logit range.  Minimizing
binary cross-entropy with projected gradient descent and simplex projection
(O(n log n) algorithm) finds optimal weights without requiring any external
ML library — pure numpy-free Python.

**Walk-forward protocol:**  60 % burn-in, then slide one step at a time
training only on strictly earlier rows and predicting the next.  Final
accuracy is the fraction of held-out steps where the model called the
correct label.

**Conservative thresholds:**
- Returns `recommendation='insufficient_data'` when fewer than 50 closed
  predictions with factor scores are available.  (The warmer logs ≤10/day,
  so this needs ~5 days of deployment before the first meaningful fit.)
- Reports `recommendation='keep'` unless fitted weights beat the current
  hand-picked weights by ≥ 2 pp.
- Does NOT automatically write weights back; the comparison is logged and
  returned so the operator can review before touching `scoring_service.py`.

**Reality-breaker placeholder:**  `fit_reality_breaker_weights()` is defined
but returns `None` because the four sub-factor z-scores (LCC, QPII, LLVE,
TRS) are not yet stored in the prediction log.  Closing that logging gap is
a future task.

### 4. Wired optimizer to background warmer (`app/services/warmer_service.py`)

The optimizer runs in a daemon thread every 500 warmer cycles (approximately
daily at typical tick rates), storing the result in `warmer_status()` under
`last_weight_optimizer` for operator inspection.  The daemon thread approach
ensures the optimizer never blocks a warmer tick even if the DB is slow.

### Verified test cases — all 24 pass (no network access required)

| Test | What it checks |
|---|---|
| `test_extract_labelled_rows_correct/incorrect` | factor scores + binary label extraction |
| `test_extract_labelled_rows_skips_all_zeros` | unfilled rows discarded |
| `test_extract_labelled_rows_skips_open` | only closed rows used |
| `test_extract_labelled_rows_skips_bad_payload` | JSON errors handled silently |
| `test_project_simplex_*` (3 cases) | simplex projection correctness |
| `test_walk_forward_fit_separable` | WF accuracy > 65 % on momentum-only signal |
| `test_walk_forward_fit_output_is_simplex` | fitted weights sum to 1, all ≥ 0 |
| `test_walk_forward_eval_current_weights` | current weights score in [0, 1] |
| `test_walk_forward_eval_vs_fit_on_known_signal` | fitted not worse than current by > 5 pp |
| `test_fit_mqts_insufficient_data` | N < 50 → `insufficient_data` |
| `test_fit_mqts_sufficient_data_structure` | N ≥ 50 → correct keys, simplex, acc in [0,1] |
| `test_evaluate_expired_bull_correct/incorrect` | directional grading |
| `test_evaluate_expired_bear_correct` | bear direction |
| `test_evaluate_expired_no_price_leaves_open` | no price → row stays open |
| `test_auto_log_stores_factor_scores` | M/Q/T/S stored in full_payload |
| `test_auto_log_accepts_symbol_strings` | plain strings don't crash |
| `test_safe_rating_score_*` (3 cases) | score extraction helpers |

---

## What's still open after this pass

- **Accumulate real outcome data**: the optimizer needs ≥50 closed `auto_scan`
  predictions with factor scores.  This requires ~5 days of live scanner
  operation after deploying this pass.  Run `GET /api/predictions/evaluate`
  (or wait for the warmer's refit cycle) to score expired predictions, then
  inspect `warmer_status().last_weight_optimizer` for the recommendation.
- **Reality-breaker weight fitting**: the four sub-factor z-scores (LCC, QPII,
  LLVE, TRS) that feed `compute_reality_breaker_multiplier` are not yet stored
  in the prediction log.  Adding that logging gap is the next concrete task.
- **Coverage of new consensus score** on the real universe (how often a symbol
  gets a validated driver/GEX view vs. returning 0.0) — requires live data.
- **Real historical backtests** of the reworked factors — requires accumulated
  `auto_scan` outcome data.

---

## Previous Pass: `quantum_interference_certainty` rename and validation

This pass closes the third item from the previous "still open" list.

### Renamed `quantum_interference_certainty` → `model_agreement_certainty` (`app/services/lab_signals.py`)

The old name implied a connection to quantum mechanics that does not exist.
The math was already defensible (see below); only the framing was dishonest.

**What the formula actually computes:**

Each probability-of-up estimate is decomposed into a conviction magnitude
`M = √(2·|p_up − 0.5|)` and a direction sign `d = sign(p_up − 0.5)`.
The signed magnitudes are vector-summed and squared:

```
certainty = ((d_fast·M_fast + d_garch·M_garch) / √2)²,  clipped to [0, 1]
```

When both models agree directionally the magnitudes add and the output
exceeds either model's individual certainty.  When they disagree they
partially or fully cancel.  This is a standard signed-combination rule —
no quantum physics involved.

**Verified test cases** (all checked by `validate_model_agreement_certainty()`):

| Case | Inputs | Output |
|---|---|---|
| Both strongly bullish | (0.80, 0.75) | 1.000 (clipped from 1.097) |
| Both bearish (same magnitude) | (0.20, 0.25) | 1.000 |
| Equal and opposite conviction | (0.75, 0.25) | 0.000 (exact cancellation) |
| Partial disagreement | (0.70, 0.40) | ≈ 0.017 |
| Single-model fallback | (0.80, None) | 0.600 = 2·|0.8−0.5| |
| Both at 50 % (no signal) | (0.50, 0.50) | 0.000 |

- Field key `lab_qi_certainty` kept for backward compatibility.
- Old function name kept as a deprecated module-level alias so any
  external notebooks/scripts don't break.
- **UI label** changed from `"QI Certainty"` → `"Model Agreement"`
  (`frontend/app.js`).
- **Guidebook tip** reworded to remove "Tier alignment" / "quantum"
  language (`app/services/guidebook_content.py`).
- **New test file** `backend/tests/test_lab_signals_agreement.py`:
  9 unit tests covering all six synthetic cases, output bounds, and
  the backward-compat alias.  All pass without network access.

---

## Previous Pass (2026-07)

This pass focused on auditing the scanner's predictive/scoring factors for
methodological soundness and fixing what didn't hold up, rather than adding
new features. Every change below was validated with a runnable test before
being shipped — not just reasoned about.

## 1. Fixed in-sample overfitting bias (`app/services/predictive_expansion.py`)

`ts_nonlinear_dependence` and `lead_lag_influence` used to compare two
nested OLS models' **in-sample R²**. A model with more free parameters is
mathematically guaranteed to fit in-sample at least as well as a simpler
one, even on pure noise — so both factors reported a false "lift" almost
regardless of whether any real structure existed.

- Added `_forward_chain_r2()`: a shared, strictly-causal walk-forward
  validator. Both models are now fit only on past data and scored on
  held-out future blocks they never saw.
- **Measured impact**: on 200 pure-noise series, the old formula reported
  a false "lift" (>0.05) **89% of the time** (mean 0.42). The new version
  reports ~0 on the same noise, while still detecting genuine nonlinear /
  lead-lag structure in synthetic series built to contain it.

## 2. Replaced the fabricated "Quantum Path Interference Index"

The original `quantum_path_interference_index` ("Mock QPII" per its own
docstring) resampled a single Gaussian GBM model 30 times and dressed the
dispersion of that one model's own noise up as complex-amplitude "quantum
interference." It had no real predictive basis.

- **First pass**: replaced it with `_model_consensus_score`, combining four
  real techniques (GARCH-conditioned drift, regime-switching drift, a
  Hurst-exponent trend-persistence tilt, and an empirical block bootstrap),
  combined via inverse-variance weighting and scored with Cochran's Q / I²
  heterogeneity (a real meta-analysis statistic).
- **Testing then showed a real flaw in that first pass**: all four views
  were derived from the same short window of the same price series, so
  they could spuriously "agree" purely from shared sampling noise rather
  than genuine confirmation. A walk-forward validator (added alongside it,
  `validate_model_consensus_score`) showed it did not reliably beat a
  naive baseline even with genuine drift embedded in test data.
- **Fix**: the score now *requires* at least one view built from data
  genuinely independent of the symbol's own price history — a sector/index
  driver-return regression (itself gated behind `lead_lag_influence`'s own
  walk-forward validation for that specific symbol) or the dealer
  gamma-exposure (options market) regime. GARCH and the bootstrap remain
  as supporting views but can no longer produce a score alone. **If no
  independent view is available, the function now returns 0.0 — silence
  instead of a manufactured number.**
- Verified: an unrelated driver series correctly produces 0 (silent), a
  genuinely-related driver correctly produces a positive-correlation,
  above-baseline signal (~62–64% directional hit rate vs ~51–56% baseline
  across trials).
- Field key kept as `quantum_path_interference_index` for backward
  compatibility; UI label changed to "Model Consensus Score." Full history
  of what changed and why is documented in the registry entry itself
  (`app/services/predictive_expansion_registry.py`).

## 3. Registry descriptions rewritten for honesty

`ts_nonlinear_dependence`, `lead_lag_influence`, and
`quantum_path_interference_index` entries in
`predictive_expansion_registry.py` now explain the actual current
methodology (including measured before/after numbers) instead of
marketing language, so the dashboard's own metric descriptions are
self-documenting for whoever reads them later.

## 4. Automated prediction logging (closed a real accuracy-measurement gap)

Found that `save_prediction()` was reachable *only* from a manual "Save"
button in the UI — meaning every accuracy number this system could ever
produce was selection-biased (a person tends to save picks that already
look good) and limited to whatever a human bothered to click.

- Wired `auto_log_scan_predictions()` into the existing warmer background
  loop (which already cycles the full symbol universe on a timer with no
  human involvement), tagging rows `source='auto_scan'`, deduplicated so a
  fast warmer tick doesn't spam new rows for symbols with an unresolved
  open prediction.
- Confirmed a background evaluator thread scores expired predictions
  hourly against real historical closes (`evaluate_expired_predictions`).
- **Route layer gap fixed**: `/api/predictions/list` and
  `/api/predictions/accuracy` didn't expose the `source` filter at all,
  even though the service layer supported it.
- **Frontend gap fixed**: the Prediction Tracker dashboard had zero
  awareness that `auto_scan` vs `user` sources existed — the on-screen
  accuracy number silently blended both. Added an always-visible
  "Scanner accuracy (auto, unbiased)" vs "My saved picks (biased)"
  comparison card, a source filter dropdown, and a per-row Source column.
- Verified end-to-end with a throwaway DB: logging, same-tick dedup, and
  evaluation all produce correct results, including a genuine scored
  **miss** (not just always "hit").
- **Not yet verified against real market data/providers** in this pass
  (sandboxed, no network access) — worth a real smoke test after deploying.

## What's still open (not done in this pass)

- **Composite/heuristic weight refitting** ("Bucket 4"): `stability`,
  `quality`, `exit_risk` in `scoring_service.py`, and the reality-breaker
  composite weights (0.30/0.25/0.25/0.20), are still hand-picked constants
  never fit to tracked outcomes. Now that auto-logging exists, this can
  finally be done against real data instead of guesses.
- **`quantum_interference_certainty`** in `lab_signals.py`: the math is a
  defensible ensemble-agreement rule, but it's still framed in "quantum"
  language and hasn't been given the honest rename + validation treatment.
- **Coverage of the new consensus score is unmeasured** on the real
  universe — how often a symbol actually gets a validated driver or GEX
  view (vs. returning 0.0) should be measured by running the live scanner,
  not assumed.
- Real historical backtests of the reworked factors (vs. the synthetic
  tests done in this pass) should be run once real `auto_scan` outcome
  data has accumulated.
