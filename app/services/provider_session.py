"""Shared provider session, budget, and priority-aware admission controls."""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager

from app.config import settings
from app.utils.time import utcnow_iso

_lock = threading.RLock()
_thread_context = threading.local()
_provider = {
    "provider_name": "yfinance",
    "session_ready": False,
    "crumb_present": False,
    "degraded": False,
    "failure_count": 0,
    "last_error": None,
    "last_warm_utc": None,
    "throttle_state": "normal",
    "minute_window_started": 0.0,
    "minute_request_count": 0,
    "tier3_request_count": 0,
    "last_request_ts": 0.0,
    "generation": 0,
    "generations": {"yfinance": 0},
    "circuit_open_until": 0.0,
    "backpressure_rejections": 0,
}


def provider_cache_identity(provider_id: str | None = None) -> str:
    """Return a generation-aware identity for cache provenance."""
    with _lock:
        provider = (provider_id or _provider["provider_name"]).strip().lower()
        generation = _provider.setdefault("generations", {}).get(provider, 0)
        return f"{provider}@{generation}"


def _invalidate_provider_artifacts(provider_id: str) -> None:
    try:
        from app.services.memory_store import memory_store
        memory_store.invalidate_provider(provider_id)
    except Exception:
        pass
    # RAM identities are not the only hot layer.  These invalidations are
    # rare (session/credential changes), so correctness wins over retaining
    # an ambiguous resolved-provider payload.
    try:
        from app.services.quote_cache import invalidate_provider_quotes
        invalidate_provider_quotes(provider_id)
    except Exception:
        pass
    try:
        from app.services.options_chain_service import invalidate_cached_chains
        invalidate_cached_chains()
    except Exception:
        pass


def warm_provider_session() -> dict:
    with _lock:
        _provider["session_ready"] = True
        _provider["crumb_present"] = True
        _provider["last_warm_utc"] = utcnow_iso()
        return dict(_provider)


def invalidate_provider_session(
    error: str | None = None,
    *,
    provider_id: str | None = None,
) -> None:
    """Reset a session and invalidate cache entries tied to its old identity."""
    with _lock:
        provider = (provider_id or _provider["provider_name"]).strip().lower()
        generations = _provider.setdefault("generations", {})
        generations[provider] = generations.get(provider, 0) + 1
        if provider == _provider["provider_name"].lower():
            _provider["generation"] = generations[provider]
        _provider["session_ready"] = False
        _provider["crumb_present"] = False
        if error:
            _provider["last_error"] = str(error)
        _provider["throttle_state"] = "refreshing-session"
    _invalidate_provider_artifacts(provider)


def mark_provider_failure(error: str) -> None:
    message = str(error)
    lowered = message.lower()
    if any(token in lowered for token in (
        "429", "too many requests", "rate limit", "timeout", "timed out", "connection",
    )):
        # Preserve the legacy transient-backoff behavior.  A network wobble
        # should not open the circuit intended for repeated hard failures.
        with _lock:
            _provider["last_error"] = message
            _provider["throttle_state"] = "backoff"
        return
    with _lock:
        _provider["last_error"] = message
        _provider["throttle_state"] = "backoff"
        _provider["failure_count"] += 1
        if _provider["failure_count"] >= settings.provider_soft_fail_threshold:
            _provider["degraded"] = True
            _provider["circuit_open_until"] = time.time() + settings.provider_circuit_breaker_seconds
            _provider["throttle_state"] = "circuit-open"
    if "invalid crumb" in lowered or "unauthorized" in lowered:
        invalidate_provider_session(message)


def clear_provider_failure() -> None:
    with _lock:
        _provider["failure_count"] = 0
        _provider["last_error"] = None
        _provider["degraded"] = False
        _provider["throttle_state"] = "normal"
        _provider["circuit_open_until"] = 0.0
        _provider["session_ready"] = True
        _provider["crumb_present"] = True
        _provider["last_warm_utc"] = utcnow_iso()


@contextmanager
def provider_priority(priority: int):
    """Apply scanner tier priority to nested legacy budget calls."""
    previous = getattr(_thread_context, "priority", None)
    _thread_context.priority = int(priority)
    try:
        yield
    finally:
        _thread_context.priority = previous


def provider_budget_allowance(
    cost: int = 1,
    *,
    priority: int | None = None,
    wait_timeout_seconds: float | None = None,
) -> bool:
    """Admit bounded provider work while reserving capacity for Tier 3.

    Calls remain non-blocking by default.  A small bounded wait is available
    for foreground work that chooses it; low-priority work is rejected during
    cache emergencies rather than growing an unbounded queue.
    """
    cost = max(1, int(cost))
    priority = getattr(_thread_context, "priority", 2) if priority is None else int(priority)
    deadline = (
        None if wait_timeout_seconds is None
        else time.monotonic() + max(0.0, float(wait_timeout_seconds))
    )
    while True:
        now = time.time()
        with _lock:
            if now - _provider["minute_window_started"] >= 60:
                _provider["minute_window_started"] = now
                _provider["minute_request_count"] = 0
                _provider["tier3_request_count"] = 0
                if _provider["throttle_state"] in ("budget-exhausted", "circuit-open"):
                    _provider["throttle_state"] = "normal"
                    _provider["degraded"] = False
            if _provider["circuit_open_until"] > now:
                _provider["throttle_state"] = "circuit-open"
                _provider["backpressure_rejections"] += 1
                return False
            if priority >= 3:
                try:
                    from app.services.tier_cache_policy import low_priority_prefetch_halted
                    if low_priority_prefetch_halted():
                        _provider["throttle_state"] = "memory-emergency"
                        _provider["backpressure_rejections"] += 1
                        return False
                except Exception:
                    pass
            reserve = max(0, settings.provider_tier3_min_requests_per_minute - _provider["tier3_request_count"])
            limit = settings.provider_budget_per_minute
            allowed = _provider["minute_request_count"] + cost <= limit
            if priority < 3:
                allowed = allowed and _provider["minute_request_count"] + cost <= max(0, limit - reserve)
            if allowed:
                gap = settings.provider_min_request_gap_ms / 1000.0 - (now - _provider["last_request_ts"])
                if gap <= 0:
                    _provider["minute_request_count"] += cost
                    if priority >= 3:
                        _provider["tier3_request_count"] += cost
                    _provider["last_request_ts"] = time.time()
                    if _provider["failure_count"] == 0:
                        _provider["degraded"] = False
                        _provider["throttle_state"] = "normal"
                    return True
            if not allowed:
                _provider["throttle_state"] = "budget-exhausted"
                _provider["last_error"] = "provider admission backpressure"
                _provider["backpressure_rejections"] += 1
                return False
            if deadline is not None and time.monotonic() >= deadline:
                _provider["throttle_state"] = "backoff"
                _provider["last_error"] = "provider admission backpressure"
                _provider["backpressure_rejections"] += 1
                return False
        if deadline is None:
            # Existing callers expect the configured inter-request gap to be
            # honored rather than a transient false negative.
            time.sleep(max(0.001, gap))
        else:
            time.sleep(min(0.05, max(0.001, deadline - time.monotonic())))


def provider_health_snapshot() -> dict:
    with _lock:
        should_warm = not _provider["session_ready"]
    if should_warm:
        warm_provider_session()
    with _lock:
        snapshot = dict(_provider)
        snapshot["cache_identity"] = provider_cache_identity(snapshot["provider_name"])
        if snapshot.get("throttle_state") == "normal" and int(snapshot.get("failure_count", 0) or 0) == 0:
            snapshot["degraded"] = False
        return snapshot
