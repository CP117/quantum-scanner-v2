"""
Orchestrator that computes the five Ultra-Scanner V1 factor families and
folds them into a row's `factor_breakdown.market` payload.

Factor families:
  1. trend_volume_delta
  2. institutional_confluence    (multi-component: rrg, flow, regime, liquidity, session)
  3. options_positioning         (real chain via yfinance for active-scan, inferred fallback)
  4. institutional_order_block   (V1 heuristic: impulse + retest of impulse origin)
  5. dark_pool_proxy             (V1 heuristic: turnover/absorption near print clusters)

Factor functions themselves live in `scoring_service` (they were already
implemented in the source zip).  This module is the thin orchestration layer
that:

  * calls them with `safe_float` defensive accessors,
  * unifies their output shape under stable keys,
  * stamps a `provenance` label per family so the UI can show real vs inferred,
  * never raises (always returns the canonical unavailable sentinel on failure).

Keeping this orchestration here means `scoring_service.score_from_prices()`
can call ONE function to populate all five families instead of weaving the
five calls through the legacy scoring path.
"""
from __future__ import annotations

import logging
from typing import Any

from app.utils.normalize import UNAVAILABLE_FACTOR_PAYLOAD

log = logging.getLogger('app.factors')


def _safe(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        result = float(value)
        if result != result:
            return default
        return result
    except (TypeError, ValueError):
        return default


def _options_gamma_level_label(score: float | None) -> str:
    """Map an options-positioning score onto a stable gamma-level label.

    The label is consumed by `scanner_metrics.options_gamma_level` and by
    the detail panel.  Keeping this in one place prevents the KeyError that
    bit the prior build (`gamma_level_label` referenced but never written).
    """
    s = _safe(score, 50.0)
    if s >= 70:
        return 'high_call_pressure'
    if s >= 58:
        return 'mild_call_pressure'
    if s <= 30:
        return 'high_put_pressure'
    if s <= 42:
        return 'mild_put_pressure'
    return 'moderate'


def _inferred_options_positioning(last_price: float, prev_close: float, info: dict | None) -> dict:
    """Inferred fallback used when real option_chain data is unavailable.

    Heuristic: combine session pressure with realized-volatility proxy and
    relative-volume to produce a directional positioning score.  Clearly
    labeled as `provenance=inferred` so the UI can present it as such.
    """
    info = info or {}
    open_p = _safe(info.get('open'))
    day_low = _safe(info.get('dayLow'))
    day_high = _safe(info.get('dayHigh'))
    volume = _safe(info.get('volume'))
    avg_volume = _safe(info.get('averageVolume'))
    range_pct = ((day_high - day_low) / last_price) * 100.0 if last_price and day_high > day_low else 0.0
    intraday_pct = ((last_price - open_p) / open_p) * 100.0 if open_p else 0.0
    rel_vol = (volume / avg_volume) if avg_volume > 0 else 1.0
    change_pct = ((last_price - prev_close) / prev_close) * 100.0 if prev_close else 0.0
    directional = max(-25.0, min(25.0, change_pct * 4.0 + intraday_pct * 2.0))
    score = max(0.0, min(100.0, 50.0 + directional + (rel_vol - 1.0) * 5.0 - max(0.0, range_pct - 5.0) * 2.0))
    bias = 'bullish' if score >= 58 else 'bearish' if score <= 42 else 'neutral'
    pin_risk = 'high' if abs(intraday_pct) <= 0.4 and range_pct <= 2.0 else 'moderate' if range_pct <= 4.0 else 'low'
    return {
        'score': round(score, 2),
        'bias': bias,
        'status': 'inferred',
        'provenance': 'inferred',
        'gamma_level_label': _options_gamma_level_label(score),
        'pin_risk': pin_risk,
        'composite': {
            'target_price': None,
            'call_wall': None,
            'put_wall': None,
            'bias': bias,
            'pressure_score': round(score, 2),
        },
        'near_term': {},
        'monthly': {},
        'expirations_used': 0,
        'inferred_inputs': {
            'change_pct': round(change_pct, 4),
            'intraday_pct': round(intraday_pct, 4),
            'range_pct': round(range_pct, 4),
            'relative_volume': round(rel_vol, 3),
        },
    }


def _safe_call(name: str, fn, *args, **kwargs) -> dict:
    try:
        result = fn(*args, **kwargs)
        if not isinstance(result, dict):
            log.warning('factor %s returned non-dict: %r', name, type(result))
            return dict(UNAVAILABLE_FACTOR_PAYLOAD)
        return result
    except Exception as exc:  # noqa: BLE001
        log.warning('factor %s raised: %s', name, exc)
        payload = dict(UNAVAILABLE_FACTOR_PAYLOAD)
        payload['status'] = f'error:{type(exc).__name__}'
        return payload


def compute_extended_factors(
    symbol: str,
    last_price: float,
    prev_close: float,
    info: dict | None,
    *,
    market: str = 'stocks',
    use_real_options: bool = False,
    hist: Any = None,
    daily_hist: Any = None,
) -> dict[str, dict]:
    """Compute and unify the five extended factor families plus the Phase 4b
    volume sentiment profile and reaction map.

    Returns a dict keyed by family name.  Each value is a dict with at least
    ``score``, ``bias``, ``status``, and ``provenance``.  Never raises.

    Args:
      daily_hist: optional 90d/1d OHLCV DataFrame used for the volume-sentiment
        profile and reaction-clustering engine.  When omitted, those payloads
        degrade to the canonical UNAVAILABLE sentinel and downstream consumers
        (IOB modulation, options pressure adjustment) skip the modulation step.
    """
    # Lazy import to avoid circular import (scoring_service imports this module).
    from app.services.scoring_service import (
        trend_volume_delta_pct,
        trend_volume_delta_score,
        trend_volume_delta_bucket,
        institutional_confluence_factor,
        options_positioning_factor,
        institutional_order_block_factor,
        dark_pool_proxy_factor,
    )
    from app.services.volume_sentiment import compute_volume_sentiment, UNAVAILABLE_VOLUME_SENTIMENT
    from app.services.reaction_clustering_service import compute_reaction_map, UNAVAILABLE_REACTION_MAP

    info = info or {}
    open_p = _safe(info.get('open'))
    day_low = _safe(info.get('dayLow'))
    day_high = _safe(info.get('dayHigh'))
    volume = _safe(info.get('volume'))
    avg_volume = _safe(info.get('averageVolume'))
    market_cap = _safe(info.get('marketCap'))
    change_pct = ((last_price - prev_close) / prev_close) * 100.0 if prev_close else 0.0

    # Phase 25: crypto has no central order-book volume, no consolidated
    # tape, and no listed options chains.  The three families below
    # therefore have NO meaningful crypto signal — they previously sat at
    # `status='inferred'` or 'implemented' but always reported neutral,
    # dragging the row's confidence score down to "low confidence" on
    # every crypto symbol.  Mark them not_applicable so the confidence
    # audit + composite scoring math excludes them entirely for crypto.
    # TODO_crypto_native: revisit with perp-futures funding rates +
    # exchange-aggregated volume profiles for true crypto-native versions.
    is_crypto = (market or '').lower() == 'crypto'
    if is_crypto:
        NA_FACTOR = {
            'score': None,
            'bias': 'n/a',
            'status': 'not_applicable',
            'provenance': 'crypto_no_equivalent',
            'reason': 'No equivalent metric exists in spot crypto markets',
        }

    # --- Phase 4b: Volume sentiment + reaction clustering (shared substrate)
    if is_crypto:
        # Phase 25: crypto has no consolidated tape — exchange-level volume
        # data is too fragmented across spot venues for the volume-sentiment
        # / reaction-clustering math to mean anything.  Skip entirely.
        vs = dict(NA_FACTOR)
        rmap = dict(NA_FACTOR)
    elif daily_hist is not None:
        vs = compute_volume_sentiment(daily_hist)
        rmap = compute_reaction_map(symbol, last_price, daily_hist, vs)
    else:
        vs = dict(UNAVAILABLE_VOLUME_SENTIMENT)
        rmap = dict(UNAVAILABLE_REACTION_MAP)

    # --- trend_volume_delta -----------------------------------------------
    tvd_pct = trend_volume_delta_pct(change_pct, volume, avg_volume)
    tvd = {
        'score': round(_safe(trend_volume_delta_score(tvd_pct), 50.0), 2),
        'bias': 'bullish' if tvd_pct > 0 else 'bearish' if tvd_pct < 0 else 'neutral',
        'bucket': trend_volume_delta_bucket(tvd_pct),
        'delta_pct': round(tvd_pct, 2),
        'status': 'implemented',
        'provenance': 'derived',
    }

    # --- institutional_confluence ----------------------------------------
    # Prefer daily history (90 bars) over intraday for the RRG/regime/ATR
    # math.  Intraday `hist` (5m bars) is rarely 25+ rows, so it usually
    # short-circuits to "insufficient_history".  Daily history reliably
    # has 60+ bars.
    icf_source = daily_hist if daily_hist is not None else hist
    icf = _safe_call('institutional_confluence', institutional_confluence_factor, symbol, info, icf_source)
    icf.setdefault('provenance', 'derived' if icf.get('status') == 'implemented_from_icm' else 'inferred')

    # --- options_positioning ---------------------------------------------
    if is_crypto:
        # Phase 25: spot crypto has no listed options chain.  Return the
        # not_applicable sentinel so the rating system + confidence audit
        # skip this family entirely instead of penalising every crypto row.
        op = dict(NA_FACTOR)
        op.update({
            'composite': {'target_price': None, 'call_wall': None, 'put_wall': None, 'bias': 'n/a', 'pressure_score': None},
            'near_term': {},
            'monthly': {},
            'expirations_used': 0,
            'gamma_level_label': 'n/a',
            'pin_risk': 'n/a',
        })
    elif use_real_options:
        # Use the new options_chain_service (TTL cache + per-symbol cooldown
        # + concurrency cap).  Falls back to inferred when chain is missing.
        try:
            from app.services.options_chain_service import get_real_options_positioning
            real_op = get_real_options_positioning(symbol, last_price)
        except Exception as exc:  # noqa: BLE001
            log.debug('real options fetch failed for %s: %s', symbol, exc)
            real_op = None
        if real_op:
            op = real_op
        else:
            op = _inferred_options_positioning(last_price, prev_close, info)
    else:
        op = _inferred_options_positioning(last_price, prev_close, info)
    # Always guarantee `gamma_level_label` so the registry never KeyErrors.
    op.setdefault('gamma_level_label', _options_gamma_level_label(op.get('score')))
    op.setdefault('pin_risk', 'low')
    op.setdefault('bias', 'neutral')
    op.setdefault('status', 'inferred')
    op.setdefault('provenance', 'inferred')

    # --- Phase 4b: volume-sentiment modulation of options pressure --------
    # The same sentiment substrate that drives the reaction classifier also
    # adjusts the options price-pressure read.  Alignment amplifies, divergence
    # dampens.  The original `score` is preserved; we add a `pressure_score_adjusted`.
    op_score = _safe(op.get('score'), 50.0)
    op_bias = op.get('bias', 'neutral')
    vs_bias = vs.get('bias', 'neutral')
    vs_conviction = _safe(vs.get('conviction_score'), 0.0)
    vs_status = vs.get('status', 'unavailable')
    if vs_status == 'implemented':
        if op_bias == vs_bias and op_bias != 'neutral':
            # Aligned: amplify away from 50 by up to +15 (or -15 if both bearish)
            amplification = (vs_conviction / 100.0) * 15.0
            direction = 1.0 if op_bias == 'bullish' else -1.0
            pressure_score_adjusted = max(0.0, min(100.0, op_score + direction * amplification))
            volume_alignment = 'aligned'
        elif (op_bias == 'bullish' and vs_bias == 'bearish') or (op_bias == 'bearish' and vs_bias == 'bullish'):
            # Divergence: pull score back toward 50 by up to 12
            dampening = (vs_conviction / 100.0) * 12.0
            if op_score > 50:
                pressure_score_adjusted = max(50.0, op_score - dampening)
            else:
                pressure_score_adjusted = min(50.0, op_score + dampening)
            volume_alignment = 'divergent'
        else:
            pressure_score_adjusted = op_score
            volume_alignment = 'mixed'
    else:
        pressure_score_adjusted = op_score
        volume_alignment = 'unavailable'
    composite = op.get('composite') or {}
    composite['pressure_score_adjusted'] = round(pressure_score_adjusted, 2)
    composite['volume_alignment'] = volume_alignment
    composite['volume_sentiment_bias'] = vs_bias
    composite['volume_sentiment_conviction'] = round(vs_conviction, 1)
    op['composite'] = composite
    op['pressure_score_adjusted'] = round(pressure_score_adjusted, 2)
    op['volume_alignment'] = volume_alignment

    # --- institutional_order_block ---------------------------------------
    # Prefer daily history when available — the heuristic needs 20+ bars to
    # detect a meaningful order block, and intraday hist usually has fewer.
    iob_hist = daily_hist if daily_hist is not None else hist
    iob = _safe_call(
        'institutional_order_block',
        institutional_order_block_factor,
        last_price, day_low, day_high, volume, avg_volume, iob_hist,
    )
    iob.setdefault('status', 'implemented' if iob.get('score') is not None else 'unavailable')
    iob.setdefault('provenance', 'heuristic_v1')
    iob.setdefault('state', 'unavailable' if iob.get('status') == 'unavailable' else iob.get('state', 'unknown'))

    # --- Phase 4b: volume sentiment + reaction map modulation of IOB ------
    # When the dominant zone (from the reaction-clustering engine) is the
    # IOB zone OR very close to it, inherit the propel/reject/chop classification.
    # Otherwise compute a standalone expected reaction for the IOB zone from the
    # volume sentiment substrate.
    iob_low = _safe(iob.get('zone_low'))
    iob_high = _safe(iob.get('zone_high'))
    iob_mid = _safe(iob.get('midpoint'))
    expected_reaction = {
        'propel_probability': 0.33, 'reject_probability': 0.33, 'chop_probability': 0.34,
        'classification': 'NEUTRAL', 'volume_alignment': 'unavailable',
        'source': 'fallback',
    }
    if iob.get('status') == 'implemented' and iob_mid > 0 and rmap.get('status') == 'implemented':
        dz = rmap.get('dominant_zone') or {}
        dz_mid = _safe(dz.get('midpoint'))
        # If the IOB midpoint is within 2% of the dominant zone midpoint,
        # treat them as the same level and inherit the classification.
        if dz_mid > 0 and abs(dz_mid - iob_mid) / max(iob_mid, 1e-6) <= 0.02:
            expected_reaction = {
                'propel_probability': rmap['propel_probability'],
                'reject_probability': rmap['reject_probability'],
                'chop_probability': rmap['chop_probability'],
                'classification': rmap['classification'],
                'volume_alignment': rmap.get('volume_sentiment_alignment', 'mixed'),
                'source': 'reaction_map_inherited',
            }
        else:
            # Compute a zone-specific reaction prediction for the IOB midpoint
            # using the same classifier with a synthesized one-zone payload.
            from app.services.reaction_clustering_service import _classify_dominant_zone
            iob_zone = {
                'low': iob_low or iob_mid,
                'high': iob_high or iob_mid,
                'midpoint': iob_mid,
                'tier': 'INTERMEDIATE' if _safe(iob.get('respect_rate')) >= 0.55 else 'MINOR',
                'touch_count': int(_safe(iob.get('touch_count'), 0)),
            }
            try:
                cls_, p, r, c, align = _classify_dominant_zone(last_price, iob_zone, vs)
                expected_reaction = {
                    'propel_probability': p,
                    'reject_probability': r,
                    'chop_probability': c,
                    'classification': cls_,
                    'volume_alignment': align,
                    'source': 'iob_specific',
                }
            except Exception:
                pass
    iob['expected_reaction'] = expected_reaction
    iob['reaction_classification'] = expected_reaction['classification']
    # A 0-100 volume-alignment score per IOB row, suitable for filter/sort
    alignment_score_map = {
        'aligned_propel': 78.0, 'aligned_reject': 78.0,
        'diverging': 35.0, 'mixed': 50.0,
        'aligned': 70.0, 'divergent': 30.0, 'unavailable': 50.0,
    }
    iob['volume_alignment_score'] = alignment_score_map.get(expected_reaction['volume_alignment'], 50.0)

    # --- dark_pool_proxy --------------------------------------------------
    dp = _safe_call(
        'dark_pool_proxy',
        dark_pool_proxy_factor,
        last_price, open_p, prev_close, day_low, day_high, volume, avg_volume, market_cap,
        daily_hist if daily_hist is not None else hist,
    )
    dp.setdefault('status', 'implemented' if dp.get('score') is not None else 'unavailable')
    dp.setdefault('provenance', 'heuristic_v1')
    attraction = 'attracting' if (dp.get('bias') == 'bullish' and _safe(dp.get('score')) >= 60) else \
                 'repelling' if (dp.get('bias') == 'bearish' and _safe(dp.get('score')) >= 60) else \
                 'neutral'
    dp['attraction_state'] = dp.get('attraction_state', attraction)

    return {
        'trend_volume_delta': tvd,
        'institutional_confluence': icf,
        'options_positioning': op,
        'institutional_order_block': iob,
        'dark_pool_proxy': dp,
        'volume_sentiment': vs,
        'reaction_map': rmap,
    }
