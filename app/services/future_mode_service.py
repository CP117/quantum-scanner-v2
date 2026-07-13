"""Phase 26.47 — Future-mode forward-metrics attachment.

Every fully-scored row gets a `forward_metrics` block carrying the
Bayesian-Kelly forecast across FIVE pre-computed horizons:

    Horizon key        Trading style match            Math path
    -----------------  -----------------------------  ---------------
    forward_1h         1-hour hold                    intraday-fast
    forward_5h         5-hour hold                    intraday-fast
    forward_1d         Short (15m/1h chart)           daily-fast
    forward_5d         Swing / default                daily-fast
    forward_20d        Long (10+ day hold)            daily-fast

Pre-computing ALL FIVE per row means the frontend's trading-style
toggle is instant (no backend round-trip required when the user
switches between styles).  Each block carries:

    drift_pct                Bayesian posterior μ × horizon
    sigma_pct                ATR (or GARCH for top-N) × √horizon
    p_up                     Φ(μ_h / σ_h)
    p_down                   1 - p_up
    directional_certainty    2·|p_up - 0.5|        (range 0..1)
    direction                'Bullish' / 'Bearish' / 'Neutral'
    kelly_rank               drift × √precision × directional_certainty
    kelly_rank_abs           |kelly_rank|          (used for the table sort)
    posterior_precision      τ_post from the Bayesian blend
    n_factors                count of factors that contributed
    shrinkage                James-Stein shrinkage factor

The **fast tier** (`compute_forward_metrics_fast`) is called for
every fully-scored row inside `score_symbol_rows`.  It runs the
Bayesian blend (already cheap, no I/O) and uses the row's cached
ATR as the σ proxy.  Per-row cost: ~50 µs × 5 horizons = 250 µs.

The **GARCH tier** (`compute_forward_metrics_garch`) is only called
for the top-N symbols by the priority lane.  It replaces the ATR-
based σ with a proper GARCH(1,1) forecast (already implemented in
`garch_volatility.py`).  This block is stored under `forward_metrics_garch`
so the detail panel can show both tiers side-by-side.

All computation respects `daily / intraday` distinction:
    1h, 5h horizons use intraday-tier factor coefficients.
    1d, 5d, 20d horizons use daily-tier factor coefficients.

(See `bayesian_factor_blend._FACTOR_DRIFT_BPS` for the per-tier
literature-cited drift coefficients.)
"""
from __future__ import annotations

import logging
import math
import threading
import time
from typing import Any

from app.services.bayesian_factor_blend import (
    blend_factors_for_drift,
    normal_cdf,
)
from app.services.advanced_math_signals import (
    AdvancedSignals,
    attach_per_symbol_signals,
    compute_advanced_signals,
    enrich_horizon_block,
)
from app.services.lab_signals import (
    LabSignals,
    attach_per_symbol_lab,
    compute_lab_signals,
    enrich_horizon_block_lab,
)
from app.services.strategy_signals import (
    StrategySignals,
    attach_per_symbol_strategy,
    compute_strategy_signals,
    enrich_horizon_block_strategy,
)
from app.services.predictive_expansion import (
    PredictiveExpansionSignals,
    attach_per_symbol_predictive,
    compute_predictive_expansion,
    enrich_horizon_block_predictive,
)

log = logging.getLogger('app.future_mode')

# Top-N priority lane symbols that get GARCH-tier forecasts (Phase 26.47).
# Phase 26.50: bumped 25 → 60 so frontend filters (Bulls/Bears, intensity
# bands) and re-sorts don't reveal fast-tier-only rows when they
# promote previously off-screen symbols into the visible window.  At
# ~10 ms per GARCH attach (cached daily history + advanced overlay),
# 60 symbols ≈ 0.6 s per priority cycle — well within the adaptive
# 2-15 s tick budget the lane uses.
GARCH_TIER_TOP_N = 60

# ---------------------------------------------------------------------------
# Per-symbol advanced-signal cache.
#
# `compute_advanced_signals()` is ~5-10 ms per symbol (dominated by R/S Hurst
# + HAR-RV OLS).  For the leveraged variant scanning ~838 symbols the cost
# would be ~5-8 s per full sweep — fine the FIRST time but wasteful when
# the priority lane re-scores top-25 every 2 s.  We memoise by
# (symbol, len(closes), last_close) with a 5-minute TTL so:
#   * intra-cycle: cache hit, advanced math is free
#   * cycle-to-cycle: cache miss only when a new daily bar arrives
# ---------------------------------------------------------------------------
_ADV_CACHE_TTL_S = 300.0
_ADV_CACHE_MAX = 4096
# Latency telemetry — exponential moving average of compute-time (ms) on
# cache-miss code paths.  Smoothing factor `_LAT_EMA_ALPHA` chosen so a
# handful of slow outliers don't dominate; ~16-sample memory.
_LAT_EMA_ALPHA = 0.125
_adv_cache: dict[tuple[str, int, float], tuple[float, AdvancedSignals]] = {}
_adv_cache_lock = threading.Lock()
_adv_cache_hits = 0
_adv_cache_misses = 0
_adv_cache_miss_latency_ms_ema = 0.0
_adv_cache_miss_latency_ms_last = 0.0


def _adv_cache_key(symbol: str, closes: list[float]) -> tuple[str, int, float]:
    if not closes:
        return (symbol.upper(), 0, 0.0)
    return (symbol.upper(), len(closes), float(closes[-1]))


def _get_or_compute_advanced(symbol: str, closes: list[float]) -> AdvancedSignals:
    """Memoised advanced-signals lookup."""
    global _adv_cache_hits, _adv_cache_misses
    global _adv_cache_miss_latency_ms_ema, _adv_cache_miss_latency_ms_last
    if not symbol or not closes or len(closes) < 30:
        return AdvancedSignals()
    key = _adv_cache_key(symbol, closes)
    now = time.monotonic()
    with _adv_cache_lock:
        cached = _adv_cache.get(key)
        if cached is not None:
            ts, sigs = cached
            if (now - ts) < _ADV_CACHE_TTL_S:
                _adv_cache_hits += 1
                return sigs
        _adv_cache_misses += 1
    # Compute outside the lock — CPU work shouldn't serialise other lookups.
    _t0 = time.perf_counter()
    sigs = compute_advanced_signals(closes)
    _dt_ms = (time.perf_counter() - _t0) * 1000.0
    with _adv_cache_lock:
        _adv_cache[key] = (now, sigs)
        _adv_cache_miss_latency_ms_last = _dt_ms
        if _adv_cache_miss_latency_ms_ema <= 0.0:
            _adv_cache_miss_latency_ms_ema = _dt_ms
        else:
            _adv_cache_miss_latency_ms_ema = (
                (1.0 - _LAT_EMA_ALPHA) * _adv_cache_miss_latency_ms_ema
                + _LAT_EMA_ALPHA * _dt_ms
            )
        if len(_adv_cache) > _ADV_CACHE_MAX:
            # Evict oldest 25 % so we don't thrash on every insertion.
            for k, _ in sorted(_adv_cache.items(), key=lambda kv: kv[1][0])[: _ADV_CACHE_MAX // 4]:
                _adv_cache.pop(k, None)
    return sigs


def get_advanced_cache_stats() -> dict[str, float | int]:
    """Telemetry helper — surfaced via the existing variant endpoint."""
    with _adv_cache_lock:
        hits = _adv_cache_hits
        misses = _adv_cache_misses
        total = hits + misses
        hit_rate = (hits / total) if total > 0 else 0.0
        return {
            'size': len(_adv_cache),
            'hits': hits,
            'misses': misses,
            'hit_rate': round(hit_rate, 4),
            'miss_latency_ms_ema': round(_adv_cache_miss_latency_ms_ema, 3),
            'miss_latency_ms_last': round(_adv_cache_miss_latency_ms_last, 3),
            'ttl_seconds': int(_ADV_CACHE_TTL_S),
        }


# ---------------------------------------------------------------------------
# Lab Mode bundle cache.
#
# Parallel to the advanced-signals cache.  Same key shape and TTL so a
# cold daily history triggers one cache miss for BOTH bundles, but the
# Lab cost (≈3-8 ms) is still cheap enough to fetch on demand.
# ---------------------------------------------------------------------------
_lab_cache: dict[tuple[str, int, float], tuple[float, LabSignals]] = {}
_lab_cache_lock = threading.Lock()
_lab_cache_hits = 0
_lab_cache_misses = 0
_lab_cache_miss_latency_ms_ema = 0.0
_lab_cache_miss_latency_ms_last = 0.0


def _get_or_compute_lab(symbol: str, closes: list[float]) -> LabSignals:
    """Memoised Lab signals lookup."""
    global _lab_cache_hits, _lab_cache_misses
    global _lab_cache_miss_latency_ms_ema, _lab_cache_miss_latency_ms_last
    if not symbol or not closes or len(closes) < 30:
        return LabSignals()
    key = _adv_cache_key(symbol, closes)
    now = time.monotonic()
    with _lab_cache_lock:
        cached = _lab_cache.get(key)
        if cached is not None:
            ts, sigs = cached
            if (now - ts) < _ADV_CACHE_TTL_S:
                _lab_cache_hits += 1
                return sigs
        _lab_cache_misses += 1
    _t0 = time.perf_counter()
    sigs = compute_lab_signals(closes)
    _dt_ms = (time.perf_counter() - _t0) * 1000.0
    with _lab_cache_lock:
        _lab_cache[key] = (now, sigs)
        _lab_cache_miss_latency_ms_last = _dt_ms
        if _lab_cache_miss_latency_ms_ema <= 0.0:
            _lab_cache_miss_latency_ms_ema = _dt_ms
        else:
            _lab_cache_miss_latency_ms_ema = (
                (1.0 - _LAT_EMA_ALPHA) * _lab_cache_miss_latency_ms_ema
                + _LAT_EMA_ALPHA * _dt_ms
            )
        if len(_lab_cache) > _ADV_CACHE_MAX:
            for k, _ in sorted(_lab_cache.items(), key=lambda kv: kv[1][0])[: _ADV_CACHE_MAX // 4]:
                _lab_cache.pop(k, None)
    return sigs


def get_lab_cache_stats() -> dict[str, float | int]:
    with _lab_cache_lock:
        hits = _lab_cache_hits
        misses = _lab_cache_misses
        total = hits + misses
        hit_rate = (hits / total) if total > 0 else 0.0
        return {
            'size': len(_lab_cache),
            'hits': hits,
            'misses': misses,
            'hit_rate': round(hit_rate, 4),
            'miss_latency_ms_ema': round(_lab_cache_miss_latency_ms_ema, 3),
            'miss_latency_ms_last': round(_lab_cache_miss_latency_ms_last, 3),
            'ttl_seconds': int(_ADV_CACHE_TTL_S),
        }


# ---------------------------------------------------------------------------
# Strategy Tier bundle cache (Phase 26.50).
# Parallel to the Lab cache; same key shape, same TTL.
# ---------------------------------------------------------------------------
_strategy_cache: dict[tuple[str, int, float], tuple[float, StrategySignals]] = {}
_strategy_cache_lock = threading.Lock()
_strategy_cache_hits = 0
_strategy_cache_misses = 0
_strategy_cache_miss_latency_ms_ema = 0.0
_strategy_cache_miss_latency_ms_last = 0.0


def _get_or_compute_strategy(symbol: str, closes: list[float]) -> StrategySignals:
    """Memoised Strategy signals lookup."""
    global _strategy_cache_hits, _strategy_cache_misses
    global _strategy_cache_miss_latency_ms_ema, _strategy_cache_miss_latency_ms_last
    if not symbol or not closes or len(closes) < 40:
        return StrategySignals()
    key = _adv_cache_key(symbol, closes)
    now = time.monotonic()
    with _strategy_cache_lock:
        cached = _strategy_cache.get(key)
        if cached is not None:
            ts, sigs = cached
            if (now - ts) < _ADV_CACHE_TTL_S:
                _strategy_cache_hits += 1
                return sigs
        _strategy_cache_misses += 1
    _t0 = time.perf_counter()
    sigs = compute_strategy_signals(closes)
    _dt_ms = (time.perf_counter() - _t0) * 1000.0
    with _strategy_cache_lock:
        _strategy_cache[key] = (now, sigs)
        _strategy_cache_miss_latency_ms_last = _dt_ms
        if _strategy_cache_miss_latency_ms_ema <= 0.0:
            _strategy_cache_miss_latency_ms_ema = _dt_ms
        else:
            _strategy_cache_miss_latency_ms_ema = (
                (1.0 - _LAT_EMA_ALPHA) * _strategy_cache_miss_latency_ms_ema
                + _LAT_EMA_ALPHA * _dt_ms
            )
        if len(_strategy_cache) > _ADV_CACHE_MAX:
            for k, _ in sorted(_strategy_cache.items(), key=lambda kv: kv[1][0])[: _ADV_CACHE_MAX // 4]:
                _strategy_cache.pop(k, None)
    return sigs


def get_strategy_cache_stats() -> dict[str, float | int]:
    with _strategy_cache_lock:
        hits = _strategy_cache_hits
        misses = _strategy_cache_misses
        total = hits + misses
        hit_rate = (hits / total) if total > 0 else 0.0
        return {
            'size': len(_strategy_cache),
            'hits': hits,
            'misses': misses,
            'hit_rate': round(hit_rate, 4),
            'miss_latency_ms_ema': round(_strategy_cache_miss_latency_ms_ema, 3),
            'miss_latency_ms_last': round(_strategy_cache_miss_latency_ms_last, 3),
            'ttl_seconds': int(_ADV_CACHE_TTL_S),
        }


def clear_future_mode_caches() -> dict[str, int]:
    """Phase 26.49 — called from the /api/system/reset endpoint.
    Drops ALL caches (AdvancedSignals, LabSignals, StrategySignals,
    PredictiveExpansionSignals) and returns a small payload describing
    what was cleared."""
    with _adv_cache_lock:
        adv_n = len(_adv_cache)
        _adv_cache.clear()
    with _lab_cache_lock:
        lab_n = len(_lab_cache)
        _lab_cache.clear()
    with _strategy_cache_lock:
        strat_n = len(_strategy_cache)
        _strategy_cache.clear()
    with _pred_cache_lock:
        pred_n = len(_pred_cache)
        _pred_cache.clear()
    return {
        'cleared_advanced': adv_n,
        'cleared_lab': lab_n,
        'cleared_strategy': strat_n,
        'cleared_predictive_expansion': pred_n,
    }


# ---------------------------------------------------------------------------
# Phase 26.60 — Predictive Expansion bundle cache.
# Mirrors the Lab/Strategy caches; same key, same TTL.  Compute is
# ~5-15 ms (lightweight HMM + AR fits + curvature polynomial), so the
# cache is mainly to avoid re-running on every priority-lane tick
# for unchanged daily history.
# ---------------------------------------------------------------------------
_pred_cache: dict[tuple[str, int, float], tuple[float, PredictiveExpansionSignals]] = {}
_pred_cache_lock = threading.Lock()
_pred_cache_hits = 0
_pred_cache_misses = 0
_pred_cache_miss_latency_ms_ema = 0.0
_pred_cache_miss_latency_ms_last = 0.0


def _get_or_compute_predictive(
    symbol: str,
    closes: list[float],
    *,
    driver_returns: list[float] | None = None,
    per_horizon_drifts_pct: dict[str, float] | None = None,
    per_horizon_drift_to_vol: dict[str, float] | None = None,
    row: dict | None = None,
    include_reality_breakers: bool = False,
) -> PredictiveExpansionSignals:
    """Memoised Predictive Expansion lookup.

    The cache key intentionally does NOT include the keyword arguments
    (driver returns, per-horizon drifts).  Those are re-applied to a
    cloned bundle so the cache stays compact while the lead-lag /
    multiscale / TRS values still reflect the latest per-row context.
    """
    global _pred_cache_hits, _pred_cache_misses
    global _pred_cache_miss_latency_ms_ema, _pred_cache_miss_latency_ms_last
    if not symbol or not closes or len(closes) < 40:
        return PredictiveExpansionSignals()
    key = _adv_cache_key(symbol, closes)
    now = time.monotonic()
    with _pred_cache_lock:
        cached = _pred_cache.get(key)
        if cached is not None:
            ts, sigs = cached
            if (now - ts) < _ADV_CACHE_TTL_S:
                # Phase 26.61d — if the caller wants reality_breakers
                # but the cached bundle didn't compute them, fall
                # through to recompute below.  Without this guard the
                # stale bundle would surface 0.000 for all 4 overlays.
                stale_rb = (
                    include_reality_breakers
                    and not getattr(sigs, 'reality_breakers_computed', False)
                )
                if not stale_rb:
                    _pred_cache_hits += 1
                    # Recompute the SYMBOL-INDEPENDENT, ROW-DEPENDENT
                    # subset (lead_lag, multiscale, TRS) so the cached
                    # signal reflects the current per-horizon context.
                    return _refresh_context_dependent(
                        sigs,
                        driver_returns=driver_returns,
                        per_horizon_drifts_pct=per_horizon_drifts_pct,
                        per_horizon_drift_to_vol=per_horizon_drift_to_vol,
                        row=row,
                        include_reality_breakers=include_reality_breakers,
                    )
        _pred_cache_misses += 1
    _t0 = time.perf_counter()
    sigs = compute_predictive_expansion(
        closes,
        driver_returns=driver_returns,
        per_horizon_drifts_pct=per_horizon_drifts_pct,
        per_horizon_drift_to_vol=per_horizon_drift_to_vol,
        row=row,
        include_reality_breakers=include_reality_breakers,
    )
    _dt_ms = (time.perf_counter() - _t0) * 1000.0
    with _pred_cache_lock:
        _pred_cache[key] = (now, sigs)
        _pred_cache_miss_latency_ms_last = _dt_ms
        if _pred_cache_miss_latency_ms_ema <= 0.0:
            _pred_cache_miss_latency_ms_ema = _dt_ms
        else:
            _pred_cache_miss_latency_ms_ema = (
                (1.0 - _LAT_EMA_ALPHA) * _pred_cache_miss_latency_ms_ema
                + _LAT_EMA_ALPHA * _dt_ms
            )
        if len(_pred_cache) > _ADV_CACHE_MAX:
            for k, _ in sorted(_pred_cache.items(), key=lambda kv: kv[1][0])[: _ADV_CACHE_MAX // 4]:
                _pred_cache.pop(k, None)
    return sigs


def _refresh_context_dependent(
    sigs: PredictiveExpansionSignals,
    *,
    driver_returns: list[float] | None,
    per_horizon_drifts_pct: dict[str, float] | None,
    per_horizon_drift_to_vol: dict[str, float] | None,
    row: dict | None,
    include_reality_breakers: bool,
) -> PredictiveExpansionSignals:
    """Recompute the row-dependent slice of the bundle WITHOUT redoing
    the heavy symbol-level math.  Cheap: 3 dict lookups + 1 small AR fit."""
    if not sigs.available:
        return sigs
    # We clone the cached bundle so concurrent callers don't see
    # transient half-updated state.
    import copy
    out = copy.copy(sigs)
    try:
        from app.services.predictive_expansion import (
            _multiscale_consistency, _temporal_renormalization_score,
        )
        if per_horizon_drifts_pct:
            out.multiscale_consistency = _multiscale_consistency(per_horizon_drifts_pct)
        if include_reality_breakers and per_horizon_drift_to_vol:
            out.temporal_renormalization_score = _temporal_renormalization_score(
                per_horizon_drift_to_vol
            )
    except Exception:  # noqa: BLE001
        pass
    # Liquidity score lookup is row-dependent and cheap to refresh.
    if row is not None:
        try:
            from app.services.predictive_expansion import (
                _liquidity_norm_from_row, _liq_adjusted_signal,
            )
            liq = _liquidity_norm_from_row(row)
            out.liquidity_score_norm = liq
            out.liq_adjusted_signal = _liq_adjusted_signal(
                out.predictability_score_norm, liq
            )
        except Exception:  # noqa: BLE001
            pass
    return out


def get_predictive_cache_stats() -> dict[str, float | int]:
    with _pred_cache_lock:
        hits = _pred_cache_hits
        misses = _pred_cache_misses
        total = hits + misses
        hit_rate = (hits / total) if total > 0 else 0.0
        return {
            'size': len(_pred_cache),
            'hits': hits,
            'misses': misses,
            'hit_rate': round(hit_rate, 4),
            'miss_latency_ms_ema': round(_pred_cache_miss_latency_ms_ema, 3),
            'miss_latency_ms_last': round(_pred_cache_miss_latency_ms_last, 3),
            'ttl_seconds': int(_ADV_CACHE_TTL_S),
        }


def _load_closes_cached(symbol: str) -> list[float]:
    """Best-effort daily-close lookup using the in-process daily-history
    cache.  Never triggers an HTTP fetch (`allow_fetch=False`) — Future
    Mode must stay non-blocking.  Returns `[]` when no history is
    available yet."""
    try:
        from app.services.daily_history_service import get_daily_history
    except Exception:  # noqa: BLE001
        return []
    try:
        df = get_daily_history(symbol, allow_fetch=False, blocking=False)
        if df is None or df.empty:
            return []
        return [float(c) for c in df['Close'].dropna().tolist()]
    except Exception:  # noqa: BLE001
        return []


# ---------------------------------------------------------------------------
# Phase 26.61d — Market-proxy returns cache.
#
# Extracted to `market_proxy_service` (Phase 26.70).  We keep local
# aliases with the historical `_` prefix so any internal callers +
# tests continue to work without a signature change.
# ---------------------------------------------------------------------------
from app.services.market_proxy_service import (  # noqa: E402
    MARKET_PROXY_SYMBOL_STOCKS as _MARKET_PROXY_SYMBOL_STOCKS,
    MARKET_PROXY_SYMBOL_CRYPTO as _MARKET_PROXY_SYMBOL_CRYPTO,
    MARKET_PROXY_TTL_S as _MARKET_PROXY_TTL_S,
    _market_proxy_cache,
    proxy_symbol_for as _proxy_symbol_for,
    load_market_proxy_returns as _load_market_proxy_returns,
)


# Trading-style → horizon mapping.  Used by the frontend ranking
# selector AND by the detail-panel "Future Forecast" card to pick
# which pre-computed horizon block to surface.
TRADING_STYLE_HORIZONS = {
    '1h_hold':  {'forward_key': 'forward_1h',  'units': 1,  'is_intraday': True,  'label': '1-hour hold'},
    '5h_hold':  {'forward_key': 'forward_5h',  'units': 5,  'is_intraday': True,  'label': '5-hour hold'},
    'short':    {'forward_key': 'forward_1d',  'units': 1,  'is_intraday': False, 'label': 'Short (1-2 day hold)'},
    'swing':    {'forward_key': 'forward_5d',  'units': 5,  'is_intraday': False, 'label': 'Swing (3-10 day hold)'},
    'long':     {'forward_key': 'forward_20d', 'units': 20, 'is_intraday': False, 'label': 'Long (10+ day hold)'},
    'overnight_hold': {'forward_key': 'forward_overnight', 'units': 1, 'is_intraday': False, 'label': 'Overnight (close→next open)'},
    'weekend_hold':   {'forward_key': 'forward_weekend',   'units': 1, 'is_intraday': False, 'label': 'Weekend (Fri close→Mon open)'},
    'default':  {'forward_key': 'forward_5d',  'units': 5,  'is_intraday': False, 'label': 'Default (5-day Kelly)'},
}

# The full set of horizons we pre-compute on every row.
# Extracted to `horizon_definitions` (Phase 26.70); alias kept for
# backwards compatibility with any internal caller.
from app.services.horizon_definitions import ALL_HORIZONS as _ALL_HORIZONS  # noqa: E402


def _extract_factor_scores(row: dict) -> dict[str, float]:
    """Pull every factor score the Bayesian blend understands off the
    row in the shape that `blend_factors_for_drift` expects (dict of
    family_name → 0-100 score).

    Robust to both compact-mode and full-mode rows.  Missing factors
    are simply omitted (the blend treats them as zero-precision and
    drops them from the posterior).
    """
    scores: dict[str, float | None] = {}
    # Phase 26.50 bugfix — cheap-pass rows from `score_from_prices()`
    # store the core ratings under `factor_breakdown.ratings.{family}.score`.
    # `algorithm_ratings` is only synthesised by `normalize_result_row`
    # at API output time, AFTER attach_forward_metrics_fast has already
    # run.  Without checking the breakdown-ratings fallback below,
    # every cheap-pass row was getting `forward_metrics = None`
    # because factor_scores was empty.  Result: Future Mode columns
    # were blank for everything except the small subset of rows that
    # had been promoted to pass-2 full scoring.
    fb = row.get('factor_breakdown') or {}
    fb_ratings = (fb.get('ratings') if isinstance(fb, dict) else None) or {}
    # Algorithm-rating cards live under .algorithm_ratings[key].score.
    ratings = row.get('algorithm_ratings') or {}
    for k in ('momentum', 'quality', 'trend', 'stability'):
        r = ratings.get(k) or fb_ratings.get(k) or {}
        if isinstance(r, dict) and r.get('score') is not None:
            scores[k] = float(r['score'])
        elif isinstance(r, (int, float)):
            # legacy flat-scalar shape, just in case
            scores[k] = float(r)
    # Extended factor families live under .factor_breakdown.market.{key}.score
    fb = row.get('factor_breakdown') or {}
    mkt = fb.get('market') or {}
    family_aliases = {
        # blend-name           : (path keys to try, in order)
        'volume_sentiment':         ('volume_sentiment',),
        'options_positioning':      ('options_positioning',),
        'institutional_confluence': ('institutional_confluence',),
        'institutional_order_block':('institutional_order_block',),
        'dark_pool_proxy':          ('dark_pool_attraction', 'dark_pool_proxy'),
        'reaction_clustering':      ('reaction_clustering',),
    }
    for name, keys in family_aliases.items():
        for k in keys:
            ext = mkt.get(k) or {}
            if isinstance(ext, dict):
                for cand_key in ('score', 'composite', 'bias_score'):
                    if ext.get(cand_key) is not None:
                        scores[name] = float(ext[cand_key])
                        break
                if name in scores:
                    break
    # Secondary-composite family scores (the 7-family bayes feed).
    sc = (fb.get('secondary_composite') or {}).get('family_scores') or {}
    for k, v in sc.items():
        if k not in scores and v is not None:
            scores[k] = float(v)
    return {k: v for k, v in scores.items() if v is not None}


def _row_atr_pct(row: dict) -> float:
    """Best-effort ATR-% lookup from the scored row.

    Different scoring tiers stash ATR under slightly different paths;
    we probe them all.  Default of 2 % matches the safe baseline used
    in `predict_price` so the forward block always has a sigma to use.
    """
    candidates = [
        row.get('atr_pct'),
        (row.get('factor_breakdown') or {}).get('atr_pct'),
        ((row.get('factor_breakdown') or {}).get('market') or {}).get('atr_pct'),
        (row.get('algorithm_ratings') or {}).get('momentum', {}).get('atr_pct'),
    ]
    for c in candidates:
        try:
            v = float(c) if c is not None else 0.0
        except (TypeError, ValueError):
            v = 0.0
        if 0.05 < v < 50.0:  # sanity band
            return v
    return 2.0  # safe baseline


def _direction_label_from_p_up(p_up: float) -> str:
    if p_up >= 0.55:
        return 'Bullish'
    if p_up <= 0.45:
        return 'Bearish'
    return 'Neutral'


def _compute_one_horizon_fast(factor_scores: dict[str, float], atr_per_unit_pct: float,
                              regulatory_signal: dict | None,
                              horizon_units: int, is_intraday: bool) -> dict[str, Any]:
    """Single-horizon forward block.  Pure math, no I/O.  ~50 µs.

    ATR is treated as the per-unit-time sigma proxy.  For daily
    horizons that's the daily ATR; for intraday horizons we scale
    daily ATR down by sqrt(6.5) to get a per-hour proxy.  This is
    a coarser estimate than the GARCH path uses but it's the right
    accuracy tier for ranking 800+ symbols every 2 s.
    """
    sigma_per_unit_pct = atr_per_unit_pct
    if is_intraday:
        # Daily ATR / sqrt(6.5 trading hours) ≈ per-hour sigma proxy.
        sigma_per_unit_pct = atr_per_unit_pct / math.sqrt(6.5)

    blend = blend_factors_for_drift(
        factor_scores=factor_scores,
        final_direction_sign=0,  # unused
        horizon=horizon_units,
        is_intraday=is_intraday,
        regulatory_signal=regulatory_signal,
    )
    drift_h = blend.total_drift_horizon_pct
    sigma_h = sigma_per_unit_pct * math.sqrt(max(1, horizon_units))
    if sigma_h > 0:
        z = drift_h / sigma_h
        p_up = normal_cdf(z)
    else:
        z = 0.0
        p_up = 0.5
    directional_certainty = max(0.0, 2.0 * abs(p_up - 0.5))
    # Kelly-style: signed expected return × √precision × directional certainty.
    # Bearish positions get a negative kelly_rank; abs is used for sort order.
    kelly_rank = drift_h * math.sqrt(max(0.0, blend.posterior_precision)) * directional_certainty
    return {
        'drift_pct': round(drift_h, 5),
        'sigma_pct': round(sigma_h, 5),
        'p_up': round(p_up, 4),
        'p_down': round(1.0 - p_up, 4),
        'directional_certainty': round(directional_certainty, 4),
        'direction': _direction_label_from_p_up(p_up),
        'kelly_rank': round(kelly_rank, 6),
        'kelly_rank_abs': round(abs(kelly_rank), 6),
        'posterior_precision': round(blend.posterior_precision, 4),
        'n_factors': blend.n_factors_used,
        'shrinkage': round(blend.shrinkage_factor, 4),
        'tier': 'fast',
    }


# =========================================================================
# Phase 26.64 — calendar-gap session horizons (overnight + weekend).
#
# Overnight (close→next open) and weekend (Fri close→Mon open) are
# market-CLOSED gap holds, so the √(trading-hours) intraday scaling does
# NOT apply.  We start from the 1-day daily blend and reshape drift +
# sigma using the empirical session structure of US equities.
# =========================================================================
_SESSION_HORIZON_PARAMS = {
    # kind:        (drift_weight, variance_share, sigma_widen, label)
    'overnight': (0.55, 0.40, 1.00, 'Overnight (close→next open)'),
    'weekend':   (0.90, 0.65, 1.15, 'Weekend (Fri close→Mon open)'),
}


def _compute_session_horizon_fast(factor_scores: dict[str, float],
                                   atr_per_unit_pct: float,
                                   regulatory_signal: dict | None,
                                   kind: str) -> dict[str, Any]:
    """Overnight / weekend gap-hold forward block.

    Empirical session structure baked into the constants:

      * Overnight: equities realise a disproportionate share of total
        return overnight (the documented "overnight return anomaly"),
        while only ~40 % of daily VARIANCE accrues overnight.  Gap/jump
        risk concentrates at the open, so the jump-drift overlay (added
        by enrich_horizon_block at h=1) keeps full daily weight.
            μ_on = 0.55·μ_day      σ_on = σ_day·√0.40
      * Weekend: ~2.5 calendar days closed accrue more news than a single
        overnight → higher variance share (~0.65) and a 1.15× widening
        for the extra gap uncertainty; drift closer to a full day.
            μ_we = 0.90·μ_day      σ_we = σ_day·√0.65·1.15
    """
    w_drift, var_share, widen, _label = _SESSION_HORIZON_PARAMS[kind]
    blend = blend_factors_for_drift(
        factor_scores=factor_scores,
        final_direction_sign=0,
        horizon=1,
        is_intraday=False,
        regulatory_signal=regulatory_signal,
    )
    mu_day = blend.total_drift_horizon_pct
    sigma_day = atr_per_unit_pct
    drift_h = w_drift * mu_day
    sigma_h = sigma_day * math.sqrt(var_share) * widen
    if sigma_h > 0:
        z = drift_h / sigma_h
        p_up = normal_cdf(z)
    else:
        p_up = 0.5
    directional_certainty = max(0.0, 2.0 * abs(p_up - 0.5))
    kelly_rank = drift_h * math.sqrt(max(0.0, blend.posterior_precision)) * directional_certainty
    return {
        'drift_pct': round(drift_h, 5),
        'sigma_pct': round(sigma_h, 5),
        'p_up': round(p_up, 4),
        'p_down': round(1.0 - p_up, 4),
        'directional_certainty': round(directional_certainty, 4),
        'direction': _direction_label_from_p_up(p_up),
        'kelly_rank': round(kelly_rank, 6),
        'kelly_rank_abs': round(abs(kelly_rank), 6),
        'posterior_precision': round(blend.posterior_precision, 4),
        'n_factors': blend.n_factors_used,
        'shrinkage': round(blend.shrinkage_factor, 4),
        'tier': 'fast',
        'session_kind': kind,
    }


def _attach_session_horizons(out: dict[str, Any], *,
                             factor_scores: dict[str, float],
                             atr_pct: float,
                             regulatory_signal: dict | None,
                             advanced_signals, lab_signals, strategy_signals,
                             predictive_signals,
                             fast_blocks: dict | None = None) -> dict[str, Any]:
    """Append fully-enriched overnight + weekend blocks to `out`.

    Called AFTER the canonical-horizon multiscale/TRS recompute so those
    per-symbol composites stay defined on the 5 standard horizons only;
    the session blocks then inherit the finalised composite fields from
    the daily (`forward_1d`) template for ranking consistency.
    """
    if not factor_scores:
        return out
    template = out.get('forward_1d') or {}
    fast_blocks = fast_blocks or {}
    for skey, kind in (('forward_overnight', 'overnight'),
                       ('forward_weekend', 'weekend')):
        block = _compute_session_horizon_fast(
            factor_scores, atr_pct, regulatory_signal, kind,
        )
        enrich_horizon_block(
            horizon_block=block, advanced=advanced_signals,
            horizon_units=1, is_intraday=False,
        )
        enrich_horizon_block_lab(
            horizon_block=block, lab=lab_signals,
            other_tier_block=fast_blocks.get(skey),
        )
        enrich_horizon_block_strategy(horizon_block=block, strategy=strategy_signals)
        enrich_horizon_block_predictive(
            horizon_block=block, signals=predictive_signals,
            include_reality_breakers=True,
        )
        for f in ('multiscale_consistency', 'strategy_v2_rank_multiplier',
                  'temporal_renormalization_score', 'reality_breaker_multiplier'):
            if f in template:
                block[f] = template[f]
        out[skey] = block
    return out


def attach_forward_metrics_fast(row: dict, regulatory_signal: dict | None = None,
                                advanced_signals: AdvancedSignals | None = None,
                                market: str | None = None) -> dict:
    """Attach `forward_metrics` (all 5 horizons, fast tier) to a row.

    Returns the row (also mutates in place for convenience).  Safe to
    call multiple times — each call recomputes from current factor
    scores + ATR (which is what we want during repeated re-scoring).

    When advanced-math signals are available (or computable from the
    daily-history cache) we ALSO enrich every horizon block with the
    Cornish-Fisher p_up, VaR/CVaR, jump-drift, regime weight, and the
    `effective_kelly_rank` field that Future Mode sorts on.

    `market` selects the LCC driver basket (SPY for stocks, BTC-USD
    for crypto).  When omitted, we infer from the `-USD` suffix so
    legacy callers keep working without a signature change.
    """
    factor_scores = _extract_factor_scores(row)
    atr_pct = _row_atr_pct(row)

    out: dict[str, Any] = {}
    # Skip the whole block when there's literally nothing to feed the
    # blend — this stops us decorating cheap-pass rows that have no
    # factor depth.  The frontend's Future Mode ranking falls back to
    # the composite when forward_metrics is absent.
    if not factor_scores:
        row['forward_metrics'] = None
        row['advanced_signals'] = None
        return row

    # Pull (or reuse) the per-symbol advanced-math bundle.  This is
    # scale-invariant across horizons, so we compute it once.
    if advanced_signals is None:
        symbol = (row.get('symbol') or '').upper()
        closes = _load_closes_cached(symbol) if symbol else []
        advanced_signals = _get_or_compute_advanced(symbol, closes)
        lab_signals = _get_or_compute_lab(symbol, closes) if symbol else LabSignals()
        strategy_signals = _get_or_compute_strategy(symbol, closes) if symbol else StrategySignals()
    else:
        symbol = (row.get('symbol') or '').upper()
        closes = _load_closes_cached(symbol) if symbol else []
        lab_signals = _get_or_compute_lab(symbol, closes) if symbol else LabSignals()
        strategy_signals = _get_or_compute_strategy(symbol, closes) if symbol else StrategySignals()

    # Phase 26.60 — Predictive Expansion bundle.  Computed once per
    # symbol (cached); enriched per-horizon below.
    #
    # Phase 26.61d — Reality-breaker overlays are ALWAYS computed
    # now.  Driver-returns come from a cached market-appropriate
    # proxy (SPY for stocks, BTC-USD for crypto) so LCC has real
    # data; the per-horizon drift/sigma context (for TRS) is
    # populated after the horizons loop below.
    _market_kind = ((market or ('crypto' if symbol.endswith('-USD') else 'stocks'))
                    if symbol else market)
    market_proxy_returns = _load_market_proxy_returns(_market_kind)
    predictive_signals = _get_or_compute_predictive(
        symbol, closes,
        driver_returns=market_proxy_returns,
        per_horizon_drifts_pct=None,  # populated after horizons loop
        row=row,
        include_reality_breakers=True,
    ) if symbol else PredictiveExpansionSignals()

    for key, units, is_intraday in _ALL_HORIZONS:
        block = _compute_one_horizon_fast(
            factor_scores=factor_scores,
            atr_per_unit_pct=atr_pct,
            regulatory_signal=regulatory_signal,
            horizon_units=units,
            is_intraday=is_intraday,
        )
        # Overlay the advanced-math signals.
        enrich_horizon_block(
            horizon_block=block,
            advanced=advanced_signals,
            horizon_units=units,
            is_intraday=is_intraday,
        )
        # Phase 26.49 — Lab Mode overlay.
        enrich_horizon_block_lab(
            horizon_block=block,
            lab=lab_signals,
            other_tier_block=None,
        )
        # Phase 26.50 — Strategy Tier overlay.
        enrich_horizon_block_strategy(
            horizon_block=block,
            strategy=strategy_signals,
        )
        # Phase 26.60 — Predictive Expansion overlay (10 new metrics +
        # 4 composite multipliers + reality_breaker_multiplier).
        # Phase 26.61d — Reality-breaker VALUES are now written into
        # the horizon block (frontend gates display via toggles).
        enrich_horizon_block_predictive(
            horizon_block=block,
            signals=predictive_signals,
            include_reality_breakers=True,
        )
        out[key] = block
    row['forward_metrics'] = out
    # Phase 26.60 — recompute multiscale_consistency now that we have
    # every horizon's drift_pct, and propagate back into each block's
    # value (cheap dict update — no full re-fit).
    # Phase 26.61d — also recompute the Temporal-Renormalisation
    # Score (TRS) using per-horizon drift/sigma ratios, and the
    # reality_breaker_multiplier composite that depends on it.
    try:
        drifts_pct = {
            k: float(v.get('drift_pct', 0.0))
            for k, v in (out or {}).items()
            if isinstance(v, dict)
        }
        drift_to_vol = {}
        for k, v in (out or {}).items():
            if not isinstance(v, dict):
                continue
            sigma = float(v.get('sigma_pct') or 0.0)
            drift = float(v.get('drift_pct') or 0.0)
            if sigma > 1e-9:
                drift_to_vol[k] = drift / sigma
        if drifts_pct:
            from app.services.predictive_expansion import (
                _multiscale_consistency, _temporal_renormalization_score,
                compute_strategy_v2_rank_multiplier,
                compute_reality_breaker_multiplier,
            )
            msc = _multiscale_consistency(drifts_pct)
            predictive_signals.multiscale_consistency = msc
            if drift_to_vol:
                trs = _temporal_renormalization_score(drift_to_vol)
                predictive_signals.temporal_renormalization_score = trs
            # Apply user weight exponent on the recomputed RB multiplier
            try:
                from app.services.metrics_hub_service import apply_multiplier_exponent
                rb_mult = apply_multiplier_exponent(
                    'reality_breaker_multiplier',
                    compute_reality_breaker_multiplier(predictive_signals),
                )
            except Exception:  # noqa: BLE001
                rb_mult = compute_reality_breaker_multiplier(predictive_signals)
            for k, blk in out.items():
                if isinstance(blk, dict):
                    blk['multiscale_consistency'] = round(msc, 5)
                    blk['strategy_v2_rank_multiplier'] = round(
                        compute_strategy_v2_rank_multiplier(predictive_signals), 4
                    )
                    if drift_to_vol:
                        blk['temporal_renormalization_score'] = round(
                            predictive_signals.temporal_renormalization_score, 5
                        )
                    blk['reality_breaker_multiplier'] = round(rb_mult, 4)
    except Exception:  # noqa: BLE001
        pass
    # Phase 26.64 — overnight + weekend gap-hold horizons (added AFTER
    # the canonical multiscale/TRS recompute so those composites stay
    # defined on the 5 standard horizons).
    _attach_session_horizons(
        out,
        factor_scores=factor_scores, atr_pct=atr_pct,
        regulatory_signal=regulatory_signal,
        advanced_signals=advanced_signals, lab_signals=lab_signals,
        strategy_signals=strategy_signals, predictive_signals=predictive_signals,
    )
    # Stamp the per-symbol bundles (advanced + lab + strategy + predictive).
    attach_per_symbol_signals(row, advanced_signals)
    attach_per_symbol_lab(row, lab_signals)
    attach_per_symbol_strategy(row, strategy_signals)
    attach_per_symbol_predictive(row, predictive_signals)
    # Scanner-context overlay: short pressure / predicted volume intensity /
    # options expiration feed straight into the forecast blocks.
    try:
        from app.services.forecast_context import apply_forecast_context, summarize_forecast
        apply_forecast_context(row, out)
        row['future_forecast_ready'] = True
        row['future_forecast_summary'] = summarize_forecast(row)
    except Exception:  # noqa: BLE001
        pass
    return row


def attach_forward_metrics_garch(row: dict, symbol: str, market: str = 'stocks') -> dict:
    """Replace the row's `forward_metrics` with the GARCH-quality
    equivalent.  Only called from the top-N priority lane (Phase
    26.47).  Falls back gracefully to the fast tier if the GARCH fit
    can't be performed (e.g. < 20 daily observations).

    Stored under `forward_metrics_garch` so the fast-tier block stays
    available as well — the detail panel surfaces both.

    Each GARCH block ALSO carries the full advanced-math overlay
    (Cornish-Fisher p_up, VaR/CVaR, jump-drift, regime weight,
    effective_kelly_rank).  When the priority lane is active, these
    are the fields the table sorts on for the top-25.
    """
    try:
        from app.services.garch_volatility import garch_forecast
    except Exception:  # noqa: BLE001
        return row

    closes = _load_closes_cached(symbol) if symbol else []
    # Phase 26.50 bugfix — if the in-process daily-history cache hasn't
    # been primed for this symbol yet (cold start, post-invalidation,
    # throttled prefetch), DON'T silently degrade to fast tier.  Make
    # ONE blocking fetch attempt so the GARCH overlay can run.  Bail
    # out only if even the blocking fetch produces no data (delisted
    # symbol, persistent provider outage, etc.).
    if len(closes) < 20 and symbol:
        try:
            from app.services.daily_history_service import get_daily_history
            df = get_daily_history(symbol, allow_fetch=True, blocking=True)
            if df is not None and not df.empty:
                closes = [float(c) for c in df['Close'].dropna().tolist()]
        except Exception:  # noqa: BLE001
            pass
    factor_scores = _extract_factor_scores(row)
    if not factor_scores or len(closes) < 20:
        # Phase 26.52 — DO NOT wipe an existing GARCH overlay just
        # because THIS tick happened to lack enough history (transient
        # provider throttle, cold cache, etc.).  Leave the field
        # unchanged so the upsert path's preserve logic carries the
        # last-known-good overlay forward.  Only set None when the row
        # has never had a GARCH overlay attached — that lets the
        # frontend correctly show fast tier on those rows.
        if 'forward_metrics_garch' not in row or not row.get('forward_metrics_garch'):
            row['forward_metrics_garch'] = None
        return row

    # Advanced-math bundle (cached — usually a hit because the fast
    # tier already populated the cache moments ago).
    advanced_signals = _get_or_compute_advanced(symbol, closes)
    # Phase 26.49 — Lab Mode bundle (same cache TTL).
    lab_signals = _get_or_compute_lab(symbol, closes)
    # Phase 26.50 — Strategy Tier bundle (same cache TTL).
    strategy_signals = _get_or_compute_strategy(symbol, closes)
    # Phase 26.60 — Predictive Expansion bundle (same cache TTL).
    # Phase 26.61d — reality_breakers always computed; market-
    # appropriate proxy (SPY for stocks, BTC-USD for crypto)
    # supplies driver_returns for LCC.
    market_proxy_returns = _load_market_proxy_returns(market)
    predictive_signals = _get_or_compute_predictive(
        symbol, closes,
        driver_returns=market_proxy_returns,
        row=row, include_reality_breakers=True,
    )

    # Pull the existing fast-tier blocks (if attached) so the GARCH
    # tier's Lab overlay can compute quantum-interference certainty
    # using both forecasts.
    fast_blocks = row.get('forward_metrics') or {}

    out: dict[str, Any] = {}
    for key, units, is_intraday in _ALL_HORIZONS:
        # GARCH is fit on daily returns. For intraday horizons we still
        # use the fast-tier sigma (intraday ATR / sqrt(6.5)) since we
        # don't fit GARCH on 5-min bars here (the dedicated detail-
        # panel call DOES, but for batch scoring this is the right
        # accuracy/cost tier).
        if is_intraday:
            block = _compute_one_horizon_fast(
                factor_scores=factor_scores,
                atr_per_unit_pct=_row_atr_pct(row),
                regulatory_signal=None,
                horizon_units=units,
                is_intraday=is_intraday,
            )
            block['tier'] = 'garch-mixed'
        else:
            gf = garch_forecast(closes, units)
            sigma_h = gf.h_period_sigma
            blend = blend_factors_for_drift(
                factor_scores=factor_scores,
                final_direction_sign=0,
                horizon=units,
                is_intraday=False,
                regulatory_signal=None,
            )
            drift_h = blend.total_drift_horizon_pct
            z = (drift_h / sigma_h) if sigma_h > 0 else 0.0
            p_up = normal_cdf(z)
            directional_certainty = max(0.0, 2.0 * abs(p_up - 0.5))
            kelly_rank = drift_h * math.sqrt(max(0.0, blend.posterior_precision)) * directional_certainty
            block = {
                'drift_pct': round(drift_h, 5),
                'sigma_pct': round(sigma_h, 5),
                'p_up': round(p_up, 4),
                'p_down': round(1.0 - p_up, 4),
                'directional_certainty': round(directional_certainty, 4),
                'direction': _direction_label_from_p_up(p_up),
                'kelly_rank': round(kelly_rank, 6),
                'kelly_rank_abs': round(abs(kelly_rank), 6),
                'posterior_precision': round(blend.posterior_precision, 4),
                'n_factors': blend.n_factors_used,
                'shrinkage': round(blend.shrinkage_factor, 4),
                'garch_alpha': gf.alpha,
                'garch_beta': gf.beta,
                'garch_persistence': round(gf.persistence, 4),
                'garch_annualised_vol_pct': round(gf.annualised_vol_pct, 2),
                'garch_n_obs': gf.n_observations,
                'garch_source': gf.source,
                'tier': 'garch',
            }
        # Apply the advanced-math overlay to both intraday and daily
        # blocks.  This guarantees the GARCH tier has the same set of
        # signal-rich fields as the fast tier (just with a better σ
        # for daily horizons).
        enrich_horizon_block(
            horizon_block=block,
            advanced=advanced_signals,
            horizon_units=units,
            is_intraday=is_intraday,
        )
        # Phase 26.49 — Lab Mode overlay.  Pass the matching FAST
        # block so quantum-interference certainty fuses both tiers.
        enrich_horizon_block_lab(
            horizon_block=block,
            lab=lab_signals,
            other_tier_block=fast_blocks.get(key),
        )
        # Phase 26.50 — Strategy Tier overlay (scale-invariant; same
        # multiplier on GARCH and fast blocks).
        enrich_horizon_block_strategy(
            horizon_block=block,
            strategy=strategy_signals,
        )
        # Phase 26.60 — Predictive Expansion overlay on GARCH blocks too.
        # Phase 26.61d — reality_breaker VALUES now flow through here
        # so the frontend's per-overlay toggles have real data to show.
        enrich_horizon_block_predictive(
            horizon_block=block,
            signals=predictive_signals,
            include_reality_breakers=True,
        )
        out[key] = block
    row['forward_metrics_garch'] = out
    # Phase 26.60 — re-apply multiscale_consistency now that we have
    # every GARCH-block's drift_pct.
    # Phase 26.61d — also recompute TRS + RB multiplier with the
    # full horizon context (same fix as the fast-attach path).
    try:
        drifts_pct = {
            k: float(v.get('drift_pct', 0.0))
            for k, v in (out or {}).items()
            if isinstance(v, dict)
        }
        drift_to_vol = {}
        for k, v in (out or {}).items():
            if not isinstance(v, dict):
                continue
            sigma = float(v.get('sigma_pct') or 0.0)
            drift = float(v.get('drift_pct') or 0.0)
            if sigma > 1e-9:
                drift_to_vol[k] = drift / sigma
        if drifts_pct:
            from app.services.predictive_expansion import (
                _multiscale_consistency, _temporal_renormalization_score,
                compute_strategy_v2_rank_multiplier,
                compute_reality_breaker_multiplier,
            )
            msc = _multiscale_consistency(drifts_pct)
            predictive_signals.multiscale_consistency = msc
            if drift_to_vol:
                predictive_signals.temporal_renormalization_score = (
                    _temporal_renormalization_score(drift_to_vol)
                )
            try:
                from app.services.metrics_hub_service import apply_multiplier_exponent
                rb_mult = apply_multiplier_exponent(
                    'reality_breaker_multiplier',
                    compute_reality_breaker_multiplier(predictive_signals),
                )
            except Exception:  # noqa: BLE001
                rb_mult = compute_reality_breaker_multiplier(predictive_signals)
            for k, blk in out.items():
                if isinstance(blk, dict):
                    blk['multiscale_consistency'] = round(msc, 5)
                    blk['strategy_v2_rank_multiplier'] = round(
                        compute_strategy_v2_rank_multiplier(predictive_signals), 4
                    )
                    if drift_to_vol:
                        blk['temporal_renormalization_score'] = round(
                            predictive_signals.temporal_renormalization_score, 5
                        )
                    blk['reality_breaker_multiplier'] = round(rb_mult, 4)
    except Exception:  # noqa: BLE001
        pass
    # Phase 26.64 — overnight + weekend gap-hold horizons (GARCH tier).
    _attach_session_horizons(
        out,
        factor_scores=factor_scores, atr_pct=_row_atr_pct(row),
        regulatory_signal=None,
        advanced_signals=advanced_signals, lab_signals=lab_signals,
        strategy_signals=strategy_signals, predictive_signals=predictive_signals,
        fast_blocks=fast_blocks,
    )
    # Refresh the per-symbol bundles on the row.
    attach_per_symbol_signals(row, advanced_signals)
    attach_per_symbol_lab(row, lab_signals)
    attach_per_symbol_strategy(row, strategy_signals)
    attach_per_symbol_predictive(row, predictive_signals)
    # Scanner-context overlay on the GARCH tier as well.
    try:
        from app.services.forecast_context import apply_forecast_context, summarize_forecast
        apply_forecast_context(row, out)
        row['future_forecast_ready'] = True
        row['future_forecast_summary'] = summarize_forecast(row)
    except Exception:  # noqa: BLE001
        pass
    return row
