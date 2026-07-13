"""
User-added symbols store + canonical NASDAQ library merger.

Goals:
  * Let an operator add ANY ticker to the universe at runtime.
  * Persist user-added tickers to disk so they survive restarts.
  * On startup (or on demand), pull the canonical NASDAQ listing from
    nasdaqtrader.com and merge any symbols we don't already have.
  * Never raise.  Network failures degrade silently and the cached universe
    is used as-is.
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger('app.universe_extras')

_DATA_DIR = Path(__file__).resolve().parent.parent.parent / 'data'
_USER_ADDED_PATH = _DATA_DIR / 'user_added_symbols.json'
_NASDAQ_CACHE_PATH = _DATA_DIR / 'nasdaq_full_listing.json'
_NASDAQ_LISTED_URL = 'https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt'
_NASDAQ_OTHER_URL = 'https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt'
_HEADERS = {'User-Agent': 'MarketRefinementDashboard/1.0'}
_TIMEOUT = 8.0

_lock = threading.Lock()


# ---------------------------------------------------------------------------
# User-added symbols persistence
# ---------------------------------------------------------------------------

def _read_user_added() -> list[dict]:
    if not _USER_ADDED_PATH.exists():
        return []
    try:
        return json.loads(_USER_ADDED_PATH.read_text(encoding='utf-8')) or []
    except Exception as exc:  # noqa: BLE001
        log.warning('failed to read user_added_symbols.json: %s', exc)
        return []


def _write_user_added(rows: list[dict]) -> None:
    try:
        _USER_ADDED_PATH.write_text(json.dumps(rows, indent=2), encoding='utf-8')
    except Exception as exc:  # noqa: BLE001
        log.warning('failed to write user_added_symbols.json: %s', exc)


def get_user_added() -> list[dict]:
    with _lock:
        return list(_read_user_added())


def add_user_symbol(symbol: str, name: str | None = None, exchange: str | None = None) -> dict:
    """Add a symbol to the persistent user list (idempotent)."""
    sym = (symbol or '').strip().upper()
    if not sym:
        return {'ok': False, 'reason': 'empty_symbol'}
    if len(sym) > 20:
        return {'ok': False, 'reason': 'symbol_too_long'}
    with _lock:
        existing = _read_user_added()
        for row in existing:
            if row.get('symbol', '').upper() == sym:
                return {'ok': True, 'reason': 'already_present', 'row': row}
        row = {
            'symbol': sym,
            'name': (name or sym).strip(),
            'exchange': (exchange or 'USER_ADDED').strip(),
            'user_added': True,
        }
        existing.append(row)
        _write_user_added(existing)
    # Bust the cached universe so the new symbol shows up on next read.
    try:
        from app.services.universe_service import bust_universe_cache
        bust_universe_cache()
    except Exception:
        pass
    return {'ok': True, 'reason': 'added', 'row': row}


def remove_user_symbol(symbol: str) -> dict:
    sym = (symbol or '').strip().upper()
    if not sym:
        return {'ok': False, 'reason': 'empty_symbol'}
    with _lock:
        existing = _read_user_added()
        new_list = [r for r in existing if r.get('symbol', '').upper() != sym]
        if len(new_list) == len(existing):
            return {'ok': False, 'reason': 'not_found'}
        _write_user_added(new_list)
    try:
        from app.services.universe_service import bust_universe_cache
        bust_universe_cache()
    except Exception:
        pass
    return {'ok': True, 'reason': 'removed', 'symbol': sym}


# ---------------------------------------------------------------------------
# NASDAQ canonical listing merger
# ---------------------------------------------------------------------------

def _parse_nasdaq_pipe(text: str, symbol_col: str, name_col: str, exchange_col: str | None = None,
                       fallback_exchange: str = 'NASDAQ') -> list[dict]:
    lines = (text or '').splitlines()
    if not lines or len(lines) < 2:
        return []
    header = lines[0].split('|')
    out: list[dict] = []
    try:
        sym_idx = header.index(symbol_col)
        name_idx = header.index(name_col)
        exch_idx = header.index(exchange_col) if exchange_col and exchange_col in header else None
    except ValueError:
        return []
    for line in lines[1:]:
        if not line or line.startswith('File Creation Time'):
            continue
        parts = line.split('|')
        if len(parts) <= sym_idx:
            continue
        sym = (parts[sym_idx] or '').strip().upper()
        name = (parts[name_idx] or '').strip() if len(parts) > name_idx else sym
        if not sym or '$' in sym or '.' in sym or '/' in sym:
            continue
        exch = fallback_exchange
        if exch_idx is not None and len(parts) > exch_idx:
            ex_code = (parts[exch_idx] or '').strip().upper()
            exch_map = {'N': 'NYSE', 'A': 'NYSE American', 'P': 'NYSE Arca', 'Z': 'BATS',
                        'V': 'IEX', 'Q': 'NASDAQ'}
            exch = exch_map.get(ex_code, fallback_exchange)
        out.append({'symbol': sym, 'name': name, 'exchange': exch})
    return out


def refresh_nasdaq_listing(force: bool = False) -> dict:
    """Download nasdaqtrader's canonical listings and cache merged result.

    Returns a small status payload.  Never raises.
    """
    if _NASDAQ_CACHE_PATH.exists() and not force:
        try:
            payload = json.loads(_NASDAQ_CACHE_PATH.read_text(encoding='utf-8'))
            if payload.get('rows'):
                return {'ok': True, 'cached': True, 'count': len(payload['rows'])}
        except Exception:
            pass

    merged: dict[str, dict] = {}
    sources_ok: list[str] = []
    sources_failed: list[str] = []
    try:
        res = requests.get(_NASDAQ_LISTED_URL, headers=_HEADERS, timeout=_TIMEOUT)
        if res.status_code == 200:
            rows = _parse_nasdaq_pipe(res.text, 'Symbol', 'Security Name',
                                       exchange_col=None, fallback_exchange='NASDAQ')
            for r in rows:
                merged[r['symbol']] = r
            sources_ok.append(f'nasdaqlisted({len(rows)})')
        else:
            sources_failed.append(f'nasdaqlisted:status={res.status_code}')
    except Exception as exc:
        sources_failed.append(f'nasdaqlisted:{type(exc).__name__}')
    try:
        res = requests.get(_NASDAQ_OTHER_URL, headers=_HEADERS, timeout=_TIMEOUT)
        if res.status_code == 200:
            rows = _parse_nasdaq_pipe(res.text, 'ACT Symbol', 'Security Name',
                                       exchange_col='Exchange', fallback_exchange='NYSE')
            for r in rows:
                merged.setdefault(r['symbol'], r)
            sources_ok.append(f'otherlisted({len(rows)})')
        else:
            sources_failed.append(f'otherlisted:status={res.status_code}')
    except Exception as exc:
        sources_failed.append(f'otherlisted:{type(exc).__name__}')

    if not merged:
        return {'ok': False, 'cached': False, 'count': 0,
                'sources_ok': sources_ok, 'sources_failed': sources_failed}

    rows = sorted(merged.values(), key=lambda r: r['symbol'])
    payload = {'rows': rows, 'sources_ok': sources_ok}
    try:
        _NASDAQ_CACHE_PATH.write_text(json.dumps(payload), encoding='utf-8')
    except Exception as exc:
        log.debug('failed to write nasdaq cache: %s', exc)
    return {'ok': True, 'cached': False, 'count': len(rows),
            'sources_ok': sources_ok, 'sources_failed': sources_failed}


def get_nasdaq_listing_rows() -> list[dict]:
    if not _NASDAQ_CACHE_PATH.exists():
        return []
    try:
        payload = json.loads(_NASDAQ_CACHE_PATH.read_text(encoding='utf-8'))
        return list(payload.get('rows') or [])
    except Exception:
        return []


def refresh_nasdaq_in_background() -> None:
    """Fire-and-forget background refresh (called from app startup)."""
    def _run():
        try:
            refresh_nasdaq_listing(force=False)
        except Exception:
            pass
    threading.Thread(target=_run, daemon=True, name='nasdaq-refresh').start()
