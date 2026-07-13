"""Serves a self-contained zip of the project source for local testing.

Packages `/app/app` (FastAPI backend), `/app/frontend` (dashboard UI),
`/app/backend` (supervisor shim), and top-level runbook files. Excludes
`__pycache__`, `node_modules`, `.git`, disk caches (`data/`), quarantine
files, and any raw ML shards.

The zip is regenerated on first request per process boot and cached at
`data/dist/quantum-market-scanner-source.zip`. A `?force=1` param
rebuilds unconditionally.
"""
from __future__ import annotations

import logging
import os
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse, JSONResponse

log = logging.getLogger('app.source_download')

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DIST_DIR = _REPO_ROOT / 'data' / 'dist'
_ZIP_PATH = _DIST_DIR / 'quantum-market-scanner-source.zip'
_TTL_SECONDS = 300  # rebuild every 5 min if requested again

# Top-level dirs to include, and per-dir exclusions.
_INCLUDE_DIRS = ['app', 'frontend', 'backend', 'scripts']
# Baseline data files that ship with the app (universe seeds, exchange
# listings, active-universe defaults, cached CoinGecko catalog).  These
# are *NOT* runtime caches — the app cannot boot properly without them
# (leveraged_universe.json, cached_universe.json, active_universes.*).
# Included at the top level of the zip so a fresh install has everything
# it needs to start scanning immediately without waiting for provider
# re-fetches.
_INCLUDE_DATA_FILES = [
    'data/leveraged_universe.json',
    'data/cached_universe.json',
    'data/nasdaq_full_listing.json',
    'data/coingecko_catalog_cache.json',
    'data/coingecko_coin_list_cache.json',
    'data/sec_ticker_cik.json',
    'data/known_bad_symbols.json',
    'data/active_universes.baseline.json',
    'data/active_universes.json',
]
_INCLUDE_ROOT_FILES = ['README.md', 'LOCAL_SETUP.md', 'requirements.txt',
                       'pyproject.toml', 'start.sh', 'start.bat',
                       'docker-compose.yml', 'Dockerfile', '.env.example',
                       'LICENSE']
_EXCLUDE_DIR_NAMES = {
    '__pycache__', '.git', '.emergent', 'node_modules', 'dist', 'build',
    '.pytest_cache', '.mypy_cache', '.ruff_cache', 'cache_quarantine',
    'daily_history_cache', 'options', 'reaction_maps', 'quote_cache',
    'blacklist', 'active_scan_pool', 'universes_extra', 'top10_priority',
    'crypto_provider', 'symbol_blacklist', 'guidebook_pdf', 'result_store',
    'coverage', '.next',
}
_EXCLUDE_FILE_SUFFIXES = {'.pyc', '.pyo', '.log', '.pid', '.sock', '.swp'}
_EXCLUDE_FILENAMES = {'.DS_Store', 'Thumbs.db', '.env'}

_lock = threading.Lock()

router = APIRouter(tags=['source-download'])


def _iter_source_paths(root: Path):
    for top in _INCLUDE_ROOT_FILES:
        p = root / top
        if p.is_file():
            yield p
    for rel in _INCLUDE_DATA_FILES:
        p = root / rel
        if p.is_file():
            yield p
    for d in _INCLUDE_DIRS:
        base = root / d
        if not base.exists():
            continue
        for p in base.rglob('*'):
            if not p.is_file():
                continue
            # skip if any parent dir name is excluded
            if any(part in _EXCLUDE_DIR_NAMES for part in p.parts):
                continue
            if p.suffix in _EXCLUDE_FILE_SUFFIXES:
                continue
            if p.name in _EXCLUDE_FILENAMES:
                continue
            yield p


def _build_zip() -> tuple[Path, int, int]:
    _DIST_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _ZIP_PATH.with_suffix('.zip.tmp')
    file_count = 0
    total_bytes = 0
    with zipfile.ZipFile(tmp, 'w', compression=zipfile.ZIP_DEFLATED,
                         compresslevel=6) as zf:
        # Manifest first so users can inspect what they got.
        manifest = _manifest_text()
        zf.writestr('MANIFEST.txt', manifest)
        for path in _iter_source_paths(_REPO_ROOT):
            try:
                arcname = 'quantum-market-scanner/' + str(path.relative_to(_REPO_ROOT))
                zf.write(path, arcname=arcname)
                file_count += 1
                total_bytes += path.stat().st_size
            except Exception as exc:  # noqa: BLE001
                log.debug('source-download: skipped %s (%s)', path, exc)
    os.replace(tmp, _ZIP_PATH)
    return _ZIP_PATH, file_count, total_bytes


def _manifest_text() -> str:
    stamp = datetime.now(timezone.utc).isoformat()
    return (
        'Quantum Market Scanner — source bundle\n'
        f'Packaged: {stamp}\n'
        '\n'
        'Contents:\n'
        '  quantum-market-scanner/\n'
        '    app/             — FastAPI backend (routes, services, models)\n'
        '    frontend/        — Vanilla-JS dashboard + Metrics Hub UI\n'
        '    backend/         — supervisor shim (uvicorn entry-point)\n'
        '    requirements.txt\n'
        '    LOCAL_SETUP.md   — one-page setup guide (start here)\n'
        '    .env.example     — copy to .env for optional API keys\n'
        '\n'
        'Run locally:\n'
        '  cd quantum-market-scanner\n'
        '  python3 -m venv .venv && source .venv/bin/activate\n'
        '  pip install -r requirements.txt\n'
        '  cp .env.example .env      # optional\n'
        '  uvicorn app.main:app --host 0.0.0.0 --port 8001\n'
        '  # Then open http://localhost:8001/frontend/market-refinement-dashboard.html\n'
        '\n'
        'Notes:\n'
        '  - Provider cache directories (data/) are EXCLUDED from this bundle;\n'
        '    they will regenerate on first scan.\n'
        '  - See LOCAL_SETUP.md for troubleshooting and configuration hints.\n'
    )


def _stale() -> bool:
    if not _ZIP_PATH.exists():
        return True
    age = time.time() - _ZIP_PATH.stat().st_mtime
    return age > _TTL_SECONDS


@router.get('/download/source.zip')
def download_source_zip(force: int = Query(0, ge=0, le=1)) -> FileResponse:
    return _download_impl(force=force)


# /api/* alias so the Emergent preview ingress (which only routes /api/*
# to the backend) can serve the zip directly to the user's browser.
@router.get('/api/download/source.zip')
def download_source_zip_api(force: int = Query(0, ge=0, le=1)) -> FileResponse:
    return _download_impl(force=force)


def _download_impl(force: int = 0) -> FileResponse:
    with _lock:
        if force or _stale():
            path, files, size = _build_zip()
            log.info('source-download: rebuilt zip with %d files (%.1f MB)',
                     files, size / 1_048_576)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    filename = f'quantum-market-scanner-source-{stamp}.zip'
    return FileResponse(
        _ZIP_PATH, media_type='application/zip', filename=filename,
        headers={'Cache-Control': 'public, max-age=60'},
    )


@router.get('/api/download/source/info')
def download_source_info() -> JSONResponse:
    """Metadata about the current source bundle — for the download page."""
    exists = _ZIP_PATH.exists()
    size = _ZIP_PATH.stat().st_size if exists else 0
    mtime = _ZIP_PATH.stat().st_mtime if exists else None
    return JSONResponse({
        'exists':          exists,
        'size_bytes':      size,
        'size_mb':         round(size / 1_048_576, 2) if size else 0,
        'built_utc':       datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat() if mtime else None,
        'stale':           _stale(),
        'ttl_seconds':     _TTL_SECONDS,
        'download_url':    '/download/source.zip',
        'force_url':       '/download/source.zip?force=1',
    })
