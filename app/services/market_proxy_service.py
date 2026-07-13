"""Market-proxy return-series loader (extracted from future_mode_service).

The Local-Causal-Cone (LCC) reality-breaker overlay in `future_mode_service`
needs a "driver basket" return series to decompose comovement against.
We pick the driver by market:

    * stocks → SPY  (broad market cap-weighted index tracker)
    * crypto → BTC-USD (crypto beta anchor)

Returns are cached PER PROXY for `_MARKET_PROXY_TTL_S` (5 minutes) so
both markets stay warm simultaneously and repeated LCC calls during a
scan sweep don't touch the daily-history layer.

This module has no dependencies on future_mode_service itself — it
imports `daily_history_service` on demand, so importing this module
does not pull in the entire forecast machinery.
"""
from __future__ import annotations

import logging
import math
import time
from typing import Optional

log = logging.getLogger('app.market_proxy')

# Public constants — used by tests / diagnostics to assert the right
# proxy is selected for a given market.
MARKET_PROXY_SYMBOL_STOCKS = 'SPY'
MARKET_PROXY_SYMBOL_CRYPTO = 'BTC-USD'
MARKET_PROXY_TTL_S = 300.0

# Internal cache keyed by proxy-symbol so both markets survive together.
_market_proxy_cache: dict[str, tuple[float, list[float]]] = {}


def proxy_symbol_for(market: Optional[str]) -> str:
    """Return the canonical proxy symbol for a market."""
    return (MARKET_PROXY_SYMBOL_CRYPTO
            if (market or '').lower() == 'crypto'
            else MARKET_PROXY_SYMBOL_STOCKS)


def _load_closes(proxy: str) -> list[float]:
    """Read the proxy's daily-history closes.  Kept intentionally
    minimal — a blocking fetch is fine because the proxy universe is
    tiny (SPY + BTC-USD) and hits are almost always a cache-hit."""
    try:
        # Try the cached-list fast path first via future_mode_service's
        # per-symbol closes cache (identical helper, avoids duplicating
        # its TTL logic).  If that helper is unavailable (import cycle
        # etc.) fall back to a direct daily-history read.
        from app.services.future_mode_service import _load_closes_cached  # type: ignore
        cached = _load_closes_cached(proxy) or []
        if cached and len(cached) >= 20:
            return cached
    except Exception:  # noqa: BLE001
        pass
    try:
        from app.services.daily_history_service import get_daily_history
        df = get_daily_history(proxy, allow_fetch=True, blocking=True)
        if df is not None and not df.empty:
            return [float(c) for c in df['Close'].dropna().tolist()]
    except Exception as exc:  # noqa: BLE001
        log.debug('market-proxy: %s daily-history unavailable (%s)', proxy, exc)
    return []


def load_market_proxy_returns(market: Optional[str] = None) -> Optional[list[float]]:
    """Return cached log-returns for the market-appropriate driver
    basket, or None if no data yet.  Result is cached per proxy for
    `MARKET_PROXY_TTL_S` seconds.

    Empty results are NOT cached at all so that a startup-window
    miss (SPY/BTC-USD daily-history not yet warm) does not lock the
    proxy to `None` for the full 5-minute TTL.  Without this, the
    cross-market radar rendered correlation = 0 for every row until
    the process was restarted — the "driver bars = 0" bug the user
    reported on 2026-07-03.
    """
    proxy = proxy_symbol_for(market)
    now = time.monotonic()
    cached = _market_proxy_cache.get(proxy)
    if cached is not None and cached[1] and (now - cached[0]) < MARKET_PROXY_TTL_S:
        return cached[1]
    closes = _load_closes(proxy)
    if not closes or len(closes) < 20:
        # Do not cache — retry on the very next call so we self-heal
        # once the daily-history layer has finished warming.
        return None
    rets: list[float] = []
    for i in range(1, len(closes)):
        if closes[i - 1] > 0 and closes[i] > 0:
            rets.append(math.log(closes[i] / closes[i - 1]))
    if not rets:
        return None
    _market_proxy_cache[proxy] = (now, rets)
    return rets


def market_proxy_cache_state() -> dict:
    """Diagnostic snapshot for /api/universes/integrity + Metrics Hub."""
    now = time.monotonic()
    out: dict[str, dict] = {}
    for proxy, (ts, rets) in list(_market_proxy_cache.items()):
        out[proxy] = {
            'return_bars': len(rets or []),
            'age_seconds': round(now - ts, 1),
            'ttl_seconds': MARKET_PROXY_TTL_S,
            'stale': (now - ts) > MARKET_PROXY_TTL_S,
        }
    return {
        'stocks_proxy': MARKET_PROXY_SYMBOL_STOCKS,
        'crypto_proxy': MARKET_PROXY_SYMBOL_CRYPTO,
        'entries': out,
    }
