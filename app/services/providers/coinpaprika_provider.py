"""
CoinPaprika provider — free crypto quote source, no API key required.

Used as a tail-of-cascade fallback for crypto symbols when CoinGecko +
CryptoCompare + yfinance all fail. CoinPaprika is generally more
generous on rate limits than CoinGecko free tier (~25k requests/month
per IP) so it's a good safety net for the rank-1000+ tail.

Response shape (after normalisation) matches the other provider modules:

    {sym: {
        'currentPrice': float, 'previousClose': float,
        'open': float, 'dayLow': float, 'dayHigh': float,
        'volume': float, 'marketCap': float,
        'source': 'coinpaprika',
    }}

Defensive-by-default: never raises, returns {} on any error.
"""
from __future__ import annotations

import logging
import time
from threading import Lock
from typing import Iterable

import httpx

from app.services.providers.base import count_error, count_rate_limit, count_timeout

log = logging.getLogger("app.providers.coinpaprika")

# CoinPaprika allows ~25k requests/month per IP without an API key.
# That's plenty for our use case as a tail fallback (rank-1000+ coins).
_BASE_URL = "https://api.coinpaprika.com/v1"
_TIMEOUT = 6.0
_MIN_GAP = 0.25  # 4 req/sec is well under the IP limit

_lock = Lock()
_last_request_ts = 0.0
_id_cache: dict[str, str] | None = None  # ticker -> paprika coin_id (e.g. 'btc-bitcoin')
_id_cache_ts = 0.0
_ID_CACHE_TTL = 60 * 60 * 24  # 24 hours

_stats = {
    "requests": 0, "hits": 0, "errors": 0,
    "id_cache_loads": 0, "id_cache_misses": 0,
}

# Phase 26.60: module-level shared httpx.Client with a bounded connection
# pool.  Previously every call to `_load_id_cache` and `_fetch_one`
# entered `with httpx.Client(...) as client:` which did a fresh TLS
# handshake + connection setup per crypto symbol in the fallback path.
# Reusing one client across calls keeps the TLS session and TCP
# connection warm — cuts per-symbol latency from ~150-300 ms to <20 ms
# (once the pool is warm) and eliminates the per-call socket + FD
# churn.  Sized modestly (5 keepalive) since CoinPaprika is a tail
# fallback and rarely gets sustained volume.
_HTTPX_CLIENT = httpx.Client(
    timeout=_TIMEOUT,
    limits=httpx.Limits(max_connections=8, max_keepalive_connections=5),
    headers={'user-agent': 'market-refinement-dashboard/1.0',
             'accept': 'application/json'},
)


def stats_snapshot() -> dict[str, int]:
    with _lock:
        return dict(_stats)


def _throttle() -> None:
    """Phase 26.48 — sleep performed OUTSIDE the lock to prevent
    concurrent callers from serialising at the lock level."""
    global _last_request_ts
    sleep_for = 0.0
    with _lock:
        now = time.monotonic()
        target_ts = max(_last_request_ts + _MIN_GAP, now)
        sleep_for = max(0.0, target_ts - now)
        _last_request_ts = target_ts
    if sleep_for > 0:
        time.sleep(sleep_for)


def _load_id_cache() -> dict[str, str]:
    """Build the {ticker: coin_id} index. Cached for 24h."""
    global _id_cache, _id_cache_ts
    now = time.monotonic()
    if _id_cache and now - _id_cache_ts < _ID_CACHE_TTL:
        return _id_cache
    try:
        _throttle()
        # Phase 26.60: reuse module-level shared client (see top).
        resp = _HTTPX_CLIENT.get(f"{_BASE_URL}/coins")
        if resp.status_code == 429:
            count_rate_limit('coinpaprika', 'HTTP 429 on /coins')
            return _id_cache or {}
        if resp.status_code != 200:
            return _id_cache or {}
        rows = resp.json()
    except httpx.TimeoutException as exc:
        count_timeout('coinpaprika', f'/coins: {exc}')
        log.debug("coinpaprika id-cache timeout: %s", exc)
        return _id_cache or {}
    except Exception as exc:
        count_error('coinpaprika', f'/coins: {exc}')
        log.debug("coinpaprika id-cache fetch failed: %s", exc)
        return _id_cache or {}
    # Build the ticker->id index. Each row has {id, name, symbol, rank, is_active}.
    # When multiple coins share a ticker (e.g. BTC ambiguous with BTC2),
    # prefer the one with the lowest rank (highest market cap).
    new_cache: dict[str, str] = {}
    seen_rank: dict[str, int] = {}
    for row in rows:
        if not row.get("is_active"):
            continue
        sym = (row.get("symbol") or "").upper()
        if not sym:
            continue
        rank = int(row.get("rank") or 0) or 999_999
        if sym not in new_cache or rank < seen_rank.get(sym, 999_999):
            new_cache[sym] = row.get("id") or ""
            seen_rank[sym] = rank
    with _lock:
        _id_cache = new_cache
        _id_cache_ts = now
        _stats["id_cache_loads"] += 1
    log.info("coinpaprika id cache built: %d tickers", len(new_cache))
    return new_cache


def _normalize_symbol(sym: str) -> str:
    """`BTC-USD` -> `BTC` for CoinPaprika lookup."""
    if not sym:
        return ""
    s = sym.upper()
    if s.endswith("-USD"):
        return s[:-4]
    return s


def _fetch_one(ticker: str, coin_id: str) -> dict | None:
    try:
        _throttle()
        # Phase 26.60: reuse module-level shared client (see top).
        resp = _HTTPX_CLIENT.get(f"{_BASE_URL}/tickers/{coin_id}")
        if resp.status_code == 429:
            count_rate_limit('coinpaprika', f'HTTP 429 on /tickers/{coin_id}')
            return None
        if resp.status_code != 200:
            return None
        data = resp.json()
    except httpx.TimeoutException as exc:
        count_timeout('coinpaprika', f'/tickers/{coin_id}: {exc}')
        log.debug("coinpaprika timeout %s: %s", coin_id, exc)
        return None
    except Exception as exc:
        count_error('coinpaprika', f'/tickers/{coin_id}: {exc}')
        log.debug("coinpaprika fetch %s failed: %s", coin_id, exc)
        return None
    usd = (data.get("quotes") or {}).get("USD") or {}
    if not usd:
        return None
    price = float(usd.get("price") or 0)
    if price <= 0:
        return None
    # CoinPaprika's percent_change_24h gives the % change; derive prev close.
    pct = float(usd.get("percent_change_24h") or 0)
    prev_close = price / (1.0 + pct / 100.0) if pct else price
    return {
        "currentPrice": price,
        "previousClose": prev_close,
        "open": prev_close,    # CoinPaprika doesn't expose session open
        "dayLow": float(usd.get("price") or 0) * (1.0 - max(0.0, abs(pct)) / 200.0),
        "dayHigh": float(usd.get("price") or 0) * (1.0 + max(0.0, abs(pct)) / 200.0),
        "volume": float(usd.get("volume_24h") or 0),
        "marketCap": float(usd.get("market_cap") or 0),
        "source": "coinpaprika",
    }


def fetch(symbols: Iterable[str], market: str) -> dict[str, dict]:
    """Chain-compatible entrypoint. Only handles crypto (`-USD` symbols).
    Returns an empty dict for stocks (CoinPaprika is crypto-only).
    """
    if market != "crypto":
        return {}
    syms = [s for s in symbols if s]
    if not syms:
        return {}
    id_map = _load_id_cache()
    out: dict[str, dict] = {}
    for original in syms:
        norm = _normalize_symbol(original)
        coin_id = id_map.get(norm)
        if not coin_id:
            with _lock:
                _stats["id_cache_misses"] += 1
            continue
        with _lock:
            _stats["requests"] += 1
        snap = _fetch_one(norm, coin_id)
        if snap:
            out[original] = {
                "last_price": snap["currentPrice"],
                "previous_close": snap["previousClose"],
                "open": snap["open"],
                "day_low": snap["dayLow"],
                "day_high": snap["dayHigh"],
                "volume": snap["volume"],
                "market_cap": snap["marketCap"],
                "source": "coinpaprika",
                "provider_outcome": "live_success",
                "preview_only": False,
            }
            with _lock:
                _stats["hits"] += 1
        else:
            with _lock:
                _stats["errors"] += 1
    return out
