"""Phase 26.61 — Metrics Hub routes.

Three endpoints:

  * GET  /api/metrics_hub/status   — full snapshot (algorithms, caches,
                                     provider health, lane status,
                                     phase_2660 registry, current weights).
  * GET  /api/metrics_hub/weights  — just the persisted weight overrides
                                     + defaults (lighter payload for the
                                     weight-tuner UI).
  * POST /api/metrics_hub/weights  — save sanitised overrides; returns
                                     the SAVED payload (post-clamp).
  * POST /api/metrics_hub/weights/reset — wipe overrides; return defaults.
"""
from __future__ import annotations

from fastapi import APIRouter, Body

from app.services.metrics_hub_service import (
    get_default_weights,
    get_hub_status,
    load_weights,
    reset_weights,
    save_weights,
)

router = APIRouter()


@router.get('/api/metrics_hub/status')
def metrics_hub_status() -> dict:
    """Full Metrics Hub status payload."""
    return get_hub_status()


@router.get('/api/metrics_hub/weights')
def metrics_hub_weights_get() -> dict:
    """Return persisted weight overrides + defaults (for reset/UX)."""
    return {
        'weights': load_weights(),
        'defaults': get_default_weights(),
    }


@router.post('/api/metrics_hub/weights')
def metrics_hub_weights_set(payload: dict = Body(...)) -> dict:
    """Persist a new set of weight overrides.  The payload is
    sanitised (clamped to spec ranges; unknown keys dropped) before
    being written.  Returns the SAVED payload (post-clamp)."""
    saved = save_weights(payload or {})
    return {
        'weights': saved,
        'defaults': get_default_weights(),
        'persisted': True,
    }


@router.post('/api/metrics_hub/weights/reset')
def metrics_hub_weights_reset() -> dict:
    """Wipe all user overrides and return the default weight matrix."""
    defaults = reset_weights()
    return {
        'weights': defaults,
        'defaults': defaults,
        'persisted': False,
    }


@router.get('/api/metrics_hub/preview_snapshot')
def metrics_hub_preview_snapshot(limit: int = 50) -> dict:
    """Thinned snapshot for the Weight Tuner's live-preview table.

    Returns the top-N rows by current effective Kelly rank along with
    the 8 multiplier values + 12 pillar-score values that feed into
    the ranking pipeline.  The frontend uses this to compute a
    *projected* re-rank on slider drag — entirely client-side, so the
    main scoring pipeline is never touched until the user clicks Save.

    Payload per row:
      * symbol, final_score, last_price, direction
      * forward_metrics[forward_1h]:
          - effective_kelly_rank, direction_cf
          - lab_rank_multiplier, strategy_rank_multiplier,
            strategy_v2_rank_multiplier, regime_risk_multiplier,
            liq_kelly_factor, ml_rank_multiplier,
            reality_breaker_multiplier
      * factor_breakdown.market.* scores for the 12 pillars
    """
    from app.services.snapshot_store import get_snapshot
    import time as _t

    limit = max(1, min(200, int(limit or 50)))
    try:
        snap = get_snapshot('stocks', limit=limit, compact=False)
    except Exception:  # noqa: BLE001
        return {'rows': [], 'error': 'snapshot unavailable', 'generated_at_ms': int(_t.time() * 1000)}

    out_rows = []
    for r in (snap.get('results') or [])[:limit]:
        if not r or not r.get('symbol'):
            continue
        fm_block = ((r.get('forward_metrics_garch') or {}).get('forward_1h')
                   or (r.get('forward_metrics') or {}).get('forward_1h')
                   or {})
        market = ((r.get('factor_breakdown') or {}).get('market') or {})
        def _score(key):
            v = market.get(key)
            if isinstance(v, dict):
                s = v.get('score')
                return float(s) if isinstance(s, (int, float)) else 0.0
            return float(v) if isinstance(v, (int, float)) else 0.0
        out_rows.append({
            'symbol':       r.get('symbol'),
            'final_score':  float(r.get('final_score') or 0.0),
            'last_price':   float(r.get('last_price') or 0.0),
            'direction':    r.get('direction') or 'Neutral',
            'rating':       r.get('rating') or '',
            'forward_metrics': {
                'effective_kelly_rank':         float(fm_block.get('effective_kelly_rank') or 0.0),
                'direction_cf':                 fm_block.get('direction_cf') or fm_block.get('direction') or 'Neutral',
                'drift_pct':                    float(fm_block.get('drift_pct') or 0.0),
                'sigma_pct':                    float(fm_block.get('sigma_pct') or 0.0),
                # 7 ranking-pipeline multipliers (defaults to 1.0 — neutral)
                'lab_rank_multiplier':          float(fm_block.get('lab_rank_multiplier') or 1.0),
                'strategy_rank_multiplier':     float(fm_block.get('strategy_rank_multiplier') or 1.0),
                'strategy_v2_rank_multiplier':  float(fm_block.get('strategy_v2_rank_multiplier') or 1.0),
                'regime_risk_multiplier':       float(fm_block.get('regime_risk_multiplier') or 1.0),
                'liq_kelly_factor':             float(fm_block.get('liq_kelly_factor') or 1.0),
                'ml_rank_multiplier':           float(fm_block.get('ml_rank_multiplier') or 1.0),
                'reality_breaker_multiplier':   float(fm_block.get('reality_breaker_multiplier') or 1.0),
            },
            'pillar_scores': {
                'momentum_strength':         _score('momentum_strength'),
                'trend_volume_delta':        _score('trend_volume_delta'),
                'institutional_confluence':  _score('institutional_confluence'),
                'options_positioning':       _score('options_positioning'),
                'institutional_order_block': _score('institutional_order_block'),
                'dark_pool_attraction':      _score('dark_pool_attraction'),
                'reaction_clustering':       _score('reaction_clustering'),
                'volume_sentiment':          _score('volume_sentiment'),
                'effort_vs_result':          _score('effort_vs_result'),
                'predictive_consensus':      _score('predictive_consensus'),
                'fundamentals':              _score('fundamentals'),
                'regulatory_signal':         _score('regulatory_signal'),
            },
        })
    return {
        'rows':            out_rows,
        'limit':           limit,
        'snapshot_meta':   snap.get('meta', {}),
        'generated_at_ms': int(_t.time() * 1000),
    }
