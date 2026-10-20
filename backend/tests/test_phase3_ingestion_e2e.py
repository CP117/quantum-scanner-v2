"""Phase 3 end-to-end slice: catalog -> explicit ingestion -> read-only market state.

Temporary DBs and sanitized fixtures only; no network access.
"""
from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.ingestion import cohort, market_state_store as store  # noqa: E402
from app.ingestion.cli import main as cli_main  # noqa: E402
from app.ingestion.contracts import DataKind, DataQuality, DataState, TimestampSemantics  # noqa: E402
from app.ingestion.resilience import FetchRefused, ProviderHealth, RateLimitBudget  # noqa: E402
from app.ingestion.rights import registry_from_config  # noqa: E402
from app.ingestion.service import Source, run_ingestion  # noqa: E402
from app.ingestion.yahoo_chart_adapter import UnusablePayload, adapt_quote  # noqa: E402
from app.routes import catalog as catalog_routes, market_state as ms_routes  # noqa: E402
from app.services import catalog_service as cs  # noqa: E402

FIX = Path(__file__).parent / 'fixtures' / 'ingestion'
NOW = 1718046600.0  # 10 min after the fixtures' regularMarketTime (1718046000)
NOSLEEP = lambda s: None  # noqa: E731


def raw(sym):
    return json.loads((FIX / f'yahoo_chart_{sym}.json').read_text())


def fixture_fetch(sym):
    return raw(sym)


@pytest.fixture()
def db(tmp_path):
    p = str(tmp_path / 'c.db')
    c = cs.connect(p)
    cohort.bootstrap(c)
    c.close()
    return p


def make_client(db, entitlements='yahoo-chart:quote:ops-review'):
    app = FastAPI()
    app.include_router(ms_routes.router)

    def dep():
        c = cs.connect(db)
        try:
            yield c
        finally:
            c.close()
    app.dependency_overrides[catalog_routes.get_catalog_conn] = dep
    app.dependency_overrides[ms_routes.get_entitlements] = lambda: registry_from_config(entitlements)
    app.dependency_overrides[ms_routes.get_now] = lambda: NOW
    return TestClient(app)


def by_ticker(resp):
    return {i['display_ticker']: i for i in resp.json()['items']}


def ingest(db, fetch=fixture_fetch, symbols=None, clock=lambda: NOW, **kw):
    c = cs.connect(db)
    try:
        return run_ingestion(c, [Source('yahoo-chart', fetch)], symbols or [r['provider_symbol'] for r in cohort.COHORT],
                             clock=clock, sleep=NOSLEEP, **kw)
    finally:
        c.close()


# -- adapter / semantics ---------------------------------------------------
def test_adapter_normalizes_real_payload_shape():
    q, venue = adapt_quote(raw('AAPL'), now_ts=NOW)
    assert venue == 'NMS' and q.symbol == 'AAPL'
    assert q.price.state is DataState.OK and q.price.value == 190.5
    assert q.meta.currency == 'USD' and q.meta.source_ts == 1718046000.0
    assert q.meta.timestamp_semantics is TimestampSemantics.PROVIDER_UTC
    assert q.meta.kind is DataKind.QUOTE and q.meta.quality is DataQuality.UNKNOWN


def test_adapter_zero_missing_and_ambiguous_timestamp():
    assert adapt_quote(raw('MSFT'), now_ts=NOW)[0].volume.state is DataState.ZERO
    assert adapt_quote(raw('JPM'), now_ts=NOW)[0].volume.state is DataState.UNAVAILABLE
    for bad in (None, 1718046000000, 'x', NOW + 10_000, True):
        p = copy.deepcopy(raw('AAPL'))
        p['chart']['result'][0]['meta']['regularMarketTime'] = bad
        q = adapt_quote(p, now_ts=NOW)[0]
        assert q.meta.source_ts is None and q.meta.timestamp_semantics is TimestampSemantics.AMBIGUOUS


def test_adapter_rejects_provider_error():
    with pytest.raises(UnusablePayload):
        adapt_quote(raw('ERR'), now_ts=NOW)


# -- the demonstrations ----------------------------------------------------
def test_browse_before_ingestion_then_quotes_appear(db):
    client = make_client(db)
    before = by_ticker(client.get('/api/market/catalog'))
    assert set(before) == {'AAPL', 'MSFT', 'JPM', 'SPY', 'KO'}
    assert all(i['market_state'] is None for i in before.values())

    summary = ingest(db)
    assert summary['counts']['applied'] == 4 and summary['counts']['provider_failed'] == 1  # KO has no fixture
    after = by_ticker(client.get('/api/market/catalog'))
    assert after['AAPL']['market_state'][0]['price'] == {'value': 190.5, 'state': 'ok'}
    assert after['MSFT']['market_state'][0]['volume'] == {'value': 0.0, 'state': 'zero'}
    assert after['JPM']['market_state'][0]['volume']['state'] == 'unavailable'
    assert after['AAPL']['market_state'][0]['venue'] == 'XNAS'
    assert after['KO']['market_state'] is None  # still visible, no quote


def test_stale_state_computed_at_read(db):
    ingest(db)
    r = make_client(db).get('/api/market/catalog', params={'max_age_seconds': 60})
    assert by_ticker(r)['AAPL']['market_state'][0]['price']['state'] == 'stale'


def test_get_does_not_contact_providers_or_ingest(db, monkeypatch):
    import app.ingestion.yahoo_chart_adapter as ya
    monkeypatch.setattr(ya, 'fetch_raw', lambda s: (_ for _ in ()).throw(AssertionError('provider called')))
    monkeypatch.setattr(ms_routes.store, 'apply_quote', lambda *a, **k: (_ for _ in ()).throw(AssertionError('write')))
    assert make_client(db).get('/api/market/catalog').status_code == 200


def test_restart_persistence(db):
    ingest(db)
    # new connection / new app = restart; no ingestion running
    got = by_ticker(make_client(db).get('/api/market/catalog'))
    assert got['SPY']['market_state'][0]['price']['value'] == 530.0


def test_older_update_rejected_and_duplicate_idempotent(db):
    ingest(db)
    older = copy.deepcopy(raw('AAPL'))
    older['chart']['result'][0]['meta'].update(regularMarketTime=1718045000, regularMarketPrice=1.0)
    s = ingest(db, fetch=lambda sym: older, symbols=['AAPL'])
    assert s['counts'] == {'rejected_older': 1}
    s = ingest(db, symbols=['AAPL'])
    assert s['counts'] == {'duplicate': 1}
    conflict = copy.deepcopy(raw('AAPL'))
    conflict['chart']['result'][0]['meta']['regularMarketPrice'] = 2.0
    assert ingest(db, fetch=lambda sym: conflict, symbols=['AAPL'])['counts'] == {'rejected_conflict': 1}
    newer = copy.deepcopy(raw('AAPL'))
    newer['chart']['result'][0]['meta'].update(regularMarketTime=1718046300, regularMarketPrice=191.0)
    assert ingest(db, fetch=lambda sym: newer, symbols=['AAPL'])['counts'] == {'applied': 1}
    px = by_ticker(make_client(db).get('/api/market/catalog'))['AAPL']['market_state'][0]['price']['value']
    assert px == 191.0


def test_unknown_timestamp_behavior(db):
    unk = copy.deepcopy(raw('AAPL'))
    unk['chart']['result'][0]['meta']['regularMarketTime'] = None
    assert ingest(db, fetch=lambda s: unk, symbols=['AAPL'])['counts'] == {'applied': 1}  # nothing stored yet
    unk2 = copy.deepcopy(unk)
    unk2['chart']['result'][0]['meta']['regularMarketPrice'] = 195.0
    assert ingest(db, fetch=lambda s: unk2, symbols=['AAPL'])['counts'] == {'applied': 1}  # ingestion order
    assert ingest(db, symbols=['AAPL'])['counts'] == {'applied': 1}  # known ts replaces unknown
    assert ingest(db, fetch=lambda s: unk, symbols=['AAPL'])['counts'] == {'rejected_unknown_ts': 1}
    q = by_ticker(make_client(db).get('/api/market/catalog'))['AAPL']['market_state'][0]
    assert q['source_ts'] == 1718046000.0 and q['price']['value'] == 190.5


def test_provider_failure_keeps_catalog_and_last_quote(db):
    ingest(db)

    def boom(sym):
        raise RuntimeError('HTTP 503')
    s = ingest(db, fetch=boom, max_retries=1)
    assert s['counts'] == {'provider_failed': 5}
    got = by_ticker(make_client(db).get('/api/market/catalog'))
    assert set(got) == {'AAPL', 'MSFT', 'JPM', 'SPY', 'KO'}
    assert got['AAPL']['market_state'][0]['price']['value'] == 190.5


def test_explicit_failover_is_labelled_and_never_blended(db):
    c = cs.connect(db)
    for a, v in cohort.VENUE_ALIASES.items():
        cs.add_venue_alias(c, 'backup', a, v)
    cs.sync_listing(c, 'backup', 'bk', [dict(r, provider_symbol=r['symbol']) for r in cohort.COHORT])

    def boom(sym):
        raise RuntimeError('down')
    s = run_ingestion(c, [Source('yahoo-chart', boom), Source('backup', fixture_fetch)], ['AAPL'],
                      clock=lambda: NOW, sleep=NOSLEEP, max_retries=0)
    assert s['detail']['AAPL'] == 'applied via backup (failover)'
    c.close()
    ms = by_ticker(make_client(db, 'backup:quote:ops').get('/api/market/catalog'))['AAPL']['market_state']
    assert [q['provider'] for q in ms] == ['backup']


def test_unaliased_or_mismatched_venue_rejected(db):
    bad = copy.deepcopy(raw('AAPL'))
    bad['chart']['result'][0]['meta']['exchangeName'] = 'XXX'
    assert ingest(db, fetch=lambda s: bad, symbols=['AAPL'], max_retries=0)['counts'] == {'provider_failed': 1}
    wrong = copy.deepcopy(raw('AAPL'))
    wrong['chart']['result'][0]['meta']['exchangeName'] = 'NYQ'
    assert ingest(db, fetch=lambda s: wrong, symbols=['AAPL'], max_retries=0)['counts'] == {'provider_failed': 1}
    assert ingest(db, symbols=['NOPE'])['counts'] == {'unmapped': 1}


def test_display_rights_enforced_at_response_boundary(db):
    ingest(db)
    for ent in ('', 'yahoo-chart:quote:', 'other:quote:ops', 'yahoo-chart:bogus:ops'):
        got = by_ticker(make_client(db, ent).get('/api/market/catalog'))
        assert set(got) == {'AAPL', 'MSFT', 'JPM', 'SPY', 'KO'}
        assert got['AAPL']['market_state'] is None
        assert got['AAPL']['market_state_withheld'] == 'display_rights_not_verified'
        assert 'market_state_withheld' not in got['KO']


def test_cli_ingest_with_fixtures_and_no_ingest_on_import(tmp_path, capsys):
    p = str(tmp_path / 'cli.db')
    assert cli_main(['ingest', '--db', p, '--fixtures', str(FIX)]) == 0  # not bootstrapped: all unmapped
    assert '"unmapped": 5' in capsys.readouterr().out
    assert cli_main(['bootstrap', '--db', p]) == 0
    capsys.readouterr()
    assert cli_main(['ingest', '--db', p, '--fixtures', str(FIX)]) == 0
    assert '"applied": 4' in capsys.readouterr().out


def test_main_mounts_market_route_and_has_no_ingestion_hook():
    # Source check: importing app.main starts legacy services and rewrites data/ files.
    main_src = (Path(__file__).parents[2] / 'app' / 'main.py').read_text(encoding='utf-8')
    assert 'app.routes.market_state' in main_src
    assert 'app.ingestion.cli' not in main_src and 'run_ingestion' not in main_src
    assert [r.path for r in ms_routes.router.routes] == ['/api/market/catalog']
    assert all('GET' in r.methods and len(r.methods) <= 2 for r in ms_routes.router.routes)
