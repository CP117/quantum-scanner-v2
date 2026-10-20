"""Basic measurements for the Phase 3 ingestion slice (synthetic data, temp DB, no network).

    python scripts/measure_ingestion.py [--n 500] [--json]

Reports ingestion time, provider-call and write counts, API latency, reader/writer lock behaviour
and RSS/fd stability. These are observations on one machine, not benchmarks or improvement claims.
"""
import json
import os
import statistics
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


def payload(sym, ts, price):
    return {'chart': {'result': [{'meta': {
        'currency': 'USD', 'symbol': sym, 'exchangeName': 'NMS', 'regularMarketTime': ts,
        'regularMarketPrice': price, 'regularMarketVolume': 1000}}], 'error': None}}


def rss_mb():
    try:
        import psutil
        p = psutil.Process()
        return round(p.memory_info().rss / 1e6, 1), p.num_fds()
    except Exception:  # noqa: BLE001
        return None, None


def pct(xs, q):
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(q * len(xs)))] * 1000, 2)


def main() -> int:
    n = int(sys.argv[sys.argv.index('--n') + 1]) if '--n' in sys.argv else 500
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.ingestion import market_state_store as store
    from app.ingestion.rights import registry_from_config
    from app.ingestion.service import Source, run_ingestion
    from app.routes import catalog as catalog_routes, market_state as ms
    from app.services import catalog_service as cs

    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, 'm.db')
    c = cs.connect(db)
    cs.add_venue_alias(c, 'yahoo-chart', 'NMS', 'XNAS')
    syms = [f'S{i:04d}' for i in range(n)]
    cs.sync_listing(c, 'yahoo-chart', 'syn', [{'asset_class': 'equity', 'venue': 'XNAS', 'symbol': s,
                                              'provider_symbol': s} for s in syms])
    calls = {'fetch': 0}
    clock_t = [1_800_000_000.0]
    lock = threading.Lock()

    def fetch(sym):
        with lock:
            calls['fetch'] += 1
        return payload(sym, int(clock_t[0]), 10.0 + clock_t[0] % 7)

    out = {'instruments': n}
    changes0 = c.total_changes
    t = time.perf_counter()
    s1 = run_ingestion(c, [Source('yahoo-chart', fetch)], syms, clock=lambda: clock_t[0], sleep=lambda x: None)
    out['ingest_first_run_s'] = round(time.perf_counter() - t, 3)
    out['first_run_counts'] = s1['counts']
    out['provider_calls_first_run'] = calls['fetch']
    out['sqlite_row_changes_first_run'] = c.total_changes - changes0

    changes0, calls['fetch'] = c.total_changes, 0
    t = time.perf_counter()
    s2 = run_ingestion(c, [Source('yahoo-chart', fetch)], syms, clock=lambda: clock_t[0], sleep=lambda x: None)
    out['ingest_rerun_identical_s'] = round(time.perf_counter() - t, 3)
    out['rerun_counts'] = s2['counts']
    out['sqlite_row_changes_rerun_identical'] = c.total_changes - changes0  # only the run-log row if idempotent

    app = FastAPI()
    app.include_router(ms.router)
    app.dependency_overrides[ms.get_entitlements] = lambda: registry_from_config('yahoo-chart:quote:measure')
    app.dependency_overrides[ms.get_now] = lambda: clock_t[0] + 1

    def dep():
        cc = cs.connect(db)
        try:
            yield cc
        finally:
            cc.close()
    app.dependency_overrides[catalog_routes.get_catalog_conn] = dep
    client = TestClient(app)
    lat = []
    for _ in range(100):
        t = time.perf_counter()
        r = client.get('/api/market/catalog', params={'limit': 200})
        lat.append(time.perf_counter() - t)
        assert r.status_code == 200
    out['api_get_limit200_ms'] = {'p50': pct(lat, .5), 'p95': pct(lat, .95), 'max': pct(lat, 1.0)}

    # lock behaviour: readers while a writer ingests new data
    stop, errs, rl = threading.Event(), [], []

    def reader():
        while not stop.is_set():
            t = time.perf_counter()
            try:
                r = client.get('/api/market/catalog', params={'limit': 50})
                if r.status_code != 200:
                    errs.append(r.status_code)
            except Exception as exc:  # noqa: BLE001
                errs.append(repr(exc)[:80])
            rl.append(time.perf_counter() - t)
    ths = [threading.Thread(target=reader) for _ in range(4)]
    [x.start() for x in ths]
    t = time.perf_counter()
    for i in range(3):
        clock_t[0] += 60
        run_ingestion(c, [Source('yahoo-chart', fetch)], syms, clock=lambda: clock_t[0], sleep=lambda x: None)
    wr = time.perf_counter() - t
    stop.set()
    [x.join() for x in ths]
    out['concurrent'] = {'writer_3_runs_s': round(wr, 3), 'reader_requests': len(rl), 'reader_errors': len(errs),
                         'reader_p95_ms': pct(rl, .95), 'reader_max_ms': pct(rl, 1.0), 'errors_sample': errs[:3]}

    # resource stability: repeated ingest + GET cycles
    r0 = rss_mb()
    for i in range(10):
        clock_t[0] += 60
        run_ingestion(c, [Source('yahoo-chart', fetch)], syms, clock=lambda: clock_t[0], sleep=lambda x: None)
        client.get('/api/market/catalog', params={'limit': 200})
    r1 = rss_mb()
    out['resources'] = {'rss_mb_before': r0[0], 'rss_mb_after_10_cycles': r1[0], 'fds_before': r0[1], 'fds_after': r1[1],
                        'db_bytes': os.path.getsize(db), 'wal_bytes': os.path.getsize(db + '-wal') if os.path.exists(db + '-wal') else 0}
    c.close()
    print(json.dumps(out, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
