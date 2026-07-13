
from __future__ import annotations
from threading import Lock
from collections import Counter
from app.config import settings
from app.services.provider_session import provider_health_snapshot
from app.services.quote_cache import cache_status
from app.services.warmer_service import warmer_status
from app.services.providers.base import provider_stats_snapshot
from app.services.providers.stooq_provider import stats_snapshot as stooq_stats
from app.services.options_chain_service import stats_snapshot as options_chain_stats
from app.services.daily_history_service import stats_snapshot as daily_history_stats
from app.services.reaction_clustering_service import stats_snapshot as reaction_clustering_stats
from app.services.active_scan_pool import pool_stats
from app.utils.normalize import get_defaulted_fields_snapshot
from app.utils.time import utcnow_iso

_runtime_lock = Lock()
_runtime = {
    'last_refresh_utc': None,
    'last_success_utc': None,
    'last_failure_utc': None,
    'recent_fetch_error_summary': None,
    'current_filters': {'preset': 'all'},
    'last_known_good': {
        'batches_cached': 0,
        'last_served_utc': None,
        'last_fallback_reason': None,
        'serves_total': 0,
    },
    'failure_classes': Counter(),
}


def mark_success() -> None:
    now = utcnow_iso()
    with _runtime_lock:
        _runtime['last_refresh_utc'] = now
        _runtime['last_success_utc'] = now


def mark_failure(error: str, failure_class: str = 'provider') -> None:
    now = utcnow_iso()
    with _runtime_lock:
        _runtime['last_refresh_utc'] = now
        _runtime['last_failure_utc'] = now
        _runtime['recent_fetch_error_summary'] = str(error)[:240]
        _runtime['failure_classes'][failure_class] += 1


def record_last_known_good_serve(batches_cached: int, reason: str) -> None:
    """Called by result_store when an LKG payload is served as fallback."""
    with _runtime_lock:
        _runtime['last_known_good']['batches_cached'] = batches_cached
        _runtime['last_known_good']['last_served_utc'] = utcnow_iso()
        _runtime['last_known_good']['last_fallback_reason'] = reason[:200]
        _runtime['last_known_good']['serves_total'] += 1


def record_lkg_batch_stored(batches_cached: int) -> None:
    with _runtime_lock:
        _runtime['last_known_good']['batches_cached'] = batches_cached


def set_current_filters(filters: dict) -> None:
    with _runtime_lock:
        _runtime['current_filters'] = filters or {'preset': 'all'}


def get_system_status(batch: int = 0) -> dict:
    provider = provider_health_snapshot()
    cache = cache_status()
    warmer = warmer_status()
    with _runtime_lock:
        rt = {
            'last_refresh_utc': _runtime['last_refresh_utc'],
            'last_success_utc': _runtime['last_success_utc'],
            'last_failure_utc': _runtime['last_failure_utc'],
            'recent_fetch_error_summary': _runtime['recent_fetch_error_summary'],
            'current_filters': dict(_runtime['current_filters']),
            'last_known_good': dict(_runtime['last_known_good']),
            'failure_classes': dict(_runtime['failure_classes']),
        }
    degraded = bool(provider.get('degraded')) or (
        rt.get('recent_fetch_error_summary') is not None and rt.get('last_success_utc') is None
    )
    offline = bool(provider.get('degraded')) and cache.get('cache_entries', 0) == 0
    return {
        'backend_ok': True,
        'current_batch': batch,
        'batch_size': settings.batch_size,
        'refresh_step_seconds': settings.refresh_step_seconds,
        'degraded_mode': degraded,
        'offline_mode': offline,
        'provider': provider,
        'cache': cache,
        'warmer': warmer,
        'last_refresh_utc': rt['last_refresh_utc'],
        'last_success_utc': rt['last_success_utc'],
        'last_failure_utc': rt['last_failure_utc'],
        'recent_fetch_error_summary': rt['recent_fetch_error_summary'],
        'current_filters': rt['current_filters'] or {'preset': 'all'},
        'defaulted_fields': get_defaulted_fields_snapshot(),
        'last_known_good': rt['last_known_good'],
        'failure_classes': rt['failure_classes'],
        'provider_stats': provider_stats_snapshot(),
        'yf_batch_executor': _yf_batch_executor_stats_safe(),
        'stooq_diagnostics': stooq_stats(),
        'options_chain_stats': options_chain_stats(),
        'daily_history_stats': daily_history_stats(),
        'reaction_clustering_stats': reaction_clustering_stats(),
        'active_scan_pool': pool_stats(),
        'gc_stats': _gc_stats_safe(),
        'state': 'ok' if not degraded else 'degraded',
    }


def _gc_stats_safe() -> dict:
    """Wrap gc_service.gc_stats in a try/except so a malformed GC state
    can never break the status endpoint."""
    try:
        from app.services.gc_service import gc_stats
        return gc_stats()
    except Exception:  # noqa: BLE001
        return {}


def _yf_batch_executor_stats_safe() -> dict:
    """Wrap scoring_service.yf_batch_executor_stats so a missing module
    can never break the status endpoint."""
    try:
        from app.services.scoring_service import yf_batch_executor_stats
        return yf_batch_executor_stats()
    except Exception:  # noqa: BLE001
        return {}
