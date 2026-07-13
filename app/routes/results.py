from fastapi import APIRouter, Query
from app.models.results import StockResultsEnvelope
from app.services.result_store import get_results_batch
from app.services.warmer_service import set_warmer_market

router = APIRouter(prefix='/stocks', tags=['stocks'])


@router.get('/results', response_model=StockResultsEnvelope)
def get_stock_results(
    batch: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=100),
    preset: str | None = Query(None),
    direction: str | None = Query(None),
    tier: str | None = Query(None),
    min_score: float | None = Query(None, ge=0, le=100),
    max_exit_risk: float | None = Query(None, ge=0, le=100),
    exit_flag: str | None = Query(None),
    market: str | None = Query('stocks'),
    sort_by: str | None = Query(None),
    sort_dir: str | None = Query('desc'),
    # Extended filter params (new factor families)
    min_institutional_confluence: float | None = Query(None, ge=0, le=100),
    max_institutional_confluence: float | None = Query(None, ge=0, le=100),
    institutional_bias_in: str | None = Query(None, description='Comma-separated: bullish,bearish,neutral'),
    min_options_positioning: float | None = Query(None, ge=0, le=100),
    max_options_positioning: float | None = Query(None, ge=0, le=100),
    options_bias_in: str | None = Query(None),
    pin_risk_in: str | None = Query(None),
    options_gamma_level_in: str | None = Query(None),
    min_trend_volume_delta: float | None = Query(None, ge=0, le=100),
    max_trend_volume_delta: float | None = Query(None, ge=0, le=100),
    trend_volume_delta_bucket_in: str | None = Query(None),
    min_iob_score: float | None = Query(None, ge=0, le=100),
    iob_state_in: str | None = Query(None),
    iob_bias_in: str | None = Query(None),
    max_iob_distance_pct: float | None = Query(None, ge=0, le=100),
    min_iob_confidence: float | None = Query(None, ge=0, le=100),
    min_dark_pool_proxy: float | None = Query(None, ge=0, le=100),
    dark_pool_bias_in: str | None = Query(None),
    dark_pool_attraction_state_in: str | None = Query(None),
    max_print_distance_pct: float | None = Query(None, ge=0, le=100),
    min_print_memory_score: float | None = Query(None, ge=0, le=100),
    pinning_effect_in: str | None = Query(None),
    # Phase 4b: Volume sentiment + reaction-clustering filters
    min_volume_sentiment_conviction: float | None = Query(None, ge=0, le=100),
    volume_sentiment_bias_in: str | None = Query(None),
    volume_sentiment_regime_in: str | None = Query(None),
    effort_vs_result_in: str | None = Query(None),
    reaction_classification_in: str | None = Query(None, description='PROPEL,REJECT,CHOP,NEUTRAL'),
    min_reaction_propel_probability: float | None = Query(None, ge=0, le=1),
    min_reaction_reject_probability: float | None = Query(None, ge=0, le=1),
    min_reaction_chop_probability: float | None = Query(None, ge=0, le=1),
    dominant_zone_tier_in: str | None = Query(None, description='MAJOR,INTERMEDIATE,MINOR'),
    max_dominant_zone_distance_pct: float | None = Query(None, ge=0, le=100),
    min_dominant_zone_evidence: float | None = Query(None, ge=0, le=100),
    iob_reaction_classification_in: str | None = Query(None),
    min_iob_volume_alignment_score: float | None = Query(None, ge=0, le=100),
    options_volume_alignment_in: str | None = Query(None),
    # Scanner-context filters (short pressure / predicted volume intensity / expirations)
    min_predicted_volume_intensity: float | None = Query(None, ge=0, le=100),
    predicted_volume_intensity_bucket_in: str | None = Query(None, description='low,moderate,high,extreme'),
    predicted_volume_event_flag: bool | None = Query(None),
    min_short_selling_pressure: float | None = Query(None, ge=0, le=100),
    max_short_selling_pressure: float | None = Query(None, ge=0, le=100),
    short_selling_pressure_label_in: str | None = Query(None),
    max_days_to_options_expiration: float | None = Query(None, ge=0, le=365),
    expiration_risk_flag: bool | None = Query(None),
    future_forecast_ready: bool | None = Query(None),
):
    set_warmer_market(market or 'stocks')

    def _split(value: str | None) -> list[str] | None:
        if not value:
            return None
        return [v.strip() for v in value.split(',') if v.strip()]

    extra_filters = {
        'min_institutional_confluence': min_institutional_confluence,
        'max_institutional_confluence': max_institutional_confluence,
        'institutional_bias_in': _split(institutional_bias_in),
        'min_options_positioning': min_options_positioning,
        'max_options_positioning': max_options_positioning,
        'options_bias_in': _split(options_bias_in),
        'pin_risk_in': _split(pin_risk_in),
        'options_gamma_level_in': _split(options_gamma_level_in),
        'min_trend_volume_delta': min_trend_volume_delta,
        'max_trend_volume_delta': max_trend_volume_delta,
        'trend_volume_delta_bucket_in': _split(trend_volume_delta_bucket_in),
        'min_iob_score': min_iob_score,
        'iob_state_in': _split(iob_state_in),
        'iob_bias_in': _split(iob_bias_in),
        'max_iob_distance_pct': max_iob_distance_pct,
        'min_iob_confidence': min_iob_confidence,
        'min_dark_pool_proxy': min_dark_pool_proxy,
        'dark_pool_bias_in': _split(dark_pool_bias_in),
        'dark_pool_attraction_state_in': _split(dark_pool_attraction_state_in),
        'max_print_distance_pct': max_print_distance_pct,
        'min_print_memory_score': min_print_memory_score,
        'pinning_effect_in': _split(pinning_effect_in),
        # Phase 4b
        'min_volume_sentiment_conviction': min_volume_sentiment_conviction,
        'volume_sentiment_bias_in': _split(volume_sentiment_bias_in),
        'volume_sentiment_regime_in': _split(volume_sentiment_regime_in),
        'effort_vs_result_in': _split(effort_vs_result_in),
        'reaction_classification_in': _split(reaction_classification_in),
        'min_reaction_propel_probability': min_reaction_propel_probability,
        'min_reaction_reject_probability': min_reaction_reject_probability,
        'min_reaction_chop_probability': min_reaction_chop_probability,
        'dominant_zone_tier_in': _split(dominant_zone_tier_in),
        'max_dominant_zone_distance_pct': max_dominant_zone_distance_pct,
        'min_dominant_zone_evidence': min_dominant_zone_evidence,
        'iob_reaction_classification_in': _split(iob_reaction_classification_in),
        'min_iob_volume_alignment_score': min_iob_volume_alignment_score,
        'options_volume_alignment_in': _split(options_volume_alignment_in),
        # Scanner-context filters
        'min_predicted_volume_intensity': min_predicted_volume_intensity,
        'predicted_volume_intensity_bucket_in': _split(predicted_volume_intensity_bucket_in),
        'predicted_volume_event_flag': (predicted_volume_event_flag or None),
        'min_short_selling_pressure': min_short_selling_pressure,
        'max_short_selling_pressure': max_short_selling_pressure,
        'short_selling_pressure_label_in': _split(short_selling_pressure_label_in),
        'max_days_to_options_expiration': max_days_to_options_expiration,
        'expiration_risk_flag': (expiration_risk_flag or None),
        'future_forecast_ready': (future_forecast_ready or None),
    }
    extra_filters = {k: v for k, v in extra_filters.items() if v is not None}

    payload = get_results_batch(
        batch=batch,
        limit=limit,
        preset=preset,
        direction=direction,
        tier=tier,
        min_score=min_score,
        max_exit_risk=max_exit_risk,
        exit_flag=exit_flag,
        market=market,
        sort_by=sort_by,
        sort_dir=sort_dir,
        extra_filters=extra_filters,
    )
    return StockResultsEnvelope(**payload)
