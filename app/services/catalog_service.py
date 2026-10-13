"""
Canonical durable instrument catalog (Phase 2 foundation).

The catalog is independent of provider success and of analysis completion: a
row exists once a provider listing has reported it, and it is never deleted
because a provider later omits it.

Entities (SQLite, ``settings.catalog_db_path``; additive, no legacy tables
are touched)::

    venue, instrument, provider_instrument_mapping,
    universe_membership, catalog_sync_run

Identity
--------
``instrument_id`` is derived from the natural key
``(asset_type, venue, native_symbol)`` — never from a bare ticker.  Hence
the same ticker on two venues, stock vs. crypto, and crypto pairs on
different exchanges all get distinct IDs.  Equity share classes keep the
class in the native symbol (``BRK.B`` vs ``BRK.A``).  Crypto records carry
``quote_symbol``; an aggregated asset (no quote, venue ``AGG``) is a
different identity from any exchange pair.

Status transitions (never a delete)::

    active --(omitted from a complete listing)--> missing
    missing --(omitted ``MISSING_GRACE_RUNS`` times in a row)--> inactive
    missing/inactive --(reported again)--> active
    any --(explicit set_instrument_status)--> suspended | delisted | ...

``suspended`` and ``delisted`` are only changed explicitly, never by sync.
Partial listings (``complete=False``) never mark anything missing.

Known follow-on work: ticker/symbol changes (a rename currently yields a new
instrument; relinking history is not implemented), wiring existing
universe_service lists into ``sync_listing``, and a Postgres port.
"""
from __future__ import annotations

import hashlib
import sqlite3
import threading
from pathlib import Path
from typing import Iterable, Optional

from app.utils.time import utcnow_iso

MISSING_GRACE_RUNS = 3
ASSET_TYPES = ('stock', 'etf', 'crypto_pair', 'crypto_asset')
STATUSES = ('active', 'missing', 'inactive', 'suspended', 'delisted')
_EXPLICIT_ONLY = ('suspended', 'delisted')

_write_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS venue (
    venue_id   TEXT PRIMARY KEY,
    kind       TEXT NOT NULL DEFAULT 'unknown',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS instrument (
    instrument_id     TEXT PRIMARY KEY,
    asset_type        TEXT NOT NULL,
    venue_id          TEXT NOT NULL REFERENCES venue(venue_id),
    native_symbol     TEXT NOT NULL,
    display_ticker    TEXT NOT NULL,
    quote_symbol      TEXT,
    share_class       TEXT,
    name              TEXT,
    status            TEXT NOT NULL DEFAULT 'active',
    status_changed_at TEXT NOT NULL,
    first_seen_at     TEXT NOT NULL,
    last_seen_at      TEXT NOT NULL,
    UNIQUE (asset_type, venue_id, native_symbol)
);
CREATE INDEX IF NOT EXISTS idx_instrument_ticker ON instrument (display_ticker);
CREATE INDEX IF NOT EXISTS idx_instrument_status ON instrument (status);
CREATE TABLE IF NOT EXISTS provider_instrument_mapping (
    provider        TEXT NOT NULL,
    provider_symbol TEXT NOT NULL,
    instrument_id   TEXT NOT NULL REFERENCES instrument(instrument_id),
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    PRIMARY KEY (provider, provider_symbol, instrument_id)
);
CREATE INDEX IF NOT EXISTS idx_pim_instrument ON provider_instrument_mapping (instrument_id);
CREATE TABLE IF NOT EXISTS universe_membership (
    universe_key  TEXT NOT NULL,
    instrument_id TEXT NOT NULL REFERENCES instrument(instrument_id),
    provider      TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active',
    miss_count    INTEGER NOT NULL DEFAULT 0,
    added_at      TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    PRIMARY KEY (universe_key, instrument_id)
);
CREATE TABLE IF NOT EXISTS catalog_sync_run (
    run_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    provider       TEXT NOT NULL,
    universe_key   TEXT NOT NULL,
    complete       INTEGER NOT NULL,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    status         TEXT NOT NULL DEFAULT 'running',
    seen           INTEGER NOT NULL DEFAULT 0,
    inserted       INTEGER NOT NULL DEFAULT 0,
    updated        INTEGER NOT NULL DEFAULT 0,
    marked_missing INTEGER NOT NULL DEFAULT 0,
    reactivated    INTEGER NOT NULL DEFAULT 0,
    error          TEXT
);
"""


def _db_path() -> Path:
    try:
        from app.config import settings
        p = Path(settings.catalog_db_path)
    except Exception:  # noqa: BLE001
        p = Path('data/catalog.db')
    if not p.is_absolute():
        p = Path(__file__).resolve().parent.parent.parent / p
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def connect(path: Optional[str] = None) -> sqlite3.Connection:
    """Open a catalog connection (creates schema; additive only)."""
    conn = sqlite3.connect(str(path or _db_path()), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def make_instrument_id(asset_type: str, venue: str, native_symbol: str) -> str:
    """Stable ID from the natural key; independent of ticker display."""
    key = f'{asset_type}|{venue.upper()}|{native_symbol.upper()}'
    return 'ins_' + hashlib.sha1(key.encode('utf-8')).hexdigest()[:16]


def _normalize(rec: dict) -> dict:
    asset_type = str(rec.get('asset_type') or '').strip().lower()
    if asset_type not in ASSET_TYPES:
        raise ValueError(f'unsupported asset_type: {asset_type!r}')
    venue = str(rec.get('venue') or '').strip().upper()
    symbol = str(rec.get('symbol') or '').strip().upper()
    if not venue or not symbol:
        raise ValueError('record requires venue and symbol')
    quote = str(rec.get('quote_symbol') or '').strip().upper() or None
    if asset_type == 'crypto_pair' and not quote:
        raise ValueError('crypto_pair requires quote_symbol')
    if asset_type == 'crypto_asset' and quote:
        raise ValueError('crypto_asset must not have quote_symbol')
    native = f'{symbol}-{quote}' if quote else symbol
    return {
        'asset_type': asset_type, 'venue': venue, 'native': native,
        'display': symbol, 'quote': quote,
        'share_class': (str(rec['share_class']).strip().upper() or None) if rec.get('share_class') else None,
        'name': rec.get('name'),
        'provider_symbol': str(rec.get('provider_symbol') or native).strip(),
        'venue_kind': 'crypto' if asset_type.startswith('crypto') else 'equity',
    }


def _set_status(conn, instrument_id: str, status: str, now: str) -> None:
    conn.execute(
        'UPDATE instrument SET status=?, status_changed_at=? WHERE instrument_id=? AND status<>?',
        (status, now, instrument_id, status))


def _recompute_status(conn, instrument_id: str, now: str) -> Optional[str]:
    """Derive instrument status from memberships; explicit statuses stick."""
    row = conn.execute('SELECT status FROM instrument WHERE instrument_id=?', (instrument_id,)).fetchone()
    if row is None or row['status'] in _EXPLICIT_ONLY:
        return None
    states = {r['status'] for r in conn.execute(
        'SELECT status FROM universe_membership WHERE instrument_id=?', (instrument_id,))}
    new = 'active' if 'active' in states else ('missing' if 'missing' in states else 'inactive')
    if new != row['status']:
        _set_status(conn, instrument_id, new, now)
        return new
    return None


def sync_listing(conn: sqlite3.Connection, provider: str, universe_key: str,
                 records: Iterable[dict], complete: bool = True) -> dict:
    """Idempotently merge one provider listing into the catalog.

    Re-running with identical input changes no instrument/mapping/membership
    fields other than ``last_seen_at``.  Omissions only affect memberships of
    the same ``(provider, universe_key)`` and only when ``complete`` is true.
    Returns the run summary (also persisted in ``catalog_sync_run``).
    """
    normalized = [_normalize(r) for r in records]  # validate before any write
    now = utcnow_iso()
    with _write_lock:
        run_id = conn.execute(
            'INSERT INTO catalog_sync_run (provider, universe_key, complete, started_at) VALUES (?,?,?,?)',
            (provider, universe_key, 1 if complete else 0, now)).lastrowid
        conn.commit()
        stats = {'seen': 0, 'inserted': 0, 'updated': 0, 'marked_missing': 0, 'reactivated': 0}
        try:
            seen_ids = set()
            for n in normalized:
                iid = make_instrument_id(n['asset_type'], n['venue'], n['native'])
                if iid in seen_ids:
                    continue
                seen_ids.add(iid)
                stats['seen'] += 1
                conn.execute('INSERT OR IGNORE INTO venue (venue_id, kind, created_at) VALUES (?,?,?)',
                             (n['venue'], n['venue_kind'], now))
                cur = conn.execute(
                    'INSERT OR IGNORE INTO instrument (instrument_id, asset_type, venue_id, native_symbol,'
                    ' display_ticker, quote_symbol, share_class, name, status, status_changed_at,'
                    " first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?, 'active', ?,?,?)",
                    (iid, n['asset_type'], n['venue'], n['native'], n['display'], n['quote'],
                     n['share_class'], n['name'], now, now, now))
                if cur.rowcount:
                    stats['inserted'] += 1
                else:
                    before = conn.execute('SELECT name, share_class FROM instrument WHERE instrument_id=?',
                                          (iid,)).fetchone()
                    if (n['name'] and n['name'] != before['name']) or \
                            (n['share_class'] and n['share_class'] != before['share_class']):
                        stats['updated'] += 1
                    conn.execute(
                        'UPDATE instrument SET name=COALESCE(?, name), share_class=COALESCE(?, share_class),'
                        ' last_seen_at=? WHERE instrument_id=?',
                        (n['name'], n['share_class'], now, iid))
                conn.execute(
                    'INSERT INTO provider_instrument_mapping (provider, provider_symbol, instrument_id,'
                    ' first_seen_at, last_seen_at) VALUES (?,?,?,?,?)'
                    ' ON CONFLICT(provider, provider_symbol, instrument_id) DO UPDATE SET last_seen_at=excluded.last_seen_at',
                    (provider, n['provider_symbol'], iid, now, now))
                prev = conn.execute(
                    'SELECT status FROM universe_membership WHERE universe_key=? AND instrument_id=?',
                    (universe_key, iid)).fetchone()
                if prev is not None and prev['status'] != 'active':
                    stats['reactivated'] += 1
                conn.execute(
                    'INSERT INTO universe_membership (universe_key, instrument_id, provider, status,'
                    " miss_count, added_at, last_seen_at) VALUES (?,?,?, 'active', 0, ?, ?)"
                    " ON CONFLICT(universe_key, instrument_id) DO UPDATE SET status='active',"
                    ' miss_count=0, provider=excluded.provider, last_seen_at=excluded.last_seen_at',
                    (universe_key, iid, provider, now, now))
                _recompute_status(conn, iid, now)
            touched = set(seen_ids)
            if complete:
                for m in conn.execute(
                        'SELECT instrument_id, miss_count FROM universe_membership'
                        ' WHERE universe_key=? AND provider=? AND status<>?',
                        (universe_key, provider, 'inactive')).fetchall():
                    if m['instrument_id'] in seen_ids:
                        continue
                    misses = m['miss_count'] + 1
                    state = 'inactive' if misses >= MISSING_GRACE_RUNS else 'missing'
                    conn.execute(
                        'UPDATE universe_membership SET miss_count=?, status=? WHERE universe_key=?'
                        ' AND instrument_id=?', (misses, state, universe_key, m['instrument_id']))
                    if misses == 1:
                        stats['marked_missing'] += 1
                    touched.add(m['instrument_id'])
                for iid in touched - seen_ids:
                    _recompute_status(conn, iid, now)
            conn.execute(
                "UPDATE catalog_sync_run SET finished_at=?, status='succeeded', seen=?, inserted=?,"
                ' updated=?, marked_missing=?, reactivated=? WHERE run_id=?',
                (utcnow_iso(), stats['seen'], stats['inserted'], stats['updated'],
                 stats['marked_missing'], stats['reactivated'], run_id))
            conn.commit()
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            conn.execute("UPDATE catalog_sync_run SET finished_at=?, status='failed', error=? WHERE run_id=?",
                         (utcnow_iso(), str(exc)[:500], run_id))
            conn.commit()
            raise
    return {'run_id': run_id, 'provider': provider, 'universe_key': universe_key,
            'complete': complete, **stats}


def set_instrument_status(conn: sqlite3.Connection, instrument_id: str, status: str) -> bool:
    """Explicit transition (e.g. suspended/delisted).  Never deletes."""
    if status not in STATUSES:
        raise ValueError(f'invalid status: {status!r}')
    with _write_lock:
        cur = conn.execute(
            'UPDATE instrument SET status=?, status_changed_at=? WHERE instrument_id=?',
            (status, utcnow_iso(), instrument_id))
        conn.commit()
        return bool(cur.rowcount)


def list_instruments(conn: sqlite3.Connection, *, q: Optional[str] = None,
                     asset_type: Optional[str] = None, venue: Optional[str] = None,
                     status: Optional[str] = None, universe_key: Optional[str] = None,
                     after: Optional[str] = None, limit: int = 50) -> dict:
    """Catalog browse with keyset pagination ordered by (display_ticker, instrument_id).

    Analysis/quote fields are intentionally absent here: catalog visibility
    never depends on them.  ``after`` is the opaque cursor from ``next_cursor``.
    """
    limit = max(1, min(int(limit), 200))
    where, args = [], []
    if q:
        where.append('(i.display_ticker LIKE ? OR i.name LIKE ?)')
        like = '%' + q.replace('%', '').replace('_', '') + '%'
        args += [like, like]
    for col, val in (('i.asset_type', asset_type), ('i.venue_id', venue.upper() if venue else None),
                     ('i.status', status)):
        if val:
            where.append(f'{col}=?')
            args.append(val)
    if universe_key:
        where.append('EXISTS (SELECT 1 FROM universe_membership u WHERE u.instrument_id=i.instrument_id'
                     ' AND u.universe_key=?)')
        args.append(universe_key)
    if after:
        tick, _, iid = after.partition('\x1f')
        where.append('(i.display_ticker > ? OR (i.display_ticker = ? AND i.instrument_id > ?))')
        args += [tick, tick, iid]
    sql = ('SELECT i.* FROM instrument i' + (' WHERE ' + ' AND '.join(where) if where else '') +
           ' ORDER BY i.display_ticker, i.instrument_id LIMIT ?')
    rows = [dict(r) for r in conn.execute(sql, args + [limit + 1])]
    more = len(rows) > limit
    rows = rows[:limit]
    nxt = f"{rows[-1]['display_ticker']}\x1f{rows[-1]['instrument_id']}" if more and rows else None
    return {'items': rows, 'next_cursor': nxt}


def catalog_counts(conn: sqlite3.Connection) -> dict:
    """Cataloged counts only; quote/analysis coverage counts are separate follow-on."""
    by_status = {r['status']: r['n'] for r in conn.execute(
        'SELECT status, COUNT(*) n FROM instrument GROUP BY status')}
    by_type = {r['asset_type']: r['n'] for r in conn.execute(
        'SELECT asset_type, COUNT(*) n FROM instrument GROUP BY asset_type')}
    return {'cataloged': sum(by_status.values()), 'by_status': by_status, 'by_asset_type': by_type}
