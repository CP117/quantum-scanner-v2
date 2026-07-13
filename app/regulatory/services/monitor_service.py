import asyncio
import logging

from app.regulatory.services.sec_service import collect_interest_events
from app.regulatory.services.usaspending_service import search_contract_awards
from app.regulatory.services.discovery_service import discover_new_insider_companies
from app.regulatory.services.storage_service import save_filings, save_awards, create_alert, list_watchlists, get_settings, list_tracked_companies, upsert_tracked_company
from app.regulatory.services.entity_service import names_match
from app.regulatory.services.cik_lookup_service import (
    initialize as init_cik_map,
    cik_for_ticker,
    all_tickers_with_cik,
)

log = logging.getLogger('app.regulatory.monitor')

_scheduler_task = None
_scheduler_enabled = False

# Phase 26.11: sweep-cancellation flag is decoupled from the scheduler's own
# enabled flag. `_scheduler_enabled` reflects whether the background scheduler
# loop is running; it is False by default in REGULATORY_INPROCESS=0 mode
# (which is the recommended deployment). Manual sweeps triggered via the
# `/api/regulatory/autoscan-trigger` route must NOT be gated by it, otherwise
# the very first iteration of the per-ticker loop bails out and the sweep
# completes with "0 / N tickers in 0 seconds" — exactly the bug the user
# reported.
#
# `_autoscan_cancel_requested` is set to True by:
#   - `stop_scheduler()` on graceful shutdown
#   - the new `/autoscan-cancel` route when the operator hits "Stop"
# and reset to False at the start of every sweep.
_autoscan_cancel_requested = False

# ---------------------------------------------------------------------------
# Universe-auto-scan progress (surfaced via /api/regulatory/autoscan-status)
# so the UI can show "Auto-scanning 132 / 8214 tickers — currently AAPL".
# ---------------------------------------------------------------------------
_autoscan_state = {
    'enabled': False,
    'tickers_total': 0,
    'tickers_done': 0,
    'tickers_with_hits': 0,
    'current_symbol': None,
    'started_at': None,
    'last_completed_at': None,
    'last_sweep_seconds': None,
    'last_error': None,
    'cik_map_size': 0,
}


_autoscan_lock = asyncio.Lock()


def autoscan_snapshot() -> dict:
    snap = dict(_autoscan_state)
    snap['busy'] = _autoscan_lock.locked()
    return snap


async def rule_settings():
    s = await get_settings()
    return {
        'large_award_threshold': float(s.get('large_award_threshold', '1000000')),
        'ownership_threshold_percent': float(s.get('ownership_threshold_percent', '5')),
        'enable_entity_linking': s.get('enable_entity_linking', '1') == '1',
        'default_scan_limit': int(s.get('default_scan_limit', '10')),
        'scheduler_interval_seconds': int(s.get('scheduler_interval_seconds', '1800')),
        'enable_scheduler': s.get('enable_scheduler', '1') == '1',
        'enable_auto_discovery': s.get('enable_auto_discovery', '1') == '1',
        'auto_discovery_interval_seconds': int(s.get('auto_discovery_interval_seconds', '3600')),
        # NEW: universe auto-scan — walks the scanner's universe and scans every
        # ticker we can resolve a CIK for. Default ON so the user gets the
        # automatic populating list they asked for. Disable via Settings panel.
        'enable_universe_autoscan': s.get('enable_universe_autoscan', '1') == '1',
        'universe_autoscan_interval_seconds': int(s.get('universe_autoscan_interval_seconds', '14400')),  # 4 hours
        'universe_autoscan_request_gap_ms': int(s.get('universe_autoscan_request_gap_ms', '120')),       # ~8 req/sec
        'universe_autoscan_limit_per_symbol': int(s.get('universe_autoscan_limit_per_symbol', '3')),
        'universe_autoscan_max_tickers': int(s.get('universe_autoscan_max_tickers', '0')),  # 0 = all
    }

async def run_scan(cik: str, recipient: str, limit: int = 10, scan_source: str = 'manual'):
    rules = await rule_settings()
    insider_events, award_events = [], []
    insider_error = None
    awards_error = None
    insider_status = 'ok'
    awards_status = 'ok'

    try:
        insider_events = await collect_interest_events(cik, limit=limit or rules['default_scan_limit'])
        for evt in insider_events:
            if evt.issuer_cik:
                await upsert_tracked_company(evt.issuer_cik, evt.issuer_name, evt.issuer_ticker, evt.filing_date, source='insider_event')
    except Exception as e:
        insider_events = []
        insider_error = str(e)
        insider_status = 'error'

    try:
        award_events = await search_contract_awards(recipient, limit=limit or rules['default_scan_limit'])
    except Exception as e:
        award_events = []
        awards_error = str(e)
        awards_status = 'error'

    try:
        if insider_events:
            await save_filings(insider_events)
    except Exception:
        pass
    try:
        if award_events:
            await save_awards(award_events)
    except Exception:
        pass

    issuer_names = {x.issuer_name for x in insider_events if x.issuer_name}
    issuer_tickers = {x.issuer_ticker for x in insider_events if x.issuer_ticker}

    for evt in insider_events:
        try:
            if evt.transaction_type in {'open_market_buy', 'open_market_sell'}:
                key = f"filing:{evt.accession_number}:{evt.transaction_code}:{evt.reporting_owner_name}:{evt.shares}"
                await create_alert('insider_transaction', key, f"{evt.reporting_owner_name} {evt.transaction_type}", f"{evt.issuer_ticker or evt.issuer_name} {evt.transaction_code or ''} shares={evt.shares} price={evt.price_per_share}")
            elif evt.percent_owned and evt.percent_owned >= rules['ownership_threshold_percent']:
                key = f"ownership:{evt.accession_number}:{evt.reporting_owner_name}:{evt.percent_owned}"
                await create_alert('beneficial_ownership', key, f"{evt.reporting_owner_name} ownership report", f"{evt.issuer_name or evt.issuer_cik} percent={evt.percent_owned}")
        except Exception:
            pass

    entity_matches = []
    for award in award_events:
        try:
            if award.amount and award.amount >= rules['large_award_threshold']:
                key = f"award:{award.generated_internal_id}"
                await create_alert('contract_award', key, f"{award.recipient_name} federal award", f"{award.awarding_agency} amount={award.amount}")
            matched = False
            if rules['enable_entity_linking']:
                for issuer_name in issuer_names:
                    if names_match(issuer_name, award.recipient_name or ''):
                        matched = True
                        entity_matches.append((issuer_name, award))
                        key = f"link:{issuer_name}:{award.generated_internal_id}"
                        await create_alert('entity_link', key, f"Issuer linked to award recipient", f"issuer={issuer_name} recipient={award.recipient_name} agency={award.awarding_agency} amount={award.amount}")
                        break
            if not matched:
                for ticker in issuer_tickers:
                    if ticker and ticker.lower() in (award.recipient_name or '').lower():
                        matched = True
                        entity_matches.append((ticker, award))
                        key = f"ticker_link:{ticker}:{award.generated_internal_id}"
                        await create_alert('entity_link', key, f"Ticker linked to award recipient", f"ticker={ticker} recipient={award.recipient_name} agency={award.awarding_agency} amount={award.amount}")
                        break
        except Exception:
            pass

    if insider_events and award_events:
        correlation_target = next(iter(issuer_names), None) or next(iter(issuer_tickers), None) or recipient
        key = f"correlation:{scan_source}:{cik}:{recipient}:{len(insider_events)}:{len(award_events)}"
        await create_alert(
            'insider_award_correlation',
            key,
            f'Correlative insider activity and contract results detected',
            f'target={correlation_target} source={scan_source} insider_events={len(insider_events)} award_results={len(award_events)}'
        )

    if entity_matches:
        for matched_name, award in entity_matches:
            key = f"correlative_match:{scan_source}:{matched_name}:{award.generated_internal_id}"
            await create_alert(
                'correlative_match',
                key,
                'Tracked company correlated with contract recipient',
                f'match={matched_name} recipient={award.recipient_name} agency={award.awarding_agency} amount={award.amount} source={scan_source}'
            )

    return {
        'insider_events': insider_events,
        'award_events': award_events,
        'insider_status': insider_status,
        'awards_status': awards_status,
        'insider_error': insider_error,
        'awards_error': awards_error,
    }

async def scan_tracked_companies(limit_per_company: int = 5):
    tracked = await list_tracked_companies(limit=200)
    processed = 0
    for company in tracked:
        cik = company['cik']
        recipient = company.get('issuer_name') or company.get('issuer_ticker') or cik
        try:
            await run_scan(cik, recipient, limit=limit_per_company, scan_source='tracked_company')
            processed += 1
        except Exception:
            pass
    await create_alert('tracked_company_scan', f'tracked_scan:{processed}:{limit_per_company}', 'Tracked companies contract comparison run', f'Processed {processed} tracked companies as search-bar-equivalent scans against insider and awards data')
    return processed

async def scheduled_poll_once(limit: int = 0):
    rules = await rule_settings()
    watchlists = await list_watchlists()
    results = []
    for w in watchlists:
        results.append(await run_scan(w['cik'], w['recipient'], limit=limit or rules['default_scan_limit'], scan_source='watchlist'))
    return results


async def auto_scan_universe() -> dict:
    """Walks the scanner's universe — for every ticker we can resolve to a CIK,
    runs a lightweight regulatory scan (SEC insider filings + USAspending contract
    awards) and persists hits to SQLite. The signal index picks them up on its
    next 60s refresh and feeds them into the composite score.

    Throttled to ~`universe_autoscan_request_gap_ms` between SEC requests so we
    stay within SEC's 10 req/sec policy comfortably.
    """
    if _autoscan_lock.locked():
        # Already sweeping — return current snapshot, don't start a duplicate.
        return autoscan_snapshot()
    async with _autoscan_lock:
        return await _auto_scan_universe_impl()


async def _auto_scan_universe_impl() -> dict:
    import time as _time
    from app.services.universe_service import get_universe

    global _autoscan_cancel_requested
    # Fresh sweep -> clear any leftover cancel flag from a prior stop.
    _autoscan_cancel_requested = False

    rules = await rule_settings()
    # Make sure the ticker→CIK map is loaded.
    try:
        cik_map_size = await init_cik_map()
    except Exception as exc:
        _autoscan_state['last_error'] = f'cik_map_init: {exc}'
        log.exception('autoscan: failed to init CIK map: %s', exc)
        return autoscan_snapshot()
    _autoscan_state['cik_map_size'] = cik_map_size

    # Take the scanner's universe and keep only tickers that map to a CIK.
    universe = get_universe('stocks') or []
    universe_tickers = [str(r.get('symbol') or '').upper() for r in universe if r.get('symbol')]
    cik_pairs: list[tuple[str, str, str]] = []
    for sym in universe_tickers:
        cik = cik_for_ticker(sym)
        if cik:
            # The recipient name we send to USAspending is the canonical issuer
            # name from the universe row (more accurate than the bare ticker).
            row_name = next((r.get('name') for r in universe if str(r.get('symbol') or '').upper() == sym), sym)
            cik_pairs.append((sym, cik, row_name or sym))

    # Optional ceiling so the first sweep finishes in a reasonable wall-clock.
    cap = int(rules.get('universe_autoscan_max_tickers', 0) or 0)
    if cap > 0:
        cik_pairs = cik_pairs[:cap]

    if not cik_pairs:
        # Nothing to do; surface a useful error so the UI doesn't show "idle
        # with 0 hits" silently when the cause is an empty universe / empty
        # CIK map.
        msg = (
            f'no tickers with resolvable CIK '
            f'(universe={len(universe_tickers)}, cik_map={cik_map_size})'
        )
        log.warning('autoscan: %s', msg)
        _autoscan_state.update({
            'enabled': True,
            'tickers_total': 0,
            'tickers_done': 0,
            'tickers_with_hits': 0,
            'current_symbol': None,
            'started_at': _time.time(),
            'last_completed_at': _time.time(),
            'last_sweep_seconds': 0.0,
            'last_error': msg,
        })
        return autoscan_snapshot()

    _autoscan_state.update({
        'enabled': True,
        'tickers_total': len(cik_pairs),
        'tickers_done': 0,
        'tickers_with_hits': 0,
        'current_symbol': None,
        'started_at': _time.time(),
        'last_completed_at': None,
        'last_sweep_seconds': None,
        'last_error': None,
    })
    log.info('autoscan: sweep starting over %d tickers (cik_map=%d)',
             len(cik_pairs), cik_map_size)

    gap_seconds = max(0.05, rules.get('universe_autoscan_request_gap_ms', 120) / 1000.0)
    per_symbol_limit = int(rules.get('universe_autoscan_limit_per_symbol', 3) or 3)
    # Phase 26.22: hard per-ticker timeout. Without this, a single ticker
    # whose SEC/USAspending fetches hang (rate-limit storm, slow CDN edge,
    # heavy filer like AAPL with dozens of attached documents) can stall
    # the autoscan loop indefinitely. The UI surfaces this as "stuck in
    # kicking off" because tickers_done never advances. With a 30-second
    # ceiling per ticker, no single bad fetch can block the loop — we log
    # it as a failure and move on. Env-tunable via
    # MRD_AUTOSCAN_TICKER_TIMEOUT_S so operators can raise it if running
    # the autoscan in a slower environment.
    import os as _os
    per_ticker_timeout = max(5.0, float(_os.environ.get('MRD_AUTOSCAN_TICKER_TIMEOUT_S', '30')))
    started = _time.monotonic()
    hits = 0

    for sym, cik, recipient_name in cik_pairs:
        # Phase 26.11: check the dedicated cancel flag instead of the
        # scheduler-enabled flag. The latter is False by default in
        # decoupled mode (REGULATORY_INPROCESS=0), which used to make this
        # loop bail on its very first iteration regardless of the trigger
        # source.
        if _autoscan_cancel_requested:
            _autoscan_state['last_error'] = 'cancelled'
            log.info('autoscan: cancel requested at %d/%d', _autoscan_state['tickers_done'], len(cik_pairs))
            break
        _autoscan_state['current_symbol'] = sym
        try:
            result = await asyncio.wait_for(
                run_scan(cik, recipient_name, limit=per_symbol_limit, scan_source='universe_autoscan'),
                timeout=per_ticker_timeout,
            )
            if (result.get('insider_events') or result.get('award_events')):
                hits += 1
                _autoscan_state['tickers_with_hits'] = hits
        except asyncio.TimeoutError:
            log.debug('autoscan: per-ticker timeout for %s (CIK %s) after %.1fs',
                      sym, cik, per_ticker_timeout)
        except Exception as exc:
            log.debug('autoscan: scan failed for %s (CIK %s): %s', sym, cik, exc)
        finally:
            _autoscan_state['tickers_done'] += 1
        # Be polite to SEC — sleep between requests. SEC permits ~10/sec sustained.
        await asyncio.sleep(gap_seconds)

    elapsed = _time.monotonic() - started
    _autoscan_state.update({
        'current_symbol': None,
        'last_completed_at': _time.time(),
        'last_sweep_seconds': round(elapsed, 1),
    })
    log.info('autoscan: sweep done — %d hits over %d tickers in %.1fs',
             hits, _autoscan_state['tickers_done'], elapsed)
    try:
        await create_alert(
            'universe_autoscan_sweep',
            f'universe_autoscan:{int(_time.time())}',
            f'Universe auto-scan complete ({hits} hits in {elapsed:.0f}s)',
            f'Scanned {_autoscan_state["tickers_done"]} of {_autoscan_state["tickers_total"]} tickers with resolvable CIKs.'
        )
    except Exception:
        pass
    return autoscan_snapshot()


def request_autoscan_cancel() -> None:
    """Cooperative cancel — the running sweep loop checks this flag between
    tickers and exits cleanly. Safe to call when no sweep is in flight (it
    just primes the flag; the next sweep clears it on entry).
    """
    global _autoscan_cancel_requested
    _autoscan_cancel_requested = True

async def scheduler_loop():
    global _scheduler_enabled
    _scheduler_enabled = True
    discovery_counter = 0
    autoscan_counter = 0
    while _scheduler_enabled:
        try:
            rules = await rule_settings()
            if rules['enable_scheduler']:
                await scheduled_poll_once(limit=rules['default_scan_limit'])
                await scan_tracked_companies(limit_per_company=min(rules['default_scan_limit'], 5))
            if rules['enable_auto_discovery']:
                discovery_counter += rules['scheduler_interval_seconds']
                if discovery_counter >= rules['auto_discovery_interval_seconds']:
                    await discover_new_insider_companies(limit=200)
                    await scan_tracked_companies(limit_per_company=min(rules['default_scan_limit'], 5))
                    discovery_counter = 0
            # NEW: universe auto-scan — runs every universe_autoscan_interval_seconds
            # (default 4h) when enabled. This is the engine that drives the new
            # auto-populating insider+award result list.
            if rules['enable_universe_autoscan']:
                autoscan_counter += rules['scheduler_interval_seconds']
                # Trigger immediately the first time (when we wake fresh and the
                # state has never completed), then on the configured cadence.
                first_run = (_autoscan_state.get('last_completed_at') is None
                             and not _autoscan_state.get('current_symbol'))
                due = autoscan_counter >= rules['universe_autoscan_interval_seconds']
                if first_run or due:
                    try:
                        await auto_scan_universe()
                    except Exception as exc:
                        log.exception('autoscan sweep error: %s', exc)
                        _autoscan_state['last_error'] = str(exc)
                    autoscan_counter = 0
            await asyncio.sleep(rules['scheduler_interval_seconds'])
        except Exception:
            await asyncio.sleep(30)

async def start_scheduler():
    """Idempotent — safe to call once on app startup. The loop self-checks the
    enable_scheduler / enable_auto_discovery settings on every tick so the user
    can toggle behavior from the UI without restarting the process.
    """
    global _scheduler_task, _scheduler_enabled
    _scheduler_enabled = True
    if _scheduler_task is None or _scheduler_task.done():
        _scheduler_task = asyncio.create_task(scheduler_loop())


async def stop_scheduler():
    global _scheduler_enabled, _scheduler_task, _autoscan_cancel_requested
    _scheduler_enabled = False
    # Also tell any in-flight sweep to bail out at the next ticker boundary.
    _autoscan_cancel_requested = True
    if _scheduler_task and not _scheduler_task.done():
        _scheduler_task.cancel()
        try:
            await _scheduler_task
        except (asyncio.CancelledError, Exception):
            pass
    _scheduler_task = None
