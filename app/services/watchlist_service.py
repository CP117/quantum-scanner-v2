"""
Watchlist Service — Phase 28
=============================

Manages user-defined symbol watchlists.

Persistence
-----------
SQLite database at the path defined in ``settings.watchlist_db_path``
(default ``data/watchlists.db``).

Schema
------
    user_watchlists(
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     TEXT    NOT NULL DEFAULT 'default',
        name        TEXT    NOT NULL,
        created_at  TEXT    NOT NULL,
        symbols_json TEXT   NOT NULL DEFAULT '[]'
    )

A "pin" is implemented as a special watchlist named ``__pinned__`` combined
with a call to ``tier_manager.pin_symbol()`` / ``unpin_symbol()``.

Public API
----------
    ``create_watchlist(name, user_id) -> dict``
    ``delete_watchlist(watchlist_id, user_id) -> bool``
    ``list_watchlists(user_id) -> list[dict]``
    ``add_symbol(watchlist_id, symbol, user_id) -> dict``
    ``remove_symbol(watchlist_id, symbol, user_id) -> dict``
    ``get_symbols(watchlist_id, user_id) -> list[str]``
    ``pin_symbol(symbol, user_id) -> dict``
    ``unpin_symbol(symbol, user_id) -> dict``
    ``get_pinned_symbols(user_id) -> list[str]``
    ``is_pinned(symbol, user_id) -> bool``
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Optional

from app.utils.time import utcnow_iso

log = logging.getLogger('app.watchlist_service')

_db_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _db_path() -> Path:
    try:
        from app.config import settings
        p = Path(settings.watchlist_db_path)
    except Exception:  # noqa: BLE001
        p = Path('data/watchlists.db')
    if not p.is_absolute():
        p = Path(__file__).resolve().parent.parent.parent / p
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is not None:
        return _conn
    db = _db_path()
    conn = sqlite3.connect(str(db), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    _ensure_schema(conn)
    _conn = conn
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS user_watchlists (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      TEXT    NOT NULL DEFAULT 'default',
            name         TEXT    NOT NULL,
            created_at   TEXT    NOT NULL,
            symbols_json TEXT    NOT NULL DEFAULT '[]'
        );
        CREATE INDEX IF NOT EXISTS idx_wl_user ON user_watchlists (user_id);
    """)
    conn.commit()


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    try:
        d['symbols'] = json.loads(d.get('symbols_json') or '[]')
    except Exception:  # noqa: BLE001
        d['symbols'] = []
    return d


# ---------------------------------------------------------------------------
# Watchlist CRUD
# ---------------------------------------------------------------------------

def create_watchlist(name: str, user_id: str = 'default') -> dict:
    """Create a new watchlist and return its record."""
    if not name or not name.strip():
        raise ValueError('Watchlist name must not be empty')
    now = utcnow_iso()
    with _db_lock:
        conn = _get_conn()
        cur = conn.execute(
            'INSERT INTO user_watchlists (user_id, name, created_at, symbols_json) VALUES (?, ?, ?, ?)',
            (user_id, name.strip(), now, '[]'),
        )
        conn.commit()
        row = conn.execute(
            'SELECT * FROM user_watchlists WHERE id = ?', (cur.lastrowid,)
        ).fetchone()
    return _row_to_dict(row)


def delete_watchlist(watchlist_id: int, user_id: str = 'default') -> bool:
    """Delete a watchlist.  Returns True if deleted, False if not found."""
    with _db_lock:
        conn = _get_conn()
        cur = conn.execute(
            'DELETE FROM user_watchlists WHERE id = ? AND user_id = ?',
            (watchlist_id, user_id),
        )
        conn.commit()
    return cur.rowcount > 0


def list_watchlists(user_id: str = 'default') -> list[dict]:
    """Return all watchlists for *user_id* (excluding internal __pinned__)."""
    with _db_lock:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT * FROM user_watchlists WHERE user_id = ? AND name != '__pinned__' ORDER BY id",
            (user_id,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def _get_watchlist(watchlist_id: int, user_id: str) -> Optional[dict]:
    with _db_lock:
        conn = _get_conn()
        row = conn.execute(
            'SELECT * FROM user_watchlists WHERE id = ? AND user_id = ?',
            (watchlist_id, user_id),
        ).fetchone()
    return _row_to_dict(row) if row else None


def add_symbol(watchlist_id: int, symbol: str, user_id: str = 'default') -> dict:
    """Add *symbol* to watchlist *watchlist_id*.  Returns updated watchlist."""
    sym = symbol.upper().strip()
    if not sym:
        raise ValueError('Symbol must not be empty')
    with _db_lock:
        conn = _get_conn()
        row = conn.execute(
            'SELECT * FROM user_watchlists WHERE id = ? AND user_id = ?',
            (watchlist_id, user_id),
        ).fetchone()
        if row is None:
            raise ValueError(f'Watchlist {watchlist_id} not found')
        symbols: list[str] = json.loads(row['symbols_json'] or '[]')
        if sym not in symbols:
            symbols.append(sym)
        conn.execute(
            'UPDATE user_watchlists SET symbols_json = ? WHERE id = ?',
            (json.dumps(symbols), watchlist_id),
        )
        conn.commit()
        updated = conn.execute(
            'SELECT * FROM user_watchlists WHERE id = ?', (watchlist_id,)
        ).fetchone()
    result = _row_to_dict(updated)
    # Symbols in watchlists are pinned to minimum Tier 2.
    try:
        from app.services import tier_manager
        tier_manager.pin_symbol(sym)
    except Exception:  # noqa: BLE001
        pass
    return result


def remove_symbol(watchlist_id: int, symbol: str, user_id: str = 'default') -> dict:
    """Remove *symbol* from watchlist *watchlist_id*.  Returns updated watchlist."""
    sym = symbol.upper().strip()
    with _db_lock:
        conn = _get_conn()
        row = conn.execute(
            'SELECT * FROM user_watchlists WHERE id = ? AND user_id = ?',
            (watchlist_id, user_id),
        ).fetchone()
        if row is None:
            raise ValueError(f'Watchlist {watchlist_id} not found')
        symbols: list[str] = json.loads(row['symbols_json'] or '[]')
        symbols = [s for s in symbols if s != sym]
        conn.execute(
            'UPDATE user_watchlists SET symbols_json = ? WHERE id = ?',
            (json.dumps(symbols), watchlist_id),
        )
        conn.commit()
        updated = conn.execute(
            'SELECT * FROM user_watchlists WHERE id = ?', (watchlist_id,)
        ).fetchone()
    result = _row_to_dict(updated)
    # Check if this symbol is still in ANY other watchlist — if not, unpin.
    _maybe_unpin(sym, user_id)
    return result


def get_symbols(watchlist_id: int, user_id: str = 'default') -> list[str]:
    """Return the list of symbols in watchlist *watchlist_id*."""
    wl = _get_watchlist(watchlist_id, user_id)
    if wl is None:
        raise ValueError(f'Watchlist {watchlist_id} not found')
    return wl.get('symbols') or []


def _maybe_unpin(symbol: str, user_id: str) -> None:
    """Unpin *symbol* if it doesn't appear in any watchlist for *user_id*."""
    sym = symbol.upper()
    with _db_lock:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT symbols_json FROM user_watchlists WHERE user_id = ?",
            (user_id,),
        ).fetchall()
    still_in_wl = any(sym in json.loads(r['symbols_json'] or '[]') for r in rows)
    # Only unpin from explicit-pin watchlist if not in any watchlist
    if not still_in_wl:
        try:
            from app.services import tier_manager
            tier_manager.unpin_symbol(sym)
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Pin / Unpin convenience (not tied to a named watchlist)
# ---------------------------------------------------------------------------

def pin_symbol(symbol: str, user_id: str = 'default') -> dict:
    """Pin *symbol* to Tier 2 minimum.  Creates/updates the __pinned__ record."""
    sym = symbol.upper().strip()
    _upsert_pinned_watchlist(sym, user_id, add=True)
    try:
        from app.services import tier_manager
        tier_manager.pin_symbol(sym)
    except Exception:  # noqa: BLE001
        pass
    return {'symbol': sym, 'pinned': True}


def unpin_symbol(symbol: str, user_id: str = 'default') -> dict:
    """Unpin *symbol* (allow future demotion to Tier 3)."""
    sym = symbol.upper().strip()
    _upsert_pinned_watchlist(sym, user_id, add=False)
    try:
        from app.services import tier_manager
        tier_manager.unpin_symbol(sym)
    except Exception:  # noqa: BLE001
        pass
    return {'symbol': sym, 'pinned': False}


def get_pinned_symbols(user_id: str = 'default') -> list[str]:
    """Return all symbols currently pinned by *user_id*."""
    with _db_lock:
        conn = _get_conn()
        row = conn.execute(
            "SELECT symbols_json FROM user_watchlists WHERE user_id = ? AND name = '__pinned__'",
            (user_id,),
        ).fetchone()
    if row is None:
        return []
    try:
        return json.loads(row['symbols_json'] or '[]')
    except Exception:  # noqa: BLE001
        return []


def is_pinned(symbol: str, user_id: str = 'default') -> bool:
    """Return True if *symbol* is pinned by *user_id*."""
    sym = symbol.upper()
    return sym in get_pinned_symbols(user_id)


def _upsert_pinned_watchlist(symbol: str, user_id: str, add: bool) -> None:
    """Add or remove *symbol* from the internal __pinned__ watchlist row."""
    with _db_lock:
        conn = _get_conn()
        row = conn.execute(
            "SELECT * FROM user_watchlists WHERE user_id = ? AND name = '__pinned__'",
            (user_id,),
        ).fetchone()
        if row is None:
            symbols: list[str] = []
            now = utcnow_iso()
            conn.execute(
                "INSERT INTO user_watchlists (user_id, name, created_at, symbols_json) VALUES (?, '__pinned__', ?, ?)",
                (user_id, now, '[]'),
            )
            row_id = conn.execute(
                "SELECT id FROM user_watchlists WHERE user_id = ? AND name = '__pinned__'",
                (user_id,),
            ).fetchone()['id']
        else:
            symbols = json.loads(row['symbols_json'] or '[]')
            row_id = row['id']
        if add and symbol not in symbols:
            symbols.append(symbol)
        elif not add:
            symbols = [s for s in symbols if s != symbol]
        conn.execute(
            'UPDATE user_watchlists SET symbols_json = ? WHERE id = ?',
            (json.dumps(symbols), row_id),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Bootstrap pinned symbols into the tier manager on startup
# ---------------------------------------------------------------------------

def restore_pins_to_tier_manager() -> None:
    """Restore persisted pins into the tier_manager after startup."""
    try:
        from app.services import tier_manager
        with _db_lock:
            conn = _get_conn()
            rows = conn.execute(
                "SELECT symbols_json FROM user_watchlists WHERE name = '__pinned__'"
            ).fetchall()
        for row in rows:
            syms = json.loads(row['symbols_json'] or '[]')
            for sym in syms:
                tier_manager.pin_symbol(sym)
        log.info('watchlist_service: restored %d pinned symbols to tier_manager',
                 sum(len(json.loads(r['symbols_json'] or '[]')) for r in rows))
    except Exception:  # noqa: BLE001
        log.warning('watchlist_service: failed to restore pins', exc_info=True)
