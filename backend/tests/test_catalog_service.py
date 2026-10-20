"""Phase 2 catalog service tests (identity, sync semantics, persistence).

These validate the additive catalog only; they are not algorithm parity tests.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from app.services import catalog_service as cs  # noqa: E402


@pytest.fixture()
def db_path(tmp_path):
    return str(tmp_path / 'catalog.db')


@pytest.fixture()
def conn(db_path):
    c = cs.connect(db_path)
    yield c
    c.close()


def _s(sym, venue='XNAS', **kw):
    return {'asset_class': 'equity', 'venue': venue, 'symbol': sym, **kw}


def _by_ticker(conn):
    return {i['display_ticker']: i['status'] for i in cs.list_instruments(conn, limit=200)['items']}


# --- identity / normalisation ------------------------------------------------

def test_same_ticker_different_venue_and_class_do_not_collide(conn):
    cs.sync_listing(conn, 'p', 'u', [
        _s('ABC', 'XNAS'), _s('ABC', 'XNYS'),
        {'asset_class': 'crypto_pair', 'venue': 'BINANCE', 'symbol': 'ABC', 'quote_symbol': 'USDT'},
        {'asset_class': 'crypto_pair', 'venue': 'KRAKEN', 'symbol': 'ABC', 'quote_symbol': 'USDT'},
        {'asset_class': 'crypto_pair', 'venue': 'KRAKEN', 'symbol': 'ABC', 'quote_symbol': 'USD'},
        {'asset_class': 'crypto_asset', 'venue': 'AGG', 'symbol': 'ABC'},
    ])
    assert cs.catalog_counts(conn)['total'] == 6


def test_share_classes_distinct_but_separator_variants_resolve_together(conn):
    cs.sync_listing(conn, 'a', 'ua', [_s('BRK-B', 'NYSE', provider_symbol='BRK-B'), _s('BRK.A', 'NYSE')])
    cs.sync_listing(conn, 'b', 'ub', [_s('brk.b', 'nyse', provider_symbol='BRK/B')])
    assert cs.catalog_counts(conn)['total'] == 2
    iid = cs.get_instrument_id(conn, 'equity', 'NYSE', 'BRK.B')
    assert cs.resolve_provider_symbol(conn, 'a', 'BRK-B') == [iid]
    assert cs.resolve_provider_symbol(conn, 'b', 'BRK/B') == [iid]
    assert cs.get_instrument_id(conn, 'equity', 'NYSE', 'NOPE') is None


def test_venue_whitespace_and_case_normalised(conn):
    cs.sync_listing(conn, 'a', 'u', [_s('SPY', 'NYSE Arca')])
    cs.sync_listing(conn, 'b', 'u2', [_s('SPY', 'nyse  arca')])
    assert cs.catalog_counts(conn)['total'] == 1


@pytest.mark.parametrize('rec', [
    {'asset_class': 'crypto_pair', 'venue': 'X', 'symbol': 'A'},
    {'asset_class': 'crypto_asset', 'venue': 'X', 'symbol': 'A', 'quote_symbol': 'USD'},
    {'asset_class': 'crypto_pair', 'venue': 'X', 'symbol': 'BTC-USD', 'quote_symbol': 'USD'},
    {'asset_class': 'equity', 'venue': 'X', 'symbol': 'A', 'quote_symbol': 'USD'},
    {'asset_class': 'equity', 'venue': '', 'symbol': 'A'},
    {'asset_class': 'stock', 'venue': 'X', 'symbol': 'A'},
])
def test_invalid_records_rejected_and_recorded(conn, rec):
    with pytest.raises(ValueError):
        cs.sync_listing(conn, 'p', 'u', [_s('OK'), rec])
    assert cs.catalog_counts(conn)['total'] == 0  # nothing from the batch persisted
    assert conn.execute("SELECT status FROM catalog_sync_run").fetchall()[0]['status'] == 'failed'


def test_instrument_type_is_not_part_of_identity(conn):
    cs.sync_listing(conn, 'p', 'u', [_s('SPY', 'ARCA')])
    first = cs.get_instrument_id(conn, 'equity', 'ARCA', 'SPY')
    cs.sync_listing(conn, 'p', 'u', [_s('SPY', 'ARCA', instrument_type='etf')])
    assert cs.get_instrument_id(conn, 'equity', 'ARCA', 'SPY') == first
    assert cs.list_instruments(conn, instrument_type='etf')['items'][0]['instrument_id'] == first


# --- sync semantics ----------------------------------------------------------

def test_sync_idempotent_and_replay_safe(conn):
    recs = [_s('AAA'), _s('BBB')]
    first = cs.sync_listing(conn, 'p', 'u', recs)
    for _ in range(3):
        again = cs.sync_listing(conn, 'p', 'u', recs)
        assert (again['inserted'], again['updated'], again['marked_missing'], again['reactivated']) == (0, 0, 0, 0)
    assert first['inserted'] == 2
    for t in ('instrument', 'provider_instrument_mapping', 'universe_membership'):
        assert conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] == 2


def test_duplicates_within_one_listing(conn):
    r = cs.sync_listing(conn, 'p', 'u', [_s('AAA'), _s('aaa')])
    assert r['seen'] == 1 and r['inserted'] == 1


def test_omission_retains_then_reactivates(conn):
    cs.sync_listing(conn, 'p', 'u', [_s('AAA'), _s('BBB')])
    r = cs.sync_listing(conn, 'p', 'u', [_s('AAA')])
    assert r['marked_missing'] == 1
    assert _by_ticker(conn) == {'AAA': 'active', 'BBB': 'missing'}
    for _ in range(cs.MISSING_GRACE_RUNS):
        cs.sync_listing(conn, 'p', 'u', [_s('AAA')])
    assert _by_ticker(conn)['BBB'] == 'inactive'
    r = cs.sync_listing(conn, 'p', 'u', [_s('AAA'), _s('BBB')])
    assert r['reactivated'] == 1 and _by_ticker(conn)['BBB'] == 'active'
    hist = [h['new_status'] for h in conn.execute(
        "SELECT new_status FROM instrument_status_history h JOIN instrument i USING(instrument_id)"
        " WHERE i.display_ticker='BBB' ORDER BY h.id")]
    assert hist == ['active', 'missing', 'inactive', 'active']


def test_partial_listing_never_creates_absence(conn):
    cs.sync_listing(conn, 'p', 'u', [_s('AAA'), _s('BBB')])
    cs.sync_listing(conn, 'p', 'u', [_s('AAA')], complete=False)
    assert _by_ticker(conn) == {'AAA': 'active', 'BBB': 'active'}


def test_empty_complete_rejected_empty_partial_ok(conn):
    cs.sync_listing(conn, 'p', 'u', [_s('AAA')])
    with pytest.raises(cs.CatalogSyncError):
        cs.sync_listing(conn, 'p', 'u', [])
    assert _by_ticker(conn) == {'AAA': 'active'}
    assert conn.execute("SELECT COUNT(*) FROM catalog_sync_run WHERE status='failed'").fetchone()[0] == 1
    cs.sync_listing(conn, 'p', 'u', [], complete=False)
    assert _by_ticker(conn) == {'AAA': 'active'}
    cs.sync_listing(conn, 'p', 'u', [], allow_empty=True)
    assert _by_ticker(conn) == {'AAA': 'missing'}


def test_scope_isolation_between_universes_and_providers(conn):
    cs.sync_listing(conn, 'p', 'u1', [_s('AAA')])
    cs.sync_listing(conn, 'p', 'u2', [_s('BBB')])
    cs.sync_listing(conn, 'p', 'u1', [_s('CCC')])  # omits AAA from u1 only
    assert _by_ticker(conn) == {'AAA': 'missing', 'BBB': 'active', 'CCC': 'active'}
    assert cs.list_instruments(conn, universe_key='u2')['items'][0]['display_ticker'] == 'BBB'


def test_provider_specific_absence_counters(conn):
    cs.sync_listing(conn, 'p1', 'u', [_s('AAA')])
    cs.sync_listing(conn, 'p2', 'u', [_s('AAA')])
    for _ in range(cs.MISSING_GRACE_RUNS + 1):
        cs.sync_listing(conn, 'p1', 'u', [_s('ZZZ')])
    # p1 dropped AAA (membership inactive) but p2 still reports it.
    assert _by_ticker(conn)['AAA'] == 'active'
    rows = {r['provider']: (r['status'], r['miss_count']) for r in conn.execute(
        'SELECT u.provider, u.status, u.miss_count FROM universe_membership u JOIN instrument i USING(instrument_id)'
        " WHERE i.display_ticker='AAA'")}
    assert rows['p1'][0] == 'inactive' and rows['p2'] == ('active', 0)
    for _ in range(cs.MISSING_GRACE_RUNS):
        cs.sync_listing(conn, 'p2', 'u', [_s('ZZZ')])
    assert _by_ticker(conn)['AAA'] == 'inactive'


def test_stale_provider_not_decayed_and_reported(conn):
    cs.sync_listing(conn, 'stale', 'u', [_s('AAA')])
    for _ in range(5):
        cs.sync_listing(conn, 'fresh', 'u2', [_s('BBB')])
    assert _by_ticker(conn)['AAA'] == 'active'
    from datetime import datetime, timedelta, timezone
    later = datetime.now(timezone.utc) + timedelta(days=2)
    st = {p['provider']: p for p in cs.provider_status(conn, 3600, now=later)}
    assert st['stale']['stale'] and st['fresh']['stale']
    st = {p['provider']: p['stale'] for p in cs.provider_status(conn, 3600)}
    assert st == {'fresh': False, 'stale': False}
    cs.sync_listing(conn, 'failing', 'u3', [_s('CCC')])
    with pytest.raises(cs.CatalogSyncError):
        cs.sync_listing(conn, 'failing', 'u3', [])
    assert {p['provider']: p for p in cs.provider_status(conn)}['failing']['last_failure_at']


def test_explicit_status_takes_precedence_over_sync(conn):
    cs.sync_listing(conn, 'p', 'u', [_s('AAA'), _s('BBB')])
    a = cs.get_instrument_id(conn, 'equity', 'XNAS', 'AAA')
    b = cs.get_instrument_id(conn, 'equity', 'XNAS', 'BBB')
    assert cs.set_instrument_status(conn, a, 'delisted', 'test')
    assert cs.set_instrument_status(conn, b, 'suspended')
    assert not cs.set_instrument_status(conn, 'ins_nope', 'delisted')
    with pytest.raises(ValueError):
        cs.set_instrument_status(conn, a, 'bogus')
    cs.sync_listing(conn, 'p', 'u', [_s('AAA'), _s('BBB')])  # still reported
    assert _by_ticker(conn) == {'AAA': 'delisted', 'BBB': 'suspended'}
    for _ in range(cs.MISSING_GRACE_RUNS):
        cs.sync_listing(conn, 'p', 'u', [_s('CCC')])  # omitted
    assert _by_ticker(conn)['AAA'] == 'delisted' and _by_ticker(conn)['BBB'] == 'suspended'
    cs.set_instrument_status(conn, a, 'active')  # explicit restore
    assert _by_ticker(conn)['AAA'] == 'active'


def test_atomic_rollback_on_mid_sync_failure(conn, monkeypatch):
    cs.sync_listing(conn, 'p', 'u', [_s('AAA')])
    before = {t: conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
              for t in ('instrument', 'universe_membership', 'provider_instrument_mapping', 'venue')}
    real = cs._recompute_status
    calls = {'n': 0}

    def boom(*a, **k):
        calls['n'] += 1
        if calls['n'] == 2:
            raise RuntimeError('boom')
        return real(*a, **k)
    monkeypatch.setattr(cs, '_recompute_status', boom)
    with pytest.raises(RuntimeError):
        cs.sync_listing(conn, 'p', 'u', [_s('NEW1', 'NEWVENUE'), _s('NEW2', 'NEWVENUE')])
    monkeypatch.undo()
    after = {t: conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in before}
    assert after == before
    runs = [r['status'] for r in conn.execute('SELECT status FROM catalog_sync_run ORDER BY run_id')]
    assert runs == ['succeeded', 'failed']
    assert not conn.in_transaction


def test_overlapping_runs_from_separate_connections(db_path):
    errors = []

    def worker(provider, tickers):
        c = None
        try:
            c = cs.connect(db_path)
            for _ in range(5):
                cs.sync_listing(c, provider, 'u', [_s(t) for t in tickers])
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            if c is not None:
                c.close()
    ts = [threading.Thread(target=worker, args=(f'p{i}', [f'T{i}A', f'T{i}B', 'SHARED'])) for i in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors
    c = cs.connect(db_path)
    assert cs.catalog_counts(c)['total'] == 9
    assert c.execute("SELECT COUNT(*) FROM catalog_sync_run WHERE status='succeeded'").fetchone()[0] == 20
    assert c.execute("SELECT COUNT(*) FROM catalog_sync_run WHERE status='running'").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM provider_instrument_mapping").fetchone()[0] == 12
    c.close()


# --- persistence / schema / connection ---------------------------------------

def test_persistence_across_restart(db_path):
    c = cs.connect(db_path)
    cs.sync_listing(c, 'p', 'u', [_s('AAA'), _s('BBB')])
    cs.sync_listing(c, 'p', 'u', [_s('AAA')])
    c.close()
    c2 = cs.connect(db_path)
    assert _by_ticker(c2) == {'AAA': 'active', 'BBB': 'missing'}
    r = cs.sync_listing(c2, 'p', 'u', [_s('AAA'), _s('BBB')])
    assert r['reactivated'] == 1 and r['inserted'] == 0
    c2.close()


def test_schema_version_foreign_keys_and_row_factory(conn, db_path):
    assert cs.schema_version(conn) == cs.SCHEMA_VERSION == 2
    assert conn.execute('PRAGMA foreign_keys').fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO universe_membership VALUES ('u','p','ins_missing','active',0,'x','x')")
    c2 = cs.connect(db_path)  # every new connection enforces FKs
    assert c2.execute('PRAGMA foreign_keys').fetchone()[0] == 1
    assert c2.execute('PRAGMA busy_timeout').fetchone()[0] == cs.BUSY_TIMEOUT_MS
    c2.close()


def test_newer_schema_refused_and_migration_idempotent(db_path):
    cs.connect(db_path).close()
    cs.connect(db_path).close()  # re-open: no re-migration error
    raw = sqlite3.connect(db_path)
    raw.execute('PRAGMA user_version=99')
    raw.close()
    with pytest.raises(cs.CatalogSchemaError):
        cs.connect(db_path)


def test_path_resolution(tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, 'catalog_db_path', 'data/_t_catalog.db')
    p = cs.resolve_db_path()
    assert p.is_absolute() and p.parts[-2:] == ('data', '_t_catalog.db')
    assert p.parent.parent == __import__('pathlib').Path(cs.__file__).resolve().parents[2]
    assert str(cs.resolve_db_path(str(tmp_path / 'x.db'))) == str(tmp_path / 'x.db')
    monkeypatch.setattr(settings, 'catalog_db_path', str(tmp_path / 'sub' / 'c.db'))
    cs.connect().close()  # creates parent dir under the temp path, not the repo
    assert (tmp_path / 'sub' / 'c.db').exists()


# --- rename continuity --------------------------------------------------------

def test_succession_links_resolve_and_reject_cycles(conn):
    cs.sync_listing(conn, 'p', 'u', [_s('OLD'), _s('NEW')])
    old = cs.get_instrument_id(conn, 'equity', 'XNAS', 'OLD')
    new = cs.get_instrument_id(conn, 'equity', 'XNAS', 'NEW')
    cs.link_succession(conn, old, new, 'ticker change', '2026-01-02')
    assert cs.resolve_canonical_id(conn, old) == new and cs.resolve_canonical_id(conn, new) == new
    assert _by_ticker(conn)['OLD'] == 'superseded'
    cs.sync_listing(conn, 'p', 'u', [_s('OLD'), _s('NEW')])
    assert _by_ticker(conn)['OLD'] == 'superseded'  # sync does not resurrect
    with pytest.raises(ValueError):
        cs.link_succession(conn, new, old, 'cycle')
    with pytest.raises(ValueError):
        cs.link_succession(conn, old, old, 'self')
    with pytest.raises(ValueError):
        cs.link_succession(conn, old, 'ins_nope', 'unknown')


# --- browse / pagination -------------------------------------------------------

def test_pagination_cursor_validation_and_filter_binding(conn):
    cs.sync_listing(conn, 'p', 'u', [_s(f'T{i:02d}') for i in range(7)] + [_s('T00', 'XNYS')])
    seen, cur = [], None
    while True:
        page = cs.list_instruments(conn, limit=3, after=cur)
        seen += [(i['display_ticker'], i['venue_id']) for i in page['items']]
        cur = page['next_cursor']
        if not cur:
            break
    assert len(seen) == len(set(seen)) == 8 == cs.catalog_counts(conn)['total']
    assert [t[0] for t in seen] == sorted(t[0] for t in seen)
    cur = cs.list_instruments(conn, limit=2)['next_cursor']
    for bad in ('!!!', 'AAAA', 'x' * 600):
        with pytest.raises(cs.CatalogQueryError):
            cs.list_instruments(conn, after=bad)
    with pytest.raises(cs.CatalogQueryError):
        cs.list_instruments(conn, after=cur, venue='XNYS')


def test_filter_and_count_consistency_and_like_escaping(conn):
    cs.sync_listing(conn, 'p', 'u', [_s('A_B', name='x'), _s('AXB', name='100% real'), _s('ZZZ', 'XNYS')])
    assert [i['display_ticker'] for i in cs.list_instruments(conn, q='A_B')['items']] == ['A_B']
    assert [i['display_ticker'] for i in cs.list_instruments(conn, q='%')['items']] == ['AXB']
    for f in ({}, {'venue': 'xnys'}, {'q': 'A'}, {'status': 'active'}, {'universe_key': 'u'}, {'universe_key': 'none'}):
        items = cs.list_instruments(conn, limit=200, **f)['items']
        assert cs.catalog_counts(conn, **f)['total'] == len(items)
        assert sum(cs.catalog_counts(conn, **f)['by_asset_class'].values()) == len(items)
    assert len(cs.list_instruments(conn, limit=10_000)['items']) == 3  # limit clamped, not an error


def test_unanalyzed_instruments_visible_without_any_analysis_tables(conn):
    cs.sync_listing(conn, 'p', 'u', [_s('AAA')])
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not any('analysis' in t or 'quote' in t or 'score' in t for t in tables)
    assert cs.list_instruments(conn)['items'][0]['status'] == 'active'


# --- symbol relink (documented continuity mechanism) -------------------------

def _ids(conn, *syms, venue='XNAS'):
    return [cs.get_instrument_id(conn, 'equity', venue, s) for s in syms]


def test_relink_chain_resolves_and_history_retained(conn):
    cs.sync_listing(conn, 'p', 'u', [_s('A1'), _s('A2'), _s('A3')])
    a1, a2, a3 = _ids(conn, 'A1', 'A2', 'A3')
    cs.link_succession(conn, a1, a2, 'rename')
    cs.link_succession(conn, a2, a3, 'rename again')
    assert cs.resolve_canonical_id(conn, a1) == a3
    assert cs.resolve_canonical_id(conn, 'ins_unknown') == 'ins_unknown'
    assert {i['display_ticker']: i['status'] for i in cs.list_instruments(conn)['items']} == {
        'A1': 'superseded', 'A2': 'superseded', 'A3': 'active'}
    hist = [h['new_status'] for h in conn.execute(
        'SELECT new_status FROM instrument_status_history WHERE instrument_id=? ORDER BY id', (a1,))]
    assert hist == ['active', 'superseded']


def test_relink_conflicts_leave_state_unchanged(conn):
    cs.sync_listing(conn, 'p', 'u', [_s('A1'), _s('A2'), _s('A3')])
    a1, a2, a3 = _ids(conn, 'A1', 'A2', 'A3')
    cs.link_succession(conn, a1, a2, 'rename')
    with pytest.raises(sqlite3.IntegrityError):  # old already has a successor
        cs.link_succession(conn, a1, a3, 'conflict')
    assert cs.resolve_canonical_id(conn, a1) == a2
    with pytest.raises(ValueError):  # a2 -> a1 would close a cycle
        cs.link_succession(conn, a2, a1, 'cycle')
    with pytest.raises(ValueError):  # indirect cycle a3 -> a1 (a1 -> a2)
        cs.link_succession(conn, a2, a3, 'ok')
        cs.link_succession(conn, a3, a1, 'cycle')
    assert not conn.in_transaction
    assert conn.execute('SELECT COUNT(*) FROM instrument_succession').fetchone()[0] == 2


def test_relink_persists_across_restart(db_path):
    c = cs.connect(db_path)
    cs.sync_listing(c, 'p', 'u', [_s('A1'), _s('A2')])
    a1, a2 = _ids(c, 'A1', 'A2')
    cs.link_succession(c, a1, a2, 'rename')
    c.close()
    c = cs.connect(db_path)
    assert cs.resolve_canonical_id(c, a1) == a2
    c.close()


# --- venue / provider alias contract -----------------------------------------

def test_alias_resolves_cross_provider_to_one_instrument(conn):
    cs.add_venue_alias(conn, 'pa', 'NASDAQ', 'XNAS')
    cs.add_venue_alias(conn, 'pb', 'Nasdaq GS', 'XNAS')
    cs.sync_listing(conn, 'pa', 'ua', [{'asset_class': 'equity', 'provider_venue': 'nasdaq', 'symbol': 'AAPL'}])
    cs.sync_listing(conn, 'pb', 'ub', [{'asset_class': 'equity', 'provider_venue': 'NASDAQ  GS', 'symbol': 'AAPL'}])
    assert cs.catalog_counts(conn)['total'] == 1
    iid = cs.get_instrument_id(conn, 'equity', 'XNAS', 'AAPL')
    assert cs.resolve_provider_symbol(conn, 'pa', 'AAPL') == cs.resolve_provider_symbol(conn, 'pb', 'AAPL') == [iid]


def test_alias_is_provider_scoped_and_never_guessed(conn):
    cs.add_venue_alias(conn, 'pa', 'NASDAQ', 'XNAS')
    assert cs.resolve_venue(conn, 'pa', 'nasdaq') == 'XNAS'
    assert cs.resolve_venue(conn, 'pb', 'NASDAQ') is None
    assert cs.resolve_venue(conn, 'pa', 'XNAS') is None  # canonical id is not an implicit alias
    with pytest.raises(ValueError):
        cs.sync_listing(conn, 'pb', 'u', [_s('OK'), {'asset_class': 'equity', 'provider_venue': 'NASDAQ', 'symbol': 'X'}])
    assert cs.catalog_counts(conn)['total'] == 0


def test_alias_conflicts_and_idempotence(conn):
    cs.add_venue_alias(conn, 'pa', 'NASDAQ', 'XNAS')
    cs.add_venue_alias(conn, 'pa', 'NASDAQ', 'xnas')  # same mapping: no-op
    with pytest.raises(ValueError):
        cs.add_venue_alias(conn, 'pa', 'NASDAQ', 'XNYS')
    with pytest.raises(ValueError):
        cs.add_venue_alias(conn, 'pa', '', 'XNAS')
    with pytest.raises(ValueError):  # explicit venue contradicting the alias
        cs.sync_listing(conn, 'pa', 'u', [{'asset_class': 'equity', 'provider_venue': 'NASDAQ',
                                           'venue': 'XNYS', 'symbol': 'X'}])
    assert conn.execute('SELECT COUNT(*) FROM venue_alias').fetchone()[0] == 1


def test_v1_database_migrates_to_v2_without_data_loss(db_path):
    raw = sqlite3.connect(db_path)
    for stmt in cs._MIGRATIONS[1]:
        raw.execute(stmt)
    raw.execute('PRAGMA user_version=1')
    raw.execute("INSERT INTO venue VALUES ('XNAS','equity','t')")
    raw.commit()
    raw.close()
    c = cs.connect(db_path)
    assert cs.schema_version(c) == 2
    assert c.execute("SELECT COUNT(*) FROM venue").fetchone()[0] == 1
    c.close()
