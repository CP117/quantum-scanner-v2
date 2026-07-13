
SCANNER_PRESETS = {
    "all": {"label": "All ranked", "directions": [], "tiers": [], "exclude_preview": False, "min_score": 0},
    "bullish": {"label": "Bullish focus", "directions": ["Bullish"], "tiers": [], "exclude_preview": False, "min_score": 0},
    "leaders": {"label": "Top leaders", "directions": ["Bullish"], "tiers": ["A", "B"], "exclude_preview": False, "min_score": 55},
    "value-safe": {"label": "Stable / quality", "directions": [], "tiers": ["A", "B", "C"], "exclude_preview": False, "min_score": 45},
    "degraded-view": {"label": "Fallback monitor", "directions": [], "tiers": [], "exclude_preview": False, "min_score": 0},
    "low-exit-risk": {"label": "Low exit risk", "directions": [], "tiers": ["A", "B", "C"], "exclude_preview": False, "min_score": 45, "max_exit_risk": 45, "exit_flags": ["hold"]},
    "reversal-watch": {"label": "Reversal watch", "directions": [], "tiers": [], "exclude_preview": False, "min_score": 0, "min_exit_risk": 60, "exit_flags": ["caution", "exit"]},
    "institutional-bullish": {"label": "Institutional bullish", "directions": [], "tiers": [], "exclude_preview": False, "min_score": 45, "min_institutional_confluence": 60, "institutional_bias_in": ["bullish"]},
    "options-bullish": {"label": "Options bullish", "directions": [], "tiers": [], "exclude_preview": False, "min_score": 45, "min_options_positioning": 60, "options_bias_in": ["bullish"]},
    "volume-confirmed": {"label": "Volume confirmed trend", "directions": [], "tiers": [], "exclude_preview": False, "min_score": 45, "trend_volume_delta_bucket_in": ["strong_bullish", "bullish", "strong_bearish", "bearish"]},
    "pin-risk": {"label": "Pin risk monitor", "directions": [], "tiers": [], "exclude_preview": False, "min_score": 0, "pin_risk_in": ["high", "moderate"]},
    # ---- Scanner-context presets (short pressure / PVI / expiration) ----
    "squeeze-watch": {
        "label": "Squeeze Watch",
        "directions": [], "tiers": [], "exclude_preview": False, "min_score": 0,
        "min_short_selling_pressure": 60,
        "short_selling_pressure_label_in": ["squeeze_risk_bullish", "elevated_squeeze_watch"],
        "min_predicted_volume_intensity": 55,
        "max_days_to_options_expiration": 14,
        # Crypto overrides (Phase 26.70): crypto SSP tops out around
        # ~50 (proxy inference, no live short interest) and label
        # rarely reaches "squeeze_risk_bullish".  We require BOTH SSP
        # and PVI signals to be present (score > 0/50 placeholders)
        # and use a strong PVI + moderate SSP combination as the
        # crypto-native squeeze signal.  No DTE gate — crypto has no
        # options expiration.
        "crypto_overrides": {
            "min_short_selling_pressure": 30,
            "short_selling_pressure_label_in": None,  # drop label gate
            "min_predicted_volume_intensity": 40,
            "predicted_volume_intensity_bucket_in": ["moderate", "high", "extreme"],
            "max_days_to_options_expiration": None,
            "require_populated_pvi": True,
        },
    },
    "volume-storm": {
        "label": "Volume Storm",
        "directions": [], "tiers": [], "exclude_preview": False, "min_score": 0,
        "min_predicted_volume_intensity": 65,
        "predicted_volume_intensity_bucket_in": ["high", "extreme"],
        "predicted_volume_event_flag": True,
        # Crypto has no scheduled earnings-event flag — drop that.
        # Crypto's PVI proxy caps lower; require moderate-or-better
        # bucket which is what a real volume storm looks like on 24/7
        # markets.
        "crypto_overrides": {
            "min_predicted_volume_intensity": 35,
            "predicted_volume_intensity_bucket_in": ["moderate", "high", "extreme"],
            "predicted_volume_event_flag": None,
            "require_populated_pvi": True,
        },
    },
    "bearish-pressure": {
        "label": "Bearish pressure",
        "directions": [], "tiers": [], "exclude_preview": False, "min_score": 0,
        "min_short_selling_pressure": 55,
        "short_selling_pressure_label_in": ["bearish_pressure", "elevated"],
        # Crypto: SSP proxy typically 20-45 range for genuinely
        # weakening coins.  Combine with negative final_direction to
        # ensure the "bearish" characterization is real.
        "crypto_overrides": {
            "min_short_selling_pressure": 25,
            "short_selling_pressure_label_in": ["bearish_pressure", "elevated", "low"],
            "directions": ["Bearish"],
            "require_populated_ssp": True,
        },
    },
    "expiration-pin": {
        "label": "Expiration pin risk",
        "directions": [], "tiers": [], "exclude_preview": False, "min_score": 0,
        "expiration_risk_flag": True,
        "max_days_to_options_expiration": 7,
        "pin_risk_in": ["high", "moderate"],
        # No options market for crypto → this preset returns nothing
        # (by design).  The frontend hides the chip on crypto.
        "crypto_overrides": {
            "_disabled_for_crypto": True,
        },
    },
}


def resolve_filters(preset: str | None, direction: str | None = None,
                    tier: str | None = None, min_score: float | None = None,
                    market: str | None = None) -> dict:
    base = dict(SCANNER_PRESETS.get((preset or 'all').strip(), SCANNER_PRESETS['all']))
    base['preset'] = preset or 'all'
    # Phase 26.70: apply market-specific overrides.  `crypto_overrides`
    # is a dict of {filter_key: value | None} where None DELETES the key
    # from the resolved filter (used to drop stock-only gates like
    # `max_days_to_options_expiration` on crypto).  `_disabled_for_crypto`
    # short-circuits the preset — a filter that never matches anything.
    if (market or '').lower() == 'crypto':
        overrides = base.pop('crypto_overrides', None) or {}
        if overrides.get('_disabled_for_crypto'):
            base['_disabled'] = True
        for k, v in overrides.items():
            if k.startswith('_'):
                continue
            if v is None:
                base.pop(k, None)
            else:
                base[k] = v
    else:
        base.pop('crypto_overrides', None)
    if direction:
        base['directions'] = [direction]
    if tier:
        base['tiers'] = [tier]
    if min_score is not None:
        base['min_score'] = float(min_score)
    return base


def _fb(row: dict) -> dict:
    return row.get('factor_breakdown') or {}


def _market(row: dict) -> dict:
    return _fb(row).get('market') or {}


def _safe_num(value, default=0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def row_exit_score(row: dict) -> float:
    return _safe_num((((_fb(row).get('exit_model') or {}).get('score', 0)) or 0))


def row_exit_flag(row: dict) -> str:
    return (((_fb(row).get('exit_model') or {}).get('exit_flag')) or 'hold')


def row_trend_volume_delta_score(row: dict) -> float:
    return _safe_num(((_market(row).get('trend_volume_delta') or {}).get('score', 0)) or 0)


def row_trend_volume_delta_bucket(row: dict) -> str:
    return (((_market(row).get('trend_volume_delta') or {}).get('bucket')) or 'neutral')


def row_institutional_confluence_score(row: dict) -> float:
    return _safe_num(((_market(row).get('institutional_confluence') or {}).get('score', 0)) or 0)


def row_institutional_bias(row: dict) -> str:
    return (((_market(row).get('institutional_confluence') or {}).get('bias')) or 'neutral')


def row_options_positioning_score(row: dict) -> float:
    return _safe_num(((_market(row).get('options_positioning') or {}).get('score', 0)) or 0)


def row_options_bias(row: dict) -> str:
    return (((_market(row).get('options_positioning') or {}).get('bias')) or 'neutral')


def row_pin_risk(row: dict) -> str:
    return (((_market(row).get('options_positioning') or {}).get('pin_risk')) or 'low')


def row_options_gamma_level(row: dict) -> float:
    return _safe_num(((_market(row).get('options_positioning') or {}).get('gamma_level', 0)) or 0)


def row_iob_score(row: dict) -> float:
    return _safe_num(((_market(row).get('institutional_order_block') or {}).get('score', 0)) or 0)


def row_iob_state(row: dict) -> str:
    return (((_market(row).get('institutional_order_block') or {}).get('state')) or 'unknown')


def row_iob_bias(row: dict) -> str:
    return (((_market(row).get('institutional_order_block') or {}).get('bias')) or 'neutral')


def row_iob_distance_pct(row: dict) -> float:
    return _safe_num(((_market(row).get('institutional_order_block') or {}).get('distance_from_price_pct', 999)) or 999)


def row_iob_confidence(row: dict) -> float:
    return _safe_num(((_market(row).get('institutional_order_block') or {}).get('confidence', 0)) or 0)


def row_dark_pool_proxy_score(row: dict) -> float:
    return _safe_num(((_market(row).get('dark_pool_proxy') or {}).get('score', 0)) or 0)


def row_dark_pool_bias(row: dict) -> str:
    return (((_market(row).get('dark_pool_proxy') or {}).get('bias')) or 'neutral')


def row_dark_pool_distance_pct(row: dict) -> float:
    return _safe_num(((_market(row).get('dark_pool_proxy') or {}).get('distance_to_print_pct', 999)) or 999)


def row_dark_pool_memory_score(row: dict) -> float:
    return _safe_num(((_market(row).get('dark_pool_proxy') or {}).get('memory_reaction_score', 0)) or 0)


def row_dark_pool_pinning(row: dict) -> str:
    return (((_market(row).get('dark_pool_proxy') or {}).get('pinning_effect')) or 'low')


def row_factor_metric(row: dict, key: str):
    """Get a sortable metric value from a row.
    
    First tries scanner_metrics (which has all Phase 4b keys), then falls back
    to legacy factor_breakdown accessors, then final_score.
    """
    key = (key or '').strip()
    
    # Try scanner_metrics first (covers all Phase 4b keys)
    sm = row.get('scanner_metrics', {})
    if key in sm:
        val = sm[key]
        # Handle None/null values
        if val is None:
            return 0.0
        # Return numeric values directly
        if isinstance(val, (int, float)):
            return float(val)
        # For string values, return 0 (they shouldn't be sorted numerically)
        return 0.0
    
    # Legacy mapping for backward compatibility
    mapping = {
        'final_score': lambda r: _safe_num(r.get('final_score', 0)),
        'exit_risk': row_exit_score,
        'trend_volume_delta': row_trend_volume_delta_score,
        'institutional_confluence': row_institutional_confluence_score,
        'options_positioning': row_options_positioning_score,
        'institutional_order_block': row_iob_score,
        'dark_pool_proxy': row_dark_pool_proxy_score,
        'options_gamma_level': row_options_gamma_level,
        'predicted_volume_intensity': lambda r: _safe_num(r.get('predicted_volume_intensity_score', 0)),
        'short_selling_pressure': lambda r: _safe_num(r.get('short_selling_pressure_score', 50)),
        # Negated so the default descending sort surfaces nearest expirations first.
        'days_to_options_expiration': lambda r: -_safe_num(r.get('days_to_options_expiration', 999), 999),
    }
    fn = mapping.get(key)
    return fn(row) if fn else _safe_num(row.get('final_score', 0))


def sort_rows(rows: list[dict], sort_by: str | None = None, descending: bool = True) -> list[dict]:
    key = (sort_by or 'final_score').strip()
    return sorted(rows, key=lambda r: row_factor_metric(r, key), reverse=descending)


def apply_filters(rows: list[dict], filters: dict) -> list[dict]:
    # Phase 26.70: if the preset was disabled for this market
    # (e.g. `expiration-pin` on crypto → no options market), short-
    # circuit to an empty result instead of returning noise.
    if filters.get('_disabled'):
        return []
    out = []
    for row in rows:
        if filters.get('exclude_preview') and row.get('preview_only'):
            continue
        if filters.get('directions') and row.get('final_direction') not in filters['directions']:
            continue
        if filters.get('tiers') and row.get('tier') not in filters['tiers']:
            continue
        if _safe_num(row.get('final_score', 0)) < _safe_num(filters.get('min_score', 0)):
            continue
        if filters.get('max_exit_risk') is not None and row_exit_score(row) > _safe_num(filters.get('max_exit_risk')):
            continue
        if filters.get('min_exit_risk') is not None and row_exit_score(row) < _safe_num(filters.get('min_exit_risk')):
            continue
        if filters.get('exit_flags') and row_exit_flag(row) not in filters.get('exit_flags', []):
            continue
        if filters.get('min_institutional_confluence') is not None and row_institutional_confluence_score(row) < _safe_num(filters.get('min_institutional_confluence')):
            continue
        if filters.get('max_institutional_confluence') is not None and row_institutional_confluence_score(row) > _safe_num(filters.get('max_institutional_confluence')):
            continue
        if filters.get('institutional_bias_in') and row_institutional_bias(row) not in filters.get('institutional_bias_in', []):
            continue
        if filters.get('min_options_positioning') is not None and row_options_positioning_score(row) < _safe_num(filters.get('min_options_positioning')):
            continue
        if filters.get('max_options_positioning') is not None and row_options_positioning_score(row) > _safe_num(filters.get('max_options_positioning')):
            continue
        if filters.get('options_bias_in') and row_options_bias(row) not in filters.get('options_bias_in', []):
            continue
        if filters.get('trend_volume_delta_bucket_in') and row_trend_volume_delta_bucket(row) not in filters.get('trend_volume_delta_bucket_in', []):
            continue
        if filters.get('min_trend_volume_delta') is not None and row_trend_volume_delta_score(row) < _safe_num(filters.get('min_trend_volume_delta')):
            continue
        if filters.get('max_trend_volume_delta') is not None and row_trend_volume_delta_score(row) > _safe_num(filters.get('max_trend_volume_delta')):
            continue
        if filters.get('pin_risk_in') and row_pin_risk(row) not in filters.get('pin_risk_in', []):
            continue
        if filters.get('min_iob_score') is not None and row_iob_score(row) < _safe_num(filters.get('min_iob_score')):
            continue
        if filters.get('iob_state_in') and row_iob_state(row) not in filters.get('iob_state_in', []):
            continue
        if filters.get('iob_bias_in') and row_iob_bias(row) not in filters.get('iob_bias_in', []):
            continue
        if filters.get('max_iob_distance_pct') is not None and abs(row_iob_distance_pct(row)) > _safe_num(filters.get('max_iob_distance_pct')):
            continue
        if filters.get('min_iob_confidence') is not None and row_iob_confidence(row) < _safe_num(filters.get('min_iob_confidence')):
            continue
        if filters.get('min_dark_pool_proxy') is not None and row_dark_pool_proxy_score(row) < _safe_num(filters.get('min_dark_pool_proxy')):
            continue
        if filters.get('dark_pool_bias_in') and row_dark_pool_bias(row) not in filters.get('dark_pool_bias_in', []):
            continue
        if filters.get('max_print_distance_pct') is not None and abs(row_dark_pool_distance_pct(row)) > _safe_num(filters.get('max_print_distance_pct')):
            continue
        if filters.get('min_print_memory_score') is not None and row_dark_pool_memory_score(row) < _safe_num(filters.get('min_print_memory_score')):
            continue
        if filters.get('pinning_effect_in') and row_dark_pool_pinning(row) not in filters.get('pinning_effect_in', []):
            continue
        if filters.get('options_gamma_level_in'):
            gamma = (((row.get('factor_breakdown') or {}).get('market') or {}).get('options_positioning') or {}).get('gamma_level_label') or 'moderate'
            if gamma not in filters.get('options_gamma_level_in', []):
                continue
        if filters.get('dark_pool_attraction_state_in'):
            attraction = (((row.get('factor_breakdown') or {}).get('market') or {}).get('dark_pool_proxy') or {}).get('attraction_state') or 'neutral'
            if attraction not in filters.get('dark_pool_attraction_state_in', []):
                continue
        # ---- Phase 4b: registry-driven filters via scanner_metrics ----
        sm = row.get('scanner_metrics') or {}
        if filters.get('min_volume_sentiment_conviction') is not None and float(sm.get('volume_sentiment_conviction', 0) or 0) < float(filters['min_volume_sentiment_conviction']):
            continue
        if filters.get('volume_sentiment_bias_in') and sm.get('volume_sentiment_bias') not in filters['volume_sentiment_bias_in']:
            continue
        if filters.get('volume_sentiment_regime_in') and sm.get('volume_sentiment_regime') not in filters['volume_sentiment_regime_in']:
            continue
        if filters.get('effort_vs_result_in') and sm.get('effort_vs_result_label') not in filters['effort_vs_result_in']:
            continue
        if filters.get('reaction_classification_in') and sm.get('reaction_classification') not in filters['reaction_classification_in']:
            continue
        if filters.get('min_reaction_propel_probability') is not None and float(sm.get('reaction_propel_probability', 0) or 0) < float(filters['min_reaction_propel_probability']):
            continue
        if filters.get('min_reaction_reject_probability') is not None and float(sm.get('reaction_reject_probability', 0) or 0) < float(filters['min_reaction_reject_probability']):
            continue
        if filters.get('min_reaction_chop_probability') is not None and float(sm.get('reaction_chop_probability', 0) or 0) < float(filters['min_reaction_chop_probability']):
            continue
        if filters.get('dominant_zone_tier_in') and sm.get('dominant_zone_tier') not in filters['dominant_zone_tier_in']:
            continue
        if filters.get('max_dominant_zone_distance_pct') is not None and float(sm.get('dominant_zone_distance_pct', 99) or 99) > float(filters['max_dominant_zone_distance_pct']):
            continue
        if filters.get('min_dominant_zone_evidence') is not None and float(sm.get('dominant_zone_evidence', 0) or 0) < float(filters['min_dominant_zone_evidence']):
            continue
        if filters.get('iob_reaction_classification_in') and sm.get('iob_reaction_classification') not in filters['iob_reaction_classification_in']:
            continue
        if filters.get('min_iob_volume_alignment_score') is not None and float(sm.get('iob_volume_alignment_score', 50) or 50) < float(filters['min_iob_volume_alignment_score']):
            continue
        if filters.get('options_volume_alignment_in') and sm.get('options_volume_alignment') not in filters['options_volume_alignment_in']:
            continue
        # ---- Scanner-context filters (short pressure / PVI / expirations) ----
        # Phase 26.70: `require_populated_pvi/ssp` filters out rows that
        # only carry the placeholder default (SSP=50/neutral or PVI=0/low)
        # — used for crypto presets where most rows lack a computed
        # signal and we want to surface only the ones that DO.
        if filters.get('require_populated_pvi'):
            _pvi_src = _safe_num(row.get('predicted_volume_intensity_score', 0))
            if _pvi_src <= 0.0:
                continue
        if filters.get('require_populated_ssp'):
            _ssp_src = row.get('short_selling_pressure_source')
            if _ssp_src in (None, 'unavailable', ''):
                continue
        if filters.get('min_predicted_volume_intensity') is not None and _safe_num(row.get('predicted_volume_intensity_score', 0)) < _safe_num(filters['min_predicted_volume_intensity']):
            continue
        if filters.get('predicted_volume_intensity_bucket_in') and (row.get('predicted_volume_intensity_bucket') or 'low') not in filters['predicted_volume_intensity_bucket_in']:
            continue
        if filters.get('predicted_volume_event_flag') and not row.get('predicted_volume_event_flag'):
            continue
        if filters.get('min_short_selling_pressure') is not None and _safe_num(row.get('short_selling_pressure_score', 50)) < _safe_num(filters['min_short_selling_pressure']):
            continue
        if filters.get('max_short_selling_pressure') is not None and _safe_num(row.get('short_selling_pressure_score', 50)) > _safe_num(filters['max_short_selling_pressure']):
            continue
        if filters.get('short_selling_pressure_label_in') and (row.get('short_selling_pressure_label') or 'neutral') not in filters['short_selling_pressure_label_in']:
            continue
        if filters.get('max_days_to_options_expiration') is not None:
            dte = row.get('days_to_options_expiration')
            if dte is None or _safe_num(dte, 999) > _safe_num(filters['max_days_to_options_expiration']):
                continue
        if filters.get('expiration_risk_flag') and not row.get('expiration_risk_flag'):
            continue
        if filters.get('future_forecast_ready') and not row.get('future_forecast_ready'):
            continue
        out.append(row)
    return out
