"""Forecast-context overlay — feeds short selling pressure, predicted
volume intensity and options-expiration proximity into the future
forecast metrics instead of leaving them as display-only sidebar stats.

`apply_forecast_context(row, out)` mutates a forward-metrics dict
(fast or GARCH tier) in place:
  * adjusts each horizon's context-adjusted p_up (`p_up_ctx`) and keeps
    the base value for transparency,
  * computes squeeze-event and volatility-event probabilities,
  * applies a confidence modifier when signals conflict near expiration,
  * attaches a `forecast_context` block with human-readable explanations
    so users can SEE how each input changed the forward outlook.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger('app.forecast_context')


def _f(v, default=0.0) -> float:
    try:
        x = float(v)
        if x != x:
            return default
        return x
    except (TypeError, ValueError):
        return default


def build_forecast_context(row: dict) -> dict[str, Any]:
    """Derive the context payload from the row's scanner analytics."""
    ssp_score = _f(row.get('short_selling_pressure_score'), 50.0)
    ssp_label = str(row.get('short_selling_pressure_label') or 'neutral')
    ssp_source = str(row.get('short_selling_pressure_source') or 'unavailable')
    pvi_score = _f(row.get('predicted_volume_intensity_score'), 0.0)
    pvi_bucket = str(row.get('predicted_volume_intensity_bucket') or 'low')
    pvi_event = bool(row.get('predicted_volume_event_flag'))
    days_to_exp = row.get('days_to_options_expiration')
    exp_risk = bool(row.get('expiration_risk_flag'))
    near_exp = days_to_exp is not None and _f(days_to_exp, 999) <= 5

    mkt = ((row.get('factor_breakdown') or {}).get('market') or {})
    op_bias = str(((mkt.get('options_positioning') or {}).get('bias')) or 'neutral')

    explanations: list[str] = []
    flags: list[str] = []

    # ---- directional bias shift (applied to p_up per horizon) ----------
    bias_shift = 0.0
    if ssp_label in ('squeeze_risk_bullish', 'elevated_squeeze_watch'):
        bias_shift = 0.02 + (ssp_score - 55.0) / 100.0 * 0.06
        explanations.append(
            f'Short pressure {ssp_score:.0f} with improving tape raises squeeze-driven upside bias.')
    elif ssp_label in ('bearish_pressure',):
        bias_shift = -(0.02 + (ssp_score - 55.0) / 100.0 * 0.05)
        explanations.append(
            f'Dominant bearish short pressure {ssp_score:.0f} reduces bullish forecast confidence.')
    elif ssp_label == 'elevated':
        bias_shift = -0.01
        explanations.append('Elevated short pressure trims bullish confidence slightly.')

    # ---- squeeze probability -------------------------------------------
    squeeze = 0.0
    if ssp_score >= 55.0:
        squeeze = (ssp_score - 55.0) / 45.0 * 0.5
        if pvi_score >= 55.0:
            squeeze += (pvi_score - 55.0) / 45.0 * 0.25
        if op_bias == 'bullish':
            squeeze += 0.10
        if near_exp:
            squeeze += 0.12
        if ssp_label in ('squeeze_risk_bullish', 'elevated_squeeze_watch'):
            squeeze += 0.08
    squeeze = max(0.0, min(0.95, squeeze))
    if squeeze >= 0.35:
        explanations.append(
            f'Squeeze-event probability {squeeze * 100:.0f}% (short pressure x volume intensity'
            f'{" x expiration window" if near_exp else ""}).')

    # ---- volatility-event probability ------------------------------------
    vol_event = pvi_score / 100.0 * 0.6
    if near_exp:
        vol_event += 0.15
    if exp_risk:
        vol_event += 0.10
    if ssp_score >= 70.0 and near_exp:
        vol_event += 0.10
        flags.append('extreme_short_pressure_near_expiration')
        explanations.append('Extreme short pressure into a near-dated expiration elevates volatility-event risk and destabilises the clean forecast.')
    vol_event = max(0.0, min(0.95, vol_event))
    if pvi_event:
        explanations.append(
            f'Predicted volume intensity {pvi_score:.0f} ({pvi_bucket}) flags a likely upcoming high-volume event.')

    # ---- confidence modifier ----------------------------------------------
    confidence_mod = 1.0
    conflicting = (ssp_label in ('bearish_pressure', 'elevated')
                   and op_bias == 'bullish') or (
                   ssp_label in ('squeeze_risk_bullish',) and op_bias == 'bearish')
    if near_exp and conflicting:
        confidence_mod = 0.80
        flags.append('conflicting_signals_near_expiration')
        explanations.append('Conflicting directional signals inside the expiration window lower clean-forecast confidence.')
    elif near_exp:
        confidence_mod = 0.92
        explanations.append(
            f'Nearest options expiration in {int(_f(days_to_exp, 0))}d — pinning/hedging flows can distort short-horizon price paths.')
    if pvi_event:
        confidence_mod *= 1.05  # decisive participation signal earns a bump
    confidence_mod = max(0.6, min(1.15, confidence_mod))

    reliability = 'full'
    if ssp_source in ('partial', 'unavailable'):
        reliability = 'reduced'
        flags.append(f'short_pressure_source_{ssp_source}')
    if str(row.get('predicted_volume_intensity_bucket') or '') == 'low' and pvi_score <= 0.0:
        pass  # low-signal is fine; not a data-quality problem

    return {
        'short_pressure_effect': {
            'score': round(ssp_score, 2),
            'label': ssp_label,
            'source': ssp_source,
            'p_up_shift': round(bias_shift, 4),
        },
        'volume_intensity_effect': {
            'score': round(pvi_score, 2),
            'bucket': pvi_bucket,
            'event_flag': pvi_event,
        },
        'expiration_effect': {
            'days_to_expiration': days_to_exp,
            'high_sensitivity_window': bool(near_exp),
            'risk_flag': exp_risk,
        },
        'squeeze_probability': round(squeeze, 4),
        'volatility_event_probability': round(vol_event, 4),
        'confidence_modifier': round(confidence_mod, 4),
        'reliability': reliability,
        'flags': flags,
        'explanations': explanations,
    }


def apply_forecast_context(row: dict, out: dict | None) -> dict | None:
    """Mutate a forward-metrics dict with the scanner-context overlay."""
    if not isinstance(out, dict) or not out:
        return out
    try:
        ctx = build_forecast_context(row)
        shift = _f((ctx.get('short_pressure_effect') or {}).get('p_up_shift'))
        conf_mod = _f(ctx.get('confidence_modifier'), 1.0)
        for key, blk in out.items():
            if not isinstance(blk, dict) or key == 'forecast_context':
                continue
            base_p = blk.get('p_up_cf', blk.get('p_up'))
            if base_p is None:
                continue
            base_p = _f(base_p, 0.5)
            adj = max(0.02, min(0.98, base_p + shift))
            blk['p_up_ctx'] = round(adj, 4)
            blk['p_up_ctx_shift'] = round(shift, 4)
            cert_key = 'directional_certainty_cf' if 'directional_certainty_cf' in blk else 'directional_certainty'
            cert = _f(blk.get(cert_key), 0.0)
            blk['directional_certainty_ctx'] = round(max(0.0, min(1.0, cert * conf_mod)), 4)
            blk['squeeze_probability'] = ctx['squeeze_probability']
            blk['volatility_event_probability'] = ctx['volatility_event_probability']
        out['forecast_context'] = ctx
        return out
    except Exception as exc:  # noqa: BLE001
        log.debug('forecast context overlay failed: %s', exc)
        return out


def summarize_forecast(row: dict) -> str | None:
    """One-line human summary for the row's `future_forecast_summary`."""
    fm = row.get('forward_metrics_garch') or row.get('forward_metrics')
    if not isinstance(fm, dict):
        return None
    blk = fm.get('forward_1d') or {}
    if not isinstance(blk, dict) or not blk:
        return None
    ctx = fm.get('forecast_context') or {}
    p_up = _f(blk.get('p_up_ctx', blk.get('p_up_cf', blk.get('p_up'))), 0.5)
    direction = 'Bullish' if p_up >= 0.55 else 'Bearish' if p_up <= 0.45 else 'Neutral'
    parts = [f'{direction} 1d P(up) {p_up * 100:.0f}%']
    sq = _f(ctx.get('squeeze_probability'))
    ve = _f(ctx.get('volatility_event_probability'))
    if sq >= 0.35:
        parts.append(f'squeeze {sq * 100:.0f}%')
    if ve >= 0.45:
        parts.append(f'vol-event {ve * 100:.0f}%')
    if ctx.get('reliability') == 'reduced':
        parts.append('reduced-confidence')
    return ' · '.join(parts)
