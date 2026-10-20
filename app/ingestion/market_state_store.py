"""Durable latest-market-state storage (one latest quote per instrument+provider).

Lives in the catalog SQLite file (same connection boundary as
``catalog_service``) but owns its own ``market_state_*`` tables and its own
schema marker, so the catalog ``PRAGMA user_version`` is untouched. Portable SQL
only (plus ``ON CONFLICT``/``INSERT OR IGNORE``), connection-injected.

Update rules (``apply_quote``):
  * known source_ts older than stored      -> ``rejected_older`` (stored row kept)
  * same source_ts, identical values       -> ``duplicate`` (idempotent no-op)
  * same source_ts, different values       -> ``rejected_conflict`` (never silently overwritten)
  * unknown source_ts, stored has known ts -> ``rejected_unknown_ts`` (cannot prove newer)
  * unknown source_ts, stored unknown/none -> ``applied`` in ingestion order, ts stays NULL
  * known source_ts newer / no stored row  -> ``applied``
Rows are keyed per provider: providers and adjustment modes are never merged.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Optional

from app.services import catalog_service as cs

from .contracts import DataState, Field, Quote

# Schema ownership: the catalog owns ``PRAGMA user_version`` (catalog_service);
# the market-state tables are versioned independently by ``market_state_schema``
# in the same file. Each migration runs in one BEGIN IMMEDIATE transaction
# together with its version bump, so a failing step rolls everything back.
SCHEMA_VERSION = 1
_MIGRATIONS = {
    1: [
        """CREATE TABLE IF NOT EXISTS market_state_latest (
            instrument_id TEXT NOT NULL REFERENCES instrument(instrument_id),
            provider      TEXT NOT NULL,
            kind          TEXT NOT NULL,
            price         REAL,
            price_state   TEXT NOT NULL,
            volume        REAL,
            volume_state  TEXT NOT NULL,
            source_ts     REAL,
            ts_semantics  TEXT NOT NULL,
            ingested_at   REAL NOT NULL,
            interval      TEXT,
            adjustment    TEXT NOT NULL,
            currency      TEXT,
            venue         TEXT,
            quality       TEXT NOT NULL,
            source_revision TEXT,
            content_hash  TEXT NOT NULL,
            PRIMARY KEY (instrument_id, provider, kind))""",
        """CREATE TABLE IF NOT EXISTS market_ingestion_run (
            run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at  REAL NOT NULL,
            finished_at REAL,
            provider    TEXT NOT NULL,
            summary     TEXT)""",
    ],
}


class MarketStateSchemaError(RuntimeError):
    """Stored market-state schema is newer than this code supports."""


def schema_version(conn: sqlite3.Connection) -> int:
    """0 = market-state tables not created yet."""
    try:
        row = conn.execute('SELECT MAX(version) AS v FROM market_state_schema').fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row['v'] or 0)


def ensure_schema(conn: sqlite3.Connection) -> int:
    """Create/upgrade market-state tables. Requires an up-to-date catalog
    schema (``cs.connect`` guarantees it). Idempotent; never drops data."""
    if schema_version(conn) == SCHEMA_VERSION:
        return SCHEMA_VERSION
    with cs._tx(conn):
        conn.execute('CREATE TABLE IF NOT EXISTS market_state_schema (version INTEGER PRIMARY KEY)')
        current = schema_version(conn)  # re-check under the write lock
        if current > SCHEMA_VERSION:
            raise MarketStateSchemaError(
                f'market-state schema v{current} is newer than supported v{SCHEMA_VERSION}')
        for version in range(current + 1, SCHEMA_VERSION + 1):
            for stmt in _MIGRATIONS[version]:
                conn.execute(stmt)
            conn.execute('INSERT INTO market_state_schema (version) VALUES (?)', (version,))
    return SCHEMA_VERSION


def _hash(q: Quote) -> str:
    return hashlib.sha256(json.dumps(
        [q.price.state.value, q.price.value, q.volume.state.value, q.volume.value,
         q.meta.currency, q.meta.venue, q.meta.adjustment.value, q.meta.interval],
        sort_keys=True).encode()).hexdigest()


APPLIED, DUPLICATE = 'applied', 'duplicate'
REJECTED_OLDER, REJECTED_CONFLICT, REJECTED_UNKNOWN_TS = 'rejected_older', 'rejected_conflict', 'rejected_unknown_ts'


def apply_quote(conn: sqlite3.Connection, q: Quote) -> str:
    if not q.instrument_id:
        raise ValueError('quote has no canonical instrument_id')
    m, h = q.meta, _hash(q)
    with cs._tx(conn):
        cur = conn.execute('SELECT source_ts, content_hash FROM market_state_latest WHERE instrument_id=?'
                           ' AND provider=? AND kind=?', (q.instrument_id, m.provider, m.kind.value)).fetchone()
        if cur is not None:
            old = cur['source_ts']
            if m.source_ts is None:
                if old is not None:
                    return REJECTED_UNKNOWN_TS
            elif old is not None:
                if m.source_ts < old:
                    return REJECTED_OLDER
                if m.source_ts == old:
                    return DUPLICATE if cur['content_hash'] == h else REJECTED_CONFLICT
        conn.execute(
            'INSERT INTO market_state_latest (instrument_id, provider, kind, price, price_state, volume,'
            ' volume_state, source_ts, ts_semantics, ingested_at, interval, adjustment, currency, venue,'
            ' quality, source_revision, content_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)'
            ' ON CONFLICT(instrument_id, provider, kind) DO UPDATE SET price=excluded.price,'
            ' price_state=excluded.price_state, volume=excluded.volume, volume_state=excluded.volume_state,'
            ' source_ts=excluded.source_ts, ts_semantics=excluded.ts_semantics, ingested_at=excluded.ingested_at,'
            ' interval=excluded.interval, adjustment=excluded.adjustment, currency=excluded.currency,'
            ' venue=excluded.venue, quality=excluded.quality, source_revision=excluded.source_revision,'
            ' content_hash=excluded.content_hash',
            (q.instrument_id, m.provider, m.kind.value, q.price.value, q.price.state.value, q.volume.value,
             q.volume.state.value, m.source_ts, m.timestamp_semantics.value, m.ingested_at, m.interval,
             m.adjustment.value, m.currency, m.venue, m.quality.value, m.source_revision, h))
    return APPLIED


def record_run(conn: sqlite3.Connection, provider: str, started: float, finished: float, summary: dict) -> int:
    with cs._tx(conn):
        cur = conn.execute('INSERT INTO market_ingestion_run (started_at, finished_at, provider, summary)'
                           ' VALUES (?,?,?,?)', (started, finished, provider, json.dumps(summary, sort_keys=True)))
        return cur.lastrowid


def _view(row, now_ts: float, max_age_s: float) -> dict:
    age = None if row['source_ts'] is None else max(0.0, now_ts - row['source_ts'])

    def fld(value, state):
        st = DataState(state)
        if st in (DataState.OK, DataState.ZERO) and age is not None and age > max_age_s:
            st = DataState.STALE  # present but too old
        return {'value': value, 'state': st.value}
    return {'provider': row['provider'], 'kind': row['kind'],
            'price': fld(row['price'], row['price_state']),
            'volume': fld(row['volume'], row['volume_state']),
            'source_ts': row['source_ts'], 'timestamp_semantics': row['ts_semantics'],
            'ingested_at': row['ingested_at'], 'age_seconds': age, 'interval': row['interval'],
            'adjustment': row['adjustment'], 'currency': row['currency'], 'venue': row['venue'],
            'quality': row['quality'], 'source_revision': row['source_revision']}


def latest_for(conn: sqlite3.Connection, instrument_ids: list[str], *, now_ts: float,
               max_age_s: float = 900.0) -> dict[str, list[dict]]:
    """Latest stored quotes per instrument (read-only; never contacts providers).
    Instruments with no stored quote are simply absent from the result."""
    out: dict[str, list[dict]] = {}
    if not instrument_ids:
        return out
    marks = ','.join('?' * len(instrument_ids))
    try:
        rows = conn.execute('SELECT * FROM market_state_latest WHERE instrument_id IN (' + marks +  # nosec B608
                            ') ORDER BY instrument_id, provider, kind', instrument_ids).fetchall()
    except sqlite3.OperationalError:  # ingestion never run: table not created yet
        return out
    for r in rows:
        out.setdefault(r['instrument_id'], []).append(_view(r, now_ts, max_age_s))
    return out
