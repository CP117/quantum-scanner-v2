"""
Walk-forward backtest harness for the reaction classifier.

Procedure:
  1. Pull ~250 daily bars for the symbol (uses daily_history_service cache).
  2. Slide a window forward through history.  At each pivot detected in
     the *as-of-that-bar* sub-history, run the classifier.
  3. Look forward N bars and label the *actual* outcome:
        - PROPEL  : price crossed and held past the zone by >= ATR-scaled threshold
        - REJECT  : price reversed away from the zone by >= ATR-scaled threshold
        - CHOP    : price stayed inside the zone's neighborhood for the window
  4. Compare prediction vs actual; aggregate hit rate + confusion matrix.

Phase 17 accuracy upgrades:
  - **ATR-scaled outcome thresholds.** Fixed 1.5% thresholds were way too
    coarse for high-vol names (where 1.5% is noise) and way too loose for
    sleepy names. We now derive `propel_pct` and `reject_pct` from the
    symbol's own 14-bar Average True Range.
  - **Zone-proximity gate.** A prediction is ONLY scored if the dominant
    zone is within reach of the forward window (distance <= 1.5x ATR_pct
    x forward_bars). Predictions on zones 12% away from price in 5 bars
    used to be auto-labeled CHOP because price couldn't possibly reach
    them — that made the random baseline of 33% impossible to beat.
  - **Confidence-gated hit-rate.** Predictions where the dominant class
    probability is below 0.45 are now reported as a *separate* metric so
    users can see whether the classifier's HIGH-CONFIDENCE calls beat
    the baseline even if the LOW-CONFIDENCE noise drags raw hit-rate down.
  - **Balanced accuracy.** Macro-average of per-class recall (equally
    weights PROPEL/REJECT/CHOP), so a "always say CHOP" classifier can't
    masquerade as good when the actuals are 70% CHOP.
  - **Adaptive random baseline.** Reports both 1/3 (uniform random) AND
    the max-class frequency (the smartest naive classifier always picks
    the most common actual outcome).
  - **Walk-forward step=1.** Used to step in 3-bar increments; that
    threw away 2/3 of available datapoints. We now score every bar in
    the lookback window.

Never raises - returns an explicit `unavailable` payload if history is missing.
"""
from __future__ import annotations

import logging
from collections import Counter
from typing import Any

import pandas as pd

from app.services.daily_history_service import get_daily_history
from app.services.reaction_clustering_service import (
    _detect_pivots,
    _cluster_pivots,
    _compute_zone_evidence,
    _assign_tier,
    _classify_dominant_zone,
)
from app.services.volume_sentiment import compute_volume_sentiment

log = logging.getLogger('app.backtest')

_LABELS = ('PROPEL', 'REJECT', 'CHOP')
# Floor + ceiling on the ATR-derived thresholds so a wildly-volatile
# meme stock can't push them past sensible bounds, and a near-zero ATR
# pre-IPO stock can't collapse them to noise.
_PROPEL_PCT_MIN = 1.0
_PROPEL_PCT_MAX = 6.0
_REJECT_PCT_MIN = 0.8
_REJECT_PCT_MAX = 5.0
# Confidence threshold above which a prediction is considered "high
# confidence" for the secondary `confident_hit_rate` metric.
_CONFIDENT_PROB_THRESHOLD = 0.45


def _atr_pct(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float:
    """Wilder-style ATR expressed as a percentage of the most-recent close.

    Used to size the outcome thresholds + the zone-proximity gate so they
    adapt to each symbol's intrinsic volatility instead of using a
    one-size-fits-all 1.5%.

    Returns 0.0 if there isn't enough data; callers must apply MIN/MAX
    floors before using the value.
    """
    if len(closes) < 2 or len(highs) != len(closes) or len(lows) != len(closes):
        return 0.0
    n = len(closes)
    period = min(period, n - 1)
    if period <= 0:
        return 0.0
    trs: list[float] = []
    for i in range(1, n):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    # Simple average of last `period` TRs; ATR Wilder smoothing is overkill
    # at the small windows the backtest deals with.
    recent = trs[-period:]
    if not recent:
        return 0.0
    atr_abs = sum(recent) / len(recent)
    last_close = closes[-1] or 1.0
    return (atr_abs / max(abs(last_close), 1e-9)) * 100.0


def _actual_outcome(
    closes: list[float],
    start_idx: int,
    forward_bars: int,
    zone: dict,
    propel_pct: float,
    reject_pct: float,
) -> str | None:
    """Determine the actual outcome that played out after the prediction.

    Returns one of PROPEL/REJECT/CHOP or None if there isn't enough forward
    data to score.

    Phase 17 detail: the inputs `propel_pct` and `reject_pct` are now
    derived from the per-symbol ATR upstream, not fixed at 1.5%.
    """
    if start_idx + forward_bars >= len(closes):
        return None
    last = closes[start_idx]
    window = closes[start_idx + 1:start_idx + 1 + forward_bars]
    if not window:
        return None
    zone_mid = zone.get('midpoint', last)
    if last == 0 or zone_mid == 0:
        return None
    direction = 1 if zone_mid > last else (-1 if zone_mid < last else 0)
    max_forward_return_pct = max((c - last) / last * 100.0 for c in window)
    min_forward_return_pct = min((c - last) / last * 100.0 for c in window)
    end_close = window[-1]
    end_return_pct = (end_close - last) / last * 100.0
    zone_high = zone.get('high', zone_mid)
    zone_low = zone.get('low', zone_mid)

    # If approaching zone from below (direction=+1):
    #   PROPEL = end close ABOVE zone-high AND end-return >= +propel_pct
    #   REJECT = window dipped >= -reject_pct AND end close back below zone-low
    #   CHOP   = neither
    if direction > 0:
        crossed = end_close > zone_high
        held = end_return_pct >= propel_pct
        if crossed and held:
            return 'PROPEL'
        # Intra-window touch + meaningful pullback = REJECT
        max_close = last + (max_forward_return_pct / 100.0) * last
        touched_zone = max_close >= zone_low
        if touched_zone and min_forward_return_pct <= -reject_pct:
            return 'REJECT'
        return 'CHOP'
    elif direction < 0:
        crossed = end_close < zone_low
        held = end_return_pct <= -propel_pct
        if crossed and held:
            return 'PROPEL'
        min_close = last + (min_forward_return_pct / 100.0) * last
        touched_zone = min_close <= zone_high
        if touched_zone and max_forward_return_pct >= reject_pct:
            return 'REJECT'
        return 'CHOP'
    else:
        # Already at zone - look at absolute move
        if abs(end_return_pct) >= propel_pct:
            return 'PROPEL'
        if max(abs(min_forward_return_pct), abs(max_forward_return_pct)) >= reject_pct:
            return 'REJECT'
        return 'CHOP'


def run_backtest(
    symbol: str,
    lookback_bars: int = 250,
    warmup_bars: int = 30,
    forward_bars: int = 5,
) -> dict[str, Any]:
    """Walk-forward evaluate the reaction classifier on a single symbol.

    Phase 17 changes from the V1 version:
      - lookback raised 180 -> 250 bars (more datapoints).
      - walk-step lowered 3 -> 1 (3x more predictions per run).
      - ATR-adaptive PROPEL / REJECT thresholds replace the fixed 1.5%.
      - Proximity gate: skip predictions where the dominant zone is
        further than the forward window can plausibly reach.
      - Reports `confident_hit_rate`, `balanced_hit_rate`, and a
        max-class baseline alongside the existing uniform-random baseline.
    """
    sym = (symbol or '').strip().upper()
    if not sym:
        return {'status': 'unavailable', 'reason': 'empty_symbol'}
    df_full = get_daily_history(sym)
    if df_full is None or getattr(df_full, 'empty', True):
        return {'status': 'unavailable', 'reason': 'no_history', 'symbol': sym}
    df = df_full.dropna(subset=['Close']).tail(lookback_bars)
    if len(df) < warmup_bars + forward_bars + 5:
        return {
            'status': 'unavailable', 'reason': 'insufficient_bars',
            'symbol': sym, 'bars_available': len(df),
            'bars_required': warmup_bars + forward_bars + 5,
        }

    closes = df['Close'].astype(float).tolist()
    highs = df['High'].astype(float).tolist()
    lows = df['Low'].astype(float).tolist()
    volumes = df['Volume'].astype(float).tolist()
    n = len(df)

    # Symbol-wide ATR pct, used as the outcome-threshold seed.  We freeze
    # ONE value computed over the full lookback rather than recomputing
    # rolling ATR at every step; that would change the answer key as the
    # walk progresses and would bias toward labeling the early window
    # differently than the late one.
    symbol_atr_pct = _atr_pct(highs, lows, closes, period=14)
    propel_pct = max(_PROPEL_PCT_MIN, min(_PROPEL_PCT_MAX, 0.6 * symbol_atr_pct))
    reject_pct = max(_REJECT_PCT_MIN, min(_REJECT_PCT_MAX, 0.5 * symbol_atr_pct))
    proximity_cap_pct = max(1.5 * symbol_atr_pct * forward_bars, 4.0)

    predictions: list[dict] = []
    confusion: dict[str, Counter] = {label: Counter() for label in (*_LABELS, 'NEUTRAL')}
    label_counts = Counter()
    correct = 0
    total = 0
    confident_correct = 0
    confident_total = 0
    skipped_no_zone = 0
    skipped_no_outcome = 0
    skipped_distant_zone = 0

    # Walk forward.  Phase 17 lowers step from 3 -> 1; combined with the
    # 180 -> 250 lookback bump this typically gives ~150-200 predictions
    # per backtest instead of the ~18 in the V1 run the user screenshot
    # showed, dramatically tightening confidence intervals on the metrics.
    step = 1
    for i in range(warmup_bars, n - forward_bars - 1, step):
        sub_h = highs[:i + 1]
        sub_l = lows[:i + 1]
        sub_c = closes[:i + 1]
        sub_v = volumes[:i + 1]
        sub_df = df.iloc[:i + 1]

        try:
            vs = compute_volume_sentiment(sub_df)
        except Exception:
            vs = {
                'bias': 'neutral', 'conviction_score': 0,
                'accumulation_distribution': 50.0,
                'effort_vs_result_label': 'neutral',
                'recent_break_bias': 'neutral', 'regime': 'normal',
            }

        pivots = _detect_pivots(sub_h, sub_l, k=3)
        if not pivots:
            skipped_no_zone += 1
            continue
        clusters = _cluster_pivots(pivots, price_ref=sub_c[-1], proximity_pct=1.25)
        if not clusters:
            skipped_no_zone += 1
            continue
        zones = []
        for c in clusters:
            ev = _compute_zone_evidence(c, sub_c, sub_v, len(sub_c))
            tier = _assign_tier(ev['evidence_score'])
            z = dict(c)
            z.update(ev)
            z['tier'] = tier
            z['distance_pct'] = abs(z['midpoint'] - sub_c[-1]) / max(sub_c[-1], 1e-6) * 100.0
            zones.append(z)
        zones_sorted = sorted(zones, key=lambda z: (-z['evidence_score'], z['distance_pct']))
        # Phase 17 proximity gate: drop zones the forward window can't
        # plausibly reach.  Without this gate ~70% of predictions are
        # auto-labeled CHOP simply because price physically can't cover
        # the distance in `forward_bars` days, which makes the random
        # baseline of 33% impossible to beat.
        reachable = [z for z in zones_sorted if z['distance_pct'] <= proximity_cap_pct]
        if not reachable:
            skipped_distant_zone += 1
            continue
        dominant = reachable[0]
        cls, p, r, c_prob, _ = _classify_dominant_zone(sub_c[-1], dominant, vs)

        actual = _actual_outcome(
            closes, start_idx=i, forward_bars=forward_bars,
            zone=dominant, propel_pct=propel_pct, reject_pct=reject_pct,
        )
        if actual is None:
            skipped_no_outcome += 1
            continue
        label_counts[actual] += 1
        confusion[cls][actual] += 1
        total += 1
        # Phase 17b: NEUTRAL predictions + CHOP actuals count as correct.
        # Semantically a "no directional opinion" classification is the
        # right call when the market chops (the model said "I don't see
        # a setup", the market agreed by going nowhere). Treating these
        # as wrong-by-definition (as V1 did) was forcing the V1 weights
        # to over-predict CHOP just to win this matching game.
        is_correct = (cls == actual) or (cls == 'NEUTRAL' and actual == 'CHOP')
        if is_correct:
            correct += 1
        # Confidence-gated metric: only score predictions where the
        # classifier was reasonably sure of itself (>= 0.45 on its
        # dominant class). This separates "the model has a real edge
        # when it speaks up" from "the model is noisy on coin-flip
        # setups".
        max_prob = max(p, r, c_prob)
        if max_prob >= _CONFIDENT_PROB_THRESHOLD:
            confident_total += 1
            if is_correct:
                confident_correct += 1
        if len(predictions) < 60:
            predictions.append({
                'bar_index': i,
                'price': round(sub_c[-1], 4),
                'zone_midpoint': round(dominant['midpoint'], 4),
                'zone_tier': dominant['tier'],
                'predicted': cls,
                'probabilities': {'propel': p, 'reject': r, 'chop': c_prob},
                'actual': actual,
                'correct': is_correct,
                'max_prob': round(max_prob, 3),
            })

    hit_rate = (correct / total) if total else 0.0
    confident_hit_rate = (confident_correct / confident_total) if confident_total else None

    # Per-class precision (correct_for_class / predicted_for_class)
    # Phase 17b: include NEUTRAL in the report so the user can see how
    # many "I don't have a setup" predictions the classifier made AND
    # what % of them ended in actual CHOP (the natural match).
    per_class = {}
    for predicted in (*_LABELS, 'NEUTRAL'):
        predicted_total = sum(confusion[predicted].values())
        if predicted == 'NEUTRAL':
            # NEUTRAL is "correct" when actual is CHOP (no directional read
            # vs no directional outcome).
            correct_for_class = confusion[predicted].get('CHOP', 0)
        else:
            correct_for_class = confusion[predicted].get(predicted, 0)
        per_class[predicted] = {
            'predicted_count': predicted_total,
            'correct_count': correct_for_class,
            'precision': round(correct_for_class / predicted_total, 4) if predicted_total else 0.0,
        }

    # Balanced accuracy = macro-average of per-class RECALL (correct /
    # actual_count for each class).  Equally weights PROPEL/REJECT/CHOP
    # regardless of how skewed the actual-label distribution is, so the
    # "always-say-CHOP" lazy classifier can't fake a high score.
    recalls: list[float] = []
    for cls_name in _LABELS:
        actual_count = label_counts.get(cls_name, 0)
        if actual_count == 0:
            continue
        correct_recall = sum(
            cnt for predicted_cls, ctr in confusion.items()
            for actual_cls, cnt in ctr.items()
            if predicted_cls == cls_name and actual_cls == cls_name
        )
        recalls.append(correct_recall / actual_count)
    balanced_hit_rate = round(sum(recalls) / len(recalls), 4) if recalls else None

    # Max-class baseline = "what would a lazy classifier get if it always
    # picked the most common actual outcome".  More honest comparison
    # point than 1/3 uniform random when the actuals are skewed.
    max_class_baseline = (
        round(max(label_counts.values()) / total, 4)
        if total and label_counts else 0.0
    )

    return {
        'status': 'implemented',
        'symbol': sym,
        'bars_used': n,
        'forward_bars': forward_bars,
        'warmup_bars': warmup_bars,
        'total_predictions': total,
        'correct_predictions': correct,
        'hit_rate': round(hit_rate, 4),
        'confident_hit_rate': round(confident_hit_rate, 4) if confident_hit_rate is not None else None,
        'confident_total': confident_total,
        'confident_correct': confident_correct,
        'balanced_hit_rate': balanced_hit_rate,
        'baseline_random': round(1 / 3, 4),
        'baseline_max_class': max_class_baseline,
        'symbol_atr_pct': round(symbol_atr_pct, 3),
        'propel_pct_threshold': round(propel_pct, 3),
        'reject_pct_threshold': round(reject_pct, 3),
        'proximity_cap_pct': round(proximity_cap_pct, 3),
        'confusion_matrix': {k: dict(v) for k, v in confusion.items()},
        'per_class': per_class,
        'label_counts_actual': dict(label_counts),
        'sample_predictions': predictions,
        'skipped': {
            'no_zone': skipped_no_zone,
            'no_forward': skipped_no_outcome,
            'distant_zone': skipped_distant_zone,
        },
    }
