# Prediction and model registry (Phase 0)

There is no single runtime registry that versions and dispatches every
prediction implementation. `predictive_expansion_registry.py` describes
predictive metrics for UI/contract use; it is not by itself a registry of
forecast models.

| Implementation | Actual behavior and inputs | Horizon/output and limits |
|---|---|---|
| `price_prediction_service.predict_price` | Uses an existing scored row, seven family scores, composite/direction, daily history/ATR, reaction-clustering and volume-sentiment modifiers; optional context modifiers also exist in the implementation. | Default 10 days; also accepts configured 1/5/10-hour paths. Emits target, confidence band, contributions, and reasons. Service documentation describes it as a deterministic estimate, not a validated forecast. |
| `advanced_prediction_service.predict_price_advanced` | Uses scored row/family state, daily or intraday returns, GARCH volatility, Hurst/regime context, Bayesian factor blend, reaction regime, dealer-GEX context, IOB barrier adjustments, and empirical bootstrap interval. | Default 10 days; caller may provide an intraday horizon. Emits direction probability and interval-related values; calibration is not established. |
| `future_mode_service.attach_forward_metrics_fast` | Attaches forward metric blocks using factor scores, ATR/context, and the configured fast path. | Shared blocks are `forward_1h`, `forward_5h`, `forward_1d`, `forward_5d`, and `forward_20d`; intraday units are hours, daily units are days. |
| `future_mode_service.attach_forward_metrics_garch` | GARCH-enriched forward metric path for rows. | Same named horizon set; distinct from the standalone price prediction services. |
| Future Mode signal bundles | Computes/attaches advanced, lab, strategy, and predictive signal structures used by Future Mode. | Scenario/Future Mode output is not interchangeable with ordinary price prediction. Model-level validation/eligibility is not centrally versioned. |
| `predict_next_day_open_direction` | Separate next-day-open direction path in `advanced_prediction_service.py`. | Different target semantics from a generic one-day close-to-close horizon. |
| Prediction tracker/backtest | `prediction_tracker_service.py` persists issued/open prediction records and accuracy stats; `prediction_backtest_service.py` records/resolves evaluations and exposes walk-forward estimates. | Does not create a unified immutable, versioned model-output contract. Point-in-time data sufficiency and look-ahead protections need case-level audit. |

## Provenance gaps

- A uniform model ID/version, feature-schema version, issue/input version, and
  outcome lifecycle is not established across the listed paths.
- Confidence, probability, and intervals have different source meanings; they
  must not be relabeled as calibrated probability without evaluation.
- Calendar-day, trading-day, bar-count, and next-session semantics differ.
- Runtime/provider cost and eligibility boundaries have not been measured
  consistently.
- Existing prediction output and tracker shapes should be captured before any
  output contract is unified.
