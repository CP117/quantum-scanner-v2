"""
Volume Sentiment Profile - the shared sentiment substrate.

A single profile is computed per symbol per scoring pass from daily OHLCV bars.
It is consumed by:
  * the institutional_order_block factor (to predict whether a zone touch will
    reject / chop / propel),
  * the options_positioning composite (to amplify or dampen the put/call
    pressure signal when volume action disagrees),
  * the reaction_clustering_service (to weight zone evidence and
    classification).

Designed to be:
  * cheap to compute (one pass over the history dataframe),
  * defensive against missing / partial data,
  * always returns a contract-shaped dict (UNAVAILABLE sentinel on failure).

Key metrics produced:
  * directional_score          0-100 (>50 bullish, <50 bearish)
  * conviction_score           0-100 (how strong the read is)
  * accumulation_distribution  0-100
  * effort_vs_result_label     absorbing | efficient | capitulating | neutral
  * regime                     expansion | normal | compression
  * volume_z_score             standard z over 20-bar window
  * buy_sell_ratio             up-bar volume / down-bar volume
  * recent_break_bias          inferred bias from last 5 bars
"""
from __future__ import annotations

import logging
import math
from typing import Any

log = logging.getLogger('app.volume_sentiment')


UNAVAILABLE_VOLUME_SENTIMENT: dict[str, Any] = {
    'status': 'unavailable',
    'provenance': 'unavailable',
    'directional_score': 50.0,
    'conviction_score': 0.0,
    'bias': 'neutral',
    'regime': 'normal',
    'accumulation_distribution': 50.0,
    'effort_vs_result_label': 'neutral',
    'volume_z_score': 0.0,
    'buy_sell_ratio': 1.0,
    'recent_break_bias': 'neutral',
    'bars_used': 0,
}


def _safe(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        result = float(value)
        if math.isnan(result) or math.isinf(result):
            return default
        return result
    except (TypeError, ValueError):
        return default


def _clip(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def compute_volume_sentiment(hist: Any) -> dict[str, Any]:
    """Compute a VolumeSentimentProfile from a yfinance daily history DataFrame.

    Required columns: Open, High, Low, Close, Volume.
    Recommended length: >=20 bars.  Will degrade gracefully on shorter data.
    Returns the UNAVAILABLE sentinel if no usable data.
    """
    try:
        if hist is None:
            return dict(UNAVAILABLE_VOLUME_SENTIMENT)
        if getattr(hist, 'empty', True):
            return dict(UNAVAILABLE_VOLUME_SENTIMENT)
        needed_cols = {'Open', 'High', 'Low', 'Close', 'Volume'}
        if not needed_cols.issubset(set(hist.columns)):
            return dict(UNAVAILABLE_VOLUME_SENTIMENT)

        # Take the last 60 bars max so older periods don't dominate.
        df = hist.dropna(subset=['Close', 'Volume']).tail(60)
        if len(df) < 5:
            return dict(UNAVAILABLE_VOLUME_SENTIMENT)

        opens = df['Open'].astype(float).tolist()
        highs = df['High'].astype(float).tolist()
        lows = df['Low'].astype(float).tolist()
        closes = df['Close'].astype(float).tolist()
        volumes = df['Volume'].astype(float).tolist()

        n = len(df)
        # --- buy vs sell pressure (up-bar volume / down-bar volume) ----------
        up_vol = 0.0
        down_vol = 0.0
        for i in range(1, n):
            if closes[i] > closes[i - 1]:
                up_vol += volumes[i]
            elif closes[i] < closes[i - 1]:
                down_vol += volumes[i]
        if down_vol > 0:
            buy_sell_ratio = up_vol / down_vol
        else:
            buy_sell_ratio = 5.0 if up_vol > 0 else 1.0

        # --- accumulation / distribution proxy (Chaikin A/D-ish) -------------
        ad_sum = 0.0
        ad_norm = 0.0
        for i in range(n):
            h, l, c, v = highs[i], lows[i], closes[i], volumes[i]
            rng = h - l
            if rng <= 0:
                continue
            money_flow_multiplier = ((c - l) - (h - c)) / rng  # -1..+1
            ad_sum += money_flow_multiplier * v
            ad_norm += v
        ad_raw = (ad_sum / ad_norm) if ad_norm > 0 else 0.0  # -1..+1
        accumulation_distribution = _clip(50.0 + ad_raw * 50.0, 0.0, 100.0)

        # --- volume z-score over last 20 bars --------------------------------
        last_volume = volumes[-1]
        win = volumes[-20:] if len(volumes) >= 20 else volumes
        mean_v = sum(win) / len(win)
        var_v = sum((x - mean_v) ** 2 for x in win) / len(win)
        std_v = math.sqrt(var_v) if var_v > 0 else 0.0
        z = (last_volume - mean_v) / std_v if std_v > 0 else 0.0
        volume_z_score = _clip(z, -5.0, 5.0)

        # --- effort vs result (the move price made vs the volume it took) ---
        recent_returns = []
        recent_vols = []
        for i in range(max(0, n - 5), n):
            if i == 0:
                continue
            change_pct = (closes[i] - closes[i - 1]) / closes[i - 1] * 100.0 if closes[i - 1] else 0.0
            recent_returns.append(change_pct)
            recent_vols.append(volumes[i])
        avg_recent_ret = sum(abs(r) for r in recent_returns) / len(recent_returns) if recent_returns else 0.0
        avg_recent_vol = sum(recent_vols) / len(recent_vols) if recent_vols else mean_v
        vol_ratio = (avg_recent_vol / mean_v) if mean_v > 0 else 1.0
        if vol_ratio > 1.5 and avg_recent_ret < 0.6:
            effort_vs_result_label = 'absorbing'      # huge volume, no movement -> battle at level
        elif vol_ratio > 1.8 and avg_recent_ret > 2.0:
            effort_vs_result_label = 'capitulating'   # huge volume + huge move -> exhaustion / capitulation
        elif vol_ratio < 0.7 and avg_recent_ret > 1.5:
            effort_vs_result_label = 'efficient'      # low volume but movement -> low conviction trend
        else:
            effort_vs_result_label = 'neutral'

        # --- regime (expansion / normal / compression) -----------------------
        ranges = [highs[i] - lows[i] for i in range(n)]
        recent_rng = sum(ranges[-5:]) / 5 if len(ranges) >= 5 else (ranges[-1] if ranges else 0.0)
        long_rng = sum(ranges) / len(ranges) if ranges else 0.0
        if long_rng > 0 and recent_rng / long_rng >= 1.3:
            regime = 'expansion'
        elif long_rng > 0 and recent_rng / long_rng <= 0.75:
            regime = 'compression'
        else:
            regime = 'normal'

        # --- recent break bias (last 5-bar net direction weighted by volume) -
        last5_net = 0.0
        last5_vol = 0.0
        for i in range(max(0, n - 5), n):
            if i == 0:
                continue
            ret = (closes[i] - closes[i - 1]) / closes[i - 1] if closes[i - 1] else 0.0
            last5_net += ret * volumes[i]
            last5_vol += volumes[i]
        if last5_vol > 0:
            vw_ret = last5_net / last5_vol
        else:
            vw_ret = 0.0
        if vw_ret > 0.005:
            recent_break_bias = 'bullish'
        elif vw_ret < -0.005:
            recent_break_bias = 'bearish'
        else:
            recent_break_bias = 'neutral'

        # --- directional + conviction scores --------------------------------
        # Directional: blend buy_sell_ratio + A/D + recent break
        bs_signal = _clip(math.log10(max(buy_sell_ratio, 0.05)) * 25 + 50, 0.0, 100.0)
        recent_signal = 70.0 if recent_break_bias == 'bullish' else 30.0 if recent_break_bias == 'bearish' else 50.0
        directional_score = round(_clip(0.4 * bs_signal + 0.4 * accumulation_distribution + 0.2 * recent_signal, 0.0, 100.0), 2)

        bias = 'bullish' if directional_score >= 58 else 'bearish' if directional_score <= 42 else 'neutral'

        # Conviction: distance from 50 + volume z-score + regime amplifier
        distance_from_neutral = abs(directional_score - 50.0) / 50.0  # 0..1
        z_amp = min(abs(volume_z_score) / 2.5, 1.0)
        regime_amp = {'expansion': 1.0, 'normal': 0.6, 'compression': 0.4}.get(regime, 0.6)
        conviction_score = round(_clip(
            (distance_from_neutral * 60.0) + (z_amp * 25.0) + (regime_amp * 15.0),
            0.0, 100.0,
        ), 2)

        return {
            'status': 'implemented',
            'provenance': 'real_history',
            'directional_score': directional_score,
            'conviction_score': conviction_score,
            'bias': bias,
            'regime': regime,
            'accumulation_distribution': round(accumulation_distribution, 2),
            'effort_vs_result_label': effort_vs_result_label,
            'volume_z_score': round(volume_z_score, 3),
            'buy_sell_ratio': round(buy_sell_ratio, 3),
            'recent_break_bias': recent_break_bias,
            'bars_used': n,
        }
    except Exception as exc:
        log.debug('volume_sentiment compute failed: %s', exc)
        out = dict(UNAVAILABLE_VOLUME_SENTIMENT)
        out['status'] = f'error:{type(exc).__name__}'
        return out
