"""Exercise the REAL mounted app (app.main) with startup/shutdown (lifespan) against a
temporary catalog DB, and print a JSON report. Intended to be run from an isolated
copy of the repo (see backend/tests/test_phase3_real_app.py and docs/phase3_validation.md).

    CATALOG_DB_PATH=/tmp/x.db python scripts/probe_real_app.py [--seed]

``--seed`` bootstraps the cohort and ingests offline fixtures first (explicit, outside the app).
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


def main() -> int:
    seed = '--seed' in sys.argv
    if seed:
        from app.services import catalog_service as cs
        from app.ingestion import cohort
        from app.ingestion.cli import main as cli_main
        c = cs.connect()
        cohort.bootstrap(c)
        c.close()
        cli_main(['ingest', '--fixtures', os.path.join(os.path.dirname(__file__), '..', 'backend', 'tests',
                                                       'fixtures', 'ingestion')])
    from fastapi.testclient import TestClient
    from app.main import app
    out = {}

    def fingerprint():
        import hashlib
        from app.services import catalog_service as cs
        c = cs.connect()
        try:
            res = {}
            for t in ('instrument', 'provider_instrument_mapping', 'market_state_latest', 'market_ingestion_run'):
                try:
                    rows = [tuple(r) for r in c.execute(f'SELECT * FROM {t} ORDER BY 1,2,3')]  # nosec B608 - fixed table names
                except Exception:
                    rows = None
                res[t] = None if rows is None else hashlib.sha256(repr(rows).encode()).hexdigest()[:16]
            return res
        finally:
            c.close()
    out['db_before'] = fingerprint()
    t0 = time.perf_counter()
    with TestClient(app) as client:
        for name, url in (('market_catalog', '/api/market/catalog'),
                          ('catalog_instruments', '/api/catalog/instruments'),
                          ('catalog_counts', '/api/catalog/counts')):
            r = client.get(url)
            out[name] = {'status': r.status_code, 'body': r.json() if r.status_code == 200 else r.text[:200]}
    out['db_after'] = fingerprint()
    out['elapsed_s'] = round(time.perf_counter() - t0, 3)
    print('PROBE_JSON=' + json.dumps(out, sort_keys=True, default=str))
    return 0


if __name__ == '__main__':
    sys.exit(main())
