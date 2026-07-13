"""
Central registry for every scanner_metrics filter / sort key.

The filter engine MUST only read flat keys from this registry.  Nested
factor payloads under `factor_breakdown.market.<family>` are debug/detail
mirrors and not the primary filter execution surface.

Each entry defines:
  * `source`:    dotted path inside the result row to project from.
  * `type`:      `enum`, `num`, or `bool`.
  * `ops`:       supported operators (`in`, `min`, `max`).
  * `default`:   value to use when the source is missing.
  * `bucket`:    optional bucket label (e.g. `options`, `institutional`).

The registry is intentionally explicit — it doubles as documentation for
the filter/sort surface the dashboard and any external consumers depend on.
"""
from __future__ import annotations

from typing import Any

SCANNER_METRICS: dict[str, dict[str, Any]] = {
    # ------------------------------------------------------------------
    # Core scoring
    # ------------------------------------------------------------------
    'final_score': {
        'source': 'final_score',
        'type': 'num',
        'ops': ['min', 'max'],
        'default': 0.0,
        'bucket': 'core',
    },
    'tier': {
        'source': 'tier',
        'type': 'enum',
        'ops': ['in'],
        'default': 'Unranked',
        'bucket': 'core',
    },
    'final_direction': {
        'source': 'final_direction',
        'type': 'enum',
        'ops': ['in'],
        'default': 'Neutral',
        'bucket': 'core',
    },

    # ------------------------------------------------------------------
    # Trend / volume delta
    # ------------------------------------------------------------------
    'trend_volume_delta': {
        'source': 'factor_breakdown.market.trend_volume_delta.score',
        'type': 'num',
        'ops': ['min', 'max'],
        'default': 50.0,
        'bucket': 'trend',
    },
    'trend_volume_delta_bucket': {
        'source': 'factor_breakdown.market.trend_volume_delta.bucket',
        'type': 'enum',
        'ops': ['in'],
        'default': 'neutral',
        'bucket': 'trend',
    },

    # ------------------------------------------------------------------
    # Institutional confluence (multi-component)
    # ------------------------------------------------------------------
    'institutional_confluence': {
        'source': 'factor_breakdown.market.institutional_confluence.score',
        'type': 'num',
        'ops': ['min', 'max'],
        'default': 50.0,
        'bucket': 'institutional',
    },
    'institutional_bias': {
        'source': 'factor_breakdown.market.institutional_confluence.bias',
        'type': 'enum',
        'ops': ['in'],
        'default': 'neutral',
        'bucket': 'institutional',
    },

    # ------------------------------------------------------------------
    # Options positioning
    # ------------------------------------------------------------------
    'options_positioning': {
        'source': 'factor_breakdown.market.options_positioning.score',
        'type': 'num',
        'ops': ['min', 'max'],
        'default': 50.0,
        'bucket': 'options',
    },
    'options_bias': {
        'source': 'factor_breakdown.market.options_positioning.bias',
        'type': 'enum',
        'ops': ['in'],
        'default': 'neutral',
        'bucket': 'options',
    },
    'options_gamma_level': {
        'source': 'factor_breakdown.market.options_positioning.gamma_level_label',
        'type': 'enum',
        'ops': ['in'],
        'default': 'moderate',
        'bucket': 'options',
    },
    'pin_risk': {
        'source': 'factor_breakdown.market.options_positioning.pin_risk',
        'type': 'enum',
        'ops': ['in'],
        'default': 'low',
        'bucket': 'options',
    },
    'options_provenance': {
        'source': 'factor_breakdown.market.options_positioning.provenance',
        'type': 'enum',
        'ops': ['in'],
        'default': 'unavailable',
        'bucket': 'options',
    },

    # ------------------------------------------------------------------
    # Institutional order block
    # ------------------------------------------------------------------
    'iob_score': {
        'source': 'factor_breakdown.market.institutional_order_block.score',
        'type': 'num',
        'ops': ['min', 'max'],
        'default': 50.0,
        'bucket': 'order_block',
    },
    'iob_state': {
        'source': 'factor_breakdown.market.institutional_order_block.state',
        'type': 'enum',
        'ops': ['in'],
        'default': 'unavailable',
        'bucket': 'order_block',
    },
    'iob_bias': {
        'source': 'factor_breakdown.market.institutional_order_block.bias',
        'type': 'enum',
        'ops': ['in'],
        'default': 'neutral',
        'bucket': 'order_block',
    },

    # ------------------------------------------------------------------
    # Dark pool proxy
    # ------------------------------------------------------------------
    'dark_pool_proxy': {
        'source': 'factor_breakdown.market.dark_pool_proxy.score',
        'type': 'num',
        'ops': ['min', 'max'],
        'default': 50.0,
        'bucket': 'dark_pool',
    },
    'dark_pool_bias': {
        'source': 'factor_breakdown.market.dark_pool_proxy.bias',
        'type': 'enum',
        'ops': ['in'],
        'default': 'neutral',
        'bucket': 'dark_pool',
    },
    'dark_pool_attraction_state': {
        'source': 'factor_breakdown.market.dark_pool_proxy.attraction_state',
        'type': 'enum',
        'ops': ['in'],
        'default': 'neutral',
        'bucket': 'dark_pool',
    },

    # ------------------------------------------------------------------
    # Exit risk
    # ------------------------------------------------------------------
    'exit_risk': {
        'source': 'factor_breakdown.exit_model.score',
        'type': 'num',
        'ops': ['min', 'max'],
        'default': 50.0,
        'bucket': 'exit',
    },
    'exit_flag': {
        'source': 'factor_breakdown.exit_model.exit_flag',
        'type': 'enum',
        'ops': ['in'],
        'default': 'hold',
        'bucket': 'exit',
    },

    # ------------------------------------------------------------------
    # Phase 4b: Volume sentiment (shared substrate)
    # ------------------------------------------------------------------
    'volume_sentiment_directional': {
        'source': 'factor_breakdown.market.volume_sentiment.directional_score',
        'type': 'num',
        'ops': ['min', 'max'],
        'default': 50.0,
        'bucket': 'volume_sentiment',
    },
    'volume_sentiment_conviction': {
        'source': 'factor_breakdown.market.volume_sentiment.conviction_score',
        'type': 'num',
        'ops': ['min', 'max'],
        'default': 0.0,
        'bucket': 'volume_sentiment',
    },
    'volume_sentiment_bias': {
        'source': 'factor_breakdown.market.volume_sentiment.bias',
        'type': 'enum',
        'ops': ['in'],
        'default': 'neutral',
        'bucket': 'volume_sentiment',
    },
    'volume_sentiment_regime': {
        'source': 'factor_breakdown.market.volume_sentiment.regime',
        'type': 'enum',
        'ops': ['in'],
        'default': 'normal',
        'bucket': 'volume_sentiment',
    },
    'effort_vs_result_label': {
        'source': 'factor_breakdown.market.volume_sentiment.effort_vs_result_label',
        'type': 'enum',
        'ops': ['in'],
        'default': 'neutral',
        'bucket': 'volume_sentiment',
    },

    # ------------------------------------------------------------------
    # Phase 4b: Reaction clustering / dominant zone
    # ------------------------------------------------------------------
    'reaction_classification': {
        'source': 'factor_breakdown.market.reaction_map.classification',
        'type': 'enum',
        'ops': ['in'],
        'default': 'NEUTRAL',
        'bucket': 'reaction',
    },
    'reaction_propel_probability': {
        'source': 'factor_breakdown.market.reaction_map.propel_probability',
        'type': 'num',
        'ops': ['min', 'max'],
        'default': 0.0,
        'bucket': 'reaction',
    },
    'reaction_reject_probability': {
        'source': 'factor_breakdown.market.reaction_map.reject_probability',
        'type': 'num',
        'ops': ['min', 'max'],
        'default': 0.0,
        'bucket': 'reaction',
    },
    'reaction_chop_probability': {
        'source': 'factor_breakdown.market.reaction_map.chop_probability',
        'type': 'num',
        'ops': ['min', 'max'],
        'default': 0.0,
        'bucket': 'reaction',
    },
    'dominant_zone_tier': {
        'source': 'factor_breakdown.market.reaction_map.dominant_zone.tier',
        'type': 'enum',
        'ops': ['in'],
        'default': 'MINOR',
        'bucket': 'reaction',
    },
    'dominant_zone_distance_pct': {
        'source': 'factor_breakdown.market.reaction_map.dominant_zone.distance_pct',
        'type': 'num',
        'ops': ['min', 'max'],
        'default': 99.0,
        'bucket': 'reaction',
    },
    'dominant_zone_evidence': {
        'source': 'factor_breakdown.market.reaction_map.dominant_zone.evidence_score',
        'type': 'num',
        'ops': ['min', 'max'],
        'default': 0.0,
        'bucket': 'reaction',
    },

    # ------------------------------------------------------------------
    # Phase 4b: IOB expected-reaction modulation
    # ------------------------------------------------------------------
    'iob_reaction_classification': {
        'source': 'factor_breakdown.market.institutional_order_block.reaction_classification',
        'type': 'enum',
        'ops': ['in'],
        'default': 'NEUTRAL',
        'bucket': 'order_block',
    },
    'iob_propel_probability': {
        'source': 'factor_breakdown.market.institutional_order_block.expected_reaction.propel_probability',
        'type': 'num',
        'ops': ['min', 'max'],
        'default': 0.0,
        'bucket': 'order_block',
    },
    'iob_reject_probability': {
        'source': 'factor_breakdown.market.institutional_order_block.expected_reaction.reject_probability',
        'type': 'num',
        'ops': ['min', 'max'],
        'default': 0.0,
        'bucket': 'order_block',
    },
    'iob_chop_probability': {
        'source': 'factor_breakdown.market.institutional_order_block.expected_reaction.chop_probability',
        'type': 'num',
        'ops': ['min', 'max'],
        'default': 0.0,
        'bucket': 'order_block',
    },
    'iob_volume_alignment_score': {
        'source': 'factor_breakdown.market.institutional_order_block.volume_alignment_score',
        'type': 'num',
        'ops': ['min', 'max'],
        'default': 50.0,
        'bucket': 'order_block',
    },

    # ------------------------------------------------------------------
    # Phase 4b: Options pressure modulation
    # ------------------------------------------------------------------
    'options_pressure_score_adjusted': {
        'source': 'factor_breakdown.market.options_positioning.pressure_score_adjusted',
        'type': 'num',
        'ops': ['min', 'max'],
        'default': 50.0,
        'bucket': 'options',
    },
    'options_volume_alignment': {
        'source': 'factor_breakdown.market.options_positioning.volume_alignment',
        'type': 'enum',
        'ops': ['in'],
        'default': 'unavailable',
        'bucket': 'options',
    },

    # ------------------------------------------------------------------
    # Scanner-context families: short selling pressure, predicted volume
    # intensity, options expiration awareness, forecast readiness.
    # Sources are the flat first-class row fields stamped by
    # `score_from_prices` (survive compact/thin rows).
    # ------------------------------------------------------------------
    'short_selling_pressure': {
        'source': 'short_selling_pressure_score',
        'type': 'num',
        'ops': ['min', 'max'],
        'default': 50.0,
        'bucket': 'short_pressure',
    },
    'short_selling_pressure_label': {
        'source': 'short_selling_pressure_label',
        'type': 'enum',
        'ops': ['in'],
        'default': 'neutral',
        'bucket': 'short_pressure',
    },
    'short_selling_pressure_source': {
        'source': 'short_selling_pressure_source',
        'type': 'enum',
        'ops': ['in'],
        'default': 'unavailable',
        'bucket': 'short_pressure',
    },
    'predicted_volume_intensity': {
        'source': 'predicted_volume_intensity_score',
        'type': 'num',
        'ops': ['min', 'max'],
        'default': 0.0,
        'bucket': 'volume_intensity',
    },
    'predicted_volume_intensity_bucket': {
        'source': 'predicted_volume_intensity_bucket',
        'type': 'enum',
        'ops': ['in'],
        'default': 'low',
        'bucket': 'volume_intensity',
    },
    'predicted_volume_event_flag': {
        'source': 'predicted_volume_event_flag',
        'type': 'bool',
        'ops': ['in'],
        'default': False,
        'bucket': 'volume_intensity',
    },
    'days_to_options_expiration': {
        'source': 'days_to_options_expiration',
        'type': 'num',
        'ops': ['min', 'max'],
        'default': None,
        'bucket': 'expiration',
    },
    'expiration_risk_flag': {
        'source': 'expiration_risk_flag',
        'type': 'bool',
        'ops': ['in'],
        'default': False,
        'bucket': 'expiration',
    },
    'future_forecast_ready': {
        'source': 'future_forecast_ready',
        'type': 'bool',
        'ops': ['in'],
        'default': False,
        'bucket': 'forecast',
    },
}


def _follow_path(row: dict, path: str) -> Any:
    """Walk a dotted path through nested dicts; return None if any step fails."""
    cur: Any = row
    for part in path.split('.'):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
        if cur is None:
            return None
    return cur


def project_scanner_metrics(row: dict) -> dict[str, Any]:
    """Flatten the registered metrics off a normalized row."""
    out: dict[str, Any] = {}
    for key, spec in SCANNER_METRICS.items():
        value = _follow_path(row, spec['source'])
        if value is None:
            value = spec['default']
        out[key] = value
    return out


def metric_value(row: dict, key: str) -> Any:
    spec = SCANNER_METRICS.get(key)
    if not spec:
        return None
    value = _follow_path(row, spec['source'])
    return value if value is not None else spec.get('default')
