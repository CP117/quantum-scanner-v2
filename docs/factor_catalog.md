# Factor and ranking catalog

This is a verified first-pass inventory, not a complete factor-by-factor
formula audit. Treat the cited implementations as authoritative. Where source
does not establish a field's formula, normalization, lookback, or runtime, that
property is marked unknown rather than inferred from a name.

## Core composite

`build_algorithm_breakdown` in `app/services/scoring_service.py` computes the
daily change from current price and previous close, then:

| Stable output key | Actual implementation | Missing/default behavior |
|---|---|---|
| `momentum` | `clamp_score(50 + change_pct * 5)` | If previous close is falsy, change is 0. |
| `quality` | `build_quality_breakdown` score | Uses its own component values/weights; see emitted `components` and `weights`. |
| `trend` | `clamp_score(50 + change_pct * 4)` | Same change input as momentum. |
| `stability` | `build_stability_breakdown` score | Uses intraday inputs and its emitted component/weight breakdown. |
| `exit_risk` penalty | `max(0, (exit_risk - 50) * 0.22)` | Missing exit risk uses 50, therefore no penalty. |
| Core `final_score` | `0.35*M + 0.25*Q + 0.20*T + 0.20*S - exit_penalty` | Rounded to two decimals; if at least two ratings are <=5, score is capped by the strongest live score and 30, and tier forced to D. |

Scores are clamped/rounded in the implementation; the output breakdown carries
component evidence. No outcome-fitted or calibrated interpretation is
established by these formulas.

## Full-depth extended families

`compute_extended_factors` invokes the following family builders. In the full
score path, `_secondary_composite_candidate` assigns each family its `score`
(fallback 50) and uses an arithmetic mean; the blend is
`0.80 * core_final + 0.20 * extended_avg`, followed by an available predictive
modifier and a [0,100] clamp. If a core score explanation exists, the final
value is capped at the core score.

| Family key | Implementation/source | Applicability / evidence notes |
|---|---|---|
| `trend_volume_delta` | `scoring_service.py` | Derived from price change and volume context; contributes a family score/bucket. Exact input-data quality varies. |
| `institutional_confluence` | `scoring_service.py` | Composite context family; constituent evidence is emitted in family payload. Do not interpret the label as verified institutional flow. |
| `options_positioning` | `scoring_service.py`, `options_chain_service.py` | May use real chain data or an inferred path; unavailable/not-applicable state must be preserved. |
| `institutional_order_block` | `scoring_service.py` | Heuristic level/price-context output; source does not establish exchange order-book data. |
| `dark_pool_proxy` | `scoring_service.py` | A proxy output; do not label it as direct dark-pool observations. |
| `volume_sentiment` | `volume_sentiment.py` | History-derived profile; output includes availability/provenance fields. |
| `reaction_clustering` | `reaction_clustering_service.py` | Builds a reaction map from price/volume history and exposes classification/zone evidence. |

The extended pipeline includes explicit unavailable and not-applicable
sentinels. A default/fallback numeric score is not equivalent to an observed
zero or a valid measurement. Asset-specific missing inputs, especially options
and short interest, need case-level parity coverage.

## Other score/context fields

The result/query code also exposes metrics such as predicted volume intensity,
short-selling pressure, options expiration, options gamma level, exit risk,
factor-family components, and regulatory context. Their implementations are
in `volume_intensity_service.py`, `short_pressure_service.py`,
`options_chain_service.py`, `scoring_service.py`, and related services. Their
presence in a row or a preset is not proof that every item participates in
`final_score`.

The requested full inventory fields—input schema, exact formula, normalization,
sign, asset applicability, lookback, missing behavior, consumers, runtime,
provider cost, validation, and migration destination—are not uniformly
available in the current code. Per-family implementation and performance
profiling remain blockers; do not fabricate values to fill those columns.

## Ranking and sorting semantics

- **Composite presentation:** `/stocks/results` defaults to `final_score`
  descending (`sort_rows` uses stable Python `sorted` and `reverse`); unknown
  metric keys fall back to `final_score`.
- **Metric sorting:** `row_factor_metric` first checks `scanner_metrics`, then
  a legacy map including `final_score`, `exit_risk`, extended scores,
  `predicted_volume_intensity`, `short_selling_pressure`, and negated days to
  options expiration. Missing numeric values commonly map to 0 (or a
  metric-specific fallback). There is no explicit secondary tie-breaker in
  `sort_rows`.
- **Snapshot presentation:** `/api/scan/snapshot` accepts only `score` and
  `predicted_volume_intensity`; other requested values are replaced with
  `score`.
- **Filters/presets:** declarative preset definitions select direction, tier,
  score, exit-risk, family, and context criteria. Some presets have market
  applicability overrides. These are query/presentation filters, not a global
  scheduler priority contract.
- **Directional and prediction ranking:** prediction/forward metrics are
  available on rows and in frontend/service code, but a complete inventory of
  every UI ranking consumer, null rule, and tie-breaker has not yet been
  established. Do not assume the snapshot and results endpoints have identical
  sort modes.

## Parity targets

Capture individual core components, each available family payload/status,
extended mean, composite before/after modifiers, direction/tier, and representative
sort/filter cases separately. This makes missing data and full-depth fallback
behavior visible rather than hiding them in one final score.
