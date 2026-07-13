"""
Canonical normalization boundary for outbound result rows.

EVERY result row that leaves the backend must pass through `normalize_result_row()`
before touching any response model.  This module is the only place that:

  * stamps the contract keys (`REQUIRED_RESULT_KEYS`),
  * fills missing freshness / provenance fields with defensible defaults,
  * guarantees the nested factor payloads always exist (`ensure_nested_payloads`),
  * records what was defaulted so `/system/status.defaulted_fields` can surface it.

Design goals:
  - Provider instability is acceptable.  Contract drift is not.
  - Stale-but-valid data is better than a 500.
  - Cache rows are real-but-aged, not synthetic; only synthetic rows get `preview_only=True`.
"""
from __future__ import annotations

from collections import Counter
from threading import Lock
from typing import Any, Iterable

from app.utils.time import utcnowiso, freshness_label_from_age

# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------

REQUIRED_RESULT_KEYS: tuple[str, ...] = (
    'symbol',
    'name',
    'exchange',
    'final_score',
    'tier',
    'final_direction',
    'resolution_label',
    'factor_breakdown',
    'as_of_utc',
    'age_seconds',
    'freshness_label',
    'stale',
    'data_source',
    'preview_only',
    'state',
)

# Nested factor families that MUST exist on every emitted row, even when
# upstream computation failed.  These are mirrored into `factor_breakdown.market`
# (legacy contract used by `scanner_presets.py`) so the filter engine never
# sees `None` for a documented factor family.
NESTED_FACTOR_FAMILIES: tuple[str, ...] = (
    'trend_volume_delta',
    'institutional_confluence',
    'options_positioning',
    'institutional_order_block',
    'dark_pool_proxy',
    'volume_sentiment',
    'reaction_map',
    'short_selling_pressure',
    'predicted_volume_intensity',
    'options_expiration',
)

# Sentinel value used when a factor family could not be computed.  Keep the
# shape stable so downstream readers (filter engine, UI) can always rely on
# the same field names and don't crash on missing keys.
UNAVAILABLE_FACTOR_PAYLOAD: dict[str, Any] = {
    'score': 50.0,
    'bias': 'neutral',
    'status': 'unavailable',
    'provenance': 'unavailable',
}

# Per-family default skeletons.  These extend the base unavailable payload
# with the family-specific keys the filter engine and UI both expect.
# Stability of these shapes is part of the contract.
FACTOR_FAMILY_DEFAULTS: dict[str, dict[str, Any]] = {
    'trend_volume_delta': {
        **UNAVAILABLE_FACTOR_PAYLOAD,
        'bucket': 'neutral',
        'delta_pct': 0.0,
    },
    'institutional_confluence': {
        **UNAVAILABLE_FACTOR_PAYLOAD,
        'rrg': {'score': 50.0, 'quadrant': 'NEUTRAL'},
        'flow': {'score': 50.0, 'bias': 'NEUTRAL', 'unusual_volume': False},
        'regime': {'score': 50.0, 'state': 'RANGING'},
        'liquidity': {'score': 50.0, 'signal': 'NONE', 'zones': 0},
        'session': {'score': 50.0, 'state': 'OFF_HOURS'},
    },
    'options_positioning': {
        **UNAVAILABLE_FACTOR_PAYLOAD,
        'gamma_level_label': 'moderate',
        'pin_risk': 'low',
        'composite': {},
        'near_term': {},
        'monthly': {},
        'expirations_used': 0,
    },
    'institutional_order_block': {
        **UNAVAILABLE_FACTOR_PAYLOAD,
        'state': 'unavailable',
        'zone_low': None,
        'zone_high': None,
        'midpoint': None,
        'distance_from_price_pct': 0.0,
        'respect_rate': 0.0,
        'touch_count': 0,
    },
    'dark_pool_proxy': {
        **UNAVAILABLE_FACTOR_PAYLOAD,
        'attraction_state': 'neutral',
        'nearest_print_level': None,
        'distance_to_print_pct': 0.0,
        'zone_density': 0,
        'pinning_effect': 'low',
    },
    'volume_sentiment': {
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
    },
    'reaction_map': {
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
    },
    'short_selling_pressure': {
        'score': 50.0,
        'raw_score': 0.0,
        'label': 'neutral',
        'direction': 'neutral',
        'confidence': 'low',
        'source': 'unavailable',
        'status': 'unavailable',
        'components': {},
    },
    'predicted_volume_intensity': {
        'score': 0.0,
        'bucket': 'low',
        'event_flag': False,
        'reasons': [],
        'source': 'unavailable',
        'status': 'unavailable',
        'components': {},
    },
    'options_expiration': {
        'nearest_expiration': None,
        'days_to_expiration': None,
        'expiration_type': None,
        'high_sensitivity_window': False,
        'risk_flag': False,
        'source': 'unavailable',
        'status': 'unavailable',
    },
}


# ---------------------------------------------------------------------------
# Defaulted-field telemetry
# ---------------------------------------------------------------------------

_telemetry_lock = Lock()
_defaulted_counter: Counter[str] = Counter()
_total_normalized_rows: int = 0


def _record_defaults(injected: Iterable[str], is_cheap: bool = False) -> None:
    """Increment counts of fields that had to be defaulted.

    Phase 26.20: when ``is_cheap`` is True the caller is normalizing a
    Pass-1 cheap row whose intentionally-thin family payloads are not a
    failure mode — those rows are replaced by Pass 2 full-depth rows for
    the top 30% within seconds. Skipping the counter for cheap rows
    keeps the dashboard "factor coverage" metric meaningful (it then
    measures only the full-depth rows that should have factors).
    """
    global _total_normalized_rows
    if is_cheap:
        return
    with _telemetry_lock:
        for key in injected:
            _defaulted_counter[key] += 1
        _total_normalized_rows += 1


def get_defaulted_fields_snapshot() -> dict[str, Any]:
    """Return a copy of the rolling defaulted-field counts for /system/status."""
    with _telemetry_lock:
        return {
            'rows_normalized': _total_normalized_rows,
            'counts': dict(_defaulted_counter),
            'top': [
                {'field': field, 'count': count}
                for field, count in _defaulted_counter.most_common(10)
            ],
        }


def reset_defaulted_fields() -> None:
    """Reset the telemetry counter (used by tests and ops tools)."""
    global _total_normalized_rows
    with _telemetry_lock:
        _defaulted_counter.clear()
        _total_normalized_rows = 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        result = float(value)
        if result != result:  # NaN check
            return default
        return result
    except (TypeError, ValueError):
        return default


def _coalesce(row: dict, *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in row and row[key] not in (None, ''):
            return row[key]
    return default


def ensure_nested_payloads(row: dict, *, injected: list[str] | None = None) -> dict:
    """Guarantee that every contractually-required nested factor payload exists.

    Mirrors compute_extended_factors output into `factor_breakdown.market.<family>`
    so the legacy filter engine in `scanner_presets.py` always finds a usable
    dict.  If a payload was missing or empty an explicit `unavailable` sentinel
    is inserted and the field path is recorded in `injected`.
    """
    injected = injected if injected is not None else []
    fb = row.get('factor_breakdown')
    if not isinstance(fb, dict):
        fb = {}
        injected.append('factor_breakdown')
    market = fb.get('market')
    if not isinstance(market, dict):
        market = {}
        injected.append('factor_breakdown.market')

    for family in NESTED_FACTOR_FAMILIES:
        existing = market.get(family)
        if not isinstance(existing, dict) or not existing:
            market[family] = dict(FACTOR_FAMILY_DEFAULTS.get(family, UNAVAILABLE_FACTOR_PAYLOAD))
            injected.append(f'factor_breakdown.market.{family}')
        else:
            # Backfill any missing family-specific keys (e.g. gamma_level_label
            # on options_positioning) without overwriting computed values.
            defaults = FACTOR_FAMILY_DEFAULTS.get(family) or UNAVAILABLE_FACTOR_PAYLOAD
            for key, default_value in defaults.items():
                if key not in existing:
                    existing[key] = default_value
                    injected.append(f'factor_breakdown.market.{family}.{key}')

    # Ratings block (momentum/quality/trend/stability/exit_risk).  Don't
    # invent component data — just guarantee top-level keys exist.
    ratings = fb.get('ratings')
    if not isinstance(ratings, dict):
        ratings = {}
        injected.append('factor_breakdown.ratings')
    for rating_name in ('momentum', 'quality', 'trend', 'stability'):
        if not isinstance(ratings.get(rating_name), dict):
            ratings[rating_name] = {'score': 0.0, 'rating': 'Unknown'}
            injected.append(f'factor_breakdown.ratings.{rating_name}')

    fb['market'] = market
    fb['ratings'] = ratings
    row['factor_breakdown'] = fb
    return row


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def normalize_result_row(row: dict | None) -> dict:
    """Stamp the full result-row contract onto an arbitrary input dict.

    Accepts raw provider rows, cached rows, preview rows, last-known-good rows,
    and detail rows.  Never raises; always returns a dict satisfying
    `REQUIRED_RESULT_KEYS`.

    Phase 26.20: rows tagged with ``_score_depth == 'cheap'`` (Pass 1 of
    the two-pass scoring loop) are intentionally thin — they're emitted
    so the ranking layer can short-list symbols for the full extended
    factor blend in Pass 2. Counting their absent families in the
    defaulted-fields ("warming") telemetry double-counts: the row will
    be re-emitted in Pass 2 with the family populated, and the cheap
    emission was never expected to carry it. We still inject the
    family skeletons so the response shape is stable, but skip the
    `_record_defaults` call for cheap rows so the dashboard's "factor
    coverage" metric reflects only the rows that should have factors.
    """
    injected: list[str] = []
    cooked: dict[str, Any] = dict(row or {})

    # --- identity ------------------------------------------------------------
    if not cooked.get('symbol'):
        cooked['symbol'] = cooked.get('symbol', '') or ''
        injected.append('symbol')
    if not cooked.get('name'):
        cooked['name'] = cooked.get('name', '') or cooked.get('symbol', '')
        injected.append('name')
    if not cooked.get('exchange'):
        cooked['exchange'] = 'unknown'
        injected.append('exchange')

    # --- scoring -------------------------------------------------------------
    if 'final_score' not in row if row else True:
        injected.append('final_score')
    cooked['final_score'] = _safe_float(
        _coalesce(cooked, 'final_score', 'finalscore', default=0.0), default=0.0
    )
    if 'tier' not in (row or {}):
        injected.append('tier')
    cooked['tier'] = cooked.get('tier') or 'Unranked'
    if 'final_direction' not in (row or {}):
        injected.append('final_direction')
    cooked['final_direction'] = (
        _coalesce(cooked, 'final_direction', 'finaldirection', default='Neutral') or 'Neutral'
    )
    if 'resolution_label' not in (row or {}):
        injected.append('resolution_label')
    cooked['resolution_label'] = (
        _coalesce(cooked, 'resolution_label', 'resolutionlabel', default='1D') or '1D'
    )

    # --- factor_breakdown + nested family payloads ---------------------------
    if 'factor_breakdown' not in (row or {}) and 'factorbreakdown' not in (row or {}):
        injected.append('factor_breakdown')
    cooked['factor_breakdown'] = (
        cooked.get('factor_breakdown') or cooked.get('factorbreakdown') or {}
    )
    ensure_nested_payloads(cooked, injected=injected)

    # --- freshness + provenance ---------------------------------------------
    if 'as_of_utc' not in (row or {}) and 'asofutc' not in (row or {}):
        injected.append('as_of_utc')
    cooked['as_of_utc'] = (
        _coalesce(cooked, 'as_of_utc', 'asofutc', default='') or utcnowiso()
    )

    if 'age_seconds' not in (row or {}) and 'ageseconds' not in (row or {}):
        injected.append('age_seconds')
    cooked['age_seconds'] = int(
        _safe_float(_coalesce(cooked, 'age_seconds', 'ageseconds', default=0), 0)
    )

    if 'freshness_label' not in (row or {}) and 'freshnesslabel' not in (row or {}):
        injected.append('freshness_label')
    cooked['freshness_label'] = (
        _coalesce(cooked, 'freshness_label', 'freshnesslabel', default='')
        or freshness_label_from_age(cooked['age_seconds'])
    )

    if 'stale' not in (row or {}):
        injected.append('stale')
    cooked['stale'] = bool(
        cooked.get('stale')
        if cooked.get('stale') is not None
        else (cooked['age_seconds'] > 60)
    )

    if 'data_source' not in (row or {}) and 'datasource' not in (row or {}):
        injected.append('data_source')
    cooked['data_source'] = (
        _coalesce(cooked, 'data_source', 'datasource', default='unknown') or 'unknown'
    )

    if 'preview_only' not in (row or {}) and 'previewonly' not in (row or {}):
        injected.append('preview_only')
    cooked['preview_only'] = bool(
        _coalesce(cooked, 'preview_only', 'previewonly', default=False)
    )

    if 'state' not in (row or {}):
        injected.append('state')
    cooked['state'] = cooked.get('state') or 'ready'

    # --- scanner-context flat fields (additive contract) ---------------------
    # These stay safe under partial data: rows normalize with defensible
    # defaults instead of undefined keys. Not counted as "warming" telemetry.
    for _ctx_key, _ctx_default in (
        ('short_selling_pressure_score', 50.0),
        ('short_selling_pressure_label', 'neutral'),
        ('short_selling_pressure_source', 'unavailable'),
        ('predicted_volume_intensity_score', 0.0),
        ('predicted_volume_intensity_bucket', 'low'),
        ('predicted_volume_event_flag', False),
        ('nearest_options_expiration', None),
        ('days_to_options_expiration', None),
        ('expiration_risk_flag', False),
        ('future_forecast_ready', False),
        ('future_forecast_summary', None),
    ):
        if _ctx_key not in cooked:
            cooked[_ctx_key] = _ctx_default

    # Algorithm ratings mirror (kept for legacy frontend compatibility).
    # If the upstream scorer already supplied algorithm_ratings, keep it.
    # Otherwise, project factor_breakdown.ratings into algorithm_ratings so
    # the frontend always shows the same numbers as the breakdown.
    existing_ar = cooked.get('algorithm_ratings')
    if not isinstance(existing_ar, dict) or not existing_ar:
        fb = cooked.get('factor_breakdown') or {}
        fb_ratings = fb.get('ratings') if isinstance(fb, dict) else None
        if isinstance(fb_ratings, dict):
            cooked['algorithm_ratings'] = {
                key: fb_ratings.get(key, {'score': 0, 'rating': 'Unknown'})
                for key in ('momentum', 'quality', 'trend', 'stability')
            }
        else:
            cooked['algorithm_ratings'] = {
                key: {'score': 0, 'rating': 'Unknown'}
                for key in ('momentum', 'quality', 'trend', 'stability')
            }

    _record_defaults(injected, is_cheap=(cooked.get('_score_depth') == 'cheap'))
    return cooked


def assert_contract_complete(row: dict) -> None:
    """Raise AssertionError if any required key is missing.  Used by tests."""
    missing = [k for k in REQUIRED_RESULT_KEYS if k not in row]
    if missing:
        raise AssertionError(f'Result row missing required keys: {missing}')
