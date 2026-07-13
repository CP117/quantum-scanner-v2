"""Rolling log of `/share/{symbol}` page hits — feeds the Shared Analyses
gallery. Each entry captures the symbol + a compact metric snapshot at
share-time so the gallery card can render even if the underlying row has
churned out of the live snapshot store.

Dedupe by symbol within `_DEDUPE_WINDOW_SECONDS`: repeated scrapes (FB
usually hits the URL 3-5 times in the first minute) collapse into a
single gallery entry with `hit_count` incremented.

Storage is a single JSON file (`data/share_events.json`) capped at
`_MAX_ENTRIES` most-recent unique symbols. Losing this file is a
tolerable outcome — it just resets the gallery.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger('app.share_gallery')

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_STORE_PATH = _REPO_ROOT / 'data' / 'share_events.json'
_MAX_ENTRIES = 200
_DEDUPE_WINDOW_SECONDS = 6 * 3600  # 6h — one entry per symbol per 6h window

_lock = threading.Lock()
_entries: list[dict[str, Any]] = []
_loaded = False


def _load_if_needed() -> None:
    global _loaded  # noqa: PLW0603
    if _loaded:
        return
    with _lock:
        if _loaded:
            return
        try:
            if _STORE_PATH.exists():
                raw = json.loads(_STORE_PATH.read_text(encoding='utf-8'))
                if isinstance(raw, list):
                    _entries.clear()
                    _entries.extend(raw[-_MAX_ENTRIES:])
        except Exception as exc:  # noqa: BLE001
            log.warning('share_gallery load failed: %s', exc)
        _loaded = True


def _persist_locked() -> None:
    """Caller MUST hold _lock."""
    try:
        _STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STORE_PATH.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(_entries[-_MAX_ENTRIES:],
                                  separators=(',', ':')), encoding='utf-8')
        tmp.replace(_STORE_PATH)
    except Exception as exc:  # noqa: BLE001
        log.warning('share_gallery persist failed: %s', exc)


def _num(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(default)


def _snapshot_from_row(row: dict | None) -> dict[str, Any]:
    """Compact metric snapshot for the gallery card — must survive the
    row aging out of the live snapshot store."""
    if not row:
        return {}
    fm_all = row.get('forward_metrics_garch') or row.get('forward_metrics') or {}
    blk = fm_all.get('forward_1d') or {}
    fc = fm_all.get('forecast_context') or {}
    market = (row.get('factor_breakdown') or {}).get('market') or {}
    ssp = market.get('short_selling_pressure') or {}
    pvi = market.get('predicted_volume_intensity') or {}
    oe = market.get('options_expiration') or {}
    return {
        'name':          row.get('name') or '',
        'exchange':      row.get('exchange') or '',
        'final_score':   _num(row.get('final_score')),
        'tier':          row.get('tier') or '?',
        'final_direction': row.get('final_direction') or 'Neutral',
        'p_up':          _num(blk.get('p_up_cf', blk.get('p_up', 0.5)), 0.5),
        'p_up_ctx':      _num(blk.get('p_up_ctx'), None) if blk.get('p_up_ctx') is not None else None,
        'kelly':         _num(blk.get('effective_kelly_rank'), None) if blk.get('effective_kelly_rank') is not None else None,
        'squeeze':       _num(fc.get('squeeze_probability'), 0.0),
        'vol_event':     _num(fc.get('volatility_event_probability'), 0.0),
        'ssp_score':     _num(ssp.get('score'), None) if ssp.get('score') is not None else None,
        'ssp_label':     ssp.get('label') or 'neutral',
        'pvi_score':     _num(pvi.get('score'), None) if pvi.get('score') is not None else None,
        'pvi_bucket':    pvi.get('bucket') or 'low',
        'dte':           oe.get('days_to_expiration'),
        'nearest_exp':   oe.get('nearest_expiration'),
    }


def record_share(symbol: str, row: dict | None,
                 ua: str | None = None, referer: str | None = None,
                 preset: str | None = None) -> None:
    """Record (or increment) a share event for `symbol`. Deduped within
    `_DEDUPE_WINDOW_SECONDS` — repeated scrapes collapse to a single
    entry with `hit_count += 1`.
    """
    symbol = (symbol or '').upper().strip()
    if not symbol:
        return
    _load_if_needed()
    now = time.time()
    with _lock:
        # Look for a recent entry for this symbol to dedupe against.
        for entry in reversed(_entries):
            if entry.get('symbol') == symbol and (now - float(entry.get('ts', 0) or 0)) < _DEDUPE_WINDOW_SECONDS:
                entry['hit_count'] = int(entry.get('hit_count', 1)) + 1
                entry['ts_last'] = now
                entry['iso_last'] = datetime.now(timezone.utc).isoformat()
                # Refresh snapshot only if we have a better row than before.
                snap = _snapshot_from_row(row)
                if snap and snap.get('final_score'):
                    entry['snapshot'] = snap
                _persist_locked()
                return
        # New entry
        new_entry = {
            'symbol':    symbol,
            'ts':        now,
            'ts_last':   now,
            'iso':       datetime.now(timezone.utc).isoformat(),
            'iso_last':  datetime.now(timezone.utc).isoformat(),
            'hit_count': 1,
            'preset':    preset or None,
            'ua_hint':   _classify_scraper(ua),
            'snapshot':  _snapshot_from_row(row),
        }
        _entries.append(new_entry)
        if len(_entries) > _MAX_ENTRIES:
            del _entries[:len(_entries) - _MAX_ENTRIES]
        _persist_locked()


def _classify_scraper(ua: str | None) -> str | None:
    if not ua:
        return None
    ua_lo = ua.lower()
    if 'facebookexternalhit' in ua_lo or 'facebot' in ua_lo:
        return 'facebook'
    if 'linkedinbot' in ua_lo:
        return 'linkedin'
    if 'twitterbot' in ua_lo:
        return 'twitter'
    if 'slackbot' in ua_lo:
        return 'slack'
    if 'discordbot' in ua_lo:
        return 'discord'
    if 'whatsapp' in ua_lo:
        return 'whatsapp'
    if 'telegram' in ua_lo:
        return 'telegram'
    if 'redditbot' in ua_lo:
        return 'reddit'
    return None


def get_recent_shares(limit: int = 50) -> list[dict[str, Any]]:
    """Return the most-recent share events, newest first."""
    _load_if_needed()
    limit = max(1, min(200, int(limit or 50)))
    with _lock:
        # newest first
        out = sorted(_entries, key=lambda e: float(e.get('ts_last', e.get('ts', 0)) or 0), reverse=True)
    return out[:limit]


def clear_gallery() -> int:
    """Admin helper — wipe the gallery. Returns entries removed."""
    _load_if_needed()
    with _lock:
        n = len(_entries)
        _entries.clear()
        _persist_locked()
    return n
