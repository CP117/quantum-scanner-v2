# Changelog — Predictive System Rework (2026-07)

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
