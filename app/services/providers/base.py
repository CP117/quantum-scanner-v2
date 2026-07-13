"""
Base types and the multi-provider chain runner.
"""
from __future__ import annotations

import logging
import re
import time
from collections import defaultdict
from threading import Lock
from typing import Any, Callable, Iterable

from app.utils.time import utcnow_iso

log = logging.getLogger('app.providers')


# ---------------------------------------------------------------------------
# Per-provider runtime stats (surfaced via /system/status.provider_stats
# and /api/providers/status). Phase 26 polish: also tracks last-success /
# last-error timestamps + explicit timeout & 429 (rate-limit) counters so
# the Providers Health page can show actionable telemetry.
#
# Phase 26.4 polish: generic per-provider circuit-breaker telemetry. Every
# call to record_hit() resets the consecutive_failures counter and closes
# the circuit; every call to record_error/timeout/rate_limit increments it
# and, once the configured threshold is hit, "trips" the circuit. The
# circuit-breaker state is informational by default (does not auto-skip
# providers) so existing chain behavior is unchanged - the UI uses it to
# show the operator which providers are repeatedly failing.
# ---------------------------------------------------------------------------

# Phase 26.16 / Tier 1.5: per-provider locks instead of one global Lock.
# The previous design serialized every provider call across the process —
# 30+ providers + concurrent scan/options/regulatory threads all contending
# on a single `threading.Lock()`. Replacing it with a fine-grained dict of
# per-provider locks lets providers' counters update independently while
# still being thread-safe for the same provider.
#
# `_provider_locks` itself is guarded by `_locks_table_lock` only for the
# rare get-or-create path; all hot reads acquire the provider-specific
# lock directly via `_lock_for(name)`.

_locks_table_lock = Lock()
_provider_locks: dict[str, Lock] = {}


def _lock_for(name: str) -> Lock:
    """Return (and lazily create) the per-provider lock.

    The get-or-create path is taken once per provider per process; after
    that it's a plain dict.get under the table lock.  We use the table
    lock only for the creation race; the returned Lock is then used
    directly without needing to re-acquire the table lock.
    """
    lk = _provider_locks.get(name)
    if lk is not None:
        return lk
    with _locks_table_lock:
        lk = _provider_locks.get(name)
        if lk is None:
            lk = Lock()
            _provider_locks[name] = lk
        return lk


# Legacy alias retained for any external module that imported the old
# `_stats_lock` symbol directly. Locking through this name now grabs the
# table lock only, which is sufficient for callers that just want a
# coarse-grained "freeze all stats" snapshot. Internal call sites have
# been migrated to `_lock_for(name)`.
_stats_lock = _locks_table_lock

# Default circuit-breaker thresholds. Providers can override via
# `configure_circuit(name, threshold=..., cooldown_seconds=...)`.
_CB_DEFAULT_THRESHOLD = 5     # consecutive failures before tripping
_CB_DEFAULT_COOLDOWN = 60.0   # seconds the circuit stays open before auto-closing

# Phase 26.9: exponential-backoff ladder used when the breaker keeps re-tripping
# in succession. Index by `circuit_trip_count` (capped at len-1) so a chronically
# broken provider backs off to a multi-hour cadence instead of hammering the
# host every minute.
_CB_COOLDOWN_LADDER_SECONDS = [
    60.0,           # 1st trip: 1 min   (matches legacy default)
    5 * 60.0,       # 2nd trip: 5 min
    15 * 60.0,      # 3rd trip: 15 min
    60 * 60.0,      # 4th trip: 1 hr
    6 * 60 * 60.0,  # 5th+ trips: 6 hr cap
]


def _cooldown_for_trip(trip_count: int, base_cooldown: float) -> float:
    """Return the cooldown seconds for the n-th trip. The first trip uses the
    provider's configured `cb_cooldown_seconds`; subsequent trips climb the
    exponential ladder so persistent failures back off correctly.
    """
    n = max(1, int(trip_count))
    if n == 1:
        return float(base_cooldown)
    idx = min(n - 1, len(_CB_COOLDOWN_LADDER_SECONDS) - 1)
    # Use max(base, ladder) so a provider configured with an aggressive
    # base cooldown still observes the exponential growth on re-trips.
    return float(max(base_cooldown, _CB_COOLDOWN_LADDER_SECONDS[idx]))


def _new_stat_row() -> dict[str, Any]:
    return {
        'calls': 0,
        'hits': 0,
        'misses': 0,
        'errors': 0,
        'timeouts': 0,
        'rate_limits': 0,
        'last_error': None,
        'last_error_utc': None,
        'last_success_utc': None,
        'last_call_utc': None,
        # Circuit-breaker telemetry.
        # circuit_state transitions: closed -> open -> half_open -> closed (on hit)
        #                                              \-> open (on any failure during half_open)
        'consecutive_failures': 0,
        'circuit_state': 'closed',
        'circuit_open_until_mono': 0.0,
        'circuit_open_until_utc': None,
        'circuit_trip_count': 0,
        'last_trip_utc': None,
        'cb_threshold': _CB_DEFAULT_THRESHOLD,
        'cb_cooldown_seconds': _CB_DEFAULT_COOLDOWN,
        # Phase 26.9: tracks the actual seconds the breaker is currently open
        # for (after applying the exponential ladder) so the UI can display
        # accurate "Cooldown" / "Remaining" cells.
        'cb_current_cooldown_seconds': _CB_DEFAULT_COOLDOWN,
    }


_stats: dict[str, dict[str, Any]] = defaultdict(_new_stat_row)


def configure_circuit(name: str, threshold: int | None = None, cooldown_seconds: float | None = None) -> None:
    """Override the circuit-breaker tunables for one provider.

    Call this near where the provider is first registered (e.g. on import)
    so the first failure cluster uses the per-provider tunables rather than
    the module defaults. Safe to call multiple times.
    """
    with _lock_for(name):
        row = _stats[name]
        if threshold is not None and threshold > 0:
            row['cb_threshold'] = int(threshold)
        if cooldown_seconds is not None and cooldown_seconds > 0:
            row['cb_cooldown_seconds'] = float(cooldown_seconds)


def _reset_circuit_if_cooled(row: dict[str, Any]) -> None:
    """Auto-transition the circuit from `open` to `half_open` when its cooldown
    has elapsed. The half-open state lets the next call probe the provider:
    a single failure immediately re-trips (with exponential backoff), while a
    success snaps the state back to `closed`. Must be called while holding
    _stats_lock.
    """
    if row.get('circuit_state') == 'open':
        opens_until = row.get('circuit_open_until_mono', 0.0)
        if opens_until and time.monotonic() >= opens_until:
            row['circuit_state'] = 'half_open'
            row['circuit_open_until_mono'] = 0.0
            row['circuit_open_until_utc'] = None
            # Reset the consecutive-failure counter so the trip check below
            # is driven by the half-open rule, not by stale failures from
            # the pre-cooldown burst.
            row['consecutive_failures'] = 0


def _maybe_trip_circuit(row: dict[str, Any]) -> bool:
    """Trip the circuit when either:
      - the consecutive_failures counter has hit the threshold, OR
      - the breaker is currently in `half_open` (single probe-failure).

    Trips use an exponential cooldown ladder based on `circuit_trip_count` so
    a chronically broken provider backs off to a multi-hour cadence instead of
    flapping every minute. Must be called while holding _stats_lock.
    """
    state = row.get('circuit_state', 'closed')
    if state == 'open':
        return False
    threshold = int(row.get('cb_threshold') or _CB_DEFAULT_THRESHOLD)
    base_cooldown = float(row.get('cb_cooldown_seconds') or _CB_DEFAULT_COOLDOWN)
    consec = int(row.get('consecutive_failures', 0) or 0)
    should_trip = (
        state == 'half_open'                    # single probe-failure trips immediately
        or consec >= threshold
    )
    if not should_trip:
        return False
    # Bump the trip counter BEFORE looking up the cooldown so the ladder
    # advances on each successive re-trip.
    trip_count = int(row.get('circuit_trip_count') or 0) + 1
    cooldown = _cooldown_for_trip(trip_count, base_cooldown)
    now_mono = time.monotonic()
    row['circuit_state'] = 'open'
    row['circuit_open_until_mono'] = now_mono + cooldown
    row['circuit_open_until_utc'] = utcnow_iso()
    row['circuit_trip_count'] = trip_count
    row['last_trip_utc'] = utcnow_iso()
    row['cb_current_cooldown_seconds'] = cooldown
    return True


def record_call(name: str) -> None:
    with _lock_for(name):
        row = _stats[name]
        _reset_circuit_if_cooled(row)
        row['calls'] += 1
        row['last_call_utc'] = utcnow_iso()


def record_hit(name: str) -> None:
    with _lock_for(name):
        row = _stats[name]
        row['hits'] += 1
        row['last_success_utc'] = utcnow_iso()
        # Successful response = circuit fully recovers. Half-open probes
        # that succeed get promoted back to `closed`.
        row['consecutive_failures'] = 0
        if row.get('circuit_state') in ('open', 'half_open'):
            row['circuit_state'] = 'closed'
            row['circuit_open_until_mono'] = 0.0
            row['circuit_open_until_utc'] = None


def record_miss(name: str) -> None:
    """Empty-result return - NOT a failure for circuit-breaker purposes."""
    with _lock_for(name):
        _stats[name]['misses'] += 1


_TIMEOUT_HINTS = re.compile(r'\b(timeout|timed out|read timeout|connect timeout)\b', re.IGNORECASE)
_RATELIMIT_HINTS = re.compile(r'\b(429|rate[- ]?limit|too many requests|throttl|quota)\b', re.IGNORECASE)


def _classify_error(err: str | None) -> tuple[bool, bool]:
    """Heuristic: (is_timeout, is_rate_limited) from the error string.

    Used as a *fallback* when providers don't classify the failure themselves
    via record_timeout/record_rate_limit. Providers that catch typed
    exceptions internally should prefer the explicit helpers below so the
    counters are exact rather than pattern-matched.
    """
    if not err:
        return False, False
    s = str(err)
    is_rl = bool(_RATELIMIT_HINTS.search(s))
    is_to = (not is_rl) and bool(_TIMEOUT_HINTS.search(s))
    return is_to, is_rl


def _bump_failure(row: dict[str, Any], err: str | None) -> None:
    """Common failure-bookkeeping. Must be called while holding _stats_lock."""
    row['errors'] += 1
    row['consecutive_failures'] = int(row.get('consecutive_failures') or 0) + 1
    if err:
        row['last_error'] = str(err)[:200]
        row['last_error_utc'] = utcnow_iso()
    if _maybe_trip_circuit(row):
        log.warning(
            'circuit breaker tripped for provider %s after %d consecutive failures '
            '(cooldown=%.0fs)',
            row.get('_name', '<unknown>'),
            row['consecutive_failures'],
            float(row.get('cb_cooldown_seconds') or _CB_DEFAULT_COOLDOWN),
        )


def record_error(name: str, err: str | None = None) -> None:
    with _lock_for(name):
        row = _stats[name]
        row['_name'] = name  # so log lines can identify the provider
        _bump_failure(row, err)
        if err:
            is_to, is_rl = _classify_error(err)
            if is_to:
                row['timeouts'] += 1
            if is_rl:
                row['rate_limits'] += 1


def record_timeout(name: str, err: str | None = None) -> None:
    """Explicit timeout-only counter + CB-failure bump.

    Use at *chain* / *batch* level (one call per batch failure). Providers
    that catch per-symbol timeouts internally should use `count_timeout`
    instead so a single 25-symbol batch doesn't trip the breaker on its
    own.
    """
    with _lock_for(name):
        row = _stats[name]
        row['_name'] = name
        row['timeouts'] += 1
        _bump_failure(row, err)


def record_rate_limit(name: str, err: str | None = None) -> None:
    """Explicit 429/quota counter + CB-failure bump.

    Use at *chain* / *batch* level (one call per batch failure). Providers
    that catch per-symbol 429s internally should use `count_rate_limit`
    instead so a single batch doesn't trip the breaker on its own.
    """
    with _lock_for(name):
        row = _stats[name]
        row['_name'] = name
        row['rate_limits'] += 1
        _bump_failure(row, err)


def count_timeout(name: str, err: str | None = None) -> None:
    """Per-symbol timeout counter bump WITHOUT triggering the circuit breaker.

    Use inside per-symbol fetch loops so the operator can see exactly how
    many symbols hit a timeout, without one batch of 25 hits prematurely
    tripping the breaker. The chain runner / batch-level handler is still
    responsible for the CB-bumping `record_*` variants.
    """
    with _lock_for(name):
        row = _stats[name]
        row['_name'] = name
        row['timeouts'] += 1
        if err:
            row['last_error'] = str(err)[:200]
            row['last_error_utc'] = utcnow_iso()


def count_rate_limit(name: str, err: str | None = None) -> None:
    """Per-symbol 429 counter bump WITHOUT triggering the circuit breaker."""
    with _lock_for(name):
        row = _stats[name]
        row['_name'] = name
        row['rate_limits'] += 1
        if err:
            row['last_error'] = str(err)[:200]
            row['last_error_utc'] = utcnow_iso()


def count_error(name: str, err: str | None = None) -> None:
    """Per-symbol generic error counter bump WITHOUT triggering the CB.

    Bumps both `errors` and the typed timeouts/rate_limits counters when
    the error string matches the relevant heuristic, but skips the
    consecutive-failure bookkeeping so a noisy batch can't trip the CB.
    """
    with _lock_for(name):
        row = _stats[name]
        row['_name'] = name
        row['errors'] += 1
        if err:
            row['last_error'] = str(err)[:200]
            row['last_error_utc'] = utcnow_iso()
            is_to, is_rl = _classify_error(err)
            if is_to:
                row['timeouts'] += 1
            if is_rl:
                row['rate_limits'] += 1


def circuit_should_skip(name: str) -> bool:
    """Read-only helper for providers that want to opt into the generic CB.

    Returns True if the circuit is currently open and the cooldown hasn't
    elapsed. Most providers do NOT need to consult this - the CB is purely
    informational by default. Stooq has its own dedicated CB.
    """
    with _lock_for(name):
        row = _stats.get(name)
        if not row:
            return False
        _reset_circuit_if_cooled(row)
        return row.get('circuit_state') == 'open'


def provider_stats_snapshot() -> dict[str, dict]:
    """Snapshot of all per-provider stats. Auto-closes any circuits whose
    cooldown has elapsed so callers see fresh state.

    Phase 26.16 / Tier 1.5: walks the per-provider lock map under the
    table lock to take a stable snapshot of provider names, then briefly
    acquires each provider's lock while copying its stats row. This keeps
    individual provider call sites contending on their own locks rather
    than blocking on a single global one.
    """
    out: dict[str, dict] = {}
    now_mono = time.monotonic()
    # Snapshot the list of names while holding the table lock so we don't
    # race with concurrent _lock_for() calls that create new entries.
    with _locks_table_lock:
        names = list(_stats.keys())
    for k in names:
        lk = _lock_for(k)
        with lk:
            v = _stats.get(k)
            if v is None:
                continue
            _reset_circuit_if_cooled(v)
            row = {kk: vv for kk, vv in v.items() if kk != '_name'}
            if row.get('circuit_state') == 'open' and row.get('circuit_open_until_mono'):
                row['circuit_remaining_seconds'] = max(
                    0.0, float(row['circuit_open_until_mono']) - now_mono
                )
            else:
                row['circuit_remaining_seconds'] = 0.0
            row.pop('circuit_open_until_mono', None)
            out[k] = row
    return out


def reset_provider_stats() -> None:
    """Wipe every provider's stats row.

    Phase 26.16 / Tier 1.5: acquires the table lock to atomically clear
    the global `_stats` dict.  Per-provider lock entries are intentionally
    NOT cleared — they're re-used by `_lock_for(name)` on the next call,
    which lazily re-creates the corresponding `_stats` row.
    """
    with _locks_table_lock:
        _stats.clear()


# ---------------------------------------------------------------------------
# Quote-chain orchestration
# ---------------------------------------------------------------------------

QuoteFetcher = Callable[[list[str], str], dict[str, dict]]


def run_quote_chain(
    symbols: list[str],
    market: str,
    fetchers: Iterable[tuple[str, QuoteFetcher]],
) -> dict[str, dict]:
    """Run providers in cascade.  Each provider only attempts the symbols
    that previous providers have not yet successfully resolved.  Provenance
    is stamped per row via the provider-specific `source` field.
    """
    resolved: dict[str, dict] = {}
    pending = list(symbols)
    for name, fetcher in fetchers:
        if not pending:
            break
        record_call(name)
        try:
            payload = fetcher(pending, market) or {}
        except Exception as exc:  # noqa: BLE001
            record_error(name, str(exc))
            log.warning('provider %s raised: %s', name, exc)
            continue
        # Provider may return an empty dict or a partial dict.
        accepted = 0
        for sym, row in payload.items():
            if not row or not isinstance(row, dict):
                continue
            if row.get('last_price') in (None, 0, 0.0):
                continue
            row.setdefault('source', name)
            row.setdefault('provider_outcome', 'live_success')
            resolved[sym] = row
            accepted += 1
        if accepted:
            record_hit(name)
        else:
            record_miss(name)
        pending = [s for s in pending if s not in resolved]
    return resolved
