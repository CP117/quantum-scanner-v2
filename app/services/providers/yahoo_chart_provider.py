"""
Direct Yahoo Finance chart-API provider.

Uses the public endpoint:
  GET https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=5d&interval=1d

No crumb required (unlike fast_info / .info on some yfinance versions).
Returns the most recent close as `last_price` and the prior close as
`previous_close`.  Used as a redundancy layer when `yfinance` fast paths
flap due to rate-limit / crumb errors.
"""
from __future__ import annotations

import logging

import requests

from app.services.providers.base import (
    count_error,
    count_rate_limit,
    count_timeout,
)
from app.utils.time import utcnowiso

log = logging.getLogger('app.providers.yahoo_chart')

UA = 'Mozilla/5.0 (compatible; MarketRefinementDashboard/1.0)'
_BASE = 'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}'
_PARAMS = {'range': '5d', 'interval': '1d'}
_HEADERS = {'User-Agent': UA, 'Accept': 'application/json'}
_TIMEOUT = 6.0


def _fetch_one(sym: str, captured_at: str) -> tuple[str, dict | None]:
    """Fetch a single symbol from the Yahoo chart endpoint."""
    try:
        url = _BASE.format(symbol=sym)
        # Phase 26.18.c: resilient_get gives us retries + backoff + UA
        # rotation + Retry-After honoring on transient 5xx/429. Yahoo's
        # chart endpoint is particularly prone to 429-bursts during
        # market-open peaks, so this is high-value here.
        from app.services.http_client import resilient_get, ResilientGetConfig
        cfg = ResilientGetConfig(
            max_attempts=3,
            connect_timeout=3.0,
            read_timeout=_TIMEOUT,
            retry_after_cap_seconds=3.0,
        )
        res = resilient_get(url, params=_PARAMS, headers={'Accept': 'application/json'}, cfg=cfg)
        if res.status_code == 0 and res.error:
            # All retries exhausted on transient transport failure.
            err_l = res.error.lower()
            if 'timeout' in err_l:
                count_timeout('yahoo-chart', f'{sym}: {res.error}')
            else:
                count_error('yahoo-chart', f'{sym}: {res.error}')
            return sym, None
        if res.status_code == 429:
            count_rate_limit('yahoo-chart', f'HTTP 429 on {sym}')
            return sym, None
        if res.status_code != 200:
            return sym, None
        try:
            data = res.json() or {}
        except Exception as exc:  # noqa: BLE001
            count_error('yahoo-chart', f'{sym}: json_decode {exc}')
            return sym, None
        chart = (data.get('chart') or {})
        if chart.get('error'):
            return sym, None
        results = chart.get('result') or []
        if not results:
            return sym, None
        r0 = results[0]
        meta = r0.get('meta') or {}
        indicators = r0.get('indicators') or {}
        quote = (indicators.get('quote') or [{}])[0]
        closes = [c for c in (quote.get('close') or []) if c is not None]
        highs = [c for c in (quote.get('high') or []) if c is not None]
        lows = [c for c in (quote.get('low') or []) if c is not None]
        opens_ = [c for c in (quote.get('open') or []) if c is not None]
        volumes = [c for c in (quote.get('volume') or []) if c is not None]
        if not closes:
            return sym, None
        last_price = float(meta.get('regularMarketPrice') or closes[-1])
        prev_close = float(
            meta.get('chartPreviousClose')
            or meta.get('previousClose')
            or (closes[-2] if len(closes) >= 2 else closes[-1])
        )
        return sym, {
            'last_price': last_price,
            'previous_close': prev_close if prev_close > 0 else last_price,
            'open': float(opens_[-1]) if opens_ else last_price,
            'day_low': float(lows[-1]) if lows else last_price,
            'day_high': float(highs[-1]) if highs else last_price,
            'volume': float(volumes[-1]) if volumes else 0.0,
            'market_cap': 0.0,
            'captured_at_utc': captured_at,
            'source': 'yahoo-chart',
            'provider_outcome': 'live_success',
            'preview_only': False,
        }
    except Exception as exc:  # noqa: BLE001
        # Defensive net for any other unexpected error reaching here
        # (resilient_get itself shouldn't raise, but the parsing block
        # above could).
        count_error('yahoo-chart', f'{sym}: {exc}')
        log.debug('yahoo-chart fetch failed for %s: %s', sym, exc)
        return sym, None


def fetch(symbols: list[str], market: str) -> dict[str, dict]:
    if not symbols or market == 'crypto':
        return {}
    out: dict[str, dict] = {}
    captured_at = utcnowiso()
    # Parallelize across symbols — the endpoint is independent per-symbol
    # and fast (<200 ms each).  When the upstream yfinance batch returns
    # sparse data (often the case on residential ISPs), this cascade is
    # invoked with up to 25 symbols and used to serialize at 5 s per batch.
    #
    # Phase 26.32: manual pool + shutdown(wait=False) so a hung HTTP
    # socket can't block the entire batch beyond as_completed's 20s
    # deadline.  See stooq_provider.fetch for the full rationale.
    from concurrent.futures import (
        ThreadPoolExecutor,
        as_completed,
        TimeoutError as _FuturesTimeoutError,
    )
    pool = ThreadPoolExecutor(max_workers=10, thread_name_prefix='yahoo-chart')
    try:
        futures = [pool.submit(_fetch_one, sym, captured_at) for sym in symbols]
        try:
            for fut in as_completed(futures, timeout=20):
                try:
                    sym, payload = fut.result()
                    if payload:
                        out[sym] = payload
                except Exception:
                    continue
        except _FuturesTimeoutError:
            log.debug('yahoo-chart fetch: as_completed timed out at 20s; %d/%d done',
                      len(out), len(futures))
    finally:
        pool.shutdown(wait=False)
    return out


def _legacy_sequential_fetch(symbols: list[str], market: str) -> dict[str, dict]:
    """Kept for fallback only — the new fetch() is the parallel version."""
    if not symbols or market == 'crypto':
        return {}
    out: dict[str, dict] = {}
    captured_at = utcnowiso()
    for sym in symbols:
        _, payload = _fetch_one(sym, captured_at)
        if payload:
            out[sym] = payload
    return out
