"""
Finnhub provider — high-quality US-stock quote source.

Requires a free API key from https://finnhub.io/dashboard (60 req/min).

The provider acts as a SECONDARY layer in the stock cascade, sitting
between Yahoo and Stooq. When a Finnhub key is configured, the cascade
adds a `finnhub` step right after `yahoo-chart`; without a key the layer
is a no-op and the cascade behaves identically to before.

Response shape (after normalisation):

    {sym: {
        'last_price', 'previous_close', 'open',
        'day_low', 'day_high', 'volume',
        'source': 'finnhub',
    }}

Defensive: never raises. Caches per-symbol rate-limit cooldowns so a
429 storm doesn't repeatedly hammer Finnhub for the same symbol.
"""
from __future__ import annotations

import logging
import time
from threading import Lock
from typing import Iterable

import httpx

from app.services import api_keys
from app.services.providers.base import count_error, count_rate_limit, count_timeout

log = logging.getLogger("app.providers.finnhub")

_BASE_URL = "https://finnhub.io/api/v1"
_TIMEOUT = 4.0
_MIN_GAP = 0.05   # 20 req/sec max — well under the 60/min free-tier limit
_RATE_COOLDOWN = 60.0  # if we hit 429, back off for 60 s globally

# Phase 26.60: module-level shared httpx.Client with bounded pool.
# Reused across fetch() calls so the TLS session stays warm and TCP
# connections are pooled instead of re-established per batch.
_HTTPX_CLIENT = httpx.Client(
    timeout=_TIMEOUT,
    limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
)

_lock = Lock()
_last_request_ts = 0.0
_rate_limited_until = 0.0
_stats = {
    "requests": 0, "hits": 0, "errors": 0,
    "rate_limited": 0, "no_key_skips": 0, "timeouts": 0,
}


def stats_snapshot() -> dict[str, int]:
    with _lock:
        return dict(_stats)


def _have_key() -> str | None:
    return api_keys.get("finnhub")


def _throttle() -> bool:
    """Returns True if the call may proceed, False if we're in cooldown.

    Phase 26.48 — sleep is performed OUTSIDE the lock so the throttle
    can never serialise concurrent callers at the lock level.  Each
    caller reserves its own slot atomically via `_last_request_ts`
    advance, then waits in its own time.
    """
    global _last_request_ts
    sleep_for = 0.0
    with _lock:
        now = time.monotonic()
        if now < _rate_limited_until:
            _stats["rate_limited"] += 1
            return False
        # Reserve our slot.  Concurrent callers stagger naturally.
        target_ts = max(_last_request_ts + _MIN_GAP, now)
        sleep_for = max(0.0, target_ts - now)
        _last_request_ts = target_ts
    if sleep_for > 0:
        time.sleep(sleep_for)
    return True


def _fetch_one(client: httpx.Client, symbol: str, token: str) -> dict | None:
    if not _throttle():
        return None
    try:
        resp = client.get(
            f"{_BASE_URL}/quote",
            params={"symbol": symbol, "token": token},
            timeout=_TIMEOUT,
        )
    except httpx.TimeoutException as exc:
        with _lock:
            _stats["timeouts"] += 1
        count_timeout('finnhub', f'{symbol}: {exc}')
        return None
    except Exception as exc:
        log.debug("finnhub fetch %s failed: %s", symbol, exc)
        with _lock:
            _stats["errors"] += 1
        count_error('finnhub', f'{symbol}: {exc}')
        return None
    if resp.status_code == 429:
        with _lock:
            global _rate_limited_until
            _rate_limited_until = time.monotonic() + _RATE_COOLDOWN
            _stats["rate_limited"] += 1
        count_rate_limit('finnhub', f'HTTP 429 on {symbol}')
        return None
    if resp.status_code != 200:
        with _lock:
            _stats["errors"] += 1
        count_error('finnhub', f'HTTP {resp.status_code} on {symbol}')
        return None
    try:
        body = resp.json()
    except Exception:
        with _lock:
            _stats["errors"] += 1
        return None
    # Finnhub /quote returns {c: current, h: high, l: low, o: open,
    # pc: previous close, t: timestamp, d: change, dp: change %, v: volume?}
    px = float(body.get("c") or 0)
    prev = float(body.get("pc") or 0)
    if px <= 0 or prev <= 0:
        return None
    return {
        "last_price": px,
        "previous_close": prev,
        "open": float(body.get("o") or prev),
        "day_high": float(body.get("h") or px),
        "day_low": float(body.get("l") or px),
        "volume": float(body.get("v") or 0),
        "source": "finnhub",
        "provider_outcome": "live_success",
        "preview_only": False,
    }


def fetch(symbols: Iterable[str], market: str) -> dict[str, dict]:
    """Chain-compatible entrypoint. Only handles US stocks.
    Returns {} for crypto (Finnhub crypto endpoint differs and is not
    wired here -- our crypto cascade is already strong without it).
    """
    if market == "crypto":
        return {}
    token = _have_key()
    if not token:
        with _lock:
            _stats["no_key_skips"] += 1
        return {}
    syms = [s for s in symbols if s]
    if not syms:
        return {}
    out: dict[str, dict] = {}
    # Phase 26.60: module-level shared httpx.Client.  Reusing across
    # invocations keeps the TLS session + connection pool warm between
    # scan batches.  Previously each fetch() call opened + closed a
    # fresh client — one TLS handshake per batch (typically 25-100
    # symbols) — which added measurable latency on cold starts and
    # amplified socket/FD churn.
    try:
        for sym in syms:
            with _lock:
                _stats["requests"] += 1
            snap = _fetch_one(_HTTPX_CLIENT, sym, token)
            if snap:
                out[sym] = snap
                with _lock:
                    _stats["hits"] += 1
    except Exception as exc:
        log.debug("finnhub batch failed: %s", exc)
    return out
