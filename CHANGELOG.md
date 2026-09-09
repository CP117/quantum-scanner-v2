# Changelog — Predictive System Rework (2026-07 / 2026-08)

---

## Phase 11 tier-aware cache orchestration

- Tier promotions now reprioritize compatible RAM artifacts; Tier 1 promotions
  queue a fresh full scoring pass with a bounded quote-age fallback and the
  existing optional-options refresh policy. Valid daily history is retained.
- Tier 3 demotions make derived artifacts first eviction candidates. Emergency
  maintenance pauses Tier 3 prefetch, checkpoints Tier 3 summaries, and
  relieves RAM pressure before affecting hotter data.
- Blacklist and active-universe removals invalidate quote, history, options,
  tier, and RAM artifacts. Provider session resets use generation-aware cache
  identities and invalidate affected provider values.
- Provider admission reserves a small Tier 3 share, reports backpressure, and
  opens a bounded circuit after consecutive failures.

## RAM-first cache layer

- Added a bounded, process-local cache foundation with monotonic TTL,
  deterministic fingerprints, namespace isolation, approximate memory
  accounting, and aggregate diagnostics.
- Added `MEMORY_CACHE_MODE` (`disabled`, `shadow_write`, or `enabled`) and a
  per-worker `MEMORY_CACHE_MAX_MB` budget. Existing route payloads are
  unchanged; disk-backed quote/history caches remain restart fallback.

## Phase 10 derived-cache migration

- Added opt-in Bayesian-prior caching keyed by validated source-history
  content, model configuration/version, and symbol/market segment scope.
  Incomplete or unvalidated histories never enter the cache.
- Added opt-in factor-narrative caching keyed by complete factor inputs and
  narrative template/configuration version. Cached results preserve the
  existing public narrative payload exactly; provenance remains internal.

## Phase 8 metadata cache migration

- Added single-flight RAM caching for active universe metadata, keyed by its
  source and grouped-universe definition with a configurable
  `UNIVERSE_METADATA_TTL_SECONDS` TTL.
- Added declarative scanner-preset validation and bounded, content-fingerprinted
  filter-pipeline compilation; no preset expression is evaluated as code.
- Added provider-, symbol-, expiration-selection-, and filter-aware options
  chain RAM keys, `OPTIONS_CHAIN_CACHE_TTL_SECONDS`, and Tier 1 target-age
  refresh gating via `OPTIONS_CHAIN_TIER1_REFRESH_SECONDS`.
- Added `POST /api/cache/dedupe/memory/invalidate` for explicit universe,
  options-chain, and scanner-preset cache invalidation. It retains provider
  failure cooldowns when only options payloads are cleared.

## Cache profiling gate

- Added `scripts/profile_cache_baseline.py`, an offline deterministic
  benchmark for cold/warm Tier 1 scoring, provider failures, Tier-shaped row
  orchestration, allocation/GC signals, CPU profiles, and concurrent
  same-symbol duplicate fetches.
- Added focused tests and operations guidance. Generated profiling reports are
  intentionally kept out of version control.

## Cache deployment topology

- Added process-local cache topology diagnostics, including worker detection
  and per-worker versus estimated all-worker memory budgeting.
- Documented the single-worker Uvicorn default and the lack of cross-worker
  cache, lock, invalidation, and warmup coordination.

## Shared memory-store controls

- Added domain-aware quote/history helpers, cache rollout modes, TTL jitter,
  bounded per-key single-flight refresh coordination, and Tier-aware priority
  eviction under configured memory pressure.

## Cache rollout configuration

- Added per-domain flags, modes, TTLs, warmup controls, and a configurable
  correctness-sampling rate. New derived-data domains default to disabled for
  staged rollout safety.

## Cache freshness contract

- Added an internal domain contract registry for freshness, stale fallback,
  cache-key dimensions, and non-sensitive provenance requirements.

## Daily-history RAM migration

- Added provider-, interval-, lookback-, adjustment-, and session-aware RAM
  history keys, copy-isolated DataFrame reads, Tier-aware residency priority,
  warmup jitter, and low-priority prefetch backpressure.

---

## Next Phase: `quantum_interference_certainty` rename and validation

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
