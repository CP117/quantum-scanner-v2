from fastapi import APIRouter, Query
from app.models.status import (
    SystemStatusEnvelope,
    ProviderStatus,
    CacheStatus,
    WarmerStatus,
    DefaultedFieldsSnapshot,
    LastKnownGoodSummary,
)
from app.services.status_service import get_system_status

router = APIRouter(prefix='/system', tags=['system'])


@router.get('/status', response_model=SystemStatusEnvelope)
def system_status(batch: int = Query(0, ge=0)):
    payload = get_system_status(batch)
    return SystemStatusEnvelope(
        backend_ok=payload['backend_ok'],
        current_batch=payload['current_batch'],
        batch_size=payload['batch_size'],
        refresh_step_seconds=payload['refresh_step_seconds'],
        degraded_mode=payload['degraded_mode'],
        offline_mode=payload['offline_mode'],
        provider=ProviderStatus(**payload['provider']),
        cache=CacheStatus(**payload['cache']),
        warmer=WarmerStatus(**payload['warmer']),
        last_refresh_utc=payload.get('last_refresh_utc'),
        last_success_utc=payload.get('last_success_utc'),
        last_failure_utc=payload.get('last_failure_utc'),
        recent_fetch_error_summary=payload.get('recent_fetch_error_summary'),
        current_filters=payload.get('current_filters') or {'preset': 'all'},
        defaulted_fields=DefaultedFieldsSnapshot(**payload['defaulted_fields']),
        last_known_good=LastKnownGoodSummary(**payload['last_known_good']),
        failure_classes=payload.get('failure_classes') or {},
        provider_stats=payload.get('provider_stats') or {},
        stooq_diagnostics=payload.get('stooq_diagnostics') or {},
        options_chain_stats=payload.get('options_chain_stats') or {},
        daily_history_stats=payload.get('daily_history_stats') or {},
        reaction_clustering_stats=payload.get('reaction_clustering_stats') or {},
        active_scan_pool=payload.get('active_scan_pool') or {},
        gc_stats=payload.get('gc_stats') or {},
        yf_batch_executor=payload.get('yf_batch_executor') or {},
        state=payload['state'],
    )
