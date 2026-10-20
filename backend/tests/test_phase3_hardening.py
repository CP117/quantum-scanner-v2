"""Phase 3 hardening: schema ownership, failover x ordering, API matrix, leakage.

Temporary DBs and sanitized fixtures only; no network access.
"""
from __future__ import annotations

import ast
import copy
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.ingestion import cohort, market_state_store as store  # noqa: E402
from app.ingestion.rights import registry_from_config  # noqa: E402
from app.ingestion.service import Source, run_ingestion  # noqa: E402
from app.routes import catalog as catalog_routes, market_state as ms_routes  # noqa: E402
from app.services import catalog_service as cs  # noqa: E402

FIX = Path(__file__).parent / 'fixtures' / 'ingestion'
T0 = 1718046000
NOW = float(T0 + 600)
NOSLEEP = lambda s: None  # noqa: E731


def raw(sym):
    return json.loads((FIX / f'yahoo_chart_{sym}.json').read_text())


def payload(sym='AAPL', ts=T0, price=190.5, **meta):
    p = copy.deepcopy(raw(sym))
    p['chart']['result'][0]['meta'].update(regularMarketTime=ts, regularMarketPrice=price, **meta)
    return p


def boom(sym):
    raise RuntimeError('HTTP 503')


# --------------------------------------------------------------- schema ownership
def _phase2_db(path):
    """A catalog DB exactly as Phase 2 leaves it (no market-state tables)."""
    c = cs.connect(str(path))
    cohort.bootstrap(c)
    assert c.execute("SELECT name FROM sqlite_master WHERE name LIKE 'market_%'").fetchall() == []
    return c


def test_upgrade_from_phase2_preserves_catalog_and_is_idempotent(tmp_path):
    c = _phase2_db(tmp_path / 'p2.db')
    before = [tuple(r) for r in c.execute('SELECT * FROM instrument ORDER BY instrument_id')]
    uv = cs.schema_version(c)
    assert store.schema_version(c) == 0
    assert store.ensure_schema(c) == store.SCHEMA_VERSION == store.schema_version(c)
    assert store.ensure_schema(c) == store.SCHEMA_VERSION  # idempotent
    assert [tuple(r) for r in c.execute('SELECT * FROM instrument ORDER BY instrument_id')] == before
    assert cs.schema_version(c) == uv  # catalog PRAGMA user_version is not touched
    c.close()
    c2 = cs.connect(str(tmp_path / 'p2.db'))  # reopen = restart
    assert store.schema_version(c2) == 1
    c2.close()


def test_failed_migration_rolls_back_tables_and_version(tmp_path, monkeypatch):
    c = _phase2_db(tmp_path / 'rb.db')
    # the valid CREATE ... IF NOT EXISTS statements succeed first; the invalid one must undo them
    bad = {1: store._MIGRATIONS[1] + ['THIS IS NOT SQL']}
    monkeypatch.setattr(store, '_MIGRATIONS', bad)
    with pytest.raises(sqlite3.Error):
        store.ensure_schema(c)
    assert store.schema_version(c) == 0
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE name LIKE 'market_%'")}
    assert names == set()  # earlier statements in the same migration were rolled back
    assert not c.in_transaction
    monkeypatch.undo()
    assert store.ensure_schema(c) == 1  # a later correct run succeeds
    c.close()


def test_unsupported_newer_version_refused_without_changes(tmp_path):
    c = _phase2_db(tmp_path / 'new.db')
    store.ensure_schema(c)
    c.execute('INSERT INTO market_state_schema (version) VALUES (99)')
    with pytest.raises(store.MarketStateSchemaError):
        store.ensure_schema(c)
    assert c.execute('SELECT COUNT(*) FROM market_state_schema').fetchone()[0] == 2
    with pytest.raises(store.MarketStateSchemaError):
        run_ingestion(c, [Source('yahoo-chart', lambda s: raw('AAPL'))], ['AAPL'], clock=lambda: NOW, sleep=NOSLEEP)
    c.close()


def test_catalog_rejects_unsupported_catalog_version_before_market_state(tmp_path):
    p = tmp_path / 'cat99.db'
    cs.connect(str(p)).close()
    raw_c = sqlite3.connect(str(p))
    raw_c.execute('PRAGMA user_version=99')
    raw_c.close()
    with pytest.raises(cs.CatalogSchemaError):
        cs.connect(str(p))


# --------------------------------------------------------------- failover x ordering
@pytest.fixture()
def two_provider_db(tmp_path):
    p = str(tmp_path / 'fo.db')
    c = cs.connect(p)
    cohort.bootstrap(c)
    for a, v in cohort.VENUE_ALIASES.items():
        cs.add_venue_alias(c, 'backup', a, v)
    cs.sync_listing(c, 'backup', 'bk', [dict(r, provider_symbol=r['symbol']) for r in cohort.COHORT])
    c.close()
    return p


def run(db, primary, backup, symbols=('AAPL',), **kw):
    c = cs.connect(db)
    try:
        return run_ingestion(c, [Source('yahoo-chart', primary), Source('backup', backup)], list(symbols),
                             clock=lambda: NOW, sleep=NOSLEEP, max_retries=0, **kw)
    finally:
        c.close()


def rows(db):
    c = cs.connect(db)
    try:
        return {r['provider']: dict(r) for r in c.execute(
            'SELECT * FROM market_state_latest WHERE instrument_id=(SELECT instrument_id FROM instrument'
            " WHERE display_ticker='AAPL')")}
    finally:
        c.close()


def test_failover_x_ordering_sequence(two_provider_db):
    db = two_provider_db
    # 1. primary healthy at T0
    s = run(db, lambda s: payload(ts=T0, price=100.0), lambda s: payload(ts=T0 + 50, price=999.0))
    assert s['detail']['AAPL'] == 'applied via yahoo-chart'
    assert set(rows(db)) == {'yahoo-chart'}  # backup never consulted while primary works

    # 2. primary down; backup has a NEWER quote -> stored under backup with correct provenance
    s = run(db, boom, lambda s: payload(ts=T0 + 100, price=101.0))
    assert s['detail']['AAPL'] == 'applied via backup (failover)'
    r = rows(db)
    assert r['backup']['price'] == 101.0 and r['backup']['source_ts'] == T0 + 100
    assert r['yahoo-chart']['price'] == 100.0 and r['yahoo-chart']['source_ts'] == T0  # untouched, not blended

    # 3. backup alone cannot overwrite itself with older data
    s = run(db, boom, lambda s: payload(ts=T0 + 10, price=5.0))
    assert s['counts'] == {'rejected_older': 1} and rows(db)['backup']['price'] == 101.0

    # 4. backup unknown timestamp cannot replace its known-ts row
    s = run(db, boom, lambda s: payload(ts=None, price=7.0))
    assert s['counts'] == {'rejected_unknown_ts': 1} and rows(db)['backup']['price'] == 101.0

    # 5. equal-time correction is a conflict: NOT silently applied, stored value kept
    s = run(db, boom, lambda s: payload(ts=T0 + 100, price=102.0))
    assert s['counts'] == {'rejected_conflict': 1} and rows(db)['backup']['price'] == 101.0
    s = run(db, boom, lambda s: payload(ts=T0 + 100, price=101.0))
    assert s['counts'] == {'duplicate': 1}

    # 6. primary recovers with a newer quote: primary row advances, backup row stays, provenance exact
    s = run(db, lambda s: payload(ts=T0 + 200, price=103.0), lambda s: (_ for _ in ()).throw(AssertionError))
    assert s['detail']['AAPL'] == 'applied via yahoo-chart'
    r = rows(db)
    assert r['yahoo-chart']['price'] == 103.0 and r['backup']['price'] == 101.0
    assert {k: v['venue'] for k, v in r.items()} == {'yahoo-chart': 'XNAS', 'backup': 'XNAS'}


def test_primary_older_response_is_ordering_rejection_not_failover(two_provider_db):
    db = two_provider_db
    run(db, lambda s: payload(ts=T0 + 100, price=1.0), boom)
    s = run(db, lambda s: payload(ts=T0, price=2.0), lambda s: payload(ts=T0 + 500, price=3.0))
    assert s['counts'] == {'rejected_older': 1}
    assert set(rows(db)) == {'yahoo-chart'}  # backup not consulted: the primary fetch succeeded


def test_both_providers_fail_keeps_all_state(two_provider_db):
    db = two_provider_db
    run(db, lambda s: payload(ts=T0, price=1.0), boom)
    s = run(db, boom, boom)
    assert s['counts'] == {'provider_failed': 1}
    assert rows(db)['yahoo-chart']['price'] == 1.0


def test_failover_venue_mismatch_does_not_store(two_provider_db):
    bad = payload(ts=T0 + 5, exchangeName='NYQ')  # AAPL is XNAS
    s = run(two_provider_db, boom, lambda s: bad)
    assert s['counts'] == {'provider_failed': 1} and rows(two_provider_db) == {}


# --------------------------------------------------------------- API matrix + leakage
def client_for(db, ent='yahoo-chart:quote:rev', now=NOW):
    app = FastAPI()
    app.include_router(ms_routes.router)

    def dep():
        c = cs.connect(db)
        try:
            yield c
        finally:
            c.close()
    app.dependency_overrides[catalog_routes.get_catalog_conn] = dep
    app.dependency_overrides[ms_routes.get_entitlements] = lambda: registry_from_config(ent)
    app.dependency_overrides[ms_routes.get_now] = lambda: now
    return TestClient(app)


@pytest.fixture()
def seeded(tmp_path):
    p = str(tmp_path / 'api.db')
    c = cs.connect(p)
    cohort.bootstrap(c)
    run_ingestion(c, [Source('yahoo-chart', lambda s: raw(s))], ['AAPL', 'MSFT', 'JPM', 'SPY'],
                  clock=lambda: NOW, sleep=NOSLEEP)
    c.close()
    return p


def items(resp):
    assert resp.status_code == 200, resp.text
    return {i['display_ticker']: i for i in resp.json()['items']}


def test_api_matrix(seeded):
    ok = items(client_for(seeded).get('/api/market/catalog'))
    assert ok['AAPL']['market_state'][0]['price']['state'] == 'ok'                 # present
    assert ok['MSFT']['market_state'][0]['volume'] == {'value': 0.0, 'state': 'zero'}  # legitimate zero
    assert ok['JPM']['market_state'][0]['volume']['state'] == 'unavailable'        # missing field
    assert ok['KO']['market_state'] is None                                        # missing quote, still listed
    stale = items(client_for(seeded, now=NOW + 86400).get('/api/market/catalog'))
    assert stale['AAPL']['market_state'][0]['price']['state'] == 'stale'
    assert stale['AAPL']['market_state'][0]['price']['value'] == 190.5            # stale keeps value + state
    unsupported = _unsupported_asset_class(seeded)
    assert unsupported['counts'] == {'unsupported_asset_class': 1}


def _unsupported_asset_class(db):
    c = cs.connect(db)
    try:
        cs.add_venue_alias(c, 'yahoo-chart', 'CCC', 'COINBASE', kind='crypto')
        cs.sync_listing(c, 'yahoo-chart', 'cx', [{'asset_class': 'crypto_pair', 'venue': 'COINBASE', 'symbol': 'BTC',
                                                  'quote_symbol': 'USD', 'provider_symbol': 'BTC-USD'}])
        return run_ingestion(c, [Source('yahoo-chart', boom)], ['BTC-USD'], clock=lambda: NOW, sleep=NOSLEEP)
    finally:
        c.close()


def test_stopped_ingestion_and_provider_failure_still_serve_last_state(seeded):
    # "stopped ingestion": no ingestion process at all, repeated GETs serve identical data
    c = client_for(seeded)
    a, b = c.get('/api/market/catalog').json(), c.get('/api/market/catalog').json()
    assert a == b
    # provider failure run in between
    cc = cs.connect(seeded)
    run_ingestion(cc, [Source('yahoo-chart', boom)], ['AAPL', 'MSFT'], clock=lambda: NOW, sleep=NOSLEEP, max_retries=0)
    cc.close()
    assert c.get('/api/market/catalog').json() == a


def test_denied_display_leaks_nothing(seeded):
    secrets = ['190.5', '420.25', '200.1', '530.0', '1718046000', 'yahoo-chart', 'price', 'volume',
               'ingested_at', 'currency', 'USD']
    for ent in ('', 'yahoo-chart:quote:', 'x:quote:y', 'yahoo-chart:short_interest:ops'):
        r = client_for(seeded, ent).get('/api/market/catalog', params={'limit': 200})
        body = r.text
        assert r.status_code == 200
        for s in secrets:
            assert s not in body, f'{s!r} leaked with entitlements {ent!r}'
        got = items(r)
        assert got['AAPL']['market_state'] is None
        assert got['AAPL']['market_state_withheld'] == 'display_rights_not_verified'
        assert set(got['AAPL']) == {'instrument_id', 'asset_class', 'instrument_type', 'venue_id', 'native_symbol',
                                    'display_ticker', 'quote_symbol', 'name', 'status', 'status_changed_at',
                                    'first_seen_at', 'last_seen_at', 'market_state', 'market_state_withheld'}


def test_partial_entitlement_shows_only_entitled_provider(two_provider_db):
    db = two_provider_db
    run(db, lambda s: payload(ts=T0, price=100.0), boom)
    run(db, boom, lambda s: payload(ts=T0 + 10, price=111.25))
    got = items(client_for(db, 'backup:quote:rev').get('/api/market/catalog'))
    ms = got['AAPL']['market_state']
    assert [q['provider'] for q in ms] == ['backup']
    body = client_for(db, 'backup:quote:rev').get('/api/market/catalog').text
    assert '100.0' not in body and 'yahoo-chart' not in body


def test_error_responses_do_not_leak_state(seeded):
    r = client_for(seeded, '').get('/api/market/catalog', params={'after': 'garbage'})
    assert r.status_code == 400 and '190.5' not in r.text
    r = client_for(seeded, '').get('/api/market/catalog', params={'limit': 0})
    assert r.status_code == 422 and '190.5' not in r.text


def test_route_has_no_diagnostic_or_write_surface():
    paths = [(r.path, sorted(r.methods)) for r in ms_routes.router.routes]
    assert paths == [('/api/market/catalog', ['GET'])]


def test_ingestion_and_route_do_not_import_analysis_or_scoring():
    """Disabled analysis, enforced statically: the ingestion boundary and the route may import
    only the catalog service from app.services (no scoring/prediction/provider modules)."""
    allowed = {'app.services.catalog_service', 'app.utils.time', 'app.services.http_client'}
    root = Path(__file__).resolve().parents[2]
    files = list((root / 'app' / 'ingestion').glob('*.py')) + [root / 'app' / 'routes' / 'market_state.py']
    offenders = []
    for f in files:
        for n in ast.walk(ast.parse(f.read_text(encoding='utf-8'))):
            mods = []
            if isinstance(n, ast.ImportFrom) and n.module:
                mods = [n.module] + [f'{n.module}.{a.name}' for a in n.names]
            elif isinstance(n, ast.Import):
                mods = [a.name for a in n.names]
            for m in mods:
                if (m.startswith(('app.services', 'app.regulatory')) and m not in allowed
                        and not any(m == a or m.startswith(a + '.') or a.startswith(m + '.') for a in allowed)):
                    offenders.append((f.name, m))
    assert not offenders, offenders


def test_get_runs_with_analysis_and_provider_entrypoints_poisoned(seeded, monkeypatch):
    import app.ingestion.yahoo_chart_adapter as ya

    def poison(*a, **k):
        raise AssertionError('must not be called from GET')
    monkeypatch.setattr(ya, 'fetch_raw', poison)
    monkeypatch.setattr(store, 'apply_quote', poison)
    monkeypatch.setattr(store, 'ensure_schema', poison)
    monkeypatch.setattr(store, 'record_run', poison)
    import socket
    monkeypatch.setattr(socket.socket, 'connect', poison)
    assert client_for(seeded).get('/api/market/catalog').status_code == 200
