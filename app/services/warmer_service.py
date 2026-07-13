
from __future__ import annotations
import logging
import threading
import time
from app.config import settings
from app.services.batch_service import get_total_batches, get_batch_slice
from app.services.scoring_service import score_symbol_rows
from app.utils.time import utcnow_iso

log = logging.getLogger('app.warmer')

_status = {
    'enabled': settings.warmer_enabled,
    'running': False,
    'interval_seconds': settings.warmer_interval_seconds,
    'last_cycle_utc': None,
    'warmed_symbols': 0,
    'last_batch': 0,
    'market': 'stocks',
    # Phase: auto prediction logging.  Populated each cycle by
    # auto_log_scan_predictions() -- see that function's docstring for
    # why this exists: without it, `save_prediction` was reachable ONLY
    # from the manual "Save prediction" button, so accuracy_stats()
    # could only ever measure a human-curated, selection-biased sample.
    # This loop already walks the FULL universe on a timer with no user
    # involvement, which makes it the correct place to log a systematic,
    # unbiased sample instead.
    'last_auto_log': None,
}
_thread = None
_stop = threading.Event()


def _loop():
    batch = 0
    last_market = _status.get('market', 'stocks')
    total_batches = get_total_batches(settings.batch_size, last_market)
    while not _stop.is_set():
        market = _status.get('market', 'stocks')
        if market != last_market:
            batch = 0
            total_batches = get_total_batches(settings.batch_size, market)
            last_market = market
        rows = get_batch_slice(batch, settings.batch_size, market)
        try:
            score_symbol_rows(rows)
            _status['warmed_symbols'] += len(rows)
            _status['last_cycle_utc'] = utcnow_iso()
            _status['last_batch'] = batch
            try:
                symbols = [r.get('symbol') for r in rows if r.get('symbol')]
                # max_new bounds the added cost per tick regardless of
                # batch size -- predict_price recomputes a full factor
                # blend, so this stays cheap even on large batches.
                from app.services.prediction_tracker_service import auto_log_scan_predictions
                _status['last_auto_log'] = auto_log_scan_predictions(
                    symbols, market=market, forward_days=10, max_new=10,
                )
            except Exception:
                log.debug('auto_log_scan_predictions failed for batch %d (%s)', batch, market, exc_info=True)
        except Exception:
            _status['last_cycle_utc'] = utcnow_iso()
            _status['last_batch'] = batch
        batch = (batch + 1) % total_batches
        _stop.wait(settings.warmer_interval_seconds)


def start_warmer():
    global _thread
    if not settings.warmer_enabled or (_thread and _thread.is_alive()):
        return
    _status['running'] = True
    _thread = threading.Thread(target=_loop, daemon=True, name='quote-warmer')
    _thread.start()


def stop_warmer():
    _stop.set()
    _status['running'] = False


def warmer_status() -> dict:
    return dict(_status)


def set_warmer_market(market: str = 'stocks') -> None:
    _status['market'] = 'crypto' if str(market).lower() == 'crypto' else 'stocks'
