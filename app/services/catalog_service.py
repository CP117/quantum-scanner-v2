"""
Canonical durable instrument catalog (Phase 2 foundation; pending validation).

The catalog is independent of provider success and of analysis completion: a
row exists once a provider listing has reported it and is never deleted
because a provider later omits it.  Nothing in the scan/result pipeline reads
or writes this module yet (additive only).

Storage: SQLite file at ``settings.catalog_db_path`` (see
docs/architecture_decisions.md for the database decision and the Postgres
migration boundary).  Schema is versioned with ``PRAGMA user_version``.

Identity (details: docs/catalog_identity.md)
--------------------------------------------
``instrument_id`` is derived from ``(asset_class, venue, native_symbol)`` and
never from a bare ticker.  ``asset_class`` is one of ``equity``,
``crypto_pair`` or ``crypto_asset``; the finer ``instrument_type`` (stock,
etf, ...) is a mutable attribute and is NOT part of the identity, so
reclassifying a stock as an ETF cannot change its ID.  Equity share-class
separators (``-``, ``/``, space) are canonicalised to ``.`` so providers that
write ``BRK-B`` and ``BRK.B`` resolve to one instrument.  Venues are
upper-cased with whitespace -> ``_``; no venue aliasing/MIC mapping is
invented.  Crypto pairs carry an explicit quote symbol; an aggregated asset
(no quote) is a different identity from any exchange pair.

Sync scope and status transitions
---------------------------------
A sync scope is ``(provider, universe_key)``.  Each scope keeps its own
membership rows with their own absence counter::

    membership: active --(omitted from a complete run)--> missing
                missing --(omitted MISSING_GRACE_RUNS runs in a row)--> inactive
                missing/inactive --(reported again)--> active

Instrument status is derived from its memberships (active if any is active,
else missing if any is missing, else inactive).  ``suspended``, ``delisted``
and ``superseded`` are explicit-only: sync never overwrites them.  Partial
(``complete=False``) listings never create absences.  An empty *complete*
listing is rejected (treated as a provider failure) unless ``allow_empty``.
Memberships of a provider that stops syncing entirely are not decayed; use
``provider_status`` to detect stale providers.

Atomicity: every sync is one ``BEGIN IMMEDIATE`` transaction (run row and all
data commit or roll back together); failures are then recorded in a separate
transaction.  ``BEGIN IMMEDIATE`` plus ``busy_timeout`` serialises overlapping
runs across threads and processes.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from app.utils.time import utcnow_iso

SCHEMA_VERSION = 1
MISSING_GRACE_RUNS = 3
BUSY_TIMEOUT_MS = 5000
ASSET_CLASSES = ('equity', 'crypto_pair', 'crypto_asset')
STATUSES = ('active', 'missing', 'inactive', 'suspended', 'delisted', 'superseded')
_EXPLICIT_ONLY = ('suspended', 'delisted', 'superseded')
_MAX_CURSOR_LEN = 512

log = logging.getLogger('app.catalog')
_write_lock = threading.RLock()


class CatalogError(Exception):
    """Base class for catalog errors."""


class CatalogSchemaError(CatalogError):
    """Database schema is newer than this code understands."""


class CatalogSyncError(CatalogError):
    """A sync run was rejected (e.g. empty complete listing)."""


class CatalogQueryError(CatalogError, ValueError):
    """Invalid browse query (e.g. malformed or mismatched cursor)."""


_MIGRATIONS = {
    1: [
        """CREATE TABLE venue (
            venue_id   TEXT PRIMARY KEY,
            kind       TEXT NOT NULL,
            created_at TEXT NOT NULL)""",
        """CREATE TABLE instrument (
            instrument_id     TEXT PRIMARY KEY,
            asset_class       TEXT NOT NULL,
            instrument_type   TEXT,
            venue_id          TEXT NOT NULL REFERENCES venue(venue_id),
            native_symbol     TEXT NOT NULL,
            display_ticker    TEXT NOT NULL,
            quote_symbol      TEXT,
            name              TEXT,
            status            TEXT NOT NULL DEFAULT 'active',
            status_changed_at TEXT NOT NULL,
            first_seen_at     TEXT NOT NULL,
            last_seen_at      TEXT NOT NULL,
            UNIQUE (asset_class, venue_id, native_symbol))""",
        "CREATE INDEX idx_instrument_browse ON instrument (display_ticker, instrument_id)",
        "CREATE INDEX idx_instrument_status ON instrument (status)",
        """CREATE TABLE instrument_status_history (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            instrument_id TEXT NOT NULL REFERENCES instrument(instrument_id),
            old_status    TEXT,
            new_status    TEXT NOT NULL,
            reason        TEXT NOT NULL,
            changed_at    TEXT NOT NULL,
            run_id        INTEGER)""",
        """CREATE TABLE instrument_succession (
            old_instrument_id TEXT PRIMARY KEY REFERENCES instrument(instrument_id),
            new_instrument_id TEXT NOT NULL REFERENCES instrument(instrument_id),
            reason            TEXT NOT NULL,
            effective_at      TEXT,
            recorded_at       TEXT NOT NULL,
            CHECK (old_instrument_id <> new_instrument_id))""",
        """CREATE TABLE provider_instrument_mapping (
            provider        TEXT NOT NULL,
            provider_symbol TEXT NOT NULL,
            instrument_id   TEXT NOT NULL REFERENCES instrument(instrument_id),
            first_seen_at   TEXT NOT NULL,
            last_seen_at    TEXT NOT NULL,
            PRIMARY KEY (provider, provider_symbol, instrument_id))""",
        "CREATE INDEX idx_pim_instrument ON provider_instrument_mapping (instrument_id)",
        """CREATE TABLE universe_membership (
            universe_key  TEXT NOT NULL,
            provider      TEXT NOT NULL,
            instrument_id TEXT NOT NULL REFERENCES instrument(instrument_id),
            status        TEXT NOT NULL DEFAULT 'active',
            miss_count    INTEGER NOT NULL DEFAULT 0,
            added_at      TEXT NOT NULL,
            last_seen_at  TEXT NOT NULL,
            PRIMARY KEY (universe_key, provider, instrument_id))""",
        "CREATE INDEX idx_membership_instrument ON universe_membership (instrument_id)",
        """CREATE TABLE catalog_sync_run (
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
            error          TEXT)""",
    ],
}


# ---------------------------------------------------------------------------
# Connection / schema
# ---------------------------------------------------------------------------

def resolve_db_path(path: Optional[str] = None) -> Path:
    """Resolve the catalog DB path (explicit arg > settings), anchored at the
    repo root when relative.  Resolved per call, never at import time."""
    if path is None:
        try:
            from app.config import settings
            path = settings.catalog_db_path
        except Exception:  # noqa: BLE001
            path = 'data/catalog.db'
    p = Path(path)
    if not p.is_absolute():
        p = Path(__file__).resolve().parent.parent.parent / p
    return p


def connect(path: Optional[str] = None) -> sqlite3.Connection:
    """Open a catalog connection with FK enforcement, busy timeout and an
    up-to-date schema.  Each caller/thread should use its own connection."""
    if path == ':memory:':
        target = ':memory:'
    else:
        p = resolve_db_path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        target = str(p)
    conn = sqlite3.connect(target, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(f'PRAGMA busy_timeout={BUSY_TIMEOUT_MS}')
    conn.execute('PRAGMA foreign_keys=ON')
    try:
        if target != ':memory:':
            # Switching journal mode ignores busy_timeout when another
            # connection is opening concurrently, so retry explicitly.
            _retry_locked(lambda: conn.execute('PRAGMA journal_mode=WAL'))
        _migrate(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def _retry_locked(fn, attempts: int = 100, delay: float = 0.05):
    for i in range(attempts):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            if 'locked' not in str(exc).lower() or i == attempts - 1:
                raise
            time.sleep(delay)


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute('PRAGMA user_version').fetchone()[0]


def _migrate(conn: sqlite3.Connection) -> None:
    if schema_version(conn) == SCHEMA_VERSION:
        return
    with _tx(conn):
        current = schema_version(conn)  # re-check under the write lock
        if current > SCHEMA_VERSION:
            raise CatalogSchemaError(
                f'catalog schema v{current} is newer than supported v{SCHEMA_VERSION}')
        for version in range(current + 1, SCHEMA_VERSION + 1):
            for stmt in _MIGRATIONS[version]:
                conn.execute(stmt)
            conn.execute(f'PRAGMA user_version={version}')


@contextmanager
def _tx(conn: sqlite3.Connection):
    """One write transaction: BEGIN IMMEDIATE ... COMMIT / ROLLBACK."""
    with _write_lock:
        conn.execute('BEGIN IMMEDIATE')
        try:
            yield conn
        except BaseException:
            conn.execute('ROLLBACK')
            raise
        else:
            conn.execute('COMMIT')


# ---------------------------------------------------------------------------
# Identity / normalisation
# ---------------------------------------------------------------------------

def make_instrument_id(asset_class: str, venue: str, native_symbol: str) -> str:
    key = f'{asset_class}|{venue}|{native_symbol}'
    return 'ins_' + hashlib.sha256(key.encode('utf-8')).hexdigest()[:20]


def _norm_venue(v) -> str:
    return re.sub(r'\s+', '_', str(v or '').strip().upper())


def normalize_record(rec: dict) -> dict:
    """Validate/canonicalise one listing record.  Venue is mandatory: it is
    never guessed."""
    asset_class = str(rec.get('asset_class') or '').strip().lower()
    if asset_class not in ASSET_CLASSES:
        raise ValueError(f'unsupported asset_class: {asset_class!r}')
    venue = _norm_venue(rec.get('venue'))
    symbol = str(rec.get('symbol') or '').strip().upper()
    if not venue or not symbol:
        raise ValueError('record requires venue and symbol')
    quote = str(rec.get('quote_symbol') or '').strip().upper() or None
    if asset_class == 'equity':
        if quote:
            raise ValueError('equity must not have quote_symbol')
        symbol = re.sub(r'[-/\s]+', '.', symbol)
        native = symbol
    else:
        if re.search(r'[-/\s.]', symbol):
            raise ValueError('crypto symbol must be the bare base asset; use quote_symbol')
        if asset_class == 'crypto_pair':
            if not quote:
                raise ValueError('crypto_pair requires quote_symbol')
            native = f'{symbol}-{quote}'
        else:
            if quote:
                raise ValueError('crypto_asset must not have quote_symbol')
            native = symbol
    itype = str(rec.get('instrument_type') or '').strip().lower() or None
    return {
        'asset_class': asset_class, 'venue': venue, 'native': native, 'display': symbol,
        'quote': quote, 'instrument_type': itype,
        'name': (str(rec['name']).strip() or None) if rec.get('name') else None,
        'provider_symbol': str(rec.get('provider_symbol') or native).strip(),
        'venue_kind': 'equity' if asset_class == 'equity' else 'crypto',
    }


def get_instrument_id(conn: sqlite3.Connection, asset_class: str, venue: str, symbol: str,
                      quote_symbol: Optional[str] = None) -> Optional[str]:
    """Resolve a (class, venue, symbol[, quote]) to an existing instrument_id."""
    n = normalize_record({'asset_class': asset_class, 'venue': venue, 'symbol': symbol,
                          'quote_symbol': quote_symbol})
    iid = make_instrument_id(n['asset_class'], n['venue'], n['native'])
    row = conn.execute('SELECT 1 FROM instrument WHERE instrument_id=?', (iid,)).fetchone()
    return iid if row else None


def resolve_provider_symbol(conn: sqlite3.Connection, provider: str, provider_symbol: str) -> list[str]:
    """All instrument IDs a provider symbol is mapped to (more than one means
    the symbol is ambiguous and the caller must disambiguate)."""
    return [r['instrument_id'] for r in conn.execute(
        'SELECT instrument_id FROM provider_instrument_mapping WHERE provider=? AND provider_symbol=?'
        ' ORDER BY instrument_id', (provider, provider_symbol))]


# ---------------------------------------------------------------------------
# Status helpers
# ---------------------------------------------------------------------------

def _set_status(conn, iid: str, new: str, now: str, reason: str, run_id: Optional[int] = None) -> bool:
    row = conn.execute('SELECT status FROM instrument WHERE instrument_id=?', (iid,)).fetchone()
    if row is None or row['status'] == new:
        return False
    conn.execute('UPDATE instrument SET status=?, status_changed_at=? WHERE instrument_id=?',
                 (new, now, iid))
    conn.execute('INSERT INTO instrument_status_history (instrument_id, old_status, new_status, reason,'
                 ' changed_at, run_id) VALUES (?,?,?,?,?,?)', (iid, row['status'], new, reason, now, run_id))
    return True


def _recompute_status(conn, iid: str, now: str, run_id: int) -> None:
    row = conn.execute('SELECT status FROM instrument WHERE instrument_id=?', (iid,)).fetchone()
    if row is None or row['status'] in _EXPLICIT_ONLY:
        return
    states = {r['status'] for r in conn.execute(
        'SELECT status FROM universe_membership WHERE instrument_id=?', (iid,))}
    new = 'active' if 'active' in states else ('missing' if 'missing' in states else 'inactive')
    _set_status(conn, iid, new, now, 'sync', run_id)


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

def sync_listing(conn: sqlite3.Connection, provider: str, universe_key: str,
                 records: Iterable[dict], complete: bool = True, allow_empty: bool = False) -> dict:
    """Idempotently merge one provider listing into the catalog (see module
    docstring).  Raises on invalid input / rejected empty listing / DB error
    after rolling back all data changes and recording a ``failed`` run."""
    started = utcnow_iso()
    try:
        provider = str(provider or '').strip()
        universe_key = str(universe_key or '').strip()
        if not provider or not universe_key:
            raise ValueError('provider and universe_key are required')
        normalized = [normalize_record(r) for r in records]
        if complete and not normalized and not allow_empty:
            raise CatalogSyncError('empty complete listing rejected (treated as provider failure)')
        return _sync_tx(conn, provider, universe_key, normalized, complete, started)
    except BaseException as exc:
        _record_failure(conn, provider, universe_key, complete, started, exc)
        raise


def _record_failure(conn, provider, universe_key, complete, started, exc) -> None:
    try:
        with _tx(conn):
            conn.execute(
                "INSERT INTO catalog_sync_run (provider, universe_key, complete, started_at, finished_at,"
                " status, error) VALUES (?,?,?,?,?, 'failed', ?)",
                (str(provider or ''), str(universe_key or ''), 1 if complete else 0, started,
                 utcnow_iso(), f'{type(exc).__name__}: {exc}'[:500]))
    except Exception:  # noqa: BLE001 - never mask the original error
        log.warning('catalog: could not record failed sync run', exc_info=True)


def _sync_tx(conn, provider, universe_key, normalized, complete, started) -> dict:
    stats = {'seen': 0, 'inserted': 0, 'updated': 0, 'marked_missing': 0, 'reactivated': 0}
    with _tx(conn):
        now = utcnow_iso()
        run_id = conn.execute(
            'INSERT INTO catalog_sync_run (provider, universe_key, complete, started_at) VALUES (?,?,?,?)',
            (provider, universe_key, 1 if complete else 0, started)).lastrowid
        seen_ids: set[str] = set()
        for n in normalized:
            iid = make_instrument_id(n['asset_class'], n['venue'], n['native'])
            if iid not in seen_ids:
                seen_ids.add(iid)
                stats['seen'] += 1
                conn.execute('INSERT OR IGNORE INTO venue (venue_id, kind, created_at) VALUES (?,?,?)',
                             (n['venue'], n['venue_kind'], now))
                cur = conn.execute(
                    'INSERT OR IGNORE INTO instrument (instrument_id, asset_class, instrument_type, venue_id,'
                    ' native_symbol, display_ticker, quote_symbol, name, status, status_changed_at,'
                    " first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?, 'active', ?,?,?)",
                    (iid, n['asset_class'], n['instrument_type'], n['venue'], n['native'], n['display'],
                     n['quote'], n['name'], now, now, now))
                if cur.rowcount:
                    stats['inserted'] += 1
                    conn.execute('INSERT INTO instrument_status_history (instrument_id, old_status,'
                                 " new_status, reason, changed_at, run_id) VALUES (?, NULL, 'active',"
                                 " 'created', ?, ?)", (iid, now, run_id))
                else:
                    before = conn.execute('SELECT name, instrument_type FROM instrument WHERE instrument_id=?',
                                          (iid,)).fetchone()
                    if (n['name'] and n['name'] != before['name']) or \
                            (n['instrument_type'] and n['instrument_type'] != before['instrument_type']):
                        stats['updated'] += 1
                    conn.execute('UPDATE instrument SET name=COALESCE(?, name),'
                                 ' instrument_type=COALESCE(?, instrument_type), last_seen_at=?'
                                 ' WHERE instrument_id=?', (n['name'], n['instrument_type'], now, iid))
                prev = conn.execute(
                    'SELECT status FROM universe_membership WHERE universe_key=? AND provider=?'
                    ' AND instrument_id=?', (universe_key, provider, iid)).fetchone()
                if prev is not None and prev['status'] != 'active':
                    stats['reactivated'] += 1
                conn.execute(
                    'INSERT INTO universe_membership (universe_key, provider, instrument_id, status,'
                    " miss_count, added_at, last_seen_at) VALUES (?,?,?, 'active', 0, ?, ?)"
                    " ON CONFLICT(universe_key, provider, instrument_id) DO UPDATE SET status='active',"
                    ' miss_count=0, last_seen_at=excluded.last_seen_at',
                    (universe_key, provider, iid, now, now))
            conn.execute(
                'INSERT INTO provider_instrument_mapping (provider, provider_symbol, instrument_id,'
                ' first_seen_at, last_seen_at) VALUES (?,?,?,?,?) ON CONFLICT(provider, provider_symbol,'
                ' instrument_id) DO UPDATE SET last_seen_at=excluded.last_seen_at',
                (provider, n['provider_symbol'], iid, now, now))
        touched = set(seen_ids)
        if complete:
            absent = conn.execute(
                'SELECT instrument_id, status, miss_count FROM universe_membership'
                " WHERE universe_key=? AND provider=? AND status<>'inactive'",
                (universe_key, provider)).fetchall()
            for m in absent:
                if m['instrument_id'] in seen_ids:
                    continue
                misses = m['miss_count'] + 1
                state = 'inactive' if misses >= MISSING_GRACE_RUNS else 'missing'
                conn.execute('UPDATE universe_membership SET miss_count=?, status=? WHERE universe_key=?'
                             ' AND provider=? AND instrument_id=?',
                             (misses, state, universe_key, provider, m['instrument_id']))
                if m['status'] == 'active':
                    stats['marked_missing'] += 1
                touched.add(m['instrument_id'])
        for iid in touched:
            _recompute_status(conn, iid, now, run_id)
        conn.execute(
            "UPDATE catalog_sync_run SET finished_at=?, status='succeeded', seen=?, inserted=?, updated=?,"
            ' marked_missing=?, reactivated=? WHERE run_id=?',
            (utcnow_iso(), stats['seen'], stats['inserted'], stats['updated'], stats['marked_missing'],
             stats['reactivated'], run_id))
    return {'run_id': run_id, 'provider': provider, 'universe_key': universe_key,
            'complete': complete, 'status': 'succeeded', **stats}


# ---------------------------------------------------------------------------
# Explicit transitions / rename continuity
# ---------------------------------------------------------------------------

def set_instrument_status(conn: sqlite3.Connection, instrument_id: str, status: str,
                          reason: str = 'explicit') -> bool:
    """Explicit status change (history recorded).  Never deletes.  Returns
    False if the instrument does not exist."""
    if status not in STATUSES:
        raise ValueError(f'invalid status: {status!r}')
    with _tx(conn):
        if conn.execute('SELECT 1 FROM instrument WHERE instrument_id=?', (instrument_id,)).fetchone() is None:
            return False
        _set_status(conn, instrument_id, status, utcnow_iso(), reason)
    return True


def resolve_canonical_id(conn: sqlite3.Connection, instrument_id: str) -> str:
    """Follow succession links (rename continuity) to the current ID."""
    seen = {instrument_id}
    cur = instrument_id
    while True:
        row = conn.execute('SELECT new_instrument_id FROM instrument_succession WHERE old_instrument_id=?',
                           (cur,)).fetchone()
        if row is None:
            return cur
        cur = row['new_instrument_id']
        if cur in seen:  # defensive; link_succession prevents cycles
            raise CatalogError('succession cycle detected')
        seen.add(cur)


def link_succession(conn: sqlite3.Connection, old_id: str, new_id: str, reason: str,
                    effective_at: Optional[str] = None) -> None:
    """Explicitly record that ``old_id`` was replaced by ``new_id`` (symbol
    change, merger re-listing...).  The old ID is retained, marked
    ``superseded`` and keeps resolving via ``resolve_canonical_id``.
    Automatic rename detection is deferred."""
    if old_id == new_id:
        raise ValueError('old and new instrument must differ')
    with _tx(conn):
        for iid in (old_id, new_id):
            if conn.execute('SELECT 1 FROM instrument WHERE instrument_id=?', (iid,)).fetchone() is None:
                raise ValueError(f'unknown instrument: {iid}')
        if resolve_canonical_id(conn, new_id) == old_id:
            raise ValueError('succession would create a cycle')
        conn.execute('INSERT INTO instrument_succession (old_instrument_id, new_instrument_id, reason,'
                     ' effective_at, recorded_at) VALUES (?,?,?,?,?)',
                     (old_id, new_id, reason, effective_at, utcnow_iso()))
        _set_status(conn, old_id, 'superseded', utcnow_iso(), f'succession: {reason}')


# ---------------------------------------------------------------------------
# Read paths (never touch providers or analysis)
# ---------------------------------------------------------------------------

def _filters(q, asset_class, instrument_type, venue, status, universe_key):
    where, args = [], []
    if q:
        esc = q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        where.append("(i.display_ticker LIKE ? ESCAPE '\\' OR i.name LIKE ? ESCAPE '\\')")
        args += [f'%{esc}%', f'%{esc}%']
    for col, val in (('i.asset_class', asset_class), ('i.instrument_type', instrument_type),
                     ('i.venue_id', _norm_venue(venue) if venue else None), ('i.status', status)):
        if val:
            where.append(f'{col}=?')
            args.append(val)
    if universe_key:
        where.append('EXISTS (SELECT 1 FROM universe_membership u WHERE u.instrument_id=i.instrument_id'
                     ' AND u.universe_key=?)')
        args.append(universe_key)
    fp = hashlib.sha256(json.dumps([q, asset_class, instrument_type, venue and _norm_venue(venue), status,
                                  universe_key]).encode()).hexdigest()[:12]
    return where, args, fp


def _encode_cursor(ticker: str, iid: str, fp: str) -> str:
    return base64.urlsafe_b64encode(json.dumps([ticker, iid, fp]).encode()).decode().rstrip('=')


def _decode_cursor(cursor: str, fp: str):
    if len(cursor) > _MAX_CURSOR_LEN:
        raise CatalogQueryError('invalid cursor')
    try:
        ticker, iid, cfp = json.loads(base64.urlsafe_b64decode(cursor + '=' * (-len(cursor) % 4)))
        if not (isinstance(ticker, str) and isinstance(iid, str) and isinstance(cfp, str)):
            raise ValueError
    except Exception as exc:  # noqa: BLE001
        raise CatalogQueryError('invalid cursor') from exc
    if cfp != fp:
        raise CatalogQueryError('cursor does not match the current filters')
    return ticker, iid


def list_instruments(conn: sqlite3.Connection, *, q: Optional[str] = None,
                     asset_class: Optional[str] = None, instrument_type: Optional[str] = None,
                     venue: Optional[str] = None, status: Optional[str] = None,
                     universe_key: Optional[str] = None, after: Optional[str] = None,
                     limit: int = 50) -> dict:
    """Keyset-paginated browse ordered by (display_ticker, instrument_id).
    The cursor is opaque and bound to the filter set."""
    limit = max(1, min(int(limit), 200))
    where, args, fp = _filters(q, asset_class, instrument_type, venue, status, universe_key)
    if after:
        tick, iid = _decode_cursor(after, fp)
        where.append('(i.display_ticker > ? OR (i.display_ticker = ? AND i.instrument_id > ?))')
        args += [tick, tick, iid]
    sql = ('SELECT i.* FROM instrument i' + (' WHERE ' + ' AND '.join(where) if where else '') +
           ' ORDER BY i.display_ticker, i.instrument_id LIMIT ?')
    rows = [dict(r) for r in conn.execute(sql, args + [limit + 1])]
    more = len(rows) > limit
    rows = rows[:limit]
    nxt = _encode_cursor(rows[-1]['display_ticker'], rows[-1]['instrument_id'], fp) if more else None
    return {'items': rows, 'next_cursor': nxt}


def catalog_counts(conn: sqlite3.Connection, *, q: Optional[str] = None,
                   asset_class: Optional[str] = None, instrument_type: Optional[str] = None,
                   venue: Optional[str] = None, status: Optional[str] = None,
                   universe_key: Optional[str] = None) -> dict:
    """Cataloged counts under the same filters as ``list_instruments``.
    Quote/analysis coverage counts are not part of this slice."""
    where, args, _ = _filters(q, asset_class, instrument_type, venue, status, universe_key)
    w = (' WHERE ' + ' AND '.join(where)) if where else ''

    def group(col):
        return {r['k']: r['n'] for r in conn.execute(
            f'SELECT i.{col} AS k, COUNT(*) AS n FROM instrument i{w} GROUP BY i.{col}', args)}
    by_status = group('status')
    total = sum(by_status.values())
    return {'total': total, 'cataloged': total, 'by_status': by_status,
            'by_asset_class': group('asset_class')}


def provider_status(conn: sqlite3.Connection, stale_after_seconds: float = 86400.0,
                    now: Optional[datetime] = None) -> list[dict]:
    """Per-provider last successful/failed sync and a ``stale`` flag.  A stale
    provider's memberships are never decayed by other providers' runs."""
    now = now or datetime.now(timezone.utc)
    out = []
    for r in conn.execute(
            "SELECT provider, MAX(CASE WHEN status='succeeded' THEN finished_at END) AS last_ok,"
            " MAX(CASE WHEN status='failed' THEN finished_at END) AS last_failed"
            ' FROM catalog_sync_run GROUP BY provider ORDER BY provider'):
        stale = True
        if r['last_ok']:
            age = (now - datetime.fromisoformat(r['last_ok'])).total_seconds()
            stale = age > stale_after_seconds
        out.append({'provider': r['provider'], 'last_success_at': r['last_ok'],
                    'last_failure_at': r['last_failed'], 'stale': stale})
    return out
