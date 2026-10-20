"""Adapter: raw Yahoo chart-API JSON -> normalized ``Quote``.

Pure function over an already-fetched payload (no network). ``fetch_raw`` is
the only network code and is injected into ingestion by the caller.
"""
from __future__ import annotations

from typing import Any, Optional

from .contracts import (AdjustmentMode, DataKind, DataQuality, Field, IngestionMeta, Quote,
                        TimestampSemantics)
from .semantics import parse_epoch_seconds

PROVIDER = 'yahoo-chart'
_URL = 'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}'


class UnusablePayload(ValueError):
    """Provider returned an error/empty payload (counts as a provider failure)."""


def _num(v) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float('inf'), float('-inf')) else None


def adapt_quote(payload: dict[str, Any], *, now_ts: float, ingested_at: Optional[float] = None) -> tuple[Quote, str]:
    """Return ``(Quote, provider_venue_label)``; raises ``UnusablePayload``."""
    chart = (payload or {}).get('chart') or {}
    if chart.get('error'):
        raise UnusablePayload(f"provider error: {(chart['error'] or {}).get('code')}")
    results = chart.get('result') or []
    if not results or not isinstance(results[0], dict) or not isinstance(results[0].get('meta'), dict):
        raise UnusablePayload('no result')
    meta = results[0]['meta']
    symbol = str(meta.get('symbol') or '').strip()
    venue_label = str(meta.get('exchangeName') or '').strip()
    if not symbol:
        raise UnusablePayload('no symbol')
    src_ts, sem = parse_epoch_seconds(meta.get('regularMarketTime'), now_ts)

    price = _num(meta.get('regularMarketPrice'))
    if price is None:
        pf: Field = Field.unavailable('regularMarketPrice missing')
    elif price < 0:
        pf = Field.unavailable('negative price rejected')
    else:
        pf = Field.of(price)  # 0 -> ZERO (kept distinct from missing)
    vol = _num(meta.get('regularMarketVolume'))
    vf = Field.unavailable('regularMarketVolume missing') if vol is None or vol < 0 else Field.of(vol)

    delay = _num(meta.get('exchangeDataDelayedBy'))
    quality = DataQuality.DELAYED if delay and delay > 0 else DataQuality.UNKNOWN
    cur = str(meta.get('currency') or '').strip().upper() or None
    im = IngestionMeta(provider=PROVIDER, kind=DataKind.QUOTE,
                       ingested_at=now_ts if ingested_at is None else ingested_at,
                       source_ts=src_ts, timestamp_semantics=sem, interval=None,
                       adjustment=AdjustmentMode.RAW, currency=cur, venue=None, quality=quality,
                       source_revision=None)
    return Quote(im, symbol, pf, vf), venue_label


def fetch_raw(symbol: str) -> dict:
    """Live fetch of the raw chart payload (network). Never called by GET routes."""
    from app.services.http_client import ResilientGetConfig, resilient_get
    cfg = ResilientGetConfig(max_attempts=1, connect_timeout=3.0, read_timeout=6.0)
    res = resilient_get(_URL.format(symbol=symbol), params={'range': '5d', 'interval': '1d'},
                        headers={'Accept': 'application/json'}, cfg=cfg)
    if res.status_code != 200:
        raise RuntimeError(f'HTTP {res.status_code} {res.error or ""}'.strip())
    return res.json() or {}
