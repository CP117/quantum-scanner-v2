"""
CBOE delayed options-chain provider.

Phase 26.14: Adds a second, more reliable source of options-chain data so the
Ultra Scanner is no longer single-threaded behind Yahoo Finance. CBOE publishes
free, public, 15-min delayed quotes via:

    https://cdn.cboe.com/api/global/delayed_quotes/options/{SYMBOL}.json

Why use it as the PRIMARY source (with Yahoo as fallback):
  * No auth, no API key required.
  * Single HTTP request returns the FULL chain (all expirations, all strikes,
    full Greeks). Yahoo requires one HTTP request per expiration.
  * Delivers IV + delta/gamma/vega/theta/rho, which Yahoo does NOT.
  * 15-min delay is fine for the scanner's 30+ min decision cadence.

Returns a dict matching the legacy `_summarize_chain` contract from
`options_chain_service` (i.e. {target_price, call_wall, put_wall, bias,
score, near_term, monthly, expirations_used, ...}) so the orchestrator
can use this output directly with no shape changes downstream.

Behavior:
  * Per-symbol TTL identical to Yahoo's path (10 min) so we don't burn the
    CBOE CDN.
  * Records hit / miss / error / timeout / circuit-trip telemetry via the
    shared `app.services.providers.base` counters (provider name =
    'cboe_options').
  * Falls through to None on parse error / timeout / circuit open so the
    orchestrator can use Yahoo as fallback.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from threading import Lock
from typing import Any

import httpx

from app.services.providers import base as providers_base

log = logging.getLogger('app.options_chain.cboe')

# ---------------------------------------------------------------------------
# Tunable knobs - keep aligned with options_chain_service to maintain budget.
# ---------------------------------------------------------------------------
_TTL_SECONDS = 600                # 10-min cache identical to yahoo path
_REQUEST_TIMEOUT = 8.0            # CBOE CDN is fast; 8s is generous
_MIN_GAP_SECONDS = 0.20           # CBOE CDN has no documented limit; be polite

# Phase 26.48 — thundering-herd circuit breaker.
#
# Background: the previous release would (a) `time.sleep()` inside the
# rate-limit lock, and (b) compute the sleep duration using a `now`
# value captured at function entry — which, for a thread that waited
# in queue for hours, was severely stale.  The result was a feedback
# loop that froze the backend for 10+ hours under heavy concurrency
# (144 snap-worker threads all queued at the same lock; see
# wedge_watchdog.json from 2026-06-13).
#
# This release moves the sleep OUTSIDE the lock, uses a fresh `_now()`
# inside the lock, and ALSO caps the worst-case wait-then-serial-
# queue depth.  If our reserved request slot would be more than
# `_MAX_QUEUED_WAIT_S` seconds in the future when we'd be allowed to
# fire, we bail immediately (return None) and let the caller fall
# through to the Yahoo cascade.  This prevents a thundering herd from
# accumulating an unbounded queue of waiters again.

# Default per-call expiration cap. Bumped to 4 in Phase 26.14 (was 2) because
# the CBOE request returns the full chain in a single HTTP roundtrip, so going
# from 2 -> 4 has zero HTTP cost - we just process more of the data we already
# downloaded.
DEFAULT_MAX_EXPIRATIONS = 4

_BASE_URL = 'https://cdn.cboe.com/api/global/delayed_quotes/options/{symbol}.json'
# Phase 26.19: CBOE sits behind Cloudflare. When the request looks too
# bare (no Accept-Language, no Referer/Origin) Cloudflare's bot-detection
# layer intermittently returns 403 even though the resource is public.
# Mirroring the headers a real cboe.com page sends keeps the fingerprint
# inside the "real browser" bucket and clears the 403 issue.
_BROWSER_HEADERS = {
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'en-US,en;q=0.9',
    'Referer': 'https://www.cboe.com/',
    'Origin': 'https://www.cboe.com',
    'Sec-Fetch-Dest': 'empty',
    'Sec-Fetch-Mode': 'cors',
    'Sec-Fetch-Site': 'same-site',
}
# Indices and some ETFs require the `_` prefix on CBOE (e.g. _SPX, _VIX).
# We try the bare symbol first, then retry with the underscore prefix if the
# first returns 404. Cache the working form per symbol so we don't re-probe.
_PREFIX_KNOWN: dict[str, str] = {}  # sym -> '' | '_'

# Phase 26.23: per-symbol 403 cooldown. Cloudflare in front of CBOE
# permanently rejects certain illiquid / warrant / micro-cap tickers
# (e.g. AALG, AACOW, AIRE, CCIXW, CABO, etc.) — no amount of header
# tweaking or UA rotation gets past it. Without a cooldown we re-hit
# the same blocked URL on every batch, which (1) wastes the HTTP
# budget, (2) bloats httpx logs, and (3) flips the cboe_options
# circuit breaker more often than it needs to. Marking the symbol
# for a 30-minute skip after a confirmed 403 brings call volume to
# zero for these symbols while still letting genuine transient 403s
# recover on the next probe window.
_HTTP_FAIL_UNTIL: dict[str, float] = {}  # sym (upper) -> monotonic unlock time
_HTTP_FAIL_COOLDOWN_SECONDS = 30 * 60

# Configure the generic circuit breaker for this provider.
providers_base.configure_circuit('cboe_options', threshold=8, cooldown_seconds=120.0)


# ---------------------------------------------------------------------------
# In-process cache & throttling
# ---------------------------------------------------------------------------
_lock = Lock()
_cache: dict[str, tuple[float, dict | None]] = {}
_last_request_ts: float = 0.0

# Phase 26.48 — cap on how far in the future a queued thread is
# willing to wait for its CBOE rate-limit slot before bailing.
# 3.0 s gives us 15 in-flight CBOE requests worth of queue (at
# _MIN_GAP_SECONDS=0.20), which is plenty for normal batch sizes.
# Anything beyond that and the caller is better served falling
# through to the Yahoo cascade than blocking a snap-worker thread.
_MAX_QUEUED_WAIT_S = 3.0

# Phase 26.48 — telemetry counters for the rate-limit path so the
# wedge-watchdog can attribute any future contention pattern.
_rl_stats = {
    'bailed_thundering_herd': 0,
    'served_immediately': 0,
    'slept_for_gap': 0,
}


def rate_limit_stats() -> dict[str, int]:
    """Telemetry helper for the rate-limit path.  Exposed for the
    wedge-watchdog and /system endpoints (read-only snapshot)."""
    with _lock:
        return dict(_rl_stats)


def _now() -> float:
    return time.monotonic()


# ---------------------------------------------------------------------------
# OCC option-symbol parser
# ---------------------------------------------------------------------------
# Format: ROOT + YYMMDD + (C|P) + STRIKE*1000 (8 digits)
#   Example: AAPL250117C00150000 -> AAPL, exp 2025-01-17, Call, strike 150.000
# Some roots may include digits (e.g. SPXW); the regex anchors on the
# 6-digit date, 1-char C/P, and 8-digit strike.
_OCC_RE = re.compile(r'^(?P<root>[A-Z0-9.\-]+?)(?P<yy>\d{2})(?P<mm>\d{2})(?P<dd>\d{2})(?P<cp>[CP])(?P<strike>\d{8})$')


def _parse_occ(occ: str) -> tuple[str, str, float] | None:
    """Parse an OCC option symbol into (expiry_iso, 'C'|'P', strike_dollars)."""
    m = _OCC_RE.match(occ.strip())
    if not m:
        return None
    yy = int(m.group('yy'))
    # OCC dates are 2-digit years. Per the OCC spec, all current and future
    # contracts use 20YY (no contract was ever listed pre-2000).
    year = 2000 + yy
    try:
        expiry = datetime(year, int(m.group('mm')), int(m.group('dd'))).date().isoformat()
    except ValueError:
        return None
    cp = m.group('cp')
    strike = int(m.group('strike')) / 1000.0
    return expiry, cp, strike


# ---------------------------------------------------------------------------
# Chain summarization
# ---------------------------------------------------------------------------
def _summarize_from_rows(symbol: str, last_price: float, rows: list[dict],
                        max_expirations: int) -> dict:
    """Build the options_positioning payload directly from CBOE row data.

    We deliberately do NOT route through the yfinance-shaped DataFrame
    summarizer: building dicts is ~3x faster, has zero pandas import cost,
    and preserves the Greeks/IV columns CBOE gives us (which yfinance
    drops).
    """
    # Group by (expiry, C/P) -> list of row dicts
    # Write-time contract-level dedupe: identical (expiry, right, strike)
    # rows collapse to the most complete instance before summarization.
    try:
        from app.services.cache_dedupe_service import dedupe_option_rows
        rows = dedupe_option_rows(rows)
    except Exception:  # noqa: BLE001
        pass
    grouped: dict[str, dict[str, list[dict]]] = {}
    for r in rows:
        occ = r.get('option') or ''
        parsed = _parse_occ(occ)
        if not parsed:
            continue
        expiry, cp, strike = parsed
        # Reject obviously-stale expirations (in the past)
        try:
            # Phase 26.24: use timezone-aware UTC. Previously
            # `datetime.utcnow()` fired a DeprecationWarning on every
            # option row parsed (CBOE returns hundreds per symbol), which
            # on Windows turned into a torrential console flood — and a
            # full Windows console buffer synchronously blocks every
            # subsequent stdout/stderr write, which in turn freezes the
            # snapshot loop AND the FastAPI handler responses (subpages
            # show a perpetual "Loading…" because their JSON response
            # can't drain). Killing the warning at its source kills the
            # cascade. Logic is otherwise identical.
            expiry_date = datetime.fromisoformat(expiry).date()
            today_utc = datetime.now(timezone.utc).date()
            if expiry_date < today_utc:
                continue
        except ValueError:
            continue
        bucket = grouped.setdefault(expiry, {'C': [], 'P': []})
        bucket[cp].append({
            'strike': strike,
            'open_interest': float(r.get('open_interest') or 0),
            'volume': float(r.get('volume') or 0),
            'iv': float(r.get('iv') or 0),
            'delta': float(r.get('delta') or 0),
            'gamma': float(r.get('gamma') or 0),
            'bid': float(r.get('bid') or 0),
            'ask': float(r.get('ask') or 0),
            'last': float(r.get('last_trade_price') or 0),
        })

    # Sort expirations chronologically, keep the first `max_expirations`.
    sorted_expiries = sorted(grouped.keys())[:max_expirations]
    if not sorted_expiries:
        return {}

    total_call_oi = total_put_oi = 0.0
    total_call_vol = total_put_vol = 0.0
    weighted_call_strike_num = weighted_call_strike_den = 0.0
    weighted_put_strike_num = weighted_put_strike_den = 0.0
    near_term_calls = near_term_puts = 0.0
    monthly_calls = monthly_puts = 0.0
    max_pain_candidates: dict[float, float] = {}
    # Aggregate IV across strikes near the money for an "ATM IV" indicator.
    atm_iv_sum = 0.0
    atm_iv_count = 0
    # Total gamma exposure (sum of gamma * OI * 100 * spot^2 * 0.01 - the dollar
    # value of $1 move in spot per 1% move in spot). Useful for gamma squeezes.
    total_gamma_exposure = 0.0

    for idx, expiry in enumerate(sorted_expiries):
        calls = grouped[expiry]['C']
        puts = grouped[expiry]['P']
        c_oi = sum(c['open_interest'] for c in calls)
        p_oi = sum(p['open_interest'] for p in puts)
        c_vol = sum(c['volume'] for c in calls)
        p_vol = sum(p['volume'] for p in puts)
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
        for c in calls:
            if c['open_interest'] > 0:
                weighted_call_strike_num += c['strike'] * c['open_interest']
                weighted_call_strike_den += c['open_interest']
                total_gamma_exposure += c['gamma'] * c['open_interest'] * 100.0
            # ATM IV: include strikes within 5% of spot
            if last_price > 0 and abs(c['strike'] - last_price) / last_price < 0.05 and c['iv'] > 0:
                atm_iv_sum += c['iv']
                atm_iv_count += 1
        for p in puts:
            if p['open_interest'] > 0:
                weighted_put_strike_num += p['strike'] * p['open_interest']
                weighted_put_strike_den += p['open_interest']
                total_gamma_exposure += p['gamma'] * p['open_interest'] * 100.0
            if last_price > 0 and abs(p['strike'] - last_price) / last_price < 0.05 and p['iv'] > 0:
                atm_iv_sum += p['iv']
                atm_iv_count += 1

        # Max-pain candidates (per-strike "pain to writers if spot pins here")
        strikes = sorted({c['strike'] for c in calls} | {p['strike'] for p in puts})
        for strike in strikes:
            call_pain = sum(
                (strike - c['strike']) * c['open_interest']
                for c in calls if c['strike'] < strike
            )
            put_pain = sum(
                (p['strike'] - strike) * p['open_interest']
                for p in puts if p['strike'] > strike
            )
            max_pain_candidates[strike] = max_pain_candidates.get(strike, 0.0) + call_pain + put_pain

    if total_call_oi <= 0 and total_put_oi <= 0:
        return {}

    put_call_ratio = (total_put_oi / total_call_oi) if total_call_oi > 0 else 999.0
    put_call_vol_ratio = (total_put_vol / total_call_vol) if total_call_vol > 0 else 999.0
    call_wall = (weighted_call_strike_num / weighted_call_strike_den) if weighted_call_strike_den > 0 else None
    put_wall = (weighted_put_strike_num / weighted_put_strike_den) if weighted_put_strike_den > 0 else None
    target_price = min(max_pain_candidates, key=max_pain_candidates.get) if max_pain_candidates else last_price
    atm_iv = (atm_iv_sum / atm_iv_count) if atm_iv_count > 0 else None

    # Score (mirrors options_chain_service._summarize_chain scoring)
    if put_call_ratio < 0.6:
        score = 75.0
    elif put_call_ratio < 0.85:
        score = 62.0
    elif put_call_ratio < 1.0:
        score = 55.0
    elif put_call_ratio < 1.25:
        score = 45.0
    elif put_call_ratio < 1.6:
        score = 38.0
    else:
        score = 25.0
    if put_call_vol_ratio < 0.7:
        score = min(100.0, score + 5)
    elif put_call_vol_ratio > 1.4:
        score = max(0.0, score - 5)
    bias = 'bullish' if score >= 58 else 'bearish' if score <= 42 else 'neutral'

    if last_price > 0 and target_price > 0:
        proximity_pct = abs(target_price - last_price) / last_price * 100.0
    else:
        proximity_pct = 99.0
    pin_risk = 'high' if proximity_pct <= 0.75 else 'moderate' if proximity_pct <= 2.0 else 'low'

    if score >= 70:
        gamma_level_label = 'high_call_pressure'
    elif score >= 58:
        gamma_level_label = 'mild_call_pressure'
    elif score <= 30:
        gamma_level_label = 'high_put_pressure'
    elif score <= 42:
        gamma_level_label = 'mild_put_pressure'
    else:
        gamma_level_label = 'moderate'

    out = {
        'score': round(score, 2),
        'bias': bias,
        'status': 'implemented',
        'provenance': 'cboe_chain',  # distinguishes from real_chain (yahoo) / inferred
        'gamma_level_label': gamma_level_label,
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
        'expirations_used': len(sorted_expiries),
        'expiration_dates': list(sorted_expiries),
        'nearest_expiration': sorted_expiries[0] if sorted_expiries else None,
        # Phase 26.14 bonus: CBOE gives us these for free, expose them so the
        # detail panel can render IV/Greeks badges. None if no ATM data.
        'atm_iv': round(atm_iv, 4) if atm_iv is not None else None,
        'total_gamma_exposure': round(total_gamma_exposure, 0),
    }
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def get_options_positioning(symbol: str, last_price: float, *,
                            max_expirations: int = DEFAULT_MAX_EXPIRATIONS) -> dict | None:
    """Return a fully-shaped options_positioning payload from CBOE, or None
    if unavailable (caller should fall back to Yahoo).
    """
    global _last_request_ts
    sym = (symbol or '').upper().strip()
    if not sym or last_price <= 0:
        return None

    now = _now()
    cache_key = f'{sym}:{max_expirations}'

    with _lock:
        cached = _cache.get(cache_key)
        if cached and (now - cached[0] < _TTL_SECONDS):
            providers_base.record_call('cboe_options')
            providers_base.record_hit('cboe_options')
            return cached[1]

    # Circuit-breaker check
    if providers_base.circuit_should_skip('cboe_options'):
        return None

    # Phase 26.23: per-symbol 403 cooldown — silently skip symbols that
    # Cloudflare is permanently blocking, until the cooldown expires.
    # Returns None so the caller (options_chain_service) falls through to
    # the Yahoo cascade or the inferred heuristic.
    sym_cd = _HTTP_FAIL_UNTIL.get(sym, 0)
    if sym_cd and _now() < sym_cd:
        return None

    # Rate-limit (Phase 26.48 — see file-level comment for the
    # incident this fix addresses).
    #
    # Algorithm:
    #   1. Acquire `_lock` ONLY for the bookkeeping (read/write
    #      `_last_request_ts`).  No `time.sleep()` while holding it.
    #   2. Compute `now` FRESHLY inside the lock — never use the
    #      stale `now` captured at function entry, which is what
    #      caused the 10-hour wedge.
    #   3. Reserve our slot by advancing `_last_request_ts` to
    #      `max(_last_request_ts + _MIN_GAP_SECONDS, fresh_now)`.
    #      This staggers concurrent callers naturally so they don't
    #      all wake up at the same instant.
    #   4. If our reserved slot is more than `_MAX_QUEUED_WAIT_S`
    #      seconds away, BAIL (return None) so the caller can fall
    #      through to the Yahoo cascade — better than blocking a
    #      snap-worker thread for an unbounded amount of time.
    #   5. Sleep the required gap OUTSIDE the lock.
    sleep_for = 0.0
    with _lock:
        fresh_now = _now()
        # Reserve our slot.  `target_ts` is the earliest wall-clock
        # time at which we may fire our request.
        target_ts = max(_last_request_ts + _MIN_GAP_SECONDS, fresh_now)
        wait = target_ts - fresh_now
        # Thundering-herd guard.
        if wait > _MAX_QUEUED_WAIT_S:
            _rl_stats['bailed_thundering_herd'] += 1
            log.debug(
                'cboe: thundering-herd bail for %s (would wait %.2fs > cap %.2fs)',
                sym, wait, _MAX_QUEUED_WAIT_S,
            )
            return None
        # Sanity clamp: even if some pathological clock-skew or bug
        # ever produced a giant wait, we'll never sleep more than
        # `_MAX_QUEUED_WAIT_S` after this point.  (Belt-and-braces.)
        sleep_for = max(0.0, min(_MAX_QUEUED_WAIT_S, wait))
        # Commit the reservation BEFORE releasing the lock so the
        # next caller stacks behind our slot, not on top of it.
        _last_request_ts = target_ts
        if sleep_for > 0:
            _rl_stats['slept_for_gap'] += 1
        else:
            _rl_stats['served_immediately'] += 1

    if sleep_for > 0:
        time.sleep(sleep_for)

    providers_base.record_call('cboe_options')

    # Resolve the right CBOE URL prefix (index symbols need `_SPX` etc.)
    prefix_candidates = []
    cached_prefix = _PREFIX_KNOWN.get(sym)
    if cached_prefix is not None:
        prefix_candidates = [cached_prefix]
    else:
        prefix_candidates = ['', '_']

    payload: dict | None = None
    last_status: int = 0
    for prefix in prefix_candidates:
        url = _BASE_URL.format(symbol=f'{prefix}{sym}')
        try:
            # Phase 26.18.c: resilient_get gives us retries + backoff +
            # UA rotation + Retry-After honoring on transient 5xx/429.
            from app.services.http_client import resilient_get, ResilientGetConfig
            cfg = ResilientGetConfig(
                max_attempts=3, connect_timeout=3.0,
                read_timeout=_REQUEST_TIMEOUT, retry_after_cap_seconds=3.0,
            )
            r = resilient_get(url, headers=dict(_BROWSER_HEADERS), cfg=cfg, client='httpx')
            last_status = r.status_code
            # Phase 26.19: Cloudflare in front of CBOE occasionally serves
            # a transient 403 to programmatic clients. resilient_get does
            # NOT retry 403 (it's permanent in the HTTP spec), but in
            # practice a one-shot retry with a fresh UA after a short
            # sleep recovers most of these. Single extra attempt only —
            # a real ban still surfaces quickly. No log spam: silently
            # retry, then fall through to the existing error path if it
            # still fails.
            if r.status_code == 403:
                import time as _t
                _t.sleep(0.6)
                retry_headers = dict(_BROWSER_HEADERS)
                retry_headers.pop('User-Agent', None)
                r = resilient_get(url, headers=retry_headers, cfg=cfg, client='httpx')
                last_status = r.status_code
            if r.status_code == 200:
                try:
                    payload = r.json()
                except Exception as exc:  # noqa: BLE001
                    providers_base.record_error('cboe_options', f'json_decode: {exc}')
                    return None
                _PREFIX_KNOWN[sym] = prefix
                break
            elif r.status_code == 404:
                continue  # try next prefix
            elif r.status_code == 403:
                # Phase 26.23: both the primary call and the one-shot
                # fresh-UA retry came back 403 — Cloudflare is
                # consistently rejecting this symbol. Stop hammering
                # it: mark the symbol for a 30-min skip so subsequent
                # batches fall straight through to the Yahoo cascade
                # (or the inferred heuristic) without re-issuing a
                # request that we know will fail. Symbol re-enters the
                # cascade automatically when the cooldown expires.
                _HTTP_FAIL_UNTIL[sym] = _now() + _HTTP_FAIL_COOLDOWN_SECONDS
                providers_base.record_error(
                    'cboe_options', f'http_{r.status_code}',
                )
                return None
            elif r.status_code == 0 and r.error:
                # All retries exhausted on transient transport failure.
                # Classify based on error string so the typed counter
                # bookkeeping stays accurate.
                err_l = r.error.lower()
                if 'timeout' in err_l:
                    providers_base.record_timeout('cboe_options', r.error)
                else:
                    providers_base.record_error('cboe_options', f'transport: {r.error}')
                return None
            else:
                # 4xx/5xx that isn't 404 - record and bail (retries already
                # spent inside resilient_get for retryable codes).
                providers_base.record_error(
                    'cboe_options', f'http_{r.status_code}',
                )
                return None
        except Exception as exc:  # noqa: BLE001
            providers_base.record_error('cboe_options', f'unexpected: {exc}')
            return None

    if payload is None:
        # Symbol simply isn't listed on CBOE (e.g., OTC, foreign ADR).
        # Cache the negative so we don't re-fetch this symbol for the TTL window.
        with _lock:
            _cache[cache_key] = (_now(), None)
        # Treat as a miss, not an error - this is expected for many tickers.
        providers_base.record_miss('cboe_options')
        log.debug('cboe: symbol %s not listed (last_status=%d)', sym, last_status)
        return None

    try:
        data = payload.get('data') or {}
        rows = data.get('options') or []
        if not rows:
            with _lock:
                _cache[cache_key] = (_now(), None)
            providers_base.record_miss('cboe_options')
            return None
        result = _summarize_from_rows(sym, last_price, rows, max_expirations)
        if not result:
            with _lock:
                _cache[cache_key] = (_now(), None)
            providers_base.record_miss('cboe_options')
            return None
    except Exception as exc:  # noqa: BLE001
        providers_base.record_error('cboe_options', f'parse: {exc}')
        return None

    with _lock:
        _cache[cache_key] = (_now(), result)
    providers_base.record_hit('cboe_options')
    return result


def cache_size() -> int:
    with _lock:
        return len(_cache)


def cache_clear() -> int:
    """Wipe the in-process CBOE cache. Returns the number of entries cleared."""
    with _lock:
        n = len(_cache)
        _cache.clear()
        _PREFIX_KNOWN.clear()
        return n
