"""Cross-market squeeze radar (Phase 26.70).

Scans BOTH stocks AND crypto simultaneously, ranks each row by a
market-appropriate squeeze-conviction score, and returns the top
candidates in each market side-by-side.

Conviction score components (all 0-1 after normalization):
    - SSP score (short-selling pressure, populated)  → primary signal
    - PVI score (predicted volume intensity, populated) → confirmation
    - Correlation-to-driver-basket:
         * stocks → correlation to BTC-USD returns (cross-market
           coupling; high correlation means the stock is "moving with
           crypto risk-on/off flows" which amplifies squeeze potential)
         * crypto → correlation to SPY returns (macro-linked coins are
           more squeeze-prone under macro shocks)

    conviction = 0.45 * ssp_norm + 0.35 * pvi_norm + 0.20 * corr_boost

Result envelope:
    {
        "generated_utc": "...",
        "stocks":   [ {symbol, name, final_score, conviction, ssp, pvi, corr, ...}, ... ],
        "crypto":   [ ... ],
        "meta":     { limits, thresholds, driver_basket, ... }
    }
"""
from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger('app.cross_market_radar')

_CACHE: dict[str, Any] = {'ts': 0.0, 'payload': None}
_CACHE_TTL_S = 20.0  # Cheap enough to redo often; short TTL keeps it fresh


def _safe_num(v, default=0.0) -> float:
    try:
        f = float(v)
        if f != f:  # NaN
            return default
        return f
    except (TypeError, ValueError):
        return default


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _correlation_boost(row_closes: list[float], driver_returns: list[float]) -> float:
    """Return a 0-1 boost derived from the |Pearson correlation|
    between the row's log-returns and the driver's log-returns over
    the overlapping window."""
    if not row_closes or not driver_returns or len(row_closes) < 22:
        return 0.0
    row_rets: list[float] = []
    for i in range(1, len(row_closes)):
        if row_closes[i - 1] > 0 and row_closes[i] > 0:
            row_rets.append(math.log(row_closes[i] / row_closes[i - 1]))
    n = min(len(row_rets), len(driver_returns), 60)
    if n < 20:
        return 0.0
    r = row_rets[-n:]
    d = driver_returns[-n:]
    mr = sum(r) / n
    md = sum(d) / n
    num = sum((r[i] - mr) * (d[i] - md) for i in range(n))
    denr = math.sqrt(sum((r[i] - mr) ** 2 for i in range(n)))
    dend = math.sqrt(sum((d[i] - md) ** 2 for i in range(n)))
    if denr <= 0 or dend <= 0:
        return 0.0
    corr = num / (denr * dend)
    return _clip01(abs(corr))


def _rank_market(rows: list[dict], market: str, driver_returns_stocks: list[float],
                 driver_returns_crypto: list[float], limit: int) -> list[dict]:
    """Score + sort every row in `rows` for the given market."""
    # For stocks, driver = crypto (BTC-USD) returns.
    # For crypto, driver = stocks (SPY) returns.
    driver = driver_returns_crypto if market == 'stocks' else driver_returns_stocks
    scored: list[dict] = []
    from app.services.future_mode_service import _load_closes_cached
    for row in rows:
        ssp = _safe_num(row.get('short_selling_pressure_score'))
        pvi = _safe_num(row.get('predicted_volume_intensity_score'))
        ssp_src = row.get('short_selling_pressure_source')
        # Populated signal gate — same idea as scanner_presets crypto
        # overrides.  Rows without a real SSP/PVI signal are excluded
        # from the radar (they contribute noise, not conviction).
        if ssp_src in (None, 'unavailable', '') or ssp <= 50.1 and market == 'crypto':
            # crypto default is 50/neutral placeholder — skip.
            if market == 'crypto' and ssp <= 50.1 and pvi <= 0.5:
                continue
        if pvi <= 0.1:
            continue
        # Normalize scores.  SSP is 0-100 centered at 50 (neutral); we
        # care about *deviation* from neutral for both bullish squeeze
        # and bearish pressure.
        ssp_norm = _clip01(abs(ssp - 50.0) / 50.0)
        pvi_norm = _clip01(pvi / 100.0)
        # Correlation boost from cross-market coupling.
        closes = _load_closes_cached(row.get('symbol') or '')
        corr = _correlation_boost(closes, driver)
        conviction = 0.45 * ssp_norm + 0.35 * pvi_norm + 0.20 * corr
        # Directional interpretation — reuse the label logic where
        # possible; for undefined labels fall back to final_direction.
        label = row.get('short_selling_pressure_label') or 'neutral'
        direction = row.get('final_direction') or 'Neutral'
        squeeze_kind = ('bullish_squeeze' if label in ('squeeze_risk_bullish', 'elevated_squeeze_watch')
                        else 'bearish_pressure' if label in ('bearish_pressure', 'elevated')
                        else 'neutral')
        scored.append({
            'symbol':        row.get('symbol'),
            'name':          row.get('name') or '',
            'market':        market,
            'final_score':   _safe_num(row.get('final_score')),
            'final_direction': direction,
            'tier':          row.get('tier') or '?',
            'conviction':    round(conviction, 4),
            'conviction_pct': round(conviction * 100, 1),
            'ssp':           round(ssp, 2),
            'ssp_label':     label,
            'pvi':           round(pvi, 2),
            'pvi_bucket':    row.get('predicted_volume_intensity_bucket') or 'low',
            'correlation':   round(corr, 3),
            'squeeze_kind':  squeeze_kind,
            'squeeze_direction': (
                'up' if direction == 'Bullish' and squeeze_kind == 'bullish_squeeze'
                else 'down' if direction == 'Bearish' and squeeze_kind == 'bearish_pressure'
                else 'neutral'
            ),
        })
    scored.sort(key=lambda x: x['conviction'], reverse=True)
    return scored[:limit]


def compute_cross_market_radar(limit_per_market: int = 20, universe_scan_limit: int = 100) -> dict:
    """Assemble the radar payload — cached for `_CACHE_TTL_S`."""
    now = time.monotonic()
    cached = _CACHE.get('payload')
    if cached is not None and (now - _CACHE.get('ts', 0.0)) < _CACHE_TTL_S:
        return cached

    from app.services.market_proxy_service import load_market_proxy_returns
    from app.services.result_store import get_results_batch

    driver_stocks = load_market_proxy_returns('stocks') or []
    driver_crypto = load_market_proxy_returns('crypto') or []

    # Pull the currently-scored broadcast rows from each market's
    # snapshot batches.  Crypto is capped tighter than stocks because
    # coingecko fetches are per-symbol (unlike yfinance's batch quote)
    # and a 100-row crypto scan takes minutes end-to-end.
    stock_rows: list[dict] = []
    crypto_rows: list[dict] = []
    try:
        env = get_results_batch(batch=0, limit=min(universe_scan_limit, 200), market='stocks')
        stock_rows = env.get('results') or []
    except Exception as exc:  # noqa: BLE001
        log.warning('cross-market-radar: stock scan failed: %s', exc)
    try:
        env = get_results_batch(batch=0, limit=min(universe_scan_limit, 50), market='crypto')
        crypto_rows = env.get('results') or []
    except Exception as exc:  # noqa: BLE001
        log.warning('cross-market-radar: crypto scan failed: %s', exc)

    stocks_ranked = _rank_market(stock_rows, 'stocks', driver_stocks, driver_crypto, limit_per_market)
    crypto_ranked = _rank_market(crypto_rows, 'crypto', driver_stocks, driver_crypto, limit_per_market)

    payload = {
        'generated_utc': datetime.now(timezone.utc).isoformat(),
        'meta': {
            'limit_per_market': limit_per_market,
            'universe_scan_limit': universe_scan_limit,
            'driver_basket_stocks': 'BTC-USD',
            'driver_basket_crypto': 'SPY',
            'driver_stocks_bars': len(driver_stocks),
            'driver_crypto_bars': len(driver_crypto),
            'scanned_stocks': len(stock_rows),
            'scanned_crypto': len(crypto_rows),
            'cache_ttl_seconds': _CACHE_TTL_S,
            'weights': {'ssp_norm': 0.45, 'pvi_norm': 0.35, 'correlation': 0.20},
            'method': (
                "conviction = 0.45·|SSP-50|/50 + 0.35·PVI/100 + 0.20·|corr|; "
                "stocks are cross-correlated against BTC-USD (crypto driver), "
                "crypto rows against SPY (stock driver). Only rows with a "
                "populated SSP + PVI signal are included."
            ),
        },
        'stocks': stocks_ranked,
        'crypto': crypto_ranked,
        'top_combined': sorted(stocks_ranked + crypto_ranked,
                               key=lambda x: x['conviction'], reverse=True)[:limit_per_market],
    }
    _CACHE['ts'] = now
    _CACHE['payload'] = payload
    return payload
