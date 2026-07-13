"""/api/predict/{symbol} - forward-price prediction endpoint."""
from __future__ import annotations

from fastapi import APIRouter, Query

from app.services.price_prediction_service import predict_price
from app.services.advanced_prediction_service import (
    predict_price_advanced,
    predict_next_day_open_direction,
)
from app.services.prediction_backtest_service import (
    record_prediction, resolve_pending, accuracy_stats, walk_forward_estimate,
)

router = APIRouter(prefix='/api/predict', tags=['predict'])


@router.get('/accuracy')
def accuracy() -> dict:
    """Phase 19: aggregate accuracy of every resolved prediction.

    Returns hit rate, MAE, signed mean error, per-confidence-bucket
    stats, and the 25 most-recent resolved predictions. Hits the
    forward-persistence layer that lives at
    `data/prediction_history.jsonl`.
    """
    # Resolve any pending predictions whose 10-day window has elapsed,
    # then return the aggregate stats.
    resolve_pending()
    return accuracy_stats()


@router.get('/walk-forward/{symbol}')
def walk_forward(
    symbol: str,
    lookback: int = Query(250, ge=30, le=2000),
    forward_days: int = Query(10, ge=1, le=60),
) -> dict:
    """Phase 19: cold-start backtest of the prediction model using
    naive directional inputs (5d momentum + 20d SMA + ATR). Acts as
    a baseline -- the live forward-persistence model is the
    authoritative accuracy report once it has resolved predictions.
    """
    return walk_forward_estimate(symbol, lookback=lookback, forward_days=forward_days)


@router.get('/advanced/{symbol}')
def predict_advanced_endpoint(
    symbol: str,
    forward_days: int = Query(10, ge=1, le=60),
    forward_hours: int | None = Query(None, ge=1, le=24,
        description='Sub-daily horizon in trading hours.  Overrides forward_days when set.'),
    market: str = Query('stocks', regex='^(stocks|crypto)$'),
) -> dict:
    """Phase 26.42: quant-grade advanced prediction engine.

    Combines:
        1. GARCH(1,1) volatility forecast (replaces ATR × √t).
        2. Bayesian inverse-variance-weighted factor drift.
        3. Hurst-exponent regime detection (trending vs mean-reverting).
        4. Reaction-clustering × Hurst conditional drift adjustment.
        5. Dealer-gamma-exposure regime sign (mean-reverting vs amplifying).
        6. IOB barrier-clipped price target.
        7. Probit direction probability P(up) = Φ(μ_post / σ_post).
        8. Bootstrap empirical 95% CI (handles fat tails / skew).

    Returns the same envelope shape as the legacy `/api/predict/{symbol}`
    plus an `engine: 'advanced'` field, `p_up` / `p_down` probabilities,
    a `directional_certainty_pct`, the full `bayesian_blend.contributions`
    breakdown, the GARCH parameters under `volatility_model`, and an
    `advanced_rank_score` (used by the leveraged-variant Advanced
    Ranking toggle).

    Predictions ARE persisted to `data/prediction_history.jsonl` the
    same way the legacy engine's are, so the forward-resolver grades
    them at horizon end.  Use `/api/predict/accuracy` to see the
    running hit-rate.
    """
    from app.services.market_activity_service import stamp_active
    stamp_active(market)
    payload = predict_price_advanced(
        symbol,
        forward_days=forward_days,
        forward_hours=forward_hours,
        market=market,
    )
    record_prediction(payload)
    return payload


@router.get('/next-day-direction/{symbol}')
def predict_next_day_direction_endpoint(
    symbol: str,
    market: str = Query('stocks', regex='^(stocks|crypto)$'),
) -> dict:
    """Phase 26.42: probability that tomorrow's OPEN is above today's CLOSE.

    Pure direction call — no price target, no range, no horizon-units
    field.  Combines:
        * Last-30-min intraday drift (closing flow).
        * Intraday VWAP deviation (order imbalance proxy).
        * Reaction-clustering at close (PROPEL / REJECT).
        * Institutional-confluence z-score (broad-market gap risk).
        * Dealer-GEX sign (positive = mean-revert overnight; negative = amplify).

    Returns `direction: 'Up' | 'Down' | 'Even'`, `p_up`, `p_down`,
    `directional_certainty_pct`, and the per-component breakdown.
    """
    from app.services.market_activity_service import stamp_active
    stamp_active(market)
    payload = predict_next_day_open_direction(symbol, market=market)
    # Next-day-open predictions ARE persisted using the same JSONL with
    # `forward_hours=8` (one overnight session ≈ 6.5 trading hours plus
    # buffer) so the resolver can grade them against the next session's
    # opening 5-min bar.  This way the accuracy report aggregates them
    # alongside the other intraday calls.
    if payload.get('status') == 'ok':
        # Synthesise the shape `record_prediction` expects.
        record_prediction({
            'status': 'ok',
            'symbol': payload['symbol'],
            'forward_days': None,
            'forward_hours': 8,
            'horizon_label': 'Next-Day Open',
            'horizon_unit_label': 'hour',
            'current_price': payload['current_price'],
            'target_price': payload['current_price'],   # no point target
            'expected_pct_move': 0.0,                    # direction-only
            'direction': payload['direction'],
            'composite_direction': payload['direction'],
            'confidence': payload['confidence'],
            'agreement_pct': None,
            'atr_pct': None,
            'engine': 'next_day_open_direction',
            'p_up': payload['p_up'],
        })
    return payload


@router.get('/{symbol}')
def predict(
    symbol: str,
    forward_days: int = Query(10, ge=1, le=60),
    forward_hours: int | None = Query(None, ge=1, le=24, description='Sub-daily horizon in trading hours (1, 5, or 10). Overrides forward_days when set.'),
    market: str = Query('stocks', regex='^(stocks|crypto)$'),
) -> dict:
    """Return a forward price-point projection blending every
    factor family the scanner computes.

    Default behaviour: 10-day projection using daily ATR + sqrt(days)
    sigma scaling (see `app/services/price_prediction_service.py`).

    Phase 26.41: pass `forward_hours=1` (or 5 / 10) to switch to the
    sub-daily intraday math path — uses 5-day/5-min ATR scaled to
    per-hour, options-gamma proximity boost, IOB target-band
    dampening, and a predictive-consensus confidence bonus.

    Every successful prediction is persisted to
    `data/prediction_history.jsonl` so the forward-resolver can grade
    it at horizon end. Use `/api/predict/accuracy` to see the
    running hit-rate.
    """
    # Phase 25: predicting on a crypto symbol counts as crypto activity
    # so the live provider cascade re-opens for the next ~10 min.
    from app.services.market_activity_service import stamp_active
    stamp_active(market)
    payload = predict_price(symbol, forward_days=forward_days, forward_hours=forward_hours, market=market)
    # Phase 19: persist for forward-accuracy tracking.
    record_prediction(payload)
    return payload
