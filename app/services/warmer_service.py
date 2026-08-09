
from __future__ import annotations
import logging
import threading
import time
from app.config import settings
from app.services.batch_service import get_total_batches, get_batch_slice
from app.services.scoring_service import score_symbol_rows
from app.utils.time import utcnow_iso

log = logging.getLogger('app.warmer')

# How many warmer cycles between weight-optimizer refit passes.
# The warmer ticks every warmer_interval_seconds; a full universe sweep is
# O(total_batches) ticks.  REFIT_EVERY_N_CYCLES is intentionally large so
# the optimizer only runs once the universe has been swept several times
# and predictions have had time to close and be evaluated.
_REFIT_EVERY_N_CYCLES = 500

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
    # Bucket 4: weight optimizer refit.  Populated every _REFIT_EVERY_N_CYCLES
    # ticks.  None until the first refit attempt.
    'last_weight_optimizer': None,
}
_thread = None
_stop = threading.Event()
_cycle_counter = 0


def _loop():
    global _cycle_counter
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
                # Pass the already-scored rows (not just symbol strings) so
                # auto_log_scan_predictions can extract M/Q/T/S factor scores
                # and store them with each prediction for weight-optimizer use.
                # max_new bounds the added cost per tick regardless of batch size.
                from app.services.prediction_tracker_service import auto_log_scan_predictions
                _status['last_auto_log'] = auto_log_scan_predictions(
                    rows, market=market, forward_days=10, max_new=10,
                )
            except Exception:
                log.debug('auto_log_scan_predictions failed for batch %d (%s)', batch, market, exc_info=True)
            # Bucket 4: periodic weight-optimizer refit.  Runs in a daemon
            # thread so it doesn't block the warmer tick.
            _cycle_counter += 1
            if _cycle_counter % _REFIT_EVERY_N_CYCLES == 0:
                _trigger_weight_refit()
        except Exception:
            _status['last_cycle_utc'] = utcnow_iso()
            _status['last_batch'] = batch
        batch = (batch + 1) % total_batches
        _stop.wait(settings.warmer_interval_seconds)


def _trigger_weight_refit() -> None:
    """Fire a daemon thread to run the weight optimizer without blocking the warmer."""
    def _refit():
        try:
            from app.services.weight_optimizer_service import fit_mqts_weights
            result = fit_mqts_weights()
            _status['last_weight_optimizer'] = result
            if result:
                log.info(
                    'weight_optimizer refit: n=%d  wf_acc_fitted=%.3f  wf_acc_current=%.3f  '
                    'recommendation=%s',
                    result.get('n_samples', 0),
                    result.get('wf_accuracy_fitted') or 0.0,
                    result.get('wf_accuracy_current') or 0.0,
                    result.get('recommendation', '?'),
                )
        except Exception:
            log.debug('weight_optimizer refit failed', exc_info=True)

    t = threading.Thread(target=_refit, daemon=True, name='weight-optimizer-refit')
    t.start()


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
