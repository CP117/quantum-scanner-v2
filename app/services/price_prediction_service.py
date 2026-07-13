"""
10-day price-point prediction service.

Aggregates every signal the scanner already computes into a deterministic
forward-price estimate:

  - 7 factor-family scores (trend_volume_delta, institutional_confluence,
    options_positioning, institutional_order_block, dark_pool_proxy,
    volume_sentiment, reaction_clustering).
  - The blended composite score + final_direction.
  - ATR-based volatility (drives the magnitude + the confidence band).
  - reaction_clustering classification (PROPEL amplifies, REJECT inverts,
    CHOP dampens).
  - volume_sentiment conviction (scales the daily drift).

Output is a target price + 95% confidence band + per-factor contributions
+ human-readable reasoning bullets so the user can audit WHY the model
projected what it projected. The math is fully transparent -- no black-box
ML; every multiplier is documented inline.

This is intentionally NOT a back-tested high-frequency signal. It's a
"if today's read holds, here's where price would drift in 10 days"
estimator that materialises everything the rest of the dashboard already
computes into a single number the user can act on.

Never raises. Returns a structured `unavailable` payload when the symbol
isn't in the result store, isn't in the daily-history cache, or fails
any sanity check.
"""
from __future__ import annotations

import logging
import math
from typing import Any

from app.services.daily_history_service import get_daily_history
from app.services.detail_service import get_symbol_detail

log = logging.getLogger('app.price_prediction')

# Tuning constants -- kept at module-top so we can revisit them after
# user feedback without hunting through the function body.
_DEFAULT_FORWARD_DAYS = 10
_ATR_PERIOD = 14
# Daily-drift scaler: how much of one ATR a "perfectly conviction-aligned"
# directional read should claim per day. 0.5 = a strong trending stock
# moves ~0.5 ATR per day on average in its trend direction. We anchor
# here, then modulate by per-factor agreement + reaction-clustering +
# volume-sentiment conviction.
_BASE_DRIFT_PER_ATR = 0.5
# Hard cap on the projection - never predict more than 25% in 10 days
# regardless of how alignment-strong the read is. This stops single-bar
# anomalies from blowing the projection out.
_MAX_PCT_MOVE = 25.0
# 95% CI = mean +/- 2 * sigma. Sigma = ATR_pct * sqrt(forward_days)
# under the standard random-walk volatility-scaling assumption.
_CI_SIGMA_MULT = 2.0

# Phase 26.41 — sub-daily horizon support.
# US equities trade ~6.5 hours per session.  We use this to scale
# between daily and hourly drift/volatility.
_TRADING_HOURS_PER_DAY = 6.5
# Intraday hard cap is tighter than the daily cap because over a 1-10
# hour horizon a 25% move is essentially never directionally
# predictable — it's a tail event, not a forecast.
_MAX_INTRADAY_PCT_MOVE = 8.0
# Allowed horizons for the sub-daily prediction buttons in the UI.
# The endpoint will accept any value in this list (anything else falls
# back to the existing daily path with `forward_days`).
_ALLOWED_FORWARD_HOURS = (1, 5, 10)

# Per-family display-order + canonical key names so the frontend can
# render contributions in a predictable order.
_FAMILY_KEYS = (
    'trend_volume_delta',
    'institutional_confluence',
    'options_positioning',
    'institutional_order_block',
    'dark_pool_proxy',
    'volume_sentiment',
    'reaction_clustering',
)
# Family display weights used for the "agreement-weighted edge" — these
# match the weights the live scorer uses to blend the 7 families into
# the secondary composite (equal-ish but biased toward institutional
# confluence + reaction clustering, the two most consistently
# predictive families).
_FAMILY_WEIGHTS = {
    'trend_volume_delta':        0.10,
    'institutional_confluence':  0.22,
    'options_positioning':       0.13,
    'institutional_order_block': 0.15,
    'dark_pool_proxy':           0.08,
    'volume_sentiment':          0.14,
    'reaction_clustering':       0.18,
}


def _atr_pct(df, period: int = _ATR_PERIOD) -> float:
    """Average true range as % of the most-recent close. 0 if insufficient."""
    if df is None or getattr(df, 'empty', True) or len(df) < 2:
        return 0.0
    highs = df['High'].astype(float).tolist()
    lows = df['Low'].astype(float).tolist()
    closes = df['Close'].astype(float).tolist()
    n = len(closes)
    period = min(period, n - 1)
    if period <= 0:
        return 0.0
    trs: list[float] = []
    for i in range(1, n):
        trs.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        ))
    recent = trs[-period:]
    if not recent:
        return 0.0
    atr_abs = sum(recent) / len(recent)
    last_close = closes[-1] or 1.0
    return (atr_abs / max(abs(last_close), 1e-9)) * 100.0


def _intraday_atr_pct(symbol: str, market: str) -> tuple[float, str]:
    """Phase 26.41 — intraday ATR via yfinance 5-day / 5-minute bars.

    Used for the sub-daily prediction horizons (1 h / 5 h / 10 h).
    Returns (atr_pct_per_hour, source_note).

    Why per-hour: we want a volatility number that, when multiplied by
    `sqrt(forward_hours)`, yields the equivalent 95 %-band sigma at the
    forecast horizon.  The 5-minute ATR is computed in % of close
    (same shape as `_atr_pct`), then scaled up to per-hour by
    `sqrt(12)` (12 five-minute bars per hour).

    Returns (0, reason_string) when no intraday history is available
    so the caller can fall back to the daily-derived estimate.
    """
    if not symbol:
        return 0.0, 'no_symbol'
    # Lazy import — avoids pulling yfinance unless someone actually
    # asks for an intraday prediction.
    try:
        import yfinance as yf  # noqa: WPS433
        ticker = yf.Ticker(symbol)
        # 5-day / 5-min history is the sweet spot — covers ~390 bars,
        # enough for a 14-period ATR plus headroom, AND is cached by
        # yfinance after the first call.
        hist = ticker.history(period='5d', interval='5m', auto_adjust=False)
    except Exception as exc:  # noqa: BLE001
        log.debug('_intraday_atr_pct: yf history fetch failed for %s: %s', symbol, exc)
        return 0.0, f'intraday_history_unavailable ({type(exc).__name__})'
    if hist is None or getattr(hist, 'empty', True) or len(hist) < 15:
        return 0.0, 'intraday_history_too_short'
    # 5-minute ATR % of close.
    atr_pct_5m = _atr_pct(hist, period=_ATR_PERIOD)
    if atr_pct_5m <= 0:
        return 0.0, 'intraday_atr_zero'
    # Scale: ATR_per_hour = ATR_per_5min * sqrt(12 bars/hour).
    atr_pct_per_hour = atr_pct_5m * math.sqrt(12.0)
    return atr_pct_per_hour, f'5d/5m intraday ATR -> {atr_pct_per_hour:.3f}% per hour'


def _options_gamma_boost(market_block: dict, forward_horizon_factor: float) -> tuple[float, float, str]:
    """Phase 26.41 — gamma-squeeze proximity amplifier.

    When the underlying sits NEAR a high-gamma strike, dealer hedging
    flows turn into a self-reinforcing acceleration of price.  This is
    much more important on intraday horizons than on the 10-day path
    because over 1-10 hours, dealer flows dominate fundamentals.

    Returns (drift_multiplier, sigma_multiplier, reason).

    Logic:
      - `options_gamma.proximity_score` 0-100, higher = closer to a
        material gamma level.
      - At proximity ≥ 70 we apply up to a +20 % drift boost AND a
        +25 % sigma boost (wider expected range — gamma squeeze fuels
        directional AND volatility moves).
      - `forward_horizon_factor` scales the boost from 0..1:
            1.0 for 1 h, 0.7 for 5 h, 0.4 for 10 h, 0.1 for daily+.
        Rationale: closer to a gamma level + closer to the forecast
        horizon = more relevant.
    """
    gamma = market_block.get('options_gamma') or {}
    proximity = float(gamma.get('proximity_score') or 0.0)
    if proximity <= 50.0:
        return 1.0, 1.0, 'options-gamma: no material proximity to a gamma level'
    # Normalise [50..100] -> [0..1]
    intensity = (proximity - 50.0) / 50.0
    drift_mult = 1.0 + 0.20 * intensity * forward_horizon_factor
    sigma_mult = 1.0 + 0.25 * intensity * forward_horizon_factor
    level = gamma.get('level_label') or gamma.get('gamma_level') or 'gamma cluster'
    reason = (
        f'options-gamma: proximity {proximity:.0f}/100 to {level} '
        f'-> drift {drift_mult:.3f}x, sigma {sigma_mult:.3f}x '
        f'(horizon weight {forward_horizon_factor:.2f})'
    )
    return round(drift_mult, 3), round(sigma_mult, 3), reason


def _iob_target_dampener(market_block: dict, current_price: float, expected_pct_move: float,
                        direction_sign: int) -> tuple[float, str]:
    """Phase 26.41 — Institutional Order Block resistance/support dampener.

    If the predicted target would cross THROUGH an IOB bound (resistance
    on the way up, support on the way down), we shrink the projection.
    Price tends to hesitate at IOB levels before either breaking through
    OR rejecting back.  Either way, the OPEN projection is unreliable
    once it exits the active IOB band.

    Returns (drift_multiplier, reason).
    """
    iob = market_block.get('institutional_order_block') or {}
    if not iob:
        return 1.0, 'IOB: no active band detected'
    upper = iob.get('upper_bound') or iob.get('resistance')
    lower = iob.get('lower_bound') or iob.get('support')
    try:
        upper = float(upper) if upper is not None else None
        lower = float(lower) if lower is not None else None
    except (TypeError, ValueError):
        return 1.0, 'IOB: bounds non-numeric'
    if current_price <= 0:
        return 1.0, 'IOB: no current price'
    target = current_price * (1.0 + expected_pct_move / 100.0)
    if direction_sign > 0 and upper and target > upper:
        # Predicted bullish target overshoots resistance — dampen.
        overshoot_pct = (target - upper) / current_price * 100.0
        # The further past resistance, the more we dampen, capped at 0.5x.
        damp = max(0.5, 1.0 - 0.05 * overshoot_pct)
        return round(damp, 3), (
            f'IOB: bullish target {target:.2f} overshoots resistance {upper:.2f} '
            f'by {overshoot_pct:.2f}% -> drift dampened to {damp:.3f}x'
        )
    if direction_sign < 0 and lower and target < lower:
        # Predicted bearish target overshoots support — dampen.
        overshoot_pct = (lower - target) / current_price * 100.0
        damp = max(0.5, 1.0 - 0.05 * overshoot_pct)
        return round(damp, 3), (
            f'IOB: bearish target {target:.2f} overshoots support {lower:.2f} '
            f'by {overshoot_pct:.2f}% -> drift dampened to {damp:.3f}x'
        )
    return 1.0, 'IOB: target stays within active band — no dampening'


def _predictive_consensus_confidence_bonus(market_block: dict) -> tuple[float, str]:
    """Phase 26.41 — read the cross-family predictive_consensus block
    (already computed in Pass 2) and turn it into a confidence bonus.

    The consensus score is 0-100, higher = more families agree.  Maps
    [70..100] -> +0..+10 pp on the prediction confidence.
    """
    pc = market_block.get('predictive_consensus') or {}
    score = float(pc.get('consensus_score') or 0.0)
    if score < 70.0:
        return 0.0, f'predictive-consensus: {score:.0f}/100 — below threshold for bonus'
    intensity = (score - 70.0) / 30.0  # [0..1] over the 70-100 band
    bonus = round(10.0 * min(1.0, intensity), 1)
    return bonus, f'predictive-consensus: {score:.0f}/100 -> +{bonus:.1f}pp confidence'


def _family_edge(score: Any) -> float:
    """Normalise a 0-100 family score into a [-1, +1] directional edge.
    50 = no edge, 100 = full bull, 0 = full bear."""
    try:
        s = float(score)
    except (TypeError, ValueError):
        return 0.0
    return max(-1.0, min(1.0, (s - 50.0) / 50.0))


def _direction_sign(label: str) -> int:
    if not label:
        return 0
    s = label.lower()
    if 'bull' in s:
        return 1
    if 'bear' in s:
        return -1
    return 0


def _reaction_modifier(classification: str) -> tuple[float, str]:
    """Reaction-clustering acts as a directional multiplier:
       - PROPEL  : amplifies drift (1.30x).
       - REJECT  : inverts and shrinks (-0.50x) -- price reacts away
                   from the dominant zone, so our directional read
                   counter-trends.
       - CHOP    : dampens to 0.40x.
       - NEUTRAL : leaves drift unchanged (1.0x).
    """
    cls = (classification or 'NEUTRAL').upper()
    if cls == 'PROPEL':
        return 1.30, 'reaction-cluster says PROPEL: amplifying directional drift'
    if cls == 'REJECT':
        return -0.50, 'reaction-cluster says REJECT: counter-trending the directional read'
    if cls == 'CHOP':
        return 0.40, 'reaction-cluster says CHOP: damping directional drift'
    return 1.00, 'reaction-cluster neutral: drift unchanged'


def _volume_modifier(volume_sentiment: dict) -> tuple[float, str]:
    """Volume-sentiment conviction maps to a multiplier in [0.7, 1.3].
    Higher conviction = bigger projected move."""
    try:
        conv = float((volume_sentiment or {}).get('conviction_score') or 0.0)
    except (TypeError, ValueError):
        conv = 0.0
    conv = max(0.0, min(100.0, conv)) / 100.0  # [0, 1]
    mult = 0.7 + 0.6 * conv
    return round(mult, 3), f'volume-sentiment conviction {conv*100:.0f}/100 -> multiplier {mult:.2f}x'


def _regulatory_modifier(symbol: str, composite_direction_sign: int) -> tuple[float, float, str, dict]:
    """Phase 19: pull the regulatory-monitor signal (insider buys/sells +
    federal contract awards) and translate it into a directional
    modifier on the prediction drift.

    Returns: (drift_multiplier, confidence_bonus, reason_string, raw_signal)

    Math:
      - regulatory `score_delta` is the points the regulator adds to
        the composite score (range ±8 after the tanh squash).
      - We translate that into a ±10% drift multiplier ramp:
          regulator says +8 (max insider/award boost), composite
          direction bullish -> multiplier 1.10 (10% drift amplification).
          regulator says -8 (heavy insider sells), composite bearish
          -> multiplier 1.10 (still amplification because signal aligns).
          regulator says +8 but composite bearish -> multiplier 0.85
          (contra-evidence, dampen the bearish prediction).
      - Confidence bonus: if the regulator signal AGREES with the
        composite direction and the weight is meaningful, add up to
        +15 percentage points to the prediction confidence.
    """
    try:
        from app.regulatory.services.signal_service import get_signal_sync
        sig = get_signal_sync(symbol) or {}
    except Exception:
        return 1.0, 0.0, 'regulatory signal unavailable', {}
    delta = float(sig.get('score_delta') or 0.0)
    weight = float(sig.get('weight') or 0.0)
    events = int(sig.get('event_count') or 0)
    if events == 0 or weight <= 0.05:
        return 1.0, 0.0, 'no recent regulatory activity', sig
    # Normalise delta into [-1, +1]
    norm_delta = max(-1.0, min(1.0, delta / 8.0))
    # Agreement is the dot product of the regulator's sign with the
    # composite direction sign.
    if composite_direction_sign == 0:
        agreement = 0.0
    else:
        agreement = 1.0 if (norm_delta > 0) == (composite_direction_sign > 0) else -1.0
    # Multiplier: 1.0 + 0.10 * agreement * |norm_delta| * weight.
    mult = 1.0 + 0.10 * agreement * abs(norm_delta) * min(1.0, weight)
    # Confidence bonus only when agreement is positive.
    conf_bonus = max(0.0, 15.0 * agreement * abs(norm_delta) * min(1.0, weight))
    direction_word = 'bullish' if norm_delta > 0 else ('bearish' if norm_delta < 0 else 'neutral')
    agreement_word = 'aligns with' if agreement > 0 else ('contradicts' if agreement < 0 else 'is neutral to')
    reason = (
        f'regulator signal: {direction_word} '
        f'(score_delta {delta:+.2f}, weight {weight:.2f}, {events} event{"s" if events != 1 else ""}); '
        f'{agreement_word} the composite direction -> drift multiplier {mult:.3f}x'
        + (f', +{conf_bonus:.1f}pp confidence bonus' if conf_bonus > 0 else '')
    )
    return round(mult, 3), round(conf_bonus, 2), reason, sig


def predict_price(symbol: str, forward_days: int = _DEFAULT_FORWARD_DAYS, market: str = 'stocks',
                  forward_hours: int | None = None) -> dict[str, Any]:
    """Generate a forward price target by combining every factor family
    the scanner already computes.

    Phase 26.41 — sub-daily horizons:
        Pass `forward_hours` (1, 5, or 10) to switch to the intraday
        math path: 5-day/5-min ATR scaled to per-hour, sqrt(hours)
        variance scaling, options-gamma proximity boost, and IOB
        target-band dampening.  The default daily path (10-day) is
        unchanged; existing callers that pass only `forward_days` see
        identical behaviour to before this phase.

    Returns a structured dict; never raises.
    """
    sym = (symbol or '').strip().upper()
    if not sym:
        return {'status': 'unavailable', 'reason': 'empty_symbol'}

    # ---------------------------------------------------------------
    # Horizon resolution: forward_hours wins when supplied AND non-zero.
    # Otherwise the legacy `forward_days` path is honored.
    # ---------------------------------------------------------------
    use_intraday = (forward_hours is not None and int(forward_hours) > 0)
    if use_intraday:
        forward_hours = int(forward_hours)
        horizon_label = f'{forward_hours}-hour'
        # Equivalent daily count for any code that still talks days.
        forward_days_equiv = max(1, forward_hours / _TRADING_HOURS_PER_DAY)
    else:
        horizon_label = f'{forward_days}-day'
        forward_days_equiv = max(1, int(forward_days))

    # Pull the freshest scored row for the symbol. `get_symbol_detail`
    # returns a fully normalized payload with every factor breakdown
    # populated -- same shape the dashboard uses to render the detail
    # panel. force_live=False lets it pull from snapshot cache when warm.
    try:
        row = get_symbol_detail(sym, force_live=False, market=market)
    except Exception as exc:  # noqa: BLE001
        log.debug('predict_price: detail fetch failed for %s: %s', sym, exc)
        row = None
    if not row or row.get('state') in ('unavailable', 'unknown_symbol'):
        return {
            'status': 'unavailable', 'reason': 'symbol_not_scored',
            'symbol': sym,
            'hint': 'Symbol must enter the active-scan pool first so a full factor breakdown is available.',
        }

    # Need a current price. Fall back to last_price field if main isn't there.
    market_block = (row.get('factor_breakdown') or {}).get('market') or {}
    current_price = (
        row.get('last_price')
        or market_block.get('last_price')
        or row.get('current_price')
    )
    try:
        current_price = float(current_price)
    except (TypeError, ValueError):
        return {
            'status': 'unavailable', 'reason': 'no_price',
            'symbol': sym, 'hint': 'Snapshot has no live price for this symbol.',
        }
    if current_price <= 0:
        return {'status': 'unavailable', 'reason': 'invalid_price', 'symbol': sym}

    # ---------------------------------------------------------------
    # Volatility input.  Daily ATR (legacy path) OR intraday-derived
    # per-hour ATR (Phase 26.41 sub-daily path).
    # ---------------------------------------------------------------
    if use_intraday:
        atr_pct_per_hour, atr_source_note = _intraday_atr_pct(sym, market)
        # Fallback: when intraday history is unreachable, approximate
        # per-hour ATR from the daily ATR / sqrt(6.5).  Still useful
        # for direction but the band will be wider.
        if atr_pct_per_hour <= 0:
            df_daily = get_daily_history(sym, allow_fetch=False, blocking=False)
            daily_atr = _atr_pct(df_daily) if df_daily is not None else 2.0
            if daily_atr <= 0:
                daily_atr = 2.0
            atr_pct_per_hour = daily_atr / math.sqrt(_TRADING_HOURS_PER_DAY)
            atr_source_note = (
                f'intraday history unavailable ({atr_source_note}); '
                f'fallback: daily ATR {daily_atr:.2f}% / sqrt(6.5) '
                f'= {atr_pct_per_hour:.3f}% per hour'
            )
        # For the math below, `atr_pct` is the per-unit-time ATR.
        # In the daily path: 1 unit = 1 trading day.
        # In the hourly path: 1 unit = 1 trading hour.
        atr_pct = atr_pct_per_hour
        forward_units = forward_hours
        unit_label = 'hour'
    else:
        df = get_daily_history(sym, allow_fetch=False, blocking=False)
        atr_pct = _atr_pct(df) if df is not None else 0.0
        if atr_pct <= 0:
            atr_pct = 2.0  # safe default for an unknown-volatility symbol
        atr_source_note = f'daily ATR {atr_pct:.2f}% per session'
        forward_units = int(forward_days)
        unit_label = 'day'

    # Composite-blended score + final direction.
    final_score = float(row.get('final_score') or 50.0)
    final_direction = row.get('final_direction') or 'Neutral'
    direction_sign = _direction_sign(final_direction)

    # Strength of the composite signal (how far from neutral 50).
    strength = abs(final_score - 50.0) / 50.0  # [0, 1]

    # Pull per-family scores from the secondary-composite payload.
    fb = row.get('factor_breakdown') or {}
    sc = fb.get('secondary_composite') or {}
    family_scores = (sc.get('family_scores') or {})

    # Edge per family (signed [-1, +1]) and agreement count.
    family_contributions: list[dict] = []
    aligned_weight = 0.0
    contra_weight = 0.0
    weighted_edge = 0.0
    for fam in _FAMILY_KEYS:
        score = family_scores.get(fam)
        edge = _family_edge(score) if score is not None else 0.0
        weight = _FAMILY_WEIGHTS.get(fam, 0.1)
        weighted_edge += edge * weight
        # Alignment against the composite direction
        if direction_sign != 0:
            if (edge > 0 and direction_sign > 0) or (edge < 0 and direction_sign < 0):
                aligned_weight += weight
            elif (edge > 0 and direction_sign < 0) or (edge < 0 and direction_sign > 0):
                contra_weight += weight
        family_contributions.append({
            'family': fam,
            'score': round(float(score), 2) if score is not None else None,
            'edge': round(edge, 3),
            'weight': weight,
            'contribution_pct': round(edge * weight * 100, 2),  # signed
        })

    # Agreement: fraction of weight pointing the same direction as the composite.
    # Neutral composite -> agreement = 0 (no directional read to agree with).
    if direction_sign == 0:
        agreement = 0.0
    else:
        total_w = sum(_FAMILY_WEIGHTS.values())
        agreement = max(0.0, min(1.0, (aligned_weight - contra_weight) / total_w))

    # Reaction-clustering + volume-sentiment modifiers.
    rc = market_block.get('reaction_clustering') or {}
    rc_class = (rc.get('classification') or 'NEUTRAL').upper()
    rc_mult, rc_reason = _reaction_modifier(rc_class)
    vs = market_block.get('volume_sentiment') or {}
    vs_mult, vs_reason = _volume_modifier(vs)

    # Phase 19: regulatory modifier - insider transactions + federal
    # contract awards directly nudge the predicted drift AND can add
    # up to +15 pp to the confidence when they align with the
    # composite direction.
    reg_mult, reg_conf_bonus, reg_reason, reg_signal = _regulatory_modifier(sym, direction_sign)

    # Phase 26.41: intraday-specific modifiers.  These weigh more on
    # sub-daily horizons (where dealer flows + IOB levels dominate)
    # and almost zero on the 10-day horizon.
    if use_intraday:
        # Heavier near-term, lighter as the horizon expands.
        horizon_factor_map = {1: 1.0, 5: 0.7, 10: 0.4}
        horizon_factor = horizon_factor_map.get(int(forward_units), 0.4)
    else:
        # Daily horizons get a small residual boost (gamma still
        # matters over multi-day moves but much less).
        horizon_factor = 0.1
    gamma_drift_mult, gamma_sigma_mult, gamma_reason = _options_gamma_boost(
        market_block, horizon_factor,
    )
    pc_conf_bonus, pc_reason = _predictive_consensus_confidence_bonus(market_block)

    # ----- The actual prediction math -----
    # Per-unit-time directional drift = sign * strength * agreement * (0.5 * ATR_unit%)
    # Modulated by reaction-clustering AND volume-sentiment conviction
    # AND the regulatory signal AND options-gamma proximity.
    base_drift_per_unit = direction_sign * strength * agreement * (_BASE_DRIFT_PER_ATR * atr_pct)
    drift_per_unit = base_drift_per_unit * rc_mult * vs_mult * reg_mult * gamma_drift_mult
    expected_pct_move = drift_per_unit * forward_units

    # IOB dampener (applied AFTER initial projection so we can compare
    # the projected target against the active band).
    iob_mult, iob_reason = _iob_target_dampener(
        market_block, current_price, expected_pct_move, direction_sign,
    )
    if iob_mult != 1.0:
        drift_per_unit *= iob_mult
        expected_pct_move = drift_per_unit * forward_units

    # Clamp to horizon-appropriate hard cap.
    max_move_cap = _MAX_INTRADAY_PCT_MOVE if use_intraday else _MAX_PCT_MOVE
    capped = False
    if abs(expected_pct_move) > max_move_cap:
        expected_pct_move = math.copysign(max_move_cap, expected_pct_move)
        capped = True

    target_price = current_price * (1.0 + expected_pct_move / 100.0)
    # 95% confidence interval (random-walk sigma scaling), with optional
    # gamma sigma boost on intraday horizons.
    sigma_pct = atr_pct * math.sqrt(max(1, forward_units)) * gamma_sigma_mult
    band = _CI_SIGMA_MULT * sigma_pct
    low_price = current_price * (1.0 + (expected_pct_move - band) / 100.0)
    high_price = current_price * (1.0 + (expected_pct_move + band) / 100.0)

    # Confidence score (0-100) — how much faith to put in the projection.
    # Combines composite strength + factor agreement + drift magnitude
    # relative to the noise band. A 5% projected move against +/-12%
    # noise = low confidence; a 10% projected move against +/-3% noise
    # = high confidence. Phase 19: an aligned regulator signal adds up
    # to +15pp on top.  Phase 26.41: predictive-consensus alignment
    # contributes up to +10pp on top of that.
    if band > 0:
        signal_to_noise = min(2.0, abs(expected_pct_move) / band)  # cap at 2
    else:
        signal_to_noise = 0.0
    confidence_raw = (strength * 0.4 + agreement * 0.4 + (signal_to_noise / 2.0) * 0.2)
    confidence_pct = round(
        min(100.0, confidence_raw * 100.0 + reg_conf_bonus + pc_conf_bonus),
        1,
    )

    # Direction label for output.  Use a tighter threshold for intraday
    # since 0.5% over 1 hour is a meaningful directional read whereas
    # the same 0.5% over 10 days is essentially flat.
    neutral_threshold = 0.15 if use_intraday else 0.5
    if expected_pct_move > neutral_threshold:
        direction_label = 'Bullish'
    elif expected_pct_move < -neutral_threshold:
        direction_label = 'Bearish'
    else:
        direction_label = 'Neutral'

    # Human-readable reasoning bullets so the user can audit the call.
    reasoning: list[str] = []
    reasoning.append(
        f"Composite score {final_score:.1f}/100 with {final_direction} bias "
        f"(strength {strength*100:.0f}%)."
    )
    reasoning.append(
        f"Factor agreement: {agreement*100:.0f}% of weighted families align "
        f"with the composite direction "
        f"(aligned weight {aligned_weight:.2f}, contra weight {contra_weight:.2f})."
    )
    reasoning.append(f"Volatility input: {atr_source_note}.")
    reasoning.append(rc_reason)
    reasoning.append(vs_reason)
    reasoning.append(reg_reason)
    reasoning.append(gamma_reason)
    reasoning.append(iob_reason)
    if pc_conf_bonus > 0:
        reasoning.append(pc_reason)
    reasoning.append(
        f"Per-{unit_label} directional drift: {drift_per_unit:+.3f}% per {unit_label} "
        f"-> {expected_pct_move:+.2f}% across {forward_units} {unit_label}{'s' if forward_units != 1 else ''}"
        f"{f' (capped at ±{max_move_cap:.0f}%)' if capped else ''}."
    )
    reasoning.append(
        f"95% confidence band: ±{band:.2f}% (sigma = ATR × √{forward_units} {unit_label}{'s' if forward_units != 1 else ''}"
        f"{f' × {gamma_sigma_mult:.2f}x gamma' if gamma_sigma_mult != 1.0 else ''})."
    )

    return {
        'status': 'ok',
        'symbol': sym,
        'horizon_label': horizon_label,
        # Backwards-compat: daily callers still see `forward_days`.
        'forward_days': forward_days if not use_intraday else None,
        'forward_hours': forward_hours if use_intraday else None,
        'horizon_units': forward_units,
        'horizon_unit_label': unit_label,
        'current_price': round(current_price, 4),
        'target_price': round(target_price, 4),
        'expected_pct_move': round(expected_pct_move, 2),
        'low_price': round(low_price, 4),
        'high_price': round(high_price, 4),
        'direction': direction_label,
        'composite_direction': final_direction,
        'composite_score': round(final_score, 2),
        'confidence': confidence_pct,
        'agreement_pct': round(agreement * 100.0, 1),
        'strength_pct': round(strength * 100.0, 1),
        'atr_pct': round(atr_pct, 3),
        'sigma_pct': round(sigma_pct, 3),
        'capped': capped,
        'family_contributions': family_contributions,
        'modulators': {
            'reaction_classification': rc_class,
            'reaction_multiplier': rc_mult,
            'volume_conviction': float(vs.get('conviction_score') or 0.0),
            'volume_multiplier': vs_mult,
            # Phase 19: regulatory signal modulator
            'regulatory_multiplier': reg_mult,
            'regulatory_confidence_bonus': reg_conf_bonus,
            'regulatory_score_delta': float(reg_signal.get('score_delta') or 0.0),
            'regulatory_event_count': int(reg_signal.get('event_count') or 0),
            'regulatory_reason': reg_signal.get('reason') or 'no regulatory data',
            'regulatory_staleness_days': reg_signal.get('staleness_days'),
            # Phase 26.41: intraday-aware modulators
            'options_gamma_drift_multiplier': gamma_drift_mult,
            'options_gamma_sigma_multiplier': gamma_sigma_mult,
            'iob_drift_multiplier': iob_mult,
            'predictive_consensus_confidence_bonus': pc_conf_bonus,
        },
        'reasoning': reasoning,
    }
