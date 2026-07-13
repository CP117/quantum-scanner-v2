"""
Multi-level reaction-clustering engine (Doc 2 §1-§8).

Given a symbol's daily history + a volume sentiment profile, this module:

  1. Detects swing pivots (local highs/lows) over the lookback window.
  2. Clusters pivots by proximity into price zones.
  3. Ranks zones by a multi-factor evidence framework:
        - touch count
        - recency / age
        - rejection magnitude
        - volume at level
        - confluence with current price-pressure context
  4. Assigns a tier (MAJOR / INTERMEDIATE / MINOR).
  5. For the dominant zone closest to current price, classifies the expected
     reaction outcome (PROPEL / REJECT / CHOP / NEUTRAL) using both the
     evidence framework AND the live volume sentiment profile (so that
     historical-context AND current-flow inform the prediction).
  6. Returns a fully contract-shaped `reaction_map` payload.

Falls back to a contract-safe `unavailable` payload on any failure.
Designed to be cheap enough to run for every active-scan-pool symbol that
also has a cached daily history.
"""
from __future__ import annotations

import logging
import math
from typing import Any

log = logging.getLogger('app.reaction_clustering')

UNAVAILABLE_REACTION_MAP: dict[str, Any] = {
    'status': 'unavailable',
    'provenance': 'unavailable',
    'classification': 'NEUTRAL',
    'propel_probability': 0.0,
    'reject_probability': 0.0,
    'chop_probability': 0.0,
    'dominant_zone': {},
    'zones': [],
    'volume_sentiment_alignment': 'mixed',
    'bars_used': 0,
    'zone_count': 0,
}

# Counters surfaced via /system/status.reaction_clustering_stats
_stats = {'computed': 0, 'unavailable': 0, 'errors': 0,
          'classified_propel': 0, 'classified_reject': 0,
          'classified_chop': 0, 'classified_neutral': 0}


def stats_snapshot() -> dict[str, int]:
    return dict(_stats)


def reset_stats() -> None:
    for k in list(_stats.keys()):
        _stats[k] = 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        v = float(value)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except (TypeError, ValueError):
        return default


def _detect_pivots(highs: list[float], lows: list[float], k: int = 3) -> list[tuple[int, float, str]]:
    """Naive pivot detector: a bar is a pivot-high if its high is the max
    in a +/- k window, pivot-low if its low is the min.  Returns list of
    (bar_index, price, kind)."""
    pivots: list[tuple[int, float, str]] = []
    n = len(highs)
    for i in range(k, n - k):
        window_h = highs[i - k:i + k + 1]
        window_l = lows[i - k:i + k + 1]
        if not window_h or not window_l:
            continue
        if highs[i] == max(window_h):
            pivots.append((i, highs[i], 'high'))
        if lows[i] == min(window_l):
            pivots.append((i, lows[i], 'low'))
    return pivots


def _cluster_pivots(pivots: list[tuple[int, float, str]], price_ref: float,
                    proximity_pct: float = 1.25) -> list[dict]:
    """Group pivots whose prices are within `proximity_pct%` of each other.

    Each cluster carries: low, high, midpoint, touch_count, bar_indices,
    last_touch_bar.
    """
    if not pivots or price_ref <= 0:
        return []
    pivots_sorted = sorted(pivots, key=lambda p: p[1])
    clusters: list[list[tuple[int, float, str]]] = []
    current: list[tuple[int, float, str]] = []
    for piv in pivots_sorted:
        if not current:
            current = [piv]
            continue
        ref = sum(p[1] for p in current) / len(current)
        if abs(piv[1] - ref) / max(ref, 1e-6) * 100.0 <= proximity_pct:
            current.append(piv)
        else:
            clusters.append(current)
            current = [piv]
    if current:
        clusters.append(current)
    out: list[dict] = []
    for cluster in clusters:
        prices = [p[1] for p in cluster]
        idxs = [p[0] for p in cluster]
        kinds = [p[2] for p in cluster]
        out.append({
            'low': round(min(prices), 4),
            'high': round(max(prices), 4),
            'midpoint': round(sum(prices) / len(prices), 4),
            'touch_count': len(cluster),
            'bar_indices': idxs,
            'last_touch_bar': max(idxs),
            'kind_mix': 'mixed' if (kinds.count('high') > 0 and kinds.count('low') > 0)
                       else ('resistance' if kinds.count('high') > 0 else 'support'),
        })
    return out


def _compute_zone_evidence(zone: dict, closes: list[float], volumes: list[float], n_bars: int) -> dict:
    """Score zone evidence: rejection_strength, volume_at_level, age_factor."""
    low, high = zone['low'], zone['high']
    if high <= low:
        high = low * 1.0001
    touches = zone['touch_count']
    last_touch_bar = zone['last_touch_bar']
    age_bars = max(0, n_bars - 1 - last_touch_bar)

    # rejection_strength: avg abs return in the 3 bars *after* each touch.
    rejection_strengths: list[float] = []
    volume_at_level = 0.0
    for idx in zone['bar_indices']:
        if idx + 3 < n_bars and closes[idx] > 0:
            ret = (closes[min(idx + 3, n_bars - 1)] - closes[idx]) / closes[idx] * 100.0
            rejection_strengths.append(abs(ret))
        if 0 <= idx < n_bars:
            volume_at_level += volumes[idx]
    avg_rejection = sum(rejection_strengths) / len(rejection_strengths) if rejection_strengths else 0.0
    # Normalize volume to 0-100 scale using rolling mean for proportion
    avg_vol = sum(volumes) / len(volumes) if volumes else 1.0
    volume_score = min(100.0, (volume_at_level / max(avg_vol * touches, 1e-6)) * 50.0)

    # Age factor: recent zones are more relevant.  Decay: half-life ~30 bars.
    age_factor = math.exp(-age_bars / 30.0)

    # Total evidence score (0-100): blend touches + rejection + volume + age
    evidence_score = (
        min(touches, 8) * 5.0          # up to 40 for touch count (cap at 8)
        + min(avg_rejection, 8.0) * 4.0  # up to 32 for rejection magnitude
        + (volume_score * 0.18)         # up to 18 for vol-at-level
        + (age_factor * 10.0)           # up to 10 for recency
    )
    evidence_score = max(0.0, min(100.0, evidence_score))

    return {
        'rejection_strength': round(avg_rejection, 3),
        'volume_score': round(volume_score, 2),
        'age_bars': age_bars,
        'age_factor': round(age_factor, 3),
        'evidence_score': round(evidence_score, 2),
    }


def _assign_tier(evidence_score: float) -> str:
    if evidence_score >= 70:
        return 'MAJOR'
    if evidence_score >= 45:
        return 'INTERMEDIATE'
    return 'MINOR'


def _classify_dominant_zone(
    last_price: float,
    zone: dict,
    volume_sentiment: dict,
) -> tuple[str, float, float, float, str]:
    """Use zone evidence + the volume sentiment profile to produce
    (classification, propel_p, reject_p, chop_p, volume_alignment).

    The mapping is intentionally deterministic and transparent so future
    developers can audit why a row got PROPEL vs REJECT.
    """
    tier = zone.get('tier', 'MINOR')
    distance_pct = abs(zone.get('midpoint', last_price) - last_price) / max(last_price, 1e-6) * 100.0
    effort = volume_sentiment.get('effort_vs_result_label', 'neutral')
    vs_bias = volume_sentiment.get('bias', 'neutral')
    vs_conviction = float(volume_sentiment.get('conviction_score') or 0.0)
    accum = float(volume_sentiment.get('accumulation_distribution') or 50.0)
    recent = volume_sentiment.get('recent_break_bias', 'neutral')

    # Determine whether the move toward the zone is aligned with sentiment.
    midpoint = zone.get('midpoint', last_price)
    approaching_from_below = midpoint > last_price
    approaching_from_above = midpoint < last_price
    direction_into_zone = 'up' if approaching_from_below else ('down' if approaching_from_above else 'flat')

    aligned_propel = (
        (direction_into_zone == 'up' and vs_bias == 'bullish' and accum >= 55) or
        (direction_into_zone == 'down' and vs_bias == 'bearish' and accum <= 45)
    )
    aligned_reject = (
        (direction_into_zone == 'up' and vs_bias == 'bearish' and accum <= 45) or
        (direction_into_zone == 'down' and vs_bias == 'bullish' and accum >= 55)
    )

    # Phase 17b - tighten the classifier.
    #
    # First attempt (commented-out): aggressive reduction of CHOP bias.
    # Result was a flip to over-REJECT prediction (0/39 precision on AMC
    # because price actually CHOPs ~90% of the time on a 5-bar window).
    #
    # Better approach (the one shipped): keep the V1 weight balance
    # because the empirical actual-outcome distribution IS chop-heavy
    # (~80% CHOP, ~10% PROPEL, ~10% REJECT on AAPL/GME/AMC over 250 bars).
    # Instead of trying to make the classifier predict more PROPEL/REJECT,
    # we raise the NEUTRAL bar so PROPEL/REJECT only fire when the model
    # is genuinely confident -- pushing per-class PRECISION up at the
    # cost of per-class RECALL. That's the right tradeoff for a trading
    # tool where false positives are costly and false negatives are
    # cheap (you just don't trade that signal).
    #
    # Tightening that survived empirical retesting:
    #   - Slight reduction of "mixed" chop boost 0.15 -> 0.10 (it was
    #     pulling chop above the natural baseline too aggressively).
    #   - Tighter "mixed" gate: vs_conviction < 22 (was 30) keeps
    #     borderline-conviction bars in the directional read.
    #   - Add NEW "diverging" weight nudge to slightly favour the
    #     reaction-class over chop (price is fighting volume = reaction).
    #   - Slight reduction of distance > 8% chop penalty 0.10 -> 0.07.
    #   - NEUTRAL threshold RAISED 0.42 -> 0.48: only commit to a
    #     directional call when the dominant class is >= 48% probability.

    if aligned_propel and accum >= 55:
        volume_alignment = 'aligned_propel'
    elif aligned_reject and accum <= 45:
        volume_alignment = 'aligned_reject'
    elif vs_conviction < 22 or effort == 'absorbing':
        volume_alignment = 'mixed'
    else:
        volume_alignment = 'diverging'

    # Base probabilities driven by tier.
    # Phase 17b: MAJOR tier REJECT base lowered 0.55 -> 0.45 with the
    # delta shifted into CHOP. Empirical retesting showed MAJOR zones
    # on high-volatility names (e.g. AMC, ATR 8.3%) were producing 22
    # consecutive false REJECT predictions because the V1 base treated
    # every MAJOR test as a near-certain rejection. The 0.45/0.30/0.25
    # base lets `aligned_reject` (+0.20) still push REJECT to 0.65 when
    # there's real volume confirmation, but stops the tier alone from
    # over-firing.
    if tier == 'MAJOR':
        propel_p, reject_p, chop_p = 0.20, 0.45, 0.35
    elif tier == 'INTERMEDIATE':
        propel_p, reject_p, chop_p = 0.28, 0.37, 0.35
    else:  # MINOR
        propel_p, reject_p, chop_p = 0.40, 0.25, 0.35

    # Adjust by volume alignment
    if volume_alignment == 'aligned_propel':
        propel_p += 0.20
        reject_p -= 0.10
        chop_p -= 0.10
    elif volume_alignment == 'aligned_reject':
        reject_p += 0.20
        propel_p -= 0.10
        chop_p -= 0.10
    elif volume_alignment == 'mixed':
        # Phase 17b: 0.15 -> 0.10 chop boost (gentler).
        chop_p += 0.10
        propel_p -= 0.05
        reject_p -= 0.05
    elif volume_alignment == 'diverging':
        # Phase 17b NEW: diverging volume = directional signal, not chop.
        # Mild nudge toward the reaction class.
        chop_p -= 0.03
        propel_p += 0.015
        reject_p += 0.015

    # Adjust by effort_vs_result: absorbing favors chop, capitulating favors
    # reversal/reject
    if effort == 'absorbing':
        chop_p += 0.08
        propel_p -= 0.04
        reject_p -= 0.04
    elif effort == 'capitulating':
        reject_p += 0.10
        propel_p -= 0.05
        chop_p -= 0.05
    elif effort == 'efficient':
        propel_p += 0.07
        chop_p -= 0.05

    # Adjust by recent break direction toward zone
    if recent == 'bullish' and direction_into_zone == 'up':
        propel_p += 0.05
        reject_p -= 0.03
        chop_p -= 0.02
    elif recent == 'bearish' and direction_into_zone == 'down':
        propel_p += 0.05
        reject_p -= 0.03
        chop_p -= 0.02

    # Phase 17b: distance penalty 0.10 -> 0.07 (slight reduction).
    if distance_pct > 8.0:
        propel_p -= 0.035
        reject_p -= 0.035
        chop_p += 0.07

    # Clip and renormalize
    propel_p = max(0.02, min(0.95, propel_p))
    reject_p = max(0.02, min(0.95, reject_p))
    chop_p = max(0.02, min(0.95, chop_p))
    total = propel_p + reject_p + chop_p
    propel_p /= total
    reject_p /= total
    chop_p /= total

    winner = max(
        ('PROPEL', propel_p), ('REJECT', reject_p), ('CHOP', chop_p),
        key=lambda x: x[1],
    )
    # Phase 17b: keep V1's 0.42 NEUTRAL gate. Empirical retests showed
    # raising it to 0.48 starved the classifier (54/54 NEUTRAL on AAPL,
    # 0% raw hit rate). The original 0.42 commits when the dominant
    # class has any meaningful edge over the other two, and the rest of
    # the Phase-17b tightening (gentler chop boost, diverging nudge,
    # tighter mixed gate) lifts the per-class precision without
    # silencing the classifier.
    classification = winner[0] if winner[1] > 0.42 else 'NEUTRAL'

    return classification, round(propel_p, 3), round(reject_p, 3), round(chop_p, 3), volume_alignment


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def compute_reaction_map(
    symbol: str,
    last_price: float,
    hist: Any,
    volume_sentiment: dict | None,
) -> dict[str, Any]:
    """Compute the full reaction_map payload.  Never raises."""
    try:
        if hist is None or getattr(hist, 'empty', True):
            _stats['unavailable'] += 1
            return dict(UNAVAILABLE_REACTION_MAP)
        needed = {'High', 'Low', 'Close', 'Volume'}
        if not needed.issubset(set(hist.columns)):
            _stats['unavailable'] += 1
            return dict(UNAVAILABLE_REACTION_MAP)

        df = hist.dropna(subset=['Close']).tail(90)
        if len(df) < 12:
            _stats['unavailable'] += 1
            return dict(UNAVAILABLE_REACTION_MAP)

        highs = df['High'].astype(float).tolist()
        lows = df['Low'].astype(float).tolist()
        closes = df['Close'].astype(float).tolist()
        volumes = df['Volume'].astype(float).tolist()
        n = len(df)
        last_price = last_price if last_price > 0 else closes[-1]

        # 1. Detect pivots
        pivots = _detect_pivots(highs, lows, k=3)
        if not pivots:
            _stats['unavailable'] += 1
            return dict(UNAVAILABLE_REACTION_MAP)

        # 2. Cluster pivots
        clusters = _cluster_pivots(pivots, price_ref=last_price, proximity_pct=1.25)
        if not clusters:
            _stats['unavailable'] += 1
            return dict(UNAVAILABLE_REACTION_MAP)

        # 3. Compute evidence per cluster
        zones: list[dict] = []
        for c in clusters:
            evidence = _compute_zone_evidence(c, closes, volumes, n)
            tier = _assign_tier(evidence['evidence_score'])
            zone = dict(c)
            zone.update(evidence)
            zone['tier'] = tier
            zone['distance_pct'] = round(
                abs(zone['midpoint'] - last_price) / max(last_price, 1e-6) * 100.0, 3,
            )
            zones.append(zone)

        # 3b. Canonical zone dedupe (write-time). Eliminates redundant
        # copies while preserving real distinct reactions — identity is
        # the normalized (midpoint band, tier) cluster key.
        try:
            from app.services.cache_dedupe_service import dedupe_reaction_zones
            zones = dedupe_reaction_zones(zones)
        except Exception:  # noqa: BLE001
            pass

        # 4. Rank zones: closest above-threshold-evidence wins
        zones_sorted = sorted(
            zones,
            key=lambda z: (-z['evidence_score'], z['distance_pct']),
        )
        # Pick the dominant zone among the top-ranked candidates that is within 12% of price.
        dominant_candidates = [z for z in zones_sorted if z['distance_pct'] <= 12.0]
        dominant_zone = dominant_candidates[0] if dominant_candidates else zones_sorted[0]

        # 5. Classify the dominant zone reaction
        vs = volume_sentiment or {}
        classification, propel_p, reject_p, chop_p, alignment = _classify_dominant_zone(
            last_price=last_price,
            zone=dominant_zone,
            volume_sentiment=vs,
        )

        # Tally classification stats
        if classification == 'PROPEL':
            _stats['classified_propel'] += 1
        elif classification == 'REJECT':
            _stats['classified_reject'] += 1
        elif classification == 'CHOP':
            _stats['classified_chop'] += 1
        else:
            _stats['classified_neutral'] += 1

        _stats['computed'] += 1

        # Trim the public zones list to the top 6 by evidence for response.
        # Phase 5: Classify EACH zone in the response (not just the dominant
        # one) so the UI can show per-zone reaction predictions.
        public_zones: list[dict] = []
        for z in zones_sorted[:6]:
            try:
                z_cls, z_p, z_r, z_c, z_align = _classify_dominant_zone(
                    last_price, z, vs,
                )
            except Exception:
                z_cls, z_p, z_r, z_c, z_align = 'NEUTRAL', 0.33, 0.33, 0.34, 'mixed'
            public_zones.append({
                'low': z['low'], 'high': z['high'], 'midpoint': z['midpoint'],
                'tier': z['tier'], 'touch_count': z['touch_count'],
                'rejection_strength': z['rejection_strength'],
                'volume_score': z['volume_score'],
                'evidence_score': z['evidence_score'],
                'distance_pct': z['distance_pct'],
                'kind_mix': z['kind_mix'],
                'age_bars': z['age_bars'],
                # Phase 5: per-zone reaction probabilities
                'classification': z_cls,
                'propel_probability': z_p,
                'reject_probability': z_r,
                'chop_probability': z_c,
                'volume_alignment': z_align,
            })

        return {
            'status': 'implemented',
            'provenance': 'real_history',
            'classification': classification,
            'propel_probability': propel_p,
            'reject_probability': reject_p,
            'chop_probability': chop_p,
            'dominant_zone': {
                'low': dominant_zone['low'],
                'high': dominant_zone['high'],
                'midpoint': dominant_zone['midpoint'],
                'tier': dominant_zone['tier'],
                'distance_pct': dominant_zone['distance_pct'],
                'touches': dominant_zone['touch_count'],
                'last_touch_age_bars': dominant_zone['age_bars'],
                'rejection_strength': dominant_zone['rejection_strength'],
                'volume_score': dominant_zone['volume_score'],
                'evidence_score': dominant_zone['evidence_score'],
                'kind_mix': dominant_zone['kind_mix'],
            },
            'zones': public_zones,
            'volume_sentiment_alignment': alignment,
            'bars_used': n,
            'zone_count': len(zones),
        }
    except Exception as exc:
        log.debug('reaction_map compute failed for %s: %s', symbol, exc)
        _stats['errors'] += 1
        out = dict(UNAVAILABLE_REACTION_MAP)
        out['status'] = f'error:{type(exc).__name__}'
        return out
