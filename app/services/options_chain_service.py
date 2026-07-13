"""
Real options-chain fetcher for the Ultra Scanner.

Goals:
  * Use `yf.Ticker(symbol).option_chain(expiry)` for active-scan top symbols.
  * Aggressively cache by symbol with a TTL (chain doesn't change tick-by-tick).
  * Throttle requests so we never starve the main scanner budget.
  * Always return a dict matching the inferred-heuristic options payload shape,
    so the contract is identical whether real or inferred data was used.

Provenance is stamped via the `provenance` field (`real_chain` vs `inferred`).
Falls back to `None` on error/timeout so the orchestrator can use the inferred
heuristic instead — contract integrity preserved.
"""
from __future__ import annotations

import logging
import os
import time
from threading import Lock
from typing import Any

import pandas as pd

# Small helper to keep env-var lookups uniform.
def _os_env(key: str, default: str) -> str:
    return os.environ.get(key, default)


log = logging.getLogger('app.options_chain')

# ---------------------------------------------------------------------------
# Phase 26.25: Yahoo-options hard-timeout executor + warrant skip list.
# yfinance.Ticker.options and ticker.option_chain(exp) wrap requests calls
# with no timeout. For warrant/unit tickers (..W, ..U, ..WS) Yahoo's CDN
# frequently hangs the TCP socket indefinitely — a single such symbol can
# block the snapshot worker thread forever. We harden two ways:
#   1. A *long-lived* timeout executor that lets us submit any yfinance
#      call and reap it with a hard 12s ceiling. If the call hangs, we
#      abandon the thread (it leaks until the process exits, but the
#      scanner moves on) and return None so the caller falls through
#      to the inferred heuristic.
#   2. A cheap suffix check that skips symbols that are virtually
#      guaranteed not to have listed options chains. This catches the
#      bulk of the historically-hanging tickers BEFORE we ever issue
#      a network call.
# Real options-bearing tickers are unaffected — they take the same path
# as before, just with a hard wall-clock ceiling around each yfinance call.
# ---------------------------------------------------------------------------
from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FTimeoutError
from threading import Lock as _TimeoutExecLock

# Phase 26.31: bumped from 4 → 16.  These threads are CHEAP when idle
# (each just owns ~8 KB of stack) but if even a handful hang on Yahoo
# socket reads, max_workers=4 fills up and *all* subsequent calls queue
# behind dead threads — exactly the long-haul wedge symptom.  At 16
# slots we can absorb a dozen hung sockets and still serve fresh
# requests; the OS reclaims the hung sockets eventually (Windows ~21s
# SYN deadband, Linux up to a few minutes via TCP keepalive) so the
# pool naturally heals over time.  Not 32: that's overkill for the
# realistic concurrency (~2 snap-workers × ~5 calls each) and risks
# tripping Yahoo's per-client rate limit.
_YF_TIMEOUT_EXECUTOR_LOCK = _TimeoutExecLock()
_YF_TIMEOUT_EXECUTOR = ThreadPoolExecutor(
    max_workers=16, thread_name_prefix='yf-options-timeout',
)
_YF_CALL_TIMEOUT_SECONDS = 12.0
# Track hung-call stats so /system/status can surface them and so we
# can rebuild the executor if it ever truly fills up.
_YF_TIMEOUT_STATS = {
    'submits': 0,
    'timeouts': 0,
    'errors': 0,
    'completions': 0,
    'executor_rebuilds': 0,
}


def yf_timeout_executor_stats() -> dict:
    """Operator telemetry for the yfinance timeout executor.  Surfaced in
    `/system/status.options_chain_stats` so we can see at a glance if
    Yahoo is hanging us systematically."""
    with _YF_TIMEOUT_EXECUTOR_LOCK:
        return dict(_YF_TIMEOUT_STATS)


def _rebuild_yf_timeout_executor() -> None:
    """Last-ditch recovery if the timeout executor's queue fills with
    hung work and stays full for too long.  Called from
    `_call_with_timeout` when we detect chronic submission backpressure.
    The old executor is shutdown(wait=False) and abandoned — its
    daemon threads will exit when their sockets time out.
    """
    global _YF_TIMEOUT_EXECUTOR
    with _YF_TIMEOUT_EXECUTOR_LOCK:
        try:
            old = _YF_TIMEOUT_EXECUTOR
            _YF_TIMEOUT_EXECUTOR = ThreadPoolExecutor(
                max_workers=16, thread_name_prefix='yf-options-timeout',
            )
            _YF_TIMEOUT_STATS['executor_rebuilds'] += 1
            try:
                old.shutdown(wait=False)
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            pass


def _no_options_suffix(sym: str) -> bool:
    """True if the symbol looks like a warrant/unit/right with no listed options.

    These suffixes are exchange conventions for derivative listings that
    never have their own options chains. Hitting Yahoo for them just
    earns a hang or an empty response after a timeout that we can't
    control. Catching them here saves an HTTP roundtrip AND prevents
    the worker thread from getting stuck on a Yahoo CDN socket hang.
    """
    if not sym or len(sym) < 2:
        return False
    s = sym.upper()
    return (
        s.endswith('W')          # warrants (most common: ..W, ..WS, ..WW)
        or s.endswith('WS')
        or s.endswith('.WS')
        or s.endswith('-WT')
        or s.endswith('.WT')
        or s.endswith('U')       # SPAC units
        or s.endswith('.U')
        or s.endswith('-UN')
        or s.endswith('-R')      # rights
        or s.endswith('.R')
    )


def _call_with_timeout(fn, *args, timeout: float | None = None, **kwargs):
    """Run a (potentially) hanging callable on the timeout executor.

    Returns the callable's result if it completes within `timeout`
    seconds, otherwise returns None. The hung thread is abandoned —
    the underlying socket eventually times out at the OS layer (Windows
    typically 21 s on a SYN/keepalive deadband) and the thread will
    clean itself up; in the worst case it leaks until process exit,
    but the snapshot scanner keeps making progress.
    """
    if timeout is None:
        timeout = _YF_CALL_TIMEOUT_SECONDS
    # Snapshot the executor reference so a concurrent rebuild can't
    # leave us calling .submit() on a freshly-shutdown executor.
    with _YF_TIMEOUT_EXECUTOR_LOCK:
        execr = _YF_TIMEOUT_EXECUTOR
        _YF_TIMEOUT_STATS['submits'] += 1
    try:
        fut = execr.submit(fn, *args, **kwargs)
    except RuntimeError:
        # Executor was shut down between snapshot and submit; rebuild
        # and try once more.  If that still fails, return None — the
        # caller falls through to the inferred-heuristic path.
        _rebuild_yf_timeout_executor()
        try:
            with _YF_TIMEOUT_EXECUTOR_LOCK:
                execr = _YF_TIMEOUT_EXECUTOR
            fut = execr.submit(fn, *args, **kwargs)
        except Exception:  # noqa: BLE001
            with _YF_TIMEOUT_EXECUTOR_LOCK:
                _YF_TIMEOUT_STATS['errors'] += 1
            return None
    try:
        result = fut.result(timeout=timeout)
        with _YF_TIMEOUT_EXECUTOR_LOCK:
            _YF_TIMEOUT_STATS['completions'] += 1
        return result
    except _FTimeoutError:
        with _YF_TIMEOUT_EXECUTOR_LOCK:
            _YF_TIMEOUT_STATS['timeouts'] += 1
        return None
    except Exception:
        with _YF_TIMEOUT_EXECUTOR_LOCK:
            _YF_TIMEOUT_STATS['errors'] += 1
        return None


# ---------------------------------------------------------------------------
# Tunable knobs
# ---------------------------------------------------------------------------
# Per-symbol TTL bumped from 180s → 600s (10 min). Option chains don't change
# meaningfully on a 3-min cadence and the previous TTL was causing huge
# rate-limit-skip churn — every cache eviction triggered a fresh multi-HTTP
# fetch for the same symbol within minutes.
_TTL_SECONDS = 600
_MIN_GAP_SECONDS = 0.75     # rate limit per request to the same host (Yahoo options endpoint)
_MAX_CONCURRENT_INFLIGHT = 2  # parallel option-chain fetches in flight
_FAIL_COOLDOWN = 600        # 10 min cooldown after a transient fetch failure
_NO_OPTIONS_COOLDOWN = 60 * 60  # 1 hr cooldown once we get an empty options list. Previously 6 hr — that was too long on rate-limited residential ISPs where Yahoo intermittently returns an empty `options` attr even for symbols that DO have listed chains. After 1 hr a re-probe is cheap (one HTTP call) and self-correcting: if the symbol genuinely has no options, the cooldown re-arms; if Yahoo just throttled us, the symbol re-enters the cascade.
# Phase 26.14: default expirations bumped 2 -> 4 because the new CBOE primary
# returns the FULL chain in a single HTTP roundtrip, so going deeper has zero
# HTTP cost. Yahoo (fallback) still pays per-expiry HTTP, so its path caps to
# `min(max_expirations, _YAHOO_MAX_EXPIRATIONS_CAP)` to preserve its budget.
_DEFAULT_MAX_EXPIRATIONS = 4
_YAHOO_MAX_EXPIRATIONS_CAP = 2  # never make more than 2 HTTP roundtrips to Yahoo per symbol

# ---------------------------------------------------------------------------
# In-process cache & throttling
# ---------------------------------------------------------------------------
_lock = Lock()
_cache: dict[str, tuple[float, dict | None]] = {}
_last_request_ts: float = 0.0
_inflight: int = 0

# Per-symbol cooldown after a failed fetch — prevents hammering a problem ticker.
_fail_until: dict[str, float] = {}
# Per-symbol skip reason — populated alongside _fail_until so the UI can explain
# why a symbol was skipped. Possible values: 'no_options_listed', 'fetch_error',
# 'rate_limited' (transient — not stored long-term).
_skip_reason: dict[str, str] = {}

# Stats surfaced via /system/status.options_chain_stats
_stats = {'attempts': 0, 'hits_real': 0, 'cache_hits': 0, 'errors': 0,
          'cooldown_skips': 0, 'throttle_skips': 0,
          'no_options_skips': 0, 'fetch_error_skips': 0,
          'no_options_unique_symbols': 0,
          # Phase 26.14: per-source attribution. `hits_real` remains the
          # total ("any real chain succeeded") for backward compat with
          # existing dashboards. The new counters break it down by source
          # and also track the fallback chain.
          'cboe_attempts': 0, 'cboe_hits': 0, 'cboe_misses': 0, 'cboe_errors': 0,
          'yahoo_attempts': 0, 'yahoo_hits': 0, 'yahoo_misses': 0, 'yahoo_errors': 0,
          'fallback_to_yahoo': 0,  # times CBOE returned None and we tried Yahoo
          }


def stats_snapshot() -> dict[str, int]:
    with _lock:
        snap = dict(_stats)
    # Merge in the yfinance timeout executor telemetry under a clear
    # namespace so operators can see hang rates without scrolling through
    # an unrelated section of /system/status.
    yf_exec = yf_timeout_executor_stats()
    for k, v in yf_exec.items():
        snap[f'yf_timeout_executor.{k}'] = v
    # Phase 26.33: surface bookkeeping-dict sizes so operators can spot
    # unbounded growth before it becomes a CPU problem.
    snap['cache_entries'] = len(_cache)
    snap['fail_until_entries'] = len(_fail_until)
    snap['skip_reason_entries'] = len(_skip_reason)
    return snap


def prune_expired_state() -> dict[str, int]:
    """Drop expired entries from `_cache`, `_fail_until`, and `_skip_reason`.

    Called from the sweep-boundary hook (gc_service) so the per-symbol
    bookkeeping dicts don't grow unboundedly across many universal
    passes.  Without pruning, after ~10 days every option-tried symbol
    in the universe has a sticky entry here, and the periodic O(N)
    iteration paths (telemetry, status endpoints) start showing up in
    profiles.

    Returns a count breakdown for /system/status telemetry.
    """
    now = _now()
    pruned_cache = 0
    pruned_fail = 0
    pruned_skip = 0
    with _lock:
        # _cache TTL is _TTL_SECONDS; entries older than 2x TTL
        # are definitely safe to drop (a refetch will fill them again).
        cutoff_cache = 2 * _TTL_SECONDS
        stale_keys = [
            sym for sym, (ts, _payload) in _cache.items()
            if (now - ts) > cutoff_cache
        ]
        for k in stale_keys:
            del _cache[k]
            pruned_cache += 1
        # _fail_until entries whose cooldown elapsed >1 hr ago.
        # (Keeping recently-expired ones lets the cooldown info show
        # up in the UI for a bit.)
        stale_fail = [
            sym for sym, until in _fail_until.items()
            if until + 3600 < now
        ]
        for k in stale_fail:
            del _fail_until[k]
            pruned_fail += 1
        # Drop skip_reason entries that no longer have a matching
        # fail_until entry — these are vestigial bookkeeping.
        stale_skip = [
            sym for sym in _skip_reason
            if sym not in _fail_until
        ]
        for k in stale_skip:
            del _skip_reason[k]
            pruned_skip += 1
    if pruned_cache or pruned_fail or pruned_skip:
        log.info(
            'options_chain prune: cache=%d, fail_until=%d, skip_reason=%d',
            pruned_cache, pruned_fail, pruned_skip,
        )
    return {
        'cache_pruned': pruned_cache,
        'fail_until_pruned': pruned_fail,
        'skip_reason_pruned': pruned_skip,
    }


def _now() -> float:
    return time.monotonic()


def _options_gamma_level_label(score: float) -> str:
    if score >= 70: return 'high_call_pressure'
    if score >= 58: return 'mild_call_pressure'
    if score <= 30: return 'high_put_pressure'
    if score <= 42: return 'mild_put_pressure'
    return 'moderate'


def _summarize_chain(symbol: str, last_price: float, chains: list[tuple[str, Any, Any]]) -> dict:
    """Distill multiple expirations into the unified options_positioning shape.

    chains: list of (expiry_str, calls_df, puts_df)
    """
    total_call_oi = 0.0
    total_put_oi = 0.0
    total_call_vol = 0.0
    total_put_vol = 0.0
    weighted_call_strike = 0.0
    weighted_put_strike = 0.0
    weighted_call_strike_oi = 0.0
    weighted_put_strike_oi = 0.0
    near_term_calls = 0.0
    near_term_puts = 0.0
    monthly_calls = 0.0
    monthly_puts = 0.0
    max_pain_candidates: dict[float, float] = {}

    for idx, (expiry, calls_df, puts_df) in enumerate(chains):
        try:
            if calls_df is None or puts_df is None:
                continue
            c_oi = float(calls_df['openInterest'].fillna(0).sum())
            p_oi = float(puts_df['openInterest'].fillna(0).sum())
            c_vol = float(calls_df['volume'].fillna(0).sum())
            p_vol = float(puts_df['volume'].fillna(0).sum())
            total_call_oi += c_oi
            total_put_oi += p_oi
            total_call_vol += c_vol
            total_put_vol += p_vol
            if idx == 0:
                near_term_calls = c_oi + c_vol * 0.5
                near_term_puts = p_oi + p_vol * 0.5
            else:
                monthly_calls += c_oi + c_vol * 0.25
                monthly_puts += p_oi + p_vol * 0.25
            if c_oi > 0:
                weighted_call_strike += float((calls_df['strike'] * calls_df['openInterest'].fillna(0)).sum())
                weighted_call_strike_oi += c_oi
            if p_oi > 0:
                weighted_put_strike += float((puts_df['strike'] * puts_df['openInterest'].fillna(0)).sum())
                weighted_put_strike_oi += p_oi
            # Approximate max-pain: strike where ITM-equivalent open interest is minimized.
            strikes = sorted(set(list(calls_df['strike'].dropna()) + list(puts_df['strike'].dropna())))
            for strike in strikes:
                call_pain = float(calls_df[calls_df['strike'] < strike].apply(
                    lambda r: (strike - r['strike']) * (r['openInterest'] or 0), axis=1
                ).sum()) if not calls_df.empty else 0.0
                put_pain = float(puts_df[puts_df['strike'] > strike].apply(
                    lambda r: (r['strike'] - strike) * (r['openInterest'] or 0), axis=1
                ).sum()) if not puts_df.empty else 0.0
                max_pain_candidates[strike] = max_pain_candidates.get(strike, 0.0) + call_pain + put_pain
        except Exception as exc:
            log.debug('chain summary error for %s expiry %s: %s', symbol, expiry, exc)
            continue

    if total_call_oi <= 0 and total_put_oi <= 0:
        return {}

    put_call_ratio = (total_put_oi / total_call_oi) if total_call_oi > 0 else 999.0
    put_call_vol_ratio = (total_put_vol / total_call_vol) if total_call_vol > 0 else 999.0
    # Composite weighted strikes (call wall = highest OI call strike weighted)
    call_wall = (weighted_call_strike / weighted_call_strike_oi) if weighted_call_strike_oi > 0 else None
    put_wall = (weighted_put_strike / weighted_put_strike_oi) if weighted_put_strike_oi > 0 else None
    # Find max-pain strike
    target_price = min(max_pain_candidates, key=max_pain_candidates.get) if max_pain_candidates else last_price

    # Score:  ratio < 1 = call-heavy = bullish positioning
    if put_call_ratio < 0.6: score = 75.0
    elif put_call_ratio < 0.85: score = 62.0
    elif put_call_ratio < 1.0: score = 55.0
    elif put_call_ratio < 1.25: score = 45.0
    elif put_call_ratio < 1.6: score = 38.0
    else: score = 25.0
    # Volume nudge
    if put_call_vol_ratio < 0.7: score = min(100.0, score + 5)
    elif put_call_vol_ratio > 1.4: score = max(0.0, score - 5)
    bias = 'bullish' if score >= 58 else 'bearish' if score <= 42 else 'neutral'

    # Pin risk: how close is current price to weighted target?
    if last_price > 0 and target_price > 0:
        proximity_pct = abs(target_price - last_price) / last_price * 100.0
    else:
        proximity_pct = 99.0
    pin_risk = 'high' if proximity_pct <= 0.75 else 'moderate' if proximity_pct <= 2.0 else 'low'

    return {
        'score': round(score, 2),
        'bias': bias,
        'status': 'implemented',
        'provenance': 'real_chain',
        'gamma_level_label': _options_gamma_level_label(score),
        'pin_risk': pin_risk,
        'composite': {
            'target_price': round(target_price, 2),
            'call_wall': round(call_wall, 2) if call_wall else None,
            'put_wall': round(put_wall, 2) if put_wall else None,
            'bias': bias,
            'pressure_score': round(score, 2),
            'put_call_ratio': round(put_call_ratio, 3),
            'put_call_vol_ratio': round(put_call_vol_ratio, 3),
        },
        'near_term': {
            'call_oi': near_term_calls,
            'put_oi': near_term_puts,
        },
        'monthly': {
            'call_oi': monthly_calls,
            'put_oi': monthly_puts,
        },
        'expirations_used': len(chains),
        'expiration_dates': sorted({str(e) for (e, _c, _p) in chains}),
        'nearest_expiration': min((str(e) for (e, _c, _p) in chains), default=None),
    }


def _can_make_request_now() -> bool:
    global _last_request_ts, _inflight
    now = _now()
    if _inflight >= _MAX_CONCURRENT_INFLIGHT:
        return False
    if now - _last_request_ts < _MIN_GAP_SECONDS:
        return False
    return True


def get_real_options_positioning(symbol: str, last_price: float, *,
                                  max_expirations: int = _DEFAULT_MAX_EXPIRATIONS) -> dict | None:
    """Return a fully-shaped options_positioning payload from real chain data,
    or None if every source is unavailable / cooldown active / throttled.

    Phase 26.14: tries CBOE delayed quotes FIRST (single HTTP roundtrip for
    the full chain, no auth, gives us IV + Greeks), then falls back to Yahoo
    Finance (per-expiry HTTP, no Greeks). The orchestrator caller substitutes
    the inferred heuristic on None.

    Caller is responsible for substituting the inferred heuristic on None.
    """
    sym = (symbol or '').upper()
    if not sym or last_price <= 0:
        return None

    # 1) PRIMARY: CBOE delayed quotes. Single HTTP, full chain, free Greeks.
    try:
        from app.services.providers import cboe_options_provider as _cboe
        with _lock:
            _stats['cboe_attempts'] += 1
            _stats['attempts'] += 1
        cboe_payload = _cboe.get_options_positioning(sym, last_price,
                                                     max_expirations=max_expirations)
        if cboe_payload:
            with _lock:
                _stats['cboe_hits'] += 1
                _stats['hits_real'] += 1
            return cboe_payload
        # CBOE returned None - either symbol not listed, no data, or transient.
        # Record a miss and fall through to Yahoo.
        with _lock:
            _stats['cboe_misses'] += 1
            _stats['fallback_to_yahoo'] += 1
    except Exception as exc:  # noqa: BLE001
        with _lock:
            _stats['cboe_errors'] += 1
            _stats['fallback_to_yahoo'] += 1
        log.debug('cboe options provider raised for %s (falling back to yahoo): %s',
                  sym, exc)

    # 2) FALLBACK: Yahoo Finance via yfinance.
    yahoo_max_exp = min(max_expirations, _YAHOO_MAX_EXPIRATIONS_CAP)
    return _get_yahoo_options_positioning(sym, last_price, max_expirations=yahoo_max_exp)


def _get_yahoo_options_positioning(symbol: str, last_price: float, *,
                                   max_expirations: int = _DEFAULT_MAX_EXPIRATIONS) -> dict | None:
    """Yahoo Finance options-chain fetch (the original implementation, now
    used as a fallback after CBOE).
    """
    global _last_request_ts, _inflight
    sym = (symbol or '').upper()
    if not sym or last_price <= 0:
        return None

    now = _now()

    with _lock:
        cooldown = _fail_until.get(sym, 0)
        if cooldown and now < cooldown:
            _stats['cooldown_skips'] += 1
            # Attribute the skip to its specific reason so the UI can explain
            # the count to operators.
            reason = _skip_reason.get(sym, 'fetch_error')
            if reason == 'no_options_listed':
                _stats['no_options_skips'] += 1
            else:
                _stats['fetch_error_skips'] += 1
            return None
        # Cache check first
        cached = _cache.get(sym)
        if cached and (now - cached[0] <= _TTL_SECONDS):
            payload = cached[1]
            if payload is not None:
                _stats['cache_hits'] += 1
                return dict(payload)
            return None
        if not _can_make_request_now():
            _stats['throttle_skips'] += 1
            return None
        _inflight += 1
        _last_request_ts = now
        _stats['yahoo_attempts'] += 1

    payload: dict | None = None
    try:
        # Phase 26.25: skip symbols whose suffix tells us up-front they
        # don't have listed options (warrants, units, rights). This
        # avoids the documented Yahoo CDN hang on these tickers.
        if _no_options_suffix(sym):
            with _lock:
                _skip_reason[sym] = 'no_options_listed'
                _fail_until[sym] = _now() + _NO_OPTIONS_COOLDOWN
                _stats['yahoo_misses'] += 1
            return None

        import yfinance as yf
        ticker = yf.Ticker(sym)
        # Hard-timeout the metadata fetch. `ticker.options` is a
        # property that triggers an HTTP roundtrip to Yahoo under the
        # hood; on warrant tickers it can wedge a TCP socket and never
        # return.
        expirations = _call_with_timeout(
            lambda: list(getattr(ticker, 'options', []) or []),
        )
        if expirations is None:
            # The yfinance call hung — treat as a transient fetch error
            # (short cooldown so the symbol can be re-probed later).
            with _lock:
                _stats['yahoo_errors'] += 1
                _fail_until[sym] = _now() + _FAIL_COOLDOWN
                _skip_reason[sym] = 'fetch_error'
            return None
        if not expirations:
            # Symbol simply has no listed options (common for warrants, units,
            # micro-caps, etc.). Mark with a LONG cooldown so we don't burn an
            # HTTP round-trip on this symbol every 10 min for the next 6 hrs.
            with _lock:
                if sym not in _skip_reason or _skip_reason.get(sym) != 'no_options_listed':
                    _stats['no_options_unique_symbols'] = _stats.get('no_options_unique_symbols', 0) + 1
                _skip_reason[sym] = 'no_options_listed'
                _fail_until[sym] = _now() + _NO_OPTIONS_COOLDOWN
                _stats['yahoo_misses'] += 1
            payload = None
        else:
            chosen = expirations[:max_expirations]
            chains: list[tuple[str, Any, Any]] = []
            for expiry in chosen:
                # Hard-timeout each option_chain fetch. yfinance issues a
                # fresh HTTP call per expiry, and any one of them can
                # hang.
                chain = _call_with_timeout(ticker.option_chain, expiry)
                if chain is None:
                    log.debug('option_chain fetch timed out or failed for %s %s', sym, expiry)
                    continue
                chains.append((expiry, chain.calls, chain.puts))
            payload = _summarize_chain(sym, last_price, chains) if chains else None
            if payload and payload.get('score') is not None:
                with _lock:
                    _stats['yahoo_hits'] += 1
                    _stats['hits_real'] += 1
                    # Successful fetch clears any stale skip reason.
                    _skip_reason.pop(sym, None)
            else:
                with _lock:
                    _stats['yahoo_misses'] += 1
                payload = None
    except Exception as exc:  # noqa: BLE001
        log.debug('options_chain top-level failure for %s: %s', sym, exc)
        with _lock:
            _stats['errors'] += 1
            _stats['yahoo_errors'] += 1
            _fail_until[sym] = _now() + _FAIL_COOLDOWN
            _skip_reason[sym] = 'fetch_error'
        payload = None
    finally:
        with _lock:
            _inflight = max(0, _inflight - 1)
            _cache[sym] = (_now(), payload)

    return dict(payload) if payload else None


def clear_cache() -> None:
    global _last_request_ts, _inflight
    with _lock:
        _cache.clear()
        _fail_until.clear()
        _skip_reason.clear()
        for k in list(_stats.keys()):
            _stats[k] = 0
        _last_request_ts = 0.0
        _inflight = 0


# ---------------------------------------------------------------------------
# Phase 26.18 / Tier 2.3 — parallel pre-fetch
# ---------------------------------------------------------------------------
# Pure-async refactor of the scoring pipeline (the original Tier 2.3 spec)
# would have a huge blast radius; instead we achieve the same throughput
# benefit by pre-fetching the options chains for the active-scan-pool set
# IN PARALLEL via a ThreadPoolExecutor BEFORE the sync scoring loop reads
# them. Both CBOE (httpx.get) and yfinance release the GIL during HTTP
# I/O so the thread pool gets real concurrency.
#
# Side-effect contract: every prefetch result lands in `_cache` under the
# symbol's key with a fresh timestamp, exactly as if it had been fetched
# inline. The scoring loop's subsequent `get_real_options_positioning`
# call hits that cache and skips the HTTP entirely.
#
# Returns: dict[symbol -> 'hit'|'miss'|'error'] for telemetry. Never raises.

_PREFETCH_MAX_WORKERS = int(_os_env('MRD_OPTIONS_PREFETCH_WORKERS', '6'))
_PREFETCH_TIMEOUT_SECONDS = float(_os_env('MRD_OPTIONS_PREFETCH_TIMEOUT', '8'))


def prefetch_options_chains(
    symbols_and_prices: list[tuple[str, float]],
    *,
    max_expirations: int = _DEFAULT_MAX_EXPIRATIONS,
    max_workers: int | None = None,
    timeout_seconds: float | None = None,
) -> dict[str, str]:
    """Pre-warm `_cache` for a batch of symbols in parallel.

    Skips symbols that already have a fresh cache entry (within TTL) or
    are on a fail-cooldown — both of those would no-op inside
    `get_real_options_positioning` anyway, so we save the worker spawn.

    Args:
        symbols_and_prices: ordered (symbol, last_price) pairs.
        max_expirations: forwarded to `get_real_options_positioning`.
        max_workers: thread-pool size; defaults to MRD_OPTIONS_PREFETCH_WORKERS
            env var (default 6).
        timeout_seconds: wall-time cap before bailing — pending workers
            continue in the background but the function returns early.
            Defaults to MRD_OPTIONS_PREFETCH_TIMEOUT (default 8 s).

    Returns:
        A dict {symbol: outcome} where outcome is 'hit', 'miss', 'cached',
        'cooldown', 'error', or 'timeout'. Pure telemetry — callers can
        ignore.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    workers = max(1, int(max_workers if max_workers is not None else _PREFETCH_MAX_WORKERS))
    deadline_s = float(timeout_seconds if timeout_seconds is not None else _PREFETCH_TIMEOUT_SECONDS)

    # Filter the work set to symbols that actually need a fetch.
    todo: list[tuple[str, float]] = []
    outcomes: dict[str, str] = {}
    now = _now()
    with _lock:
        for sym, px in symbols_and_prices:
            sym_u = (sym or '').upper()
            if not sym_u or not px or px <= 0:
                continue
            # Already cached AND fresh -> noop.
            cached = _cache.get(sym_u)
            if cached and (now - cached[0] <= _TTL_SECONDS):
                outcomes[sym_u] = 'cached'
                continue
            # Symbol-level cooldown still active -> noop.
            cd = _fail_until.get(sym_u, 0)
            if cd and now < cd:
                outcomes[sym_u] = 'cooldown'
                continue
            todo.append((sym_u, float(px)))
    if not todo:
        return outcomes

    def _fetch_one(sym_px):
        sym_u, px = sym_px
        try:
            payload = get_real_options_positioning(sym_u, px, max_expirations=max_expirations)
            return sym_u, ('hit' if payload else 'miss')
        except Exception as exc:  # noqa: BLE001
            log.debug('prefetch_options_chains worker raised for %s: %s', sym_u, exc)
            return sym_u, 'error'

    # Phase 26.32: manual pool + shutdown(wait=False) so a hung options
    # fetch can't block the entire prefetch past the as_completed deadline.
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='opt-prefetch')
    try:
        futures = {pool.submit(_fetch_one, sp): sp[0] for sp in todo}
        try:
            for fut in as_completed(futures, timeout=deadline_s):
                sym_u, outcome = fut.result()
                outcomes[sym_u] = outcome
        except Exception:  # noqa: BLE001 - includes concurrent.futures.TimeoutError
            for sp_sym in futures.values():
                outcomes.setdefault(sp_sym, 'timeout')
    finally:
        pool.shutdown(wait=False)
    return outcomes
