from __future__ import annotations
import time
from app.config import settings
from app.utils.time import utcnow_iso

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
    "last_request_ts": 0.0,
}


def warm_provider_session() -> dict:
    _provider["session_ready"] = True
    _provider["crumb_present"] = True
    _provider["last_warm_utc"] = utcnow_iso()
    return dict(_provider)


def invalidate_provider_session(error: str | None = None) -> None:
    _provider["session_ready"] = False
    _provider["crumb_present"] = False
    if error:
        _provider["last_error"] = str(error)
    _provider["throttle_state"] = "refreshing-session"


def mark_provider_failure(error: str) -> None:
    message = str(error)
    lowered = message.lower()
    if '429' in lowered or 'too many requests' in lowered or 'rate limit' in lowered or 'timeout' in lowered or 'timed out' in lowered or 'connection' in lowered:
        _provider['last_error'] = message
        _provider['throttle_state'] = 'backoff'
        return
    _provider["failure_count"] += 1
    _provider["last_error"] = message
    _provider["throttle_state"] = "backoff"
    if 'Invalid Crumb' in message or 'Unauthorized' in message:
        invalidate_provider_session(message)
    if _provider["failure_count"] >= settings.provider_soft_fail_threshold:
        _provider["degraded"] = True


def clear_provider_failure() -> None:
    _provider["failure_count"] = 0
    _provider["last_error"] = None
    _provider["degraded"] = False
    _provider["throttle_state"] = "normal"
    _provider["session_ready"] = True
    _provider["crumb_present"] = True
    _provider["last_warm_utc"] = utcnow_iso()


def provider_budget_allowance(cost: int = 1) -> bool:
    now = time.time()
    if now - _provider['minute_window_started'] >= 60:
        _provider['minute_window_started'] = now
        _provider['minute_request_count'] = 0
        if _provider['throttle_state'] == 'budget-exhausted' and _provider['failure_count'] == 0:
            _provider['degraded'] = False
            _provider['throttle_state'] = 'normal'
    if _provider['minute_request_count'] + cost > settings.provider_budget_per_minute:
        _provider['throttle_state'] = 'budget-exhausted'
        _provider['last_error'] = 'provider budget exhausted for current minute window'
        return False
    wait = settings.provider_min_request_gap_ms / 1000.0 - (now - _provider['last_request_ts'])
    if wait > 0:
        time.sleep(wait)
    _provider['minute_request_count'] += cost
    _provider['last_request_ts'] = time.time()
    if _provider['failure_count'] == 0 and _provider['throttle_state'] in ('backoff', 'budget-exhausted'):
        _provider['degraded'] = False
        _provider['throttle_state'] = 'normal'
        if _provider.get('last_error') == 'provider budget exhausted for current minute window':
            _provider['last_error'] = None
    return True


def provider_health_snapshot() -> dict:
    if not _provider["session_ready"]:
        warm_provider_session()
    snapshot = dict(_provider)
    if snapshot.get('throttle_state') == 'normal' and int(snapshot.get('failure_count', 0) or 0) == 0:
        snapshot['degraded'] = False
    return snapshot
