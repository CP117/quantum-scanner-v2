"""Phase 2 catalog: identity/collision, idempotency, non-destructive retention."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from app.services import catalog_service as cs  # noqa: E402


@pytest.fixture()
def conn(tmp_path):
    c = cs.connect(str(tmp_path / 'catalog.db'))
    yield c
    c.close()


def _s(sym, venue='XNAS', **kw):
    return {'asset_type': 'stock', 'venue': venue, 'symbol': sym, **kw}


def test_same_ticker_different_venue_and_asset_type_do_not_collide(conn):
    cs.sync_listing(conn, 'p', 'u', [
        _s('ABC', 'XNAS'), _s('ABC', 'XNYS'),
        {'asset_type': 'crypto_pair', 'venue': 'BINANCE', 'symbol': 'ABC', 'quote_symbol': 'USDT'},
        {'asset_type': 'crypto_pair', 'venue': 'KRAKEN', 'symbol': 'ABC', 'quote_symbol': 'USDT'},
        {'asset_type': 'crypto_asset', 'venue': 'AGG', 'symbol': 'ABC'},
    ])
    assert cs.catalog_counts(conn)['cataloged'] == 5


def test_share_classes_are_distinct(conn):
    cs.sync_listing(conn, 'p', 'u', [_s('BRK.A'), _s('BRK.B')])
    assert cs.catalog_counts(conn)['cataloged'] == 2


def test_crypto_validation(conn):
    with pytest.raises(ValueError):
        cs.sync_listing(conn, 'p', 'u', [{'asset_type': 'crypto_pair', 'venue': 'X', 'symbol': 'A'}])
    assert cs.catalog_counts(conn)['cataloged'] == 0


def test_sync_is_idempotent(conn):
    recs = [_s('AAA'), _s('BBB')]
    first = cs.sync_listing(conn, 'p', 'u', recs)
    second = cs.sync_listing(conn, 'p', 'u', recs)
    assert first['inserted'] == 2 and second['inserted'] == 0
    assert second['updated'] == 0 and second['marked_missing'] == 0
    assert cs.catalog_counts(conn)['cataloged'] == 2
    assert conn.execute('SELECT COUNT(*) FROM provider_instrument_mapping').fetchone()[0] == 2
    assert conn.execute('SELECT COUNT(*) FROM universe_membership').fetchone()[0] == 2


def test_omission_retains_then_reactivates(conn):
    cs.sync_listing(conn, 'p', 'u', [_s('AAA'), _s('BBB')])
    r = cs.sync_listing(conn, 'p', 'u', [_s('AAA')])
    assert r['marked_missing'] == 1
    by = {i['display_ticker']: i['status'] for i in cs.list_instruments(conn)['items']}
    assert by == {'AAA': 'active', 'BBB': 'missing'}
    for _ in range(cs.MISSING_GRACE_RUNS):
        cs.sync_listing(conn, 'p', 'u', [_s('AAA')])
    assert cs.list_instruments(conn, status='inactive')['items'][0]['display_ticker'] == 'BBB'
    r = cs.sync_listing(conn, 'p', 'u', [_s('AAA'), _s('BBB')])
    assert r['reactivated'] == 1
    assert cs.catalog_counts(conn)['by_status'] == {'active': 2}


def test_partial_listing_and_failed_provider_do_not_mark_missing(conn):
    cs.sync_listing(conn, 'p', 'u', [_s('AAA'), _s('BBB')])
    cs.sync_listing(conn, 'p', 'u', [_s('AAA')], complete=False)
    assert cs.catalog_counts(conn)['by_status'] == {'active': 2}


def test_other_provider_keeps_instrument_active_and_explicit_status_sticks(conn):
    cs.sync_listing(conn, 'p1', 'u1', [_s('AAA')])
    cs.sync_listing(conn, 'p2', 'u2', [_s('AAA')])
    cs.sync_listing(conn, 'p1', 'u1', [])
    assert cs.catalog_counts(conn)['by_status'] == {'active': 1}
    iid = cs.list_instruments(conn)['items'][0]['instrument_id']
    cs.set_instrument_status(conn, iid, 'delisted')
    cs.sync_listing(conn, 'p2', 'u2', [_s('AAA')])
    assert cs.catalog_counts(conn)['by_status'] == {'delisted': 1}


def test_keyset_pagination_and_visibility_without_analysis(conn):
    cs.sync_listing(conn, 'p', 'u', [_s(f'T{i:02d}') for i in range(5)])
    seen, cur = [], None
    while True:
        page = cs.list_instruments(conn, limit=2, after=cur)
        seen += [i['display_ticker'] for i in page['items']]
        cur = page['next_cursor']
        if not cur:
            break
    assert seen == [f'T{i:02d}' for i in range(5)]
