"""
Ticker → CIK lookup map for SEC's submissions API.

SEC publishes the official map at https://www.sec.gov/files/company_tickers.json
(refreshed daily). We cache it locally under /app/data/sec_ticker_cik.json so
the auto-scan loop can resolve a scanner ticker (e.g. "AAPL") to a 10-digit
CIK (e.g. "0000320193") without burning an HTTP round-trip per ticker.

The map is also exposed to the signal pipeline so we can resolve recipient
names from USAspending awards back to a ticker symbol via the issuer-name
field stored on tracked_companies.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from app.regulatory.services.http_client import get_client

log = logging.getLogger('app.regulatory.cik_lookup')

# Sit alongside the scanner's other data caches.
_CACHE_PATH = Path(__file__).resolve().parents[3] / 'data' / 'sec_ticker_cik.json'
_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
_MAX_AGE_SECONDS = 24 * 3600  # refresh once a day

# In-memory copies for fast lookup; both keyed UPPER-CASE.
_TICKER_TO_CIK: dict[str, str] = {}
_NAME_TO_TICKER: dict[str, str] = {}
_LOADED_AT: float = 0.0
_lock = asyncio.Lock()


def _normalize_name(s: str) -> str:
    if not s:
        return ''
    # Aggressive normalization so "APPLE INC." and "Apple, Inc" both map to "apple".
    s = s.upper()
    for sfx in [' INC.', ' INC', ' CORP.', ' CORP', ' CORPORATION', ' COMPANY',
                ' CO.', ' CO', ' LTD.', ' LTD', ' LIMITED', ' PLC', ' LLC',
                ' HOLDINGS', ' GROUP', ' CLASS A', ' CLASS B', ' CLASS C',
                ' THE', ',', '.', '/']:
        s = s.replace(sfx, ' ')
    return ' '.join(s.split()).strip().lower()


def _cik10(value: str) -> str:
    import re
    digits = re.sub(r'\D', '', value or '')
    return digits.zfill(10)


async def _fetch_remote_map() -> dict:
    url = 'https://www.sec.gov/files/company_tickers.json'
    client = get_client('sec')
    r = await client.get(url)
    r.raise_for_status()
    return r.json()


def _load_from_cache() -> dict | None:
    if not _CACHE_PATH.exists():
        return None
    try:
        with _CACHE_PATH.open('r') as fh:
            payload = json.load(fh)
        if not isinstance(payload, dict):
            return None
        return payload
    except Exception:
        return None


def _persist_to_cache(payload: dict) -> None:
    try:
        with _CACHE_PATH.open('w') as fh:
            json.dump(payload, fh)
    except Exception as exc:
        log.warning('failed to persist ticker_cik cache: %s', exc)


def _rebuild_indexes(payload: dict) -> None:
    """`payload` is SEC's raw dict: { "0": {"cik_str":..., "ticker":..., "title":...}, ... }"""
    global _TICKER_TO_CIK, _NAME_TO_TICKER
    t2c: dict[str, str] = {}
    n2t: dict[str, str] = {}
    for entry in payload.values() if isinstance(payload, dict) else []:
        ticker = str(entry.get('ticker') or '').upper()
        cik = _cik10(str(entry.get('cik_str') or ''))
        title = str(entry.get('title') or '')
        if ticker and cik and cik != '0000000000':
            t2c[ticker] = cik
        if title and ticker:
            n2t[_normalize_name(title)] = ticker
    _TICKER_TO_CIK = t2c
    _NAME_TO_TICKER = n2t


async def initialize(force_refresh: bool = False) -> int:
    """Load (and refresh if stale) the ticker→CIK map. Returns the entry count.

    Safe to call multiple times; the first concurrent caller refreshes, others
    await the same lock and read the result.
    """
    global _LOADED_AT
    async with _lock:
        now = time.monotonic()
        # 1) If already loaded and fresh, return.
        if not force_refresh and _TICKER_TO_CIK and (now - _LOADED_AT) < _MAX_AGE_SECONDS:
            return len(_TICKER_TO_CIK)
        # 2) Try local cache first.
        cached = _load_from_cache()
        if cached and not force_refresh:
            try:
                stamp = float(cached.get('_loaded_at', 0))
                age = time.time() - stamp
                if age < _MAX_AGE_SECONDS and cached.get('entries'):
                    _rebuild_indexes(cached['entries'])
                    _LOADED_AT = now
                    log.info('ticker_cik map loaded from cache (%d entries, %.1fh old)',
                             len(_TICKER_TO_CIK), age / 3600)
                    return len(_TICKER_TO_CIK)
            except Exception:
                pass
        # 3) Pull fresh from SEC.
        try:
            payload = await _fetch_remote_map()
            _rebuild_indexes(payload)
            _LOADED_AT = now
            _persist_to_cache({'_loaded_at': time.time(), 'entries': payload})
            log.info('ticker_cik map fetched live from SEC (%d entries)', len(_TICKER_TO_CIK))
        except Exception as exc:
            log.warning('failed to fetch ticker_cik map: %s', exc)
            # Fall back to whatever cache we have, even if stale.
            if cached and cached.get('entries'):
                _rebuild_indexes(cached['entries'])
                _LOADED_AT = now
                log.info('ticker_cik map fell back to stale cache (%d entries)', len(_TICKER_TO_CIK))
        return len(_TICKER_TO_CIK)


def cik_for_ticker(ticker: str) -> str | None:
    if not ticker:
        return None
    return _TICKER_TO_CIK.get(ticker.upper().strip())


def ticker_for_recipient_name(name: str) -> str | None:
    """Best-effort fuzzy reverse lookup for a USAspending recipient_name → ticker.
    Returns None when no confident match exists.
    """
    if not name:
        return None
    norm = _normalize_name(name)
    if not norm:
        return None
    # Exact normalized hit
    if norm in _NAME_TO_TICKER:
        return _NAME_TO_TICKER[norm]
    # Substring fallback — only when the recipient norm is a prefix of an issuer
    # norm and is at least 4 chars (avoid "ABC" matching dozens of small caps).
    if len(norm) >= 6:
        for issuer_norm, ticker in _NAME_TO_TICKER.items():
            if issuer_norm.startswith(norm) or norm.startswith(issuer_norm):
                # Only accept when the prefix overlap is meaningful (>=70% length).
                shorter = min(len(issuer_norm), len(norm))
                longer = max(len(issuer_norm), len(norm))
                if shorter / max(1, longer) >= 0.7:
                    return ticker
    return None


def snapshot() -> dict:
    return {
        'entry_count': len(_TICKER_TO_CIK),
        'loaded_at_monotonic': _LOADED_AT,
    }


def all_tickers_with_cik() -> list[tuple[str, str]]:
    """Returns a list of (ticker, cik) for every entry in the map. Useful for the
    universe auto-scan loop.
    """
    return list(_TICKER_TO_CIK.items())
