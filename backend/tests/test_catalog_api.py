"""HTTP tests for the catalog browse API (temporary DB, no external calls).

The first group mounts the router on a bare FastAPI app with an injected temp
DB; the last test imports the real ``app.main`` (with background network
refreshes stubbed and tier scanners skipped under pytest) to prove the routes
are mounted and use the configured temp DB.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.routes import catalog as catalog_routes  # noqa: E402
from app.services import catalog_service as cs  # noqa: E402


def _s(sym, venue='XNAS', **kw):
    return {'asset_class': 'equity', 'venue': venue, 'symbol': sym, **kw}


@pytest.fixture()
def db_path(tmp_path):
    p = str(tmp_path / 'api.db')
    c = cs.connect(p)
    cs.sync_listing(c, 'p', 'u', [_s(f'T{i:02d}') for i in range(25)] + [_s('T00', 'XNYS'), _s('SPY', 'ARCA', instrument_type='etf')])
    cs.sync_listing(c, 'p', 'u', [_s(f'T{i:02d}') for i in range(24)] + [_s('T00', 'XNYS'), _s('SPY', 'ARCA')])  # T24 -> missing
    c.close()
    return p


@pytest.fixture()
def client(db_path):
    app = FastAPI()
    app.include_router(catalog_routes.router)

    def dep():
        c = cs.connect(db_path)
        try:
            yield c
        finally:
            c.close()
    app.dependency_overrides[catalog_routes.get_catalog_conn] = dep
    return TestClient(app)


def _walk(client, limit, **params):
    items, cur = [], None
    for _ in range(100):
        r = client.get('/api/catalog/instruments', params={'limit': limit, **params, **({'after': cur} if cur else {})})
        assert r.status_code == 200, r.text
        items += r.json()['items']
        cur = r.json()['next_cursor']
        if not cur:
            return items
    raise AssertionError('pagination did not terminate')


def test_pagination_complete_and_unique(client):
    items = _walk(client, 7)
    ids = [i['instrument_id'] for i in items]
    assert len(ids) == len(set(ids)) == 27
    assert client.get('/api/catalog/counts').json()['total'] == 27


def test_filter_count_consistency(client):
    for params in ({}, {'venue': 'xnys'}, {'status': 'missing'}, {'asset_class': 'equity', 'q': 'T0'},
                   {'instrument_type': 'etf'}, {'universe': 'u'}, {'universe': 'other'}):
        walked = _walk(client, 5, **params)
        counts = client.get('/api/catalog/counts', params=params).json()
        assert counts['total'] == len(walked), params
        assert sum(counts['by_status'].values()) == counts['total']
    assert client.get('/api/catalog/counts', params={'status': 'missing'}).json()['total'] == 1


@pytest.mark.parametrize('params,code', [
    ({'limit': 0}, 422), ({'limit': 201}, 422), ({'limit': 'x'}, 422),
    ({'status': 'bogus'}, 400), ({'asset_class': 'stock'}, 400),
    ({'q': 'x' * 101}, 422), ({'after': 'x' * 513}, 422),
    ({'after': '!!!not-a-cursor'}, 400), ({'after': 'e30'}, 400),
])
def test_bounds_and_validation(client, params, code):
    assert client.get('/api/catalog/instruments', params=params).status_code == code


def test_cursor_bound_to_filters(client):
    cur = client.get('/api/catalog/instruments', params={'limit': 2}).json()['next_cursor']
    assert client.get('/api/catalog/instruments', params={'limit': 2, 'after': cur}).status_code == 200
    r = client.get('/api/catalog/instruments', params={'limit': 2, 'after': cur, 'venue': 'XNYS'})
    assert r.status_code == 400


def test_max_limit_ok(client):
    r = client.get('/api/catalog/instruments', params={'limit': 200})
    assert r.status_code == 200 and len(r.json()['items']) == 27 and r.json()['next_cursor'] is None


def test_browse_never_invokes_providers_sync_or_analysis(client, monkeypatch):
    import socket
    import httpx
    import requests

    def forbidden(*a, **k):
        raise AssertionError('external/provider/sync call during browse')
    monkeypatch.setattr(socket, 'getaddrinfo', forbidden)
    monkeypatch.setattr(requests.sessions.Session, 'request', forbidden)
    monkeypatch.setattr(httpx.AsyncClient, 'send', forbidden)  # TestClient itself uses httpx.Client
    monkeypatch.setattr(cs, 'sync_listing', forbidden)
    from app.services import universe_service
    monkeypatch.setattr(universe_service, 'load_universe', forbidden)
    assert client.get('/api/catalog/instruments').status_code == 200
    assert client.get('/api/catalog/counts').status_code == 200
    import ast
    tree = ast.parse(open(catalog_routes.__file__, encoding='utf-8').read())
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {(n.module or '') + '.' + a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
    assert imported <= {'fastapi.APIRouter', 'fastapi.Depends', 'fastapi.HTTPException', 'fastapi.Query',
                        'typing.Iterator', 'typing.Optional', '__future__.annotations',
                        'app.services.catalog_service'} | {'app.services.catalog_service'}, imported


def test_real_app_mounts_catalog_with_temp_db(tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, 'catalog_db_path', str(tmp_path / 'real.db'))
    monkeypatch.setattr(settings, 'watchlist_db_path', str(tmp_path / 'watchlists.db'))
    from app.services import universe_extras, cache_dedupe_service, wedge_watchdog
    monkeypatch.setattr(wedge_watchdog, 'start_wedge_watchdog', lambda *a, **k: None)
    monkeypatch.setattr(universe_extras, 'refresh_nasdaq_in_background', lambda *a, **k: None)
    monkeypatch.setattr(cache_dedupe_service, 'start_startup_audit', lambda *a, **k: None)
    import app.main as main  # noqa: WPS433
    c = TestClient(main.app)  # no lifespan: background startup tasks are not run
    r = c.get('/api/catalog/counts')
    assert r.status_code == 200 and r.json()['total'] == 0
    assert (tmp_path / 'real.db').exists()
    assert c.get('/api/catalog/instruments').json() == {'items': [], 'next_cursor': None}
