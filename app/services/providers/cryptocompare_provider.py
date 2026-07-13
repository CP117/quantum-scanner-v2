"""
CryptoCompare free provider (no auth required for basic price quotes).

Endpoint:
  GET https://min-api.cryptocompare.com/data/pricemultifull?fsyms={SYMS}&tsyms=USD

Used as a redundancy layer alongside the existing CoinGecko provider for the
crypto market.  Bigger benefit when CoinGecko rate-limits or returns stale data.
"""
from __future__ import annotations

import logging

import requests

from app.services.providers.base import record_rate_limit, record_timeout, record_error
from app.utils.time import utcnowiso

log = logging.getLogger('app.providers.cryptocompare')

_BASE = 'https://min-api.cryptocompare.com/data/pricemultifull'
_HEADERS = {'User-Agent': 'MarketRefinementDashboard/1.0', 'Accept': 'application/json'}
_TIMEOUT = 6.0


def _to_base_symbol(sym: str) -> str:
    s = sym.upper().strip()
    return s.split('-USD')[0] if s.endswith('-USD') else s


def fetch(symbols: list[str], market: str) -> dict[str, dict]:
    if not symbols or market != 'crypto':
        return {}
    base_syms = list({_to_base_symbol(s) for s in symbols if s})
    if not base_syms:
        return {}
    captured_at = utcnowiso()
    try:
        # CryptoCompare allows up to 100 symbols per multifull call.
        # Phase 26.18.c: resilient_get hardens against transient 429/5xx.
        from app.services.http_client import resilient_get, ResilientGetConfig
        cfg = ResilientGetConfig(max_attempts=3, connect_timeout=3.0, read_timeout=_TIMEOUT)
        res = resilient_get(
            _BASE, params={'fsyms': ','.join(base_syms), 'tsyms': 'USD'}, cfg=cfg,
        )
        if res.status_code == 0 and res.error:
            err_l = res.error.lower()
            if 'timeout' in err_l:
                record_timeout('cryptocompare', f'pricemultifull: {res.error}')
            else:
                record_error('cryptocompare', f'pricemultifull: {res.error}')
            return {}
        if res.status_code == 429:
            record_rate_limit('cryptocompare', 'HTTP 429 on pricemultifull')
            return {}
        if res.status_code != 200:
            return {}
        try:
            data = res.json() or {}
        except Exception as exc:  # noqa: BLE001
            record_error('cryptocompare', f'pricemultifull json_decode: {exc}')
            return {}
        raw = (data.get('RAW') or {})
    except Exception as exc:  # noqa: BLE001
        record_error('cryptocompare', f'pricemultifull: {exc}')
        log.debug('cryptocompare batch fetch failed: %s', exc)
        return {}
    out: dict[str, dict] = {}
    for base_sym, sub in raw.items():
        usd = (sub or {}).get('USD') or {}
        price = float(usd.get('PRICE') or 0)
        if price <= 0:
            continue
        full_sym = f'{base_sym}-USD'
        out[full_sym] = {
            'last_price': price,
            'previous_close': float(usd.get('OPEN24HOUR') or price),
            'open': float(usd.get('OPEN24HOUR') or price),
            'day_low': float(usd.get('LOW24HOUR') or price),
            'day_high': float(usd.get('HIGH24HOUR') or price),
            'volume': float(usd.get('VOLUME24HOUR') or 0),
            'market_cap': float(usd.get('MKTCAP') or 0),
            'captured_at_utc': captured_at,
            'source': 'cryptocompare',
            'provider_outcome': 'live_success',
            'preview_only': False,
        }
    return out


def fetch_daily_history(symbol: str, limit: int = 90):
    """Fetch ~`limit` days of daily OHLCV bars for a crypto symbol from
    CryptoCompare's histoday endpoint. Returns a pandas DataFrame in the
    yfinance shape (Open/High/Low/Close/Volume indexed by UTC datetime)
    or None on failure.

    Why this exists
    ---------------
    yfinance has no listing for the long crypto tail (rank-200+ alts). For
    those coins the live-quote cascade falls back to CryptoCompare, but
    `daily_history_service.get_daily_history()` was yfinance-only - which
    left the three history-dependent factor families (institutional
    confluence, volume sentiment, reaction clustering) stuck on warming
    for ~90% of CryptoCompare-sourced crypto rows.

    Endpoint:
      GET https://min-api.cryptocompare.com/data/v2/histoday?fsym=BTC&tsym=USD&limit=90

    Defensive: never raises, returns None on any error.
    """
    if not symbol:
        return None
    base = _to_base_symbol(symbol)
    if not base:
        return None
    try:
        # Phase 26.18.c: resilient_get for the histoday endpoint too.
        from app.services.http_client import resilient_get, ResilientGetConfig
        cfg = ResilientGetConfig(max_attempts=3, connect_timeout=3.0, read_timeout=_TIMEOUT)
        res = resilient_get(
            'https://min-api.cryptocompare.com/data/v2/histoday',
            params={'fsym': base, 'tsym': 'USD', 'limit': max(1, min(2000, int(limit)))},
            cfg=cfg,
        )
        if res.status_code == 0 and res.error:
            err_l = res.error.lower()
            if 'timeout' in err_l:
                record_timeout('cryptocompare', f'histoday {base}: {res.error}')
            else:
                record_error('cryptocompare', f'histoday {base}: {res.error}')
            return None
        if res.status_code == 429:
            # Per-symbol histoday call, but it's a SINGLE-symbol request so
            # one CB-bumping increment is appropriate here.
            record_rate_limit('cryptocompare', f'histoday 429 for {base}')
            return None
        if res.status_code != 200:
            return None
        try:
            body = res.json() or {}
        except Exception as exc:  # noqa: BLE001
            record_error('cryptocompare', f'histoday json_decode {base}: {exc}')
            return None
        if (body.get('Response') or '').lower() != 'success':
            return None
        data = ((body.get('Data') or {}).get('Data')) or []
        if not data:
            return None
    except Exception as exc:  # noqa: BLE001
        record_error('cryptocompare', f'histoday {base}: {exc}')
        log.debug('cryptocompare histoday fetch failed for %s: %s', base, exc)
        return None

    try:
        import pandas as pd
        rows = []
        for bar in data:
            close = float(bar.get('close') or 0)
            if close <= 0:
                continue
            rows.append({
                'time': int(bar.get('time') or 0),
                'Open': float(bar.get('open') or close),
                'High': float(bar.get('high') or close),
                'Low': float(bar.get('low') or close),
                'Close': close,
                'Volume': float(bar.get('volumeto') or bar.get('volumefrom') or 0),
            })
        if not rows:
            return None
        idx = pd.to_datetime([r['time'] for r in rows], unit='s', errors='coerce', utc=True)
        df = pd.DataFrame({
            'Open':   [r['Open']   for r in rows],
            'High':   [r['High']   for r in rows],
            'Low':    [r['Low']    for r in rows],
            'Close':  [r['Close']  for r in rows],
            'Volume': [r['Volume'] for r in rows],
        }, index=idx)
        # Sort ascending by time and drop the synthetic placeholder bars
        # CryptoCompare prepends (it pads the requested window with rows
        # whose all-zero OHLC could pollute moving-average calcs).
        df = df.sort_index()
        df = df[df['Close'] > 0]
        if df.empty:
            return None
        return df
    except Exception as exc:  # noqa: BLE001
        log.debug('cryptocompare histoday parse failed for %s: %s', base, exc)
        return None
