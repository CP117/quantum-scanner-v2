"""Real mounted-app integration (app.main with lifespan) in an ISOLATED COPY of the repo.

The legacy lifespan starts legacy scans/warmers that rewrite some files under data/
(pre-existing behaviour, also on the base commit). Running in a throwaway copy keeps the
working tree clean and lets us assert exactly which files change.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
# Files the LEGACY startup/scan machinery is known to rewrite (same on the base commit).
LEGACY_RUNTIME = {'data/known_bad_symbols.json', 'data/regulatory.db', 'data/watchlists.db',
                  'data/wedge_watchdog.json'}
_SKIP = ('.git', '__pycache__', 'cloudflared.exe', 'node_modules')


def _snapshot(root: Path) -> dict[str, str]:
    out = {}
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d not in _SKIP]
        for f in fn:
            if f.endswith(('.pyc', '-wal', '-shm')):
                continue
            p = Path(dp) / f
            out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


@pytest.fixture(scope='module')
def real_app_run(tmp_path_factory):
    iso = tmp_path_factory.mktemp('iso') / 'repo'
    shutil.copytree(ROOT, iso, ignore=shutil.ignore_patterns(*_SKIP, '*.pyc'))
    db = tmp_path_factory.mktemp('db') / 'catalog.db'
    env = {**os.environ, 'CATALOG_DB_PATH': str(db), 'PYTHONDONTWRITEBYTECODE': '1',
           'MARKET_DATA_DISPLAY_ENTITLEMENTS': 'yahoo-chart:quote:test-reviewer'}
    before = _snapshot(iso)
    proc = subprocess.run([sys.executable, 'scripts/probe_real_app.py', '--seed'], cwd=iso, env=env,
                          capture_output=True, text=True, timeout=300)
    after = _snapshot(iso)
    line = [ln for ln in proc.stdout.splitlines() if ln.startswith('PROBE_JSON=')]
    assert proc.returncode == 0 and line, proc.stdout[-2000:] + proc.stderr[-2000:]
    changed = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
    return json.loads(line[0][len('PROBE_JSON='):]), changed


def test_real_route_served_by_mounted_app(real_app_run):
    out, _ = real_app_run
    m = out['market_catalog']
    assert m['status'] == 200
    items = {i['display_ticker']: i for i in m['body']['items']}
    assert set(items) == {'AAPL', 'MSFT', 'JPM', 'SPY', 'KO'}
    aapl = items['AAPL']['market_state'][0]
    assert aapl['provider'] == 'yahoo-chart' and aapl['venue'] == 'XNAS'
    assert aapl['price']['state'] == 'stale'  # 2024 fixture vs real clock: age-only policy
    assert items['KO']['market_state'] is None
    assert m['body']['freshness_policy'] == 'age_only'
    assert out['catalog_instruments']['status'] == 200 and out['catalog_counts']['status'] == 200


def test_gets_and_lifespan_do_not_modify_catalog_or_market_state(real_app_run):
    out, _ = real_app_run
    assert out['db_before'] == out['db_after']
    assert out['db_after']['market_state_latest'] is not None


def test_startup_shutdown_only_touch_known_legacy_files(real_app_run):
    _, changed = real_app_run
    unexpected = changed - LEGACY_RUNTIME
    assert not unexpected, f'unexpected files modified by import/startup/shutdown: {sorted(unexpected)}'
