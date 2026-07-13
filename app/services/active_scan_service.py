from __future__ import annotations
from app.config import settings
from app.services.universe_service import get_universe, filter_supported_provider_rows
from app.services.scoring_service import score_symbol_rows
from app.services.provider_session import clear_provider_failure
from app.utils.normalize import normalize_result_row


def get_active_scan_results(limit: int = 250, market: str = 'stocks') -> dict:
    universe = filter_supported_provider_rows(get_universe(market))[:min(limit, settings.active_scan_limit)]
    scored_rows = score_symbol_rows(universe)
    normalized_rows = [normalize_result_row(row) for row in scored_rows]
    if normalized_rows:
        clear_provider_failure()
    return {
        'batch': 0,
        'current_batch': 0,
        'total_batches': 1,
        'limit': limit,
        'total': len(universe),
        'scan_progress': {
            'batch_index': 0,
            'batch_size': limit,
            'slice_rows': len(universe),
            'scored_rows': len(normalized_rows),
            'loaded_rows': len(normalized_rows),
            'universe_size': len(universe),
            'scan_state': 'ok' if normalized_rows else 'empty',
        },
        'filters': {'market': market},
        'results': normalized_rows,
        'state': 'ok' if normalized_rows else 'empty',
    }
