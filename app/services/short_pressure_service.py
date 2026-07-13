"""Short selling pressure factor family.

Blended inference model producing a normalized short-pressure read for
every scanned symbol. Uses live short-interest fields when the
fundamentals snapshot carries them, otherwise degrades gracefully to
price/volume proxy inference from the daily-history cache. Source
transparency is preserved via the `source` flag
('live' | 'proxy' | 'partial' | 'unavailable').
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger('app.short_pressure')

UNAVAILABLE_SHORT_PRESSURE: dict[str, Any] = {
    'score': 50.0,
    'raw_score': 0.0,
    'label': 'neutral',
    'direction': 'neutral',
    'confidence': 'low',
    'source': 'unavailable',
    'status': 'unavailable',
    'components': {},
}


def _safe(v, default=0.0) -> float:
    try:
        f = float(v)
        if f != f:
            return default
        return f
    except (TypeError, ValueError):
        return default


def _label_for(score: float, price_trend: float) -> tuple[str, str]:
    """Directional interpretation: high pressure + improving price = squeeze
    risk (bullish); high pressure + weak price = bearish dominance."""
    if score >= 70.0:
        if price_trend > 0.3:
            return 'squeeze_risk_bullish', 'bullish_squeeze'
        return 'bearish_pressure', 'bearish'
    if score >= 58.0:
        if price_trend > 0.3:
            return 'elevated_squeeze_watch', 'bullish_squeeze'
        return 'elevated', 'bearish_lean'
    if score <= 35.0:
        return 'low', 'neutral'
    return 'neutral', 'neutral'


def compute_short_selling_pressure(
    symbol: str,
    last_price: float,
    prev_close: float,
    info: dict | None = None,
    daily_hist=None,
    options_payload: dict | None = None,
) -> dict[str, Any]:
    """Return the normalized short_selling_pressure family payload.

    Never raises. Components (each 0-100, blended by available weight):
      * live short-interest (shortPercentOfFloat / shortRatio) when present
      * downside persistence under elevated participation (proxy)
      * gap-down continuation with rising participation (proxy)
      * price suppression vs. participation divergence (proxy)
      * options positioning conflict (put-heavy while price holds) (proxy)
    """
    info = info or {}
    components: dict[str, float] = {}
    weights: dict[str, float] = {}
    source = 'unavailable'
    price_trend = 0.0

    # ---- live short-interest inputs -------------------------------------
    try:
        spf = _safe(info.get('shortPercentOfFloat')) * 100.0  # fraction -> pct
        short_ratio = _safe(info.get('shortRatio'))           # days to cover
        shares_short = _safe(info.get('sharesShort'))
        shares_prior = _safe(info.get('sharesShortPriorMonth'))
        live_parts: list[float] = []
        if spf > 0:
            # 0% float short -> 0 ... 25%+ -> 100
            live_parts.append(min(100.0, spf * 4.0))
        if short_ratio > 0:
            # 0 days-to-cover -> 0 ... 10+ days -> 100
            live_parts.append(min(100.0, short_ratio * 10.0))
        if shares_short > 0 and shares_prior > 0:
            growth = (shares_short - shares_prior) / shares_prior
            live_parts.append(max(0.0, min(100.0, 50.0 + growth * 200.0)))
        if live_parts:
            components['short_interest_live'] = round(sum(live_parts) / len(live_parts), 2)
            weights['short_interest_live'] = 0.45
            source = 'live'
    except Exception:  # noqa: BLE001
        pass

    # ---- daily-history proxies ------------------------------------------
    hist_ok = False
    try:
        if daily_hist is not None and not getattr(daily_hist, 'empty', True) and len(daily_hist) >= 15:
            closes = daily_hist['Close'].astype(float).tolist()
            opens = daily_hist['Open'].astype(float).tolist() if 'Open' in daily_hist else closes
            vols = daily_hist['Volume'].astype(float).tolist() if 'Volume' in daily_hist else []
            n = len(closes)
            hist_ok = True
            avg_vol = (sum(vols[-20:]) / max(1, len(vols[-20:]))) if vols else 0.0

            # Downside persistence under elevated participation (last 10 bars).
            down_elevated = 0
            checked = 0
            for i in range(max(1, n - 10), n):
                checked += 1
                is_down = closes[i] < closes[i - 1]
                vol_hot = bool(vols) and avg_vol > 0 and vols[i] > avg_vol * 1.15
                if is_down and vol_hot:
                    down_elevated += 1
            if checked:
                components['downside_persistence'] = round(min(100.0, (down_elevated / checked) * 220.0), 2)
                weights['downside_persistence'] = 0.25

            # Gap-down continuation: gapped below prior close AND closed below open.
            gapdown_cont = 0
            for i in range(max(1, n - 10), n):
                if opens[i] < closes[i - 1] * 0.995 and closes[i] < opens[i]:
                    gapdown_cont += 1
            components['gap_down_continuation'] = round(min(100.0, gapdown_cont * 30.0), 2)
            weights['gap_down_continuation'] = 0.10

            # Price suppression: heavy participation but price failing to
            # advance (volume z high while 5d return flat/negative).
            if vols and avg_vol > 0:
                recent_vol = sum(vols[-5:]) / max(1, len(vols[-5:]))
                vol_ratio = recent_vol / avg_vol
                ret5 = (closes[-1] - closes[-6]) / closes[-6] * 100.0 if n >= 6 and closes[-6] > 0 else 0.0
                if vol_ratio > 1.2 and ret5 < 0.5:
                    components['price_suppression'] = round(min(100.0, (vol_ratio - 1.0) * 80.0 + max(0.0, -ret5) * 5.0), 2)
                else:
                    components['price_suppression'] = round(max(0.0, (vol_ratio - 1.0) * 20.0), 2)
                weights['price_suppression'] = 0.15

            # Short-horizon price trend for direction interpretation.
            if n >= 11 and closes[-11] > 0:
                price_trend = (closes[-1] - closes[-11]) / closes[-11] * 100.0 / 3.0
            if source != 'live':
                source = 'proxy'
    except Exception as exc:  # noqa: BLE001
        log.debug('short pressure hist proxies failed for %s: %s', symbol, exc)

    # ---- options positioning conflict ------------------------------------
    try:
        op = options_payload or {}
        composite = op.get('composite') or {}
        pcr = _safe(composite.get('put_call_ratio'))
        if pcr > 0 and pcr < 900:
            chg = ((last_price - prev_close) / prev_close * 100.0) if prev_close else 0.0
            # Put-heavy positioning while price holds/climbs = short-conflict.
            if pcr >= 1.2:
                conflict = min(100.0, (pcr - 1.0) * 60.0 + max(0.0, chg) * 8.0)
            else:
                conflict = max(0.0, (pcr - 0.6) * 40.0)
            components['options_conflict'] = round(conflict, 2)
            weights['options_conflict'] = 0.10
            if source == 'unavailable':
                source = 'partial'
    except Exception:  # noqa: BLE001
        pass

    if not components:
        return dict(UNAVAILABLE_SHORT_PRESSURE)

    # Downgrade source when the proxy set is thin.
    if source == 'proxy' and not hist_ok:
        source = 'partial'
    if source == 'live' and not hist_ok:
        source = 'partial'

    total_w = sum(weights.get(k, 0.0) for k in components)
    raw = sum(components[k] * weights.get(k, 0.0) for k in components) / total_w if total_w > 0 else 50.0
    score = max(0.0, min(100.0, raw))
    label, direction = _label_for(score, price_trend)
    confidence = ('high' if source == 'live' and hist_ok
                  else 'medium' if hist_ok
                  else 'low')

    return {
        'score': round(score, 2),
        'raw_score': round(raw, 4),
        'label': label,
        'direction': direction,
        'confidence': confidence,
        'source': source,
        'status': 'implemented',
        'price_trend_pct_per_wk': round(price_trend * 3.0, 3),
        'components': components,
    }
