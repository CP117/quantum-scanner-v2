"""Phase 26.61 — Metrics Hub backend service.

Provides the data backing the new "Metrics Hub" subpage:
  * Algorithm catalog with status / dependencies / data flows
  * Cache hit-rate + latency telemetry for every tier
  * Provider health snapshot (yfinance / Finnhub / CBOE / SEC etc.)
  * Per-symbol metric registry for the popover system
  * Real-time priority-lane + scan progress

The hub also exposes user-editable WEIGHT OVERRIDES that thread
through:
  * `factor_weights`        — multiplicative scaling on the 11
    pillar weights that build `final_score`.
  * `multiplier_exponents`  — exponent gain ∈ [0.0, 2.0] applied
    to each of the 8 ranking-pipeline multipliers (lab, strategy,
    strategy_v2, regime_risk, liq_kelly, ml, reality_breaker).
    Exponent 0 = neutralise (multiplier^0 = 1).
    Exponent 1 = unchanged.
    Exponent 2 = amplify the multiplier's effect.
  * `enabled_metrics`       — per-metric on/off mask used by the
    scoring pipeline to optionally drop a metric from the composite.

All overrides DEGRADE TO DEFAULTS when the file is missing,
malformed, or contains a non-finite value.
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
from pathlib import Path
from typing import Any

import logging

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Storage location: persisted alongside the rest of the user-mutable
# state (`app/data/` ships with the app and is git-ignored).
# ---------------------------------------------------------------------------
_APP_DATA = Path(__file__).resolve().parent.parent / 'data'
_APP_DATA.mkdir(parents=True, exist_ok=True)
_WEIGHTS_PATH = _APP_DATA / 'metrics_hub_weights.json'

_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Default weight matrix.  These are the source-of-truth defaults; they
# match the existing pipeline so a freshly-installed app behaves
# IDENTICALLY to the pre-Phase-26.61 build.  User overrides modify
# multiplicative scaling around these defaults, not replace them.
# ---------------------------------------------------------------------------
_DEFAULT_WEIGHTS: dict[str, Any] = {
    # Pillar weights — multiplicative scalers in [0.0, 2.0].
    # Default 1.0 means "use the pipeline's baked-in weight as-is".
    # Setting a pillar's scaler to 0.0 EXCLUDES that pillar from the
    # composite (its contribution is zeroed pre-normalisation).
    'factor_weights': {
        # Snapshot-pipeline pillars (existing 26.5x semantics)
        'momentum_strength':         1.0,
        'trend_volume_delta':        1.0,
        'institutional_confluence':  1.0,
        'options_positioning':       1.0,
        'institutional_order_block': 1.0,
        'dark_pool_attraction':      1.0,
        'reaction_clustering':       1.0,
        'volume_sentiment':          1.0,
        'effort_vs_result':          1.0,
        'predictive_consensus':      1.0,
        'fundamentals':              1.0,
        'regulatory_signal':         1.0,
    },
    # Phase 26.60 multiplier-pipeline exponents in [0.0, 2.0].
    # final_multiplier_used = multiplier ** exponent.
    'multiplier_exponents': {
        'lab_rank_multiplier':         1.0,
        'strategy_rank_multiplier':    1.0,
        'strategy_v2_rank_multiplier': 1.0,
        'regime_risk_multiplier':      1.0,
        'liq_kelly_factor':            1.0,
        'ml_rank_multiplier':          1.0,
        'reality_breaker_multiplier':  1.0,
    },
    # Per-metric enable mask (default all True).  When False the
    # corresponding metric is zeroed out PRE-composite so a single
    # bad signal can be quarantined without disabling the whole tier.
    'enabled_metrics': {
        # 26.60 standard
        'msm_drift_premium':                True,
        'ts_nonlinear_dependence':          True,
        'trend_curvature_pct':              True,
        'lead_lag_influence':               True,
        'volofvol_regime_score':            True,
        'multiscale_consistency':           True,
        'entropy_regime_stability':         True,
        'drawdown_memory_score':            True,
        'liq_adjusted_signal':              True,
        'ml_residual_edge':                 True,
        # 26.60 reality breakers — default OFF in computation but
        # the user can flip them on individually here.  This mirrors
        # the `Advanced Experimental Mode` master toggle on the
        # frontend; the backend only honours these when the master
        # is also set in the request payload.
        'local_causal_cone_signal':              False,
        'quantum_path_interference_index':       False,
        'local_lyapunov_volatility_exponent':    False,
        'temporal_renormalization_score':        False,
    },
    # Power-user options.  Default values match the pipeline's baked-in
    # constants — overriding lets the user experiment.
    'pipeline_tuning': {
        # Floor / ceiling for sane multiplier composition (defends
        # against degenerate compound multipliers).
        'multiplier_floor': 0.05,
        'multiplier_ceiling': 20.0,
    },
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def get_default_weights() -> dict:
    """Deep-copy the default weight matrix — used for the 'Reset' button."""
    import copy
    return copy.deepcopy(_DEFAULT_WEIGHTS)


def _coerce_to_float(v, lo: float, hi: float, default: float) -> float:
    """Best-effort coerce + clamp.  Falls back to default on bad input."""
    try:
        f = float(v)
        if not math.isfinite(f):
            return default
        return max(lo, min(hi, f))
    except (TypeError, ValueError):
        return default


def _coerce_to_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.lower() in ('1', 'true', 'yes', 'on')
    return False


def _sanitise(incoming: dict) -> dict:
    """Validate + clamp every field in the user-submitted payload.

    Returns a NEW dict — never mutates `incoming`.  Unknown keys are
    silently dropped; missing keys fall back to the default value.
    """
    out = get_default_weights()
    # Factor weights
    incoming_fw = (incoming or {}).get('factor_weights') or {}
    for key in out['factor_weights']:
        if key in incoming_fw:
            out['factor_weights'][key] = _coerce_to_float(
                incoming_fw[key], 0.0, 2.0, out['factor_weights'][key]
            )
    # Multiplier exponents
    incoming_me = (incoming or {}).get('multiplier_exponents') or {}
    for key in out['multiplier_exponents']:
        if key in incoming_me:
            out['multiplier_exponents'][key] = _coerce_to_float(
                incoming_me[key], 0.0, 2.0, out['multiplier_exponents'][key]
            )
    # Enable mask
    incoming_em = (incoming or {}).get('enabled_metrics') or {}
    for key in out['enabled_metrics']:
        if key in incoming_em:
            out['enabled_metrics'][key] = _coerce_to_bool(incoming_em[key])
    # Pipeline tuning
    incoming_pt = (incoming or {}).get('pipeline_tuning') or {}
    if 'multiplier_floor' in incoming_pt:
        out['pipeline_tuning']['multiplier_floor'] = _coerce_to_float(
            incoming_pt['multiplier_floor'], 0.001, 1.0,
            out['pipeline_tuning']['multiplier_floor'],
        )
    if 'multiplier_ceiling' in incoming_pt:
        out['pipeline_tuning']['multiplier_ceiling'] = _coerce_to_float(
            incoming_pt['multiplier_ceiling'], 1.0, 100.0,
            out['pipeline_tuning']['multiplier_ceiling'],
        )
    return out


def load_weights() -> dict:
    """Load the persisted overrides; defaults on any error."""
    with _lock:
        if not _WEIGHTS_PATH.exists():
            return get_default_weights()
        try:
            raw = json.loads(_WEIGHTS_PATH.read_text(encoding='utf-8'))
        except Exception as exc:  # noqa: BLE001
            log.warning('metrics_hub_weights: failed to load (%s); using defaults', exc)
            return get_default_weights()
    return _sanitise(raw)


def save_weights(payload: dict) -> dict:
    """Sanitise + persist; return the SAVED payload (after clamps)."""
    sanitised = _sanitise(payload or {})
    with _lock:
        try:
            tmp = _WEIGHTS_PATH.with_suffix('.json.tmp')
            tmp.write_text(json.dumps(sanitised, indent=2), encoding='utf-8')
            os.replace(tmp, _WEIGHTS_PATH)
        except Exception as exc:  # noqa: BLE001
            log.warning('metrics_hub_weights: failed to persist (%s)', exc)
    return sanitised


def reset_weights() -> dict:
    """Wipe persisted overrides; return the defaults."""
    with _lock:
        try:
            if _WEIGHTS_PATH.exists():
                _WEIGHTS_PATH.unlink()
        except Exception:  # noqa: BLE001
            pass
    return get_default_weights()


# ---------------------------------------------------------------------------
# Hot path: applied to row payloads / multiplier composition.  Pure
# functions — caller decides when to invoke them.
# ---------------------------------------------------------------------------
def apply_multiplier_exponent(name: str, value: float, weights: dict | None = None) -> float:
    """`value ** exponent`, clamped to the configured floor/ceiling.

    Falls back to identity (`value`) on any failure — never raises.
    """
    if value is None or not math.isfinite(float(value)):
        return 1.0
    w = weights or load_weights()
    expo = (w.get('multiplier_exponents') or {}).get(name, 1.0)
    try:
        expo = float(expo)
    except (TypeError, ValueError):
        return float(value)
    if not math.isfinite(expo):
        return float(value)
    # The base value can be very small / very large; pow handles that
    # without complaint as long as base > 0.  For negative bases
    # (signed multipliers are rare but possible if the spec changes)
    # we degrade to identity to avoid complex results.
    base = float(value)
    if base <= 0:
        return base
    try:
        composed = base ** expo
    except OverflowError:
        composed = base
    floor = float((w.get('pipeline_tuning') or {}).get('multiplier_floor', 0.05))
    ceiling = float((w.get('pipeline_tuning') or {}).get('multiplier_ceiling', 20.0))
    return max(floor, min(ceiling, composed))


def factor_weight(name: str, weights: dict | None = None) -> float:
    """Return the multiplicative scaler for a pillar (default 1.0)."""
    w = weights or load_weights()
    fw = (w.get('factor_weights') or {})
    try:
        v = float(fw.get(name, 1.0))
        if not math.isfinite(v):
            return 1.0
        return max(0.0, min(2.0, v))
    except (TypeError, ValueError):
        return 1.0


def metric_enabled(name: str, weights: dict | None = None) -> bool:
    """Whether the given metric is currently enabled by the user."""
    w = weights or load_weights()
    em = w.get('enabled_metrics') or {}
    if name not in em:
        # Unknown metrics default to enabled — being conservative.
        return True
    return bool(em.get(name, True))


# ---------------------------------------------------------------------------
# Hub status payload  (used by `GET /api/metrics_hub/status`)
# ---------------------------------------------------------------------------
def _safe_call(fn, default):
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        return {'error': str(exc), '_status': 'degraded', '_fallback': default}


def get_hub_status() -> dict:
    """Compose the full Metrics-Hub status payload.  Every section is
    best-effort: a failure in any one tier never blocks the others.
    """
    from app.services.future_mode_service import (
        get_advanced_cache_stats, get_lab_cache_stats,
        get_strategy_cache_stats, get_predictive_cache_stats,
    )
    from app.services.top10_priority_service import get_status as get_lane_status
    from app.services.provider_session import provider_health_snapshot
    from app.services.predictive_expansion_registry import get_registry as get_phase_2660_registry

    caches = {
        'advanced':              _safe_call(get_advanced_cache_stats, {}),
        'lab':                   _safe_call(get_lab_cache_stats, {}),
        'strategy':              _safe_call(get_strategy_cache_stats, {}),
        'predictive_expansion':  _safe_call(get_predictive_cache_stats, {}),
    }
    lane = _safe_call(get_lane_status, {})
    provider = _safe_call(provider_health_snapshot, {})

    # Catalog of algorithms (static descriptive metadata + runtime
    # dependencies derived from cache stats).
    algorithms = [
        {
            'id': 'pass2_scoring',
            'label': 'Pass-2 Composite Scoring',
            'description': '11-pillar composite (momentum, institutional confluence, '
                           'options positioning, dark pool, reaction clustering, '
                           'volume sentiment, effort vs result, predictive consensus, '
                           'fundamentals, regulatory).  Runs at full depth on top-25 + '
                           'viewport symbols every priority-lane tick.',
            'input_sources': ['yfinance', 'finnhub', 'cboe_options', 'sec_edgar', 'usaspending'],
            'output_field': 'final_score',
            'tier': 'core',
            'tunable': True,
        },
        {
            'id': 'advanced_signals',
            'label': 'Advanced Mathematical Signals',
            'description': 'Hurst exponent (R/S), realized skew/kurt, jump intensity '
                           '(Lee-Mykland), OU half-life, HAR-RV daily sigma.',
            'input_sources': ['daily_history'],
            'output_field': 'advanced_signals',
            'cache_ref': 'advanced',
            'tier': 'experimental',
            'tunable': False,
        },
        {
            'id': 'lab_signals',
            'label': 'Lab Mode (Phase 26.49)',
            'description': 'RSV upside share, EGARCH leverage γ, GARCH-M premium, '
                           'permutation entropy, ApEn, Mahalanobis z, DFA α, SSA trend, '
                           '2-state vol HMM.  Produces `lab_rank_multiplier ∈ [0.6, 1.4]`.',
            'input_sources': ['daily_history'],
            'output_field': 'lab_signals',
            'cache_ref': 'lab',
            'multiplier': 'lab_rank_multiplier',
            'tier': 'experimental',
            'tunable': True,
        },
        {
            'id': 'strategy_signals',
            'label': 'Strategy Tier (Phase 26.50)',
            'description': 'VR(5), VR(22), AR(1), MI lag-1, spectral β, Welch dominant '
                           'cycle, RQA determinism, LZ complexity, EMD IMF1 slope, '
                           'vol-regime momentum.  Produces `strategy_rank_multiplier ∈ '
                           '[0.6, 1.4]`.',
            'input_sources': ['daily_history'],
            'output_field': 'strategy_signals',
            'cache_ref': 'strategy',
            'multiplier': 'strategy_rank_multiplier',
            'tier': 'experimental',
            'tunable': True,
        },
        {
            'id': 'predictive_expansion',
            'label': 'Predictive Expansion (Phase 26.60)',
            'description': '10 standard metrics (MSM drift, nonlinear dependence, '
                           'curvature, lead-lag, vol-of-vol, multi-scale consistency, '
                           'regime stability, drawdown memory, liq-adjusted, ML '
                           'residual edge) + 4 opt-in reality_breaker overlays.  '
                           'Produces 5 composite multipliers.',
            'input_sources': ['daily_history', 'driver_basket'],
            'output_field': 'predictive_expansion_signals',
            'cache_ref': 'predictive_expansion',
            'multiplier': 'strategy_v2_rank_multiplier + regime_risk_multiplier + '
                          'liq_kelly_factor + ml_rank_multiplier + reality_breaker_multiplier',
            'tier': 'experimental',
            'tunable': True,
        },
        {
            'id': 'garch_overlay',
            'label': 'GARCH(1,1) Forward Metrics',
            'description': 'Conditional-variance forecast on top-25 by global score + '
                           'every viewport symbol every priority-lane tick.  Generates '
                           'horizon-specific p_up_cf, drift_pct, sigma_pct, VaR95.',
            'input_sources': ['daily_history'],
            'output_field': 'forward_metrics_garch',
            'tier': 'core',
            'tunable': False,
        },
        {
            'id': 'priority_lane',
            'label': 'Top-N Priority Lane',
            'description': 'Background scheduler that keeps the top-N global + viewport '
                           'symbols under continuous deep-scan.  Adaptive throttling: '
                           'consecutive_slow / consecutive_fast counters surface here.',
            'input_sources': ['snapshot_store'],
            'output_field': 'snapshot["stocks"]',
            'tier': 'infrastructure',
            'tunable': False,
        },
        {
            'id': 'regulatory_signal',
            'label': 'Regulatory Confluence',
            'description': 'SEC EDGAR insider filings + USASpending federal contract '
                           'awards.  Surfaces a per-symbol regulatory_signal contribution.',
            'input_sources': ['sec_edgar', 'usaspending'],
            'output_field': 'regulatory_signal',
            'tier': 'core',
            'tunable': True,
        },
    ]

    # Provider catalog with last-known state.  Currently the snapshot
    # only carries `provider_session` info; we surface that and leave
    # the per-provider rows informational (a future enhancement can
    # plug in per-provider counters).
    providers = [
        {
            'id': 'yfinance',
            'label': 'Yahoo Finance',
            'role': 'Primary historical data + delayed quotes',
            'dependencies': ['curl_cffi', 'requests'],
            'live_state': provider.get('throttle_state', 'unknown'),
            'failure_count': int(provider.get('failure_count', 0) or 0),
        },
        {
            'id': 'finnhub',
            'label': 'Finnhub',
            'role': 'Backup quotes + insider transactions (when key set)',
            'dependencies': ['env: FINNHUB_API_KEY'],
            'live_state': 'optional',
            'failure_count': 0,
        },
        {
            'id': 'cboe_options',
            'label': 'CBOE Options Quote Data',
            'role': 'IV / OI / volume for options_positioning pillar',
            'dependencies': ['requests'],
            'live_state': 'best_effort',
            'failure_count': 0,
        },
        {
            'id': 'sec_edgar',
            'label': 'SEC EDGAR',
            'role': 'Insider Form-4 filings → regulatory_signal',
            'dependencies': ['SEC user-agent header'],
            'live_state': 'best_effort',
            'failure_count': 0,
        },
        {
            'id': 'usaspending',
            'label': 'USASpending.gov',
            'role': 'Federal contract awards → regulatory_signal',
            'dependencies': ['requests'],
            'live_state': 'best_effort',
            'failure_count': 0,
        },
    ]

    return {
        'version': '26.61',
        'generated_at_ms': int(time.time() * 1000),
        'algorithms': algorithms,
        'providers': providers,
        'caches': caches,
        'priority_lane': lane,
        'phase_2660_registry': _safe_call(get_phase_2660_registry, {}),
        'weights': load_weights(),
        'defaults': get_default_weights(),
    }
