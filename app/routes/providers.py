"""
Phase 26.2: Dedicated Data Providers Health endpoint.

Powers the standalone `/providers.html` health-dashboard page (opened in a new
tab from the top-right provider chip). Merges every provider-telemetry source
the backend already maintains into a single, easy-to-render payload:

  - Quote-provider cascade stats (calls / hits / misses / errors / last_error)
  - Options-chain provider stats
  - Daily-history provider stats
  - Reaction-clustering provider stats
  - Stooq fallback diagnostics
  - Configured-API-key flags (which providers are unlocked)
  - Blacklist stats (persistently-failing symbols)
  - Aggregate health: degraded / offline flags, last refresh timestamps

The intent is that the providers page can refresh this endpoint every ~10s
without disrupting the main scanner loop. Read-only.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter

from app.services.status_service import get_system_status

log = logging.getLogger('app.providers_routes')

router = APIRouter(prefix='/api/providers', tags=['providers'])


def _aggregate_quote_health(provider_stats: dict) -> dict:
    """Compute roll-up hit-rate + error-rate across all quote providers."""
    total_calls = 0
    total_hits = 0
    total_misses = 0
    total_errors = 0
    total_timeouts = 0
    total_rate_limits = 0
    for stats in (provider_stats or {}).values():
        if not isinstance(stats, dict):
            continue
        total_calls += int(stats.get('calls') or 0)
        total_hits += int(stats.get('hits') or 0)
        total_misses += int(stats.get('misses') or 0)
        total_errors += int(stats.get('errors') or 0)
        total_timeouts += int(stats.get('timeouts') or 0)
        total_rate_limits += int(stats.get('rate_limits') or 0)
    return {
        'total_calls': total_calls,
        'total_hits': total_hits,
        'total_misses': total_misses,
        'total_errors': total_errors,
        'total_timeouts': total_timeouts,
        'total_rate_limits': total_rate_limits,
        'hit_rate': round(total_hits / total_calls, 4) if total_calls else None,
        'error_rate': round(total_errors / total_calls, 4) if total_calls else None,
    }


def _provider_rows(provider_stats: dict, api_keys_configured: dict | None = None) -> list[dict]:
    """Flatten the quote-provider stats dict into a UI-friendly list."""
    api_keys_configured = api_keys_configured or {}
    rows: list[dict] = []
    for name, stats in (provider_stats or {}).items():
        if not isinstance(stats, dict):
            continue
        calls = int(stats.get('calls') or 0)
        hits = int(stats.get('hits') or 0)
        misses = int(stats.get('misses') or 0)
        errors = int(stats.get('errors') or 0)
        timeouts = int(stats.get('timeouts') or 0)
        rate_limits = int(stats.get('rate_limits') or 0)
        # A provider is "key-gated and unconfigured" if it's in our known
        # key-gated list AND the api_keys_configured map says it's off.
        # When the api_keys map is empty/unavailable, fall back to the
        # default (no special handling) so we don't accidentally mark
        # a provider idle when it's actually broken.
        is_key_gated = name.lower() in _KEY_GATED_PROVIDERS
        no_api_key = is_key_gated and (api_keys_configured.get(name) is False
                                       or name not in api_keys_configured)
        # If the api_keys map literally says True for this provider, the
        # key IS configured - clear the flag so the regular health logic
        # applies.
        if api_keys_configured.get(name) is True:
            no_api_key = False
        rows.append({
            'name': name,
            'calls': calls,
            'hits': hits,
            'misses': misses,
            'errors': errors,
            'timeouts': timeouts,
            'rate_limits': rate_limits,
            'hit_rate': round(hits / calls, 4) if calls else None,
            'error_rate': round(errors / calls, 4) if calls else None,
            'last_error': stats.get('last_error'),
            'last_error_utc': stats.get('last_error_utc'),
            'last_success_utc': stats.get('last_success_utc'),
            'last_call_utc': stats.get('last_call_utc'),
            # Phase 26.4 - generic CB telemetry
            'consecutive_failures': int(stats.get('consecutive_failures') or 0),
            'circuit_state': stats.get('circuit_state', 'closed'),
            'circuit_remaining_seconds': float(stats.get('circuit_remaining_seconds') or 0.0),
            'circuit_open_until_utc': stats.get('circuit_open_until_utc'),
            'circuit_trip_count': int(stats.get('circuit_trip_count') or 0),
            'last_trip_utc': stats.get('last_trip_utc'),
            'cb_threshold': int(stats.get('cb_threshold') or 5),
            'cb_cooldown_seconds': float(stats.get('cb_cooldown_seconds') or 60.0),
            'cb_current_cooldown_seconds': float(
                stats.get('cb_current_cooldown_seconds')
                or stats.get('cb_cooldown_seconds')
                or 60.0
            ),
            # Phase 26.8 - whether this row represents a provider that's
            # idle due to a missing API key (vs. legitimately broken).
            'no_api_key': bool(no_api_key),
            # Health verdict for the UI: green / amber / red / idle
            'health': _classify_health(calls, hits, errors, no_api_key=no_api_key),
        })
    # Sort: most-active first so the eye lands on the heavy lifters.
    rows.sort(key=lambda r: (-r['calls'], r['name']))
    return rows


def _circuit_breaker_rows(provider_rows: list[dict], stooq_diag: dict) -> list[dict]:
    """Aggregate generic + dedicated (stooq) circuit breakers for the UI.

    A provider is "tripped" when its circuit_state == 'open'. Closed circuits
    still show up so the user can see threshold tuning context, but they are
    sorted to the bottom of the list.
    """
    out: list[dict] = []
    for r in provider_rows:
        state = r.get('circuit_state', 'closed')
        out.append({
            'provider': r['name'],
            'kind': 'generic',
            'state': state,
            'consecutive_failures': r.get('consecutive_failures', 0),
            'threshold': r.get('cb_threshold', 5),
            # Show the CURRENT cooldown (after any exponential backoff) so
            # the UI matches the actual time the circuit will stay open. The
            # base cooldown is preserved separately for context.
            'cooldown_seconds': r.get('cb_current_cooldown_seconds') or r.get('cb_cooldown_seconds', 60.0),
            'base_cooldown_seconds': r.get('cb_cooldown_seconds', 60.0),
            'remaining_seconds': r.get('circuit_remaining_seconds', 0.0),
            'trip_count': r.get('circuit_trip_count', 0),
            'last_trip_utc': r.get('last_trip_utc'),
            'open_until_utc': r.get('circuit_open_until_utc'),
        })
    # Stooq has its own dedicated CB whose tunables differ from the generic
    # one (exponential cooldown, much higher threshold). Surface it as a
    # separate row so the operator sees both views.
    if isinstance(stooq_diag, dict) and stooq_diag.get('attempts'):
        circuit_open = bool(stooq_diag.get('circuit_open'))
        out.append({
            'provider': 'stooq',
            'kind': 'dedicated-exponential',
            'state': 'open' if circuit_open else 'closed',
            'consecutive_failures': int(stooq_diag.get('consecutive_failures') or 0),
            'threshold': int(stooq_diag.get('fail_threshold') or 0),
            'cooldown_seconds': float(stooq_diag.get('next_cooldown_seconds') or 0.0),
            'remaining_seconds': float(stooq_diag.get('circuit_remaining_seconds') or 0.0),
            'trip_count': int(stooq_diag.get('circuit_trip_count') or 0),
            'last_trip_utc': None,
            'open_until_utc': None,
        })
    # Tripped circuits first, then by trip count desc.
    out.sort(key=lambda r: (r['state'] != 'open', -r.get('trip_count', 0), r['provider']))
    return out


def _options_chain_provider_rows(options_stats: dict, generic_provider_stats: dict) -> list[dict]:
    """Build per-row breakdown of the options-chain provider cascade.

    Returns one row per source (CBOE primary, Yahoo fallback) so the UI can
    render a familiar table with hit/error rates and the same health-classifier
    used by the quote providers.

    Phase 26.14 also exposes the cboe_options generic-CB row inline so the
    operator can see the dedicated circuit-breaker state next to the throughput
    stats without leaving the page.
    """
    out: list[dict] = []
    if not isinstance(options_stats, dict):
        return out

    # Pull the cboe_options row from the shared provider_stats so we get the
    # circuit-breaker state surfaced too.
    cboe_cb = (generic_provider_stats or {}).get('cboe_options') or {}

    def _row(name: str, kind: str, attempts_key: str, hits_key: str,
             misses_key: str, errors_key: str, *, cb_row: dict | None = None) -> dict:
        attempts = int(options_stats.get(attempts_key) or 0)
        hits = int(options_stats.get(hits_key) or 0)
        misses = int(options_stats.get(misses_key) or 0)
        errors = int(options_stats.get(errors_key) or 0)
        hit_rate = round(hits / attempts, 4) if attempts > 0 else None
        err_rate = round(errors / attempts, 4) if attempts > 0 else None
        row = {
            'provider': name,
            'kind': kind,
            'attempts': attempts,
            'hits': hits,
            'misses': misses,
            'errors': errors,
            'hit_rate': hit_rate,
            'error_rate': err_rate,
            'health': _classify_health(attempts, hits, errors, no_api_key=False),
        }
        # Attach circuit-breaker state for sources that go through base.py.
        if cb_row:
            row.update({
                'circuit_state': cb_row.get('circuit_state', 'closed'),
                'circuit_trip_count': cb_row.get('circuit_trip_count', 0),
                'last_error': cb_row.get('last_error'),
                'last_error_utc': cb_row.get('last_error_utc'),
                'last_success_utc': cb_row.get('last_success_utc'),
                'timeouts': cb_row.get('timeouts', 0),
                'rate_limits': cb_row.get('rate_limits', 0),
            })
        return row

    out.append(_row(
        'CBOE delayed quotes', 'primary',
        'cboe_attempts', 'cboe_hits', 'cboe_misses', 'cboe_errors',
        cb_row=cboe_cb,
    ))
    out.append(_row(
        'Yahoo Finance (yfinance)', 'fallback',
        'yahoo_attempts', 'yahoo_hits', 'yahoo_misses', 'yahoo_errors',
        cb_row=None,
    ))
    return out


def _classify_health(calls: int, hits: int, errors: int,
                    no_api_key: bool = False) -> str:
    """Three-tier verdict the UI consumes for the colored health bars.

    Special-case: when a provider requires an API key that hasn't been
    configured (no_api_key=True), classify as 'idle' regardless of
    calls/hits. Without a key the provider intentionally short-circuits
    every fetch to None, which technically looks like a 0% hit rate but
    is expected behavior, not a failure mode worth coloring red.
    """
    if no_api_key:
        return 'idle'
    if calls <= 0:
        return 'idle'
    hit_rate = hits / calls
    err_rate = errors / calls
    if err_rate >= 0.4 or hit_rate <= 0.2:
        return 'critical'
    if err_rate >= 0.1 or hit_rate <= 0.6:
        return 'degraded'
    return 'healthy'


# Providers that auto-skip when no API key is configured. The UI uses
# this to render them as 'idle' rather than 'critical' so a missing key
# doesn't paint the row red.
_KEY_GATED_PROVIDERS = {'finnhub', 'alphavantage', 'fmp', 'polygon', 'tiingo'}


def _api_keys_configured() -> dict:
    """Optional providers that unlock via user-supplied API keys.

    Returns {provider_name: bool}. Empty dict if the api_keys helper isn't
    available (older builds), so the UI can degrade gracefully.
    """
    try:
        from app.services import api_keys as _ak
        # api_keys.list_configured() returns {provider: bool}
        return dict(_ak.list_configured() or {})
    except Exception as exc:  # noqa: BLE001
        log.debug('providers_routes: api-keys configured lookup failed: %s', exc)
        return {}


def _blacklist_stats() -> dict:
    """Persistently-failing symbol counts."""
    try:
        from app.services.symbol_blacklist_service import stats as _bl_stats
        return dict(_bl_stats() or {})
    except Exception as exc:  # noqa: BLE001
        log.debug('providers_routes: blacklist stats failed: %s', exc)
        return {}


@router.get('/status')
def providers_status():
    """Live provider-health payload.

    Returned shape:
        {
          'state': 'ok' | 'degraded',
          'degraded_mode': bool,
          'offline_mode': bool,
          'aggregate': {total_calls, total_hits, total_misses, total_errors,
                        hit_rate, error_rate},
          'quote_providers': [{name, calls, hits, misses, errors,
                               hit_rate, error_rate, last_error, health}, ...],
          'options_chain': {...},
          'daily_history': {...},
          'reaction_clustering': {...},
          'stooq_diagnostics': {...},
          'cache': {...},
          'warmer': {...},
          'api_keys_configured': {provider: bool},
          'blacklist': {...},
          'last_refresh_utc': str|None,
          'last_success_utc': str|None,
          'last_failure_utc': str|None,
          'recent_fetch_error_summary': str|None,
          'failure_classes': {...},
        }
    """
    sys_status = get_system_status(0)
    provider_stats = sys_status.get('provider_stats') or {}
    aggregate = _aggregate_quote_health(provider_stats)
    api_keys = _api_keys_configured()
    rows = _provider_rows(provider_stats, api_keys_configured=api_keys)
    cb_rows = _circuit_breaker_rows(rows, sys_status.get('stooq_diagnostics') or {})
    options_stats = sys_status.get('options_chain_stats') or {}
    options_providers = _options_chain_provider_rows(options_stats, provider_stats)
    return {
        'state': sys_status.get('state', 'ok'),
        'degraded_mode': bool(sys_status.get('degraded_mode')),
        'offline_mode': bool(sys_status.get('offline_mode')),
        'aggregate': aggregate,
        'quote_providers': rows,
        'circuit_breakers': cb_rows,
        'options_chain': options_stats,
        # Phase 26.14: per-source breakdown so /providers.html can render a
        # dedicated Options Chain Providers card (CBOE primary, Yahoo fallback).
        'options_chain_providers': options_providers,
        'daily_history': sys_status.get('daily_history_stats') or {},
        'reaction_clustering': sys_status.get('reaction_clustering_stats') or {},
        'stooq_diagnostics': sys_status.get('stooq_diagnostics') or {},
        'cache': sys_status.get('cache') or {},
        'warmer': sys_status.get('warmer') or {},
        'api_keys_configured': api_keys,
        'blacklist': _blacklist_stats(),
        'last_refresh_utc': sys_status.get('last_refresh_utc'),
        'last_success_utc': sys_status.get('last_success_utc'),
        'last_failure_utc': sys_status.get('last_failure_utc'),
        'recent_fetch_error_summary': sys_status.get('recent_fetch_error_summary'),
        'failure_classes': sys_status.get('failure_classes') or {},
    }
