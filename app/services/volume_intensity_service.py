"""Predicted volume intensity — forward-looking high-volume-event estimator.

NOT a restatement of current volume ratio: blends participation
acceleration, pre-breakout compression + participation creep, options
activity concentration and options-expiration proximity into a single
0-100 score with a bucket label and an event-likelihood flag.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger('app.volume_intensity')

UNAVAILABLE_VOLUME_INTENSITY: dict[str, Any] = {
    'score': 0.0,
    'bucket': 'low',
    'event_flag': False,
    'reasons': [],
    'source': 'unavailable',
    'status': 'unavailable',
    'components': {},
}

_BUCKETS = ((75.0, 'extreme'), (55.0, 'high'), (35.0, 'moderate'), (0.0, 'low'))
EVENT_FLAG_THRESHOLD = 65.0


def _safe(v, default=0.0) -> float:
    try:
        f = float(v)
        if f != f:
            return default
        return f
    except (TypeError, ValueError):
        return default


def bucket_for(score: float) -> str:
    for threshold, name in _BUCKETS:
        if score >= threshold:
            return name
    return 'low'


def compute_predicted_volume_intensity(
    symbol: str,
    last_price: float,
    info: dict | None = None,
    daily_hist=None,
    options_payload: dict | None = None,
    days_to_expiration: int | None = None,
) -> dict[str, Any]:
    """Return the predicted_volume_intensity family payload. Never raises."""
    info = info or {}
    components: dict[str, float] = {}
    reasons: list[str] = []
    source = 'unavailable'

    # ---- intraday participation (quote-level, always cheap) --------------
    vol = _safe(info.get('volume'))
    avg_vol = _safe(info.get('averageVolume'))
    if vol > 0 and avg_vol > 0:
        ratio = vol / avg_vol
        components['live_participation'] = round(min(100.0, max(0.0, (ratio - 0.6) * 55.0)), 2)
        if ratio >= 1.8:
            reasons.append(f'live volume running {ratio:.1f}x average')
        source = 'proxy'

    hist_ok = False
    try:
        if daily_hist is not None and not getattr(daily_hist, 'empty', True) and len(daily_hist) >= 21:
            closes = daily_hist['Close'].astype(float).tolist()
            highs = daily_hist['High'].astype(float).tolist() if 'High' in daily_hist else closes
            lows = daily_hist['Low'].astype(float).tolist() if 'Low' in daily_hist else closes
            vols = daily_hist['Volume'].astype(float).tolist() if 'Volume' in daily_hist else []
            n = len(closes)
            hist_ok = True

            if vols and n >= 21:
                base20 = sum(vols[-21:-1]) / 20.0
                recent5 = sum(vols[-5:]) / 5.0
                if base20 > 0:
                    accel = recent5 / base20
                    components['participation_acceleration'] = round(
                        min(100.0, max(0.0, (accel - 0.7) * 70.0)), 2)
                    if accel >= 1.5:
                        reasons.append(f'5d participation {accel:.1f}x its 20d base')
                    # Participation creep: volume trending up bar-over-bar.
                    ups = sum(1 for i in range(n - 5, n) if i > 0 and vols[i] > vols[i - 1])
                    components['participation_creep'] = round(min(100.0, ups * 22.0), 2)
                    if ups >= 4:
                        reasons.append('volume creeping up 4+ consecutive sessions')

            # Pre-breakout compression: 5d true-range vs 20d true-range.
            def _tr(i: int) -> float:
                return max(highs[i] - lows[i],
                           abs(highs[i] - closes[i - 1]) if i > 0 else 0.0,
                           abs(lows[i] - closes[i - 1]) if i > 0 else 0.0)
            trs = [_tr(i) for i in range(1, n)]
            if len(trs) >= 20 and closes[-1] > 0:
                tr5 = sum(trs[-5:]) / 5.0
                tr20 = sum(trs[-20:]) / 20.0
                if tr20 > 0:
                    compression = tr5 / tr20
                    if compression < 0.75:
                        comp_score = min(100.0, (0.75 - compression) * 250.0)
                        components['range_compression'] = round(comp_score, 2)
                        reasons.append('range compressing ahead of a potential expansion')
                        creep = components.get('participation_creep', 0.0)
                        if creep >= 40.0:
                            components['compression_plus_creep'] = round(
                                min(100.0, comp_score * 0.5 + creep * 0.6), 2)
                            reasons.append('compression + participation creep (pre-event signature)')

            # Catalyst-like instability: outsized |returns| in the last 3 bars.
            rets = [abs((closes[i] - closes[i - 1]) / closes[i - 1]) * 100.0
                    for i in range(1, n) if closes[i - 1] > 0]
            if len(rets) >= 20:
                base = sum(rets[-21:-1]) / 20.0
                recent = max(rets[-3:]) if rets[-3:] else 0.0
                if base > 0 and recent / base >= 2.0:
                    components['instability'] = round(min(100.0, (recent / base) * 18.0), 2)
                    reasons.append('recent price instability suggests a live catalyst')
            source = 'derived'
    except Exception as exc:  # noqa: BLE001
        log.debug('volume intensity hist compute failed for %s: %s', symbol, exc)

    # ---- options activity concentration -----------------------------------
    try:
        op = options_payload or {}
        composite = op.get('composite') or {}
        pc_vol = _safe(composite.get('put_call_vol_ratio'))
        pin_risk = str(op.get('pin_risk') or 'low')
        if op.get('expirations_used'):
            conc = 0.0
            if pc_vol > 0 and pc_vol < 900 and (pc_vol <= 0.55 or pc_vol >= 1.8):
                conc += 40.0
                reasons.append('one-sided options volume concentration')
            if pin_risk in ('high', 'moderate'):
                conc += 30.0 if pin_risk == 'high' else 15.0
                reasons.append(f'{pin_risk} pin risk near max-pain')
            if conc > 0:
                components['options_concentration'] = round(min(100.0, conc), 2)
    except Exception:  # noqa: BLE001
        pass

    # ---- options expiration proximity -------------------------------------
    if days_to_expiration is not None and days_to_expiration >= 0:
        if days_to_expiration <= 2:
            components['expiration_proximity'] = 85.0
            reasons.append(f'options expiration in {days_to_expiration}d (hedging/pinning flows)')
        elif days_to_expiration <= 5:
            components['expiration_proximity'] = 55.0
            reasons.append(f'options expiration in {days_to_expiration}d')
        elif days_to_expiration <= 10:
            components['expiration_proximity'] = 25.0

    if not components:
        return dict(UNAVAILABLE_VOLUME_INTENSITY)

    weight_map = {
        'live_participation': 0.20,
        'participation_acceleration': 0.22,
        'participation_creep': 0.10,
        'range_compression': 0.12,
        'compression_plus_creep': 0.14,
        'instability': 0.08,
        'options_concentration': 0.07,
        'expiration_proximity': 0.07,
    }
    total_w = sum(weight_map.get(k, 0.05) for k in components)
    score = sum(components[k] * weight_map.get(k, 0.05) for k in components) / total_w if total_w > 0 else 0.0
    score = max(0.0, min(100.0, score))
    bucket = bucket_for(score)
    event_flag = score >= EVENT_FLAG_THRESHOLD

    return {
        'score': round(score, 2),
        'bucket': bucket,
        'event_flag': bool(event_flag),
        'reasons': reasons[:5],
        'source': source if hist_ok else ('proxy' if source != 'unavailable' else 'unavailable'),
        'status': 'implemented',
        'components': components,
    }
