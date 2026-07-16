"""
Stooq EOD CSV provider.

Free, no auth, no rate-limit in practice.  CSV endpoint:
  https://stooq.com/q/d/l/?s={symbol}.US&i=d

Returns the most recent two daily closes as `last_price` and `previous_close`.
Used as a final live-data redundancy step before falling back to cache/preview.
"""
from __future__ import annotations

import csv
import io
import logging
import time
from threading import Lock

import requests

from app.services.providers.base import count_error, count_timeout
from app.utils.time import utcnowiso

log = logging.getLogger('app.providers.stooq')

_BASE = 'https://stooq.com/q/d/l/'
_HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; MarketRefinementDashboard/1.0)'}
_TIMEOUT = 3.0  # tightened from 6s — burning 6s per timeout on 943 attempts = 1.5 hrs of wasted clock

# Phase 26.60: persistent module-level executor.
# Previously we created a fresh ThreadPoolExecutor per fetch() batch and
# abandoned it via shutdown(wait=False).  At ~30 batches per universal
# sweep that was ~300 thread create/destroy events per sweep just for
# stooq — one of four such per-batch pools (see also yahoo_chart,
# scoring intraday, options prefetch).  Combined thread churn = ~1,100
# per sweep, contributing measurably to CPU spikes at large universe
# sizes.
#
# The persistent pool is sized at 2x the prior per-batch cap (10 -> 20)
# to absorb hung sockets from previous batches without blocking new
# work: hung futures release their worker slot when the underlying HTTP
# call finally times out at the OS layer (_TIMEOUT=3.0s here), and the
# pool naturally heals over time.
from concurrent.futures import ThreadPoolExecutor as _StooqTpe
_POOL = _StooqTpe(max_workers=20, thread_name_prefix='stooq')

# Diagnostics so the operator can see WHY hit-rate is zero
# (timeouts vs http_errors vs no_data vs parse_errors).
_lock = Lock()
_diag = {
    'attempts': 0,
    'successes': 0,
    'timeouts': 0,
    'http_errors': 0,
    'no_data': 0,
    'parse_errors': 0,
    'network_errors': 0,
    'consecutive_failures': 0,
    'circuit_open_until': 0.0,
    'circuit_trip_count': 0,
    'next_cooldown_seconds': 0.0,
    # Phase 26.9: half-open probe state. After cooldown elapses we flag this
    # `True` so the next failure trips immediately (with exponential backoff)
    # instead of accumulating another full _FAIL_THRESHOLD batch of failures.
    'half_open': False,
}

# Circuit breaker: after this many consecutive failures, stop trying for the
# duration of the cooldown so we don't keep burning a 3s round-trip per symbol.
# Lowered from 50 to 20 (Phase 26.7) so a fully-broken Stooq trips ~2.5x
# faster on each probationary re-open, reducing wasted timeouts/min.
_FAIL_THRESHOLD = 20
# Exponential cooldown — every subsequent trip doubles the cooldown so a
# persistently-unreachable Stooq host (typical from datacenter IPs) backs off
# to a 24-hr cadence instead of repeatedly tripping every 30 min.
_COOLDOWN_LADDER_SECONDS = [
    30 * 60,        # 1st trip: 30 min
    60 * 60,        # 2nd trip: 1 hr
    2 * 60 * 60,    # 3rd trip: 2 hr
    6 * 60 * 60,    # 4th trip: 6 hr
    24 * 60 * 60,   # 5th+ trips: 24 hr (cap)
]


def _next_cooldown_for_trip(trip_count: int) -> float:
    idx = min(max(trip_count, 1), len(_COOLDOWN_LADDER_SECONDS)) - 1
    return float(_COOLDOWN_LADDER_SECONDS[idx])


def stats_snapshot() -> dict:
    with _lock:
        snap = dict(_diag)
        now_mono = time.monotonic()
        snap['circuit_open'] = bool(snap['circuit_open_until'] and now_mono < snap['circuit_open_until'])
        snap['circuit_remaining_seconds'] = max(0.0, snap['circuit_open_until'] - now_mono) if snap['circuit_open'] else 0.0
        # Phase 26.7: expose the trip threshold + next-cooldown so the
        # /api/providers/status endpoint can render the CB row correctly.
        snap['fail_threshold'] = _FAIL_THRESHOLD
        snap['next_cooldown_seconds'] = _next_cooldown_for_trip(
            int(snap.get('circuit_trip_count') or 0) + 1
        )
        return snap


def _to_stooq_symbol(sym: str, market: str) -> str:
    s = sym.upper().strip()
    if market == 'crypto':
        return ''  # Stooq crypto symbols differ; defer to crypto-specific providers
    # Stooq prefers `aapl.us`, `msft.us` form for US equities.
    if '.' in s:
        return s.lower()
    return f'{s.lower()}.us'


def _parse_csv(text: str) -> list[dict]:
    rows: list[dict] = []
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        try:
            rows.append({
                'date': row.get('Date'),
                'open': float(row.get('Open') or 0),
                'high': float(row.get('High') or 0),
                'low': float(row.get('Low') or 0),
                'close': float(row.get('Close') or 0),
                'volume': float(row.get('Volume') or 0),
            })
        except Exception:
            continue
    return rows


def _fetch_one(sym: str, market: str, captured_at: str) -> tuple[str, dict | None]:
    stooq_sym = _to_stooq_symbol(sym, market)
    if not stooq_sym:
        return sym, None
    try:
        # Phase 26.18.c: resilient_get hardens stooq against transient
        # 5xx + connection resets it commonly emits during peak hours.
        from app.services.http_client import resilient_get, ResilientGetConfig
        cfg = ResilientGetConfig(
            max_attempts=3, connect_timeout=3.0,
            read_timeout=_TIMEOUT, retry_after_cap_seconds=3.0,
        )
        res = resilient_get(_BASE, params={'s': stooq_sym, 'i': 'd'}, cfg=cfg)
        if res.status_code == 0 and res.error:
            # Transport exhausted - classify based on error type.
            err_l = res.error.lower()
            with _lock:
                if 'timeout' in err_l:
                    _diag['timeouts'] += 1
                else:
                    _diag['network_errors'] += 1
                _diag['consecutive_failures'] += 1
            if 'timeout' in err_l:
                count_timeout('stooq', f'{sym}: {res.error}')
            else:
                count_error('stooq', f'{sym}: {res.error}')
            return sym, None
        if res.status_code != 200 or not res.text:
            with _lock:
                _diag['http_errors'] += 1
                _diag['consecutive_failures'] += 1
            return sym, None
        text = res.text.strip()
        if text.lower().startswith('no data'):
            with _lock:
                _diag['no_data'] += 1
                _diag['consecutive_failures'] += 1
            return sym, None
        rows = _parse_csv(text)
        if not rows:
            with _lock:
                _diag['parse_errors'] += 1
                _diag['consecutive_failures'] += 1
            return sym, None
        last_row = rows[-1]
        prev_row = rows[-2] if len(rows) >= 2 else last_row
        last_price = last_row['close']
        prev_close = prev_row['close'] if prev_row['close'] > 0 else last_price
        if last_price <= 0:
            with _lock:
                _diag['parse_errors'] += 1
                _diag['consecutive_failures'] += 1
            return sym, None
        with _lock:
            _diag['successes'] += 1
            _diag['consecutive_failures'] = 0
            # A successful probe ends half-open mode -> circuit is healthy.
            _diag['half_open'] = False
        return sym, {
            'last_price': last_price,
            'previous_close': prev_close,
            'open': last_row['open'] or last_price,
            'day_low': last_row['low'] or last_price,
            'day_high': last_row['high'] or last_price,
            'volume': last_row['volume'],
            'market_cap': 0.0,
            'captured_at_utc': captured_at,
            'source': 'stooq',
            'provider_outcome': 'live_success',
            'preview_only': False,
        }
    except Exception as exc:  # noqa: BLE001
        log.debug('stooq fetch failed for %s: %s', sym, exc)
        with _lock:
            _diag['network_errors'] += 1
            _diag['consecutive_failures'] += 1
        count_error('stooq', f'{sym}: {exc}')
        return sym, None


def fetch(symbols: list[str], market: str) -> dict[str, dict]:
    if not symbols or market == 'crypto':
        return {}
    # Circuit breaker: if we've had a flood of consecutive failures, skip this
    # provider entirely until the cooldown elapses. This stops Stooq from
    # burning ~3s per symbol when its host is unreachable from our network.
    now_mono = time.monotonic()
    with _lock:
        if _diag['circuit_open_until'] and now_mono < _diag['circuit_open_until']:
            return {}
        # Trip check. Two paths can fire it:
        #   1) Normal: consecutive_failures hit the threshold AND we are not
        #      currently in a cooldown window.
        #   2) Half-open probe: cooldown just elapsed and the probationary
        #      probe failed even once -> re-trip immediately with the next
        #      ladder step (so a chronically broken host doesn't take another
        #      _FAIL_THRESHOLD round-trips to re-pause).
        normal_trip = (
            _diag['consecutive_failures'] >= _FAIL_THRESHOLD
            and not _diag['circuit_open_until']
        )
        half_open_retrip = (
            _diag.get('half_open')
            and _diag['consecutive_failures'] > 0
            and not _diag['circuit_open_until']
        )
        if normal_trip or half_open_retrip:
            _diag['circuit_trip_count'] = _diag.get('circuit_trip_count', 0) + 1
            cd = _next_cooldown_for_trip(_diag['circuit_trip_count'])
            _diag['circuit_open_until'] = now_mono + cd
            _diag['next_cooldown_seconds'] = cd
            _diag['half_open'] = False
            log.warning(
                'stooq circuit breaker tripped (#%d, %s) after %d consecutive failures; '
                'pausing for %d minutes (exponential backoff)',
                _diag['circuit_trip_count'],
                'half-open probe' if half_open_retrip else 'threshold',
                _diag['consecutive_failures'], int(cd // 60),
            )
            return {}
        # Re-enable after cooldown elapses -> enter half-open probationary mode.
        if _diag['circuit_open_until'] and now_mono >= _diag['circuit_open_until']:
            _diag['circuit_open_until'] = 0.0
            _diag['consecutive_failures'] = 0
            _diag['half_open'] = True
            log.info(
                'stooq circuit breaker cooldown elapsed; entering half-open probe '
                '(next failure will re-trip with %d-min cooldown).',
                int(_next_cooldown_for_trip(_diag.get('circuit_trip_count', 0) + 1) // 60),
            )
    out: dict[str, dict] = {}
    captured_at = utcnowiso()
    with _lock:
        _diag['attempts'] += len(symbols)
    # Parallelize across symbols — same logic as the yahoo-chart cascade.
    #
    # Phase 26.32: do NOT use `with ThreadPoolExecutor() as pool:`.  The
    # context-manager exit calls `pool.shutdown(wait=True)` which blocks
    # until every submitted future completes, even if as_completed already
    # gave up at its 20-second deadline.  If any individual fetch hangs
    # past its requests.timeout (DNS deadlock, CLOSE_WAIT socket, etc.),
    # the entire batch wedges indefinitely — that's the user-reported
    # 600-second snap-worker stall on every second pass.  Manual pool
    # management with shutdown(wait=False) lets us bail at exactly the
    # 20-second budget regardless of how many fetches are still hung.
    # Phase 26.60: use the persistent module-level pool `_POOL`.
    # `as_completed(timeout=20)` still enforces the batch deadline;
    # hung futures continue running in the pool without blocking new
    # submissions from the next batch (their worker slots free when
    # the underlying HTTP call times out at the OS layer).
    from concurrent.futures import (
        as_completed,
        TimeoutError as _FuturesTimeoutError,
    )
    futures = [_POOL.submit(_fetch_one, sym, market, captured_at) for sym in symbols]
    try:
        for fut in as_completed(futures, timeout=20):
            try:
                sym, payload = fut.result()
                if payload:
                    out[sym] = payload
            except Exception:
                continue
    except _FuturesTimeoutError:
        # 20s budget exhausted; whatever completed is in `out`.
        # The remaining futures keep running in the persistent pool
        # but we won't wait for them.
        log.debug('stooq fetch: as_completed timed out at 20s; %d/%d done',
                  len(out), len(futures))
    return out
