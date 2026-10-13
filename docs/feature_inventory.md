# Feature inventory

Inventory is based on mounted routes, scanner/service callers, frontend files,
and current tests. A module's presence alone is not evidence that it is part of
the live request path.

| Feature | Current implementation / consumer | Evidence and caveats |
|---|---|---|
| Stock and crypto scan results | `scoring_service.py`, `result_store.py`, `/stocks/results` | Result query applies filters, preset, sort, and page/batch. Not catalog-first. |
| Periodic snapshots | `snapshot_store.py`, `/api/scan/snapshot` | Background scan loop; snapshot sort accepts `score` and `predicted_volume_intensity`. |
| Search and symbol detail | Search/detail routers and frontend | Existing route surface; symbol visibility is coupled to available universe/result state. |
| Core composite score | `scoring_service.py` | Momentum, quality, trend, stability, exit-risk penalty, degradation handling. |
| Extended analysis | `extended_factors.py` and related services | Seven families appear in the full-depth score; data availability and applicability differ. |
| Direction and tiers | `scoring_service.py` | Final score/direction/tier are exposed on rows; core tiers use A/B/C/D score thresholds subject to score-path rules. |
| Presets, filters, and presentation sorting | `scanner_presets.py`, `result_store.py`, route query parameters | Declarative in-code preset definitions; many factor and scanner-context filters. |
| Fast/GARCH forward metrics | `future_mode_service.py`, `horizon_definitions.py` | Five shared horizons: 1h, 5h, 1d, 5d, 20d. Daily and intraday units have different meanings. |
| Price prediction | `price_prediction_service.py`, `advanced_prediction_service.py` | Separate implementations; default daily horizon is 10 days. Neither should be treated as calibrated merely from its output fields. |
| Future Mode / scenario presentation | Future Mode service and router | Composes advanced, lab, strategy, and predictive signal bundles; not the same contract as the simple price-prediction endpoint. |
| Prediction tracking and evaluation | `prediction_tracker_service.py`, `prediction_backtest_service.py` | Persistent tracker/backtest code exists; historical point-in-time input completeness and evaluation limitations need verification. |
| Options and short-pressure enrichment | `options_chain_service.py`, `short_pressure_service.py` | Equity-specific inputs are not generally available for crypto; output includes unavailable/not-applicable paths. |
| Market/context enrichment | Volume intensity/sentiment, reaction map, cross-market radar, regulatory services | Their independent availability, coverage, and ranking influence are not equivalent. |
| Tiered work prioritization | `tier_manager.py`, Tier 1/2/3 scanner services | In-process scanner architecture, not durable cross-process jobs. Tiers describe work/scan behavior, not proof of full coverage. |
| Caching and maintenance | Memory, quote, history, snapshot, tier, and factor cache services | Multiple cache domains and persistence mechanisms; caches are not durable catalog truth. |
| Frontend dashboard | Static pages and vanilla JavaScript under `frontend/` | No React/Vite application or frontend test/build command is configured. |

## Known test evidence

The current pytest suite includes deterministic tests for lab-signal agreement,
cache contracts, state stores, and service behavior, as well as API integration
tests that expect a running service and may use live market data. The new
`test_parity_scaffolding.py` covers fixture loading and the capture helper only;
it does not establish scorer or prediction parity.
