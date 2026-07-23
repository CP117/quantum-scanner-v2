
from contextlib import asynccontextmanager
import os
import warnings

# --- WARNING FILTERS (must register BEFORE any import that pulls in yfinance/pandas) -----
try:
    # Suppress yfinance-internal Pandas FutureWarning/DeprecationWarning floods.
    # yfinance still uses pd.Timestamp.utcnow() internally as of 2026; we can't
    # patch their code, but we can silence the noise so it doesn't drown the
    # operator console every poll tick.
    warnings.filterwarnings(
        'ignore',
        message=r'.*Timestamp\.utcnow.*deprecated.*',
        category=FutureWarning,
    )
    warnings.filterwarnings(
        'ignore',
        message=r'.*Timestamp\.utcnow.*',
    )
    warnings.filterwarnings(
        'ignore',
        message=r'.*utcnow\(\) is deprecated.*',
    )
    warnings.filterwarnings(
        'ignore',
        category=FutureWarning,
        module=r'yfinance.*',
    )
    warnings.filterwarnings(
        'ignore',
        category=DeprecationWarning,
        module=r'yfinance.*',
    )
    warnings.filterwarnings(
        'ignore',
        message=r'.*possibly delisted.*',
    )
    warnings.filterwarnings(
        'ignore',
        message=r'.*No data found.*',
    )
    # Belt-and-suspenders: silence anything mentioning "Timestamp" + "deprecated".
    warnings.filterwarnings(
        'ignore',
        message=r'.*deprecated.*Timestamp.*',
    )
except Exception:
    pass

from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from app.routes.health import router as health_router
from app.routes.search import router as search_router
from app.routes.results import router as results_router
from app.routes.status import router as status_router
from app.routes.detail import router as detail_router
from app.routes.active_scan import router as active_scan_router
from app.routes.guidebook import router as guidebook_router
from app.services.warmer_service import start_warmer, stop_warmer

FRONTEND_DIR = Path(__file__).resolve().parent.parent / 'frontend'


# --- structured logging defaults ---------------------------------------------
import logging
import warnings
# Phase 26.24: hard guard against console floods. A single misplaced
# `datetime.utcnow()` deep in the options-chain hot path emitted a
# DeprecationWarning for every option row parsed (hundreds per CBOE
# symbol). On Windows, console writes block synchronously when the
# terminal buffer fills, which then chain-froze the snapshot loop AND
# every FastAPI handler trying to flush its response — subpages
# perpetually "Loading…", scanner appears to freeze 3 sweeps in. The
# root-cause utcnow() was replaced, but we also install a process-wide
# filter so any FUTURE deprecation / resource / pending-deprecation
# warning anywhere in the dependency tree can't ever produce that
# cascade again. Real warnings still go through Python's warning
# machinery (just not duplicated thousands of times to stdout).
warnings.filterwarnings('ignore', category=DeprecationWarning)
warnings.filterwarnings('ignore', category=PendingDeprecationWarning)
warnings.filterwarnings('ignore', category=ResourceWarning)

logging.getLogger('yfinance').setLevel(logging.CRITICAL)
logging.getLogger('peewee').setLevel(logging.ERROR)
# Phase 26.23: silence the per-request INFO chatter from httpx + httpcore.
# These libraries log every outbound HTTP call ("HTTP Request: GET ... 200 OK"
# / "... 403 Forbidden"), which on a busy CBOE polling loop produces hundreds
# of lines per minute. The provider-level call counters surfaced on
# /api/providers/status already capture the same telemetry in aggregate.
# Real connection errors still surface at WARNING.
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s %(message)s',
)


class ForceHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        # Phase 26.16 / Tier 2.5: do NOT clobber a Cache-Control header
        # the route already set. Routes that want browser-side caching
        # (e.g. /api/scan/snapshot with ETag + If-None-Match) need their
        # `private, must-revalidate` directive preserved; everything else
        # defaults to `no-store` so stale envelopes don't linger.
        if 'cache-control' not in {k.lower() for k in response.headers.keys()}:
            response.headers['Cache-Control'] = 'no-store'
        return response


async def _signal_index_refresher():
    """Keep the in-process regulatory signal index warm so the scoring service
    can read it synchronously on every batch assemble. Rebuilds every 60s
    (cheap — just an SQLite scan over the last 5 days of filings).
    """
    import asyncio
    from app.regulatory.services.signal_service import refresh_signal_index
    while True:
        try:
            await refresh_signal_index()
        except Exception:
            pass
        await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Phase 26.33: tune the GC for long-haul scans before anything else
    # populates the heap.  Defers gen-2 collections out of the scoring
    # hot path; without this, the gen-2 walk fires randomly during
    # batches once the heap grows past ~400 MB (after pass 1) and
    # produces the 10%↔100% CPU bouncing pattern the user observed.
    try:
        from app.services.gc_service import tune_for_long_haul
        tune_for_long_haul()
    except Exception:
        pass
    # Phase 26.35: widen FastAPI/anyio's sync-endpoint threadpool.
    # FastAPI runs every non-async route handler on this pool.  Default
    # is `min(32, cpu+4)` which on a 4-core box is only 8 threads — so
    # if 8 concurrent requests touch a lock that's held by a hung
    # snap-worker, the pool saturates and EVERY subsequent request
    # (including /system/status and /system/threads) queues forever.
    # That's the user-reported "all tabs spin forever" symptom.  64 is
    # generous headroom; each thread is ~8 KB of stack so the cost is
    # ~512 KB total, negligible.
    try:
        import anyio
        limiter = anyio.to_thread.current_default_thread_limiter()
        if limiter.total_tokens < 64:
            limiter.total_tokens = 64
            import logging as _logging
            _logging.getLogger('app.startup').info(
                'anyio threadpool widened to 64 tokens '
                '(prevents /system/status spinning when locks are contested)',
            )
    except Exception:
        pass
    start_warmer()
    # Phase 24: cold-start daily-history pre-warm.  Each cold launch otherwise
    # has to lazily fetch ~7,200 90-day OHLCV blobs during the first scan
    # sweep, which dominates first-sweep wall time.  We enqueue every
    # symbol in the active universe into the bounded prefetch pool on
    # startup; the 10-worker pool drains it in the background at ~10
    # symbols/sec (throttled).  After ~12 min the cache is warm for the
    # entire universe and the scoring loop stops hitting the network for
    # daily history.
    #
    # Skip the prewarm when `PREFETCH_DAILY_HISTORY=0` (lets the user
    # disable it on memory-constrained machines).
    if os.environ.get('PREFETCH_DAILY_HISTORY', '1') != '0':
        try:
            from app.services.universe_service import load_universe
            from app.services.daily_history_service import prefetch_daily_history
            stock_uni = load_universe('stocks') or []
            crypto_uni = load_universe('crypto') or []
            queued = 0
            for row in stock_uni:
                sym = row.get('symbol') if isinstance(row, dict) else None
                if sym:
                    prefetch_daily_history(sym)
                    queued += 1
            for row in crypto_uni:
                sym = row.get('symbol') if isinstance(row, dict) else None
                if sym:
                    prefetch_daily_history(sym)
                    queued += 1
            import logging as _log
            _log.getLogger('app.daily_history').info(
                'cold-start prewarm: enqueued %d symbols (stocks=%d crypto=%d) for background fetch',
                queued, len(stock_uni), len(crypto_uni),
            )
        except Exception as exc:
            import logging as _log
            _log.getLogger('app.daily_history').warning(
                'cold-start prewarm failed: %s', exc,
            )

    # Regulatory monitor: initialize SQLite + signal index refresher in every
    # process.  The HEAVY scheduler loop (which fires the 7,000-ticker SEC
    # autoscan) is now off by default — set REGULATORY_INPROCESS=1 to keep
    # the legacy single-process behavior, OR run `start_regulatory.{bat,sh}`
    # to launch a separate decoupled writer process.  See
    # `app/regulatory/standalone_runner.py` for the rationale.
    import asyncio as _asyncio
    try:
        from app.regulatory.db.database import init_db as _reg_init_db
        from app.regulatory.services.cik_lookup_service import initialize as _reg_init_cik
        await _reg_init_db()
        # Load the SEC ticker→CIK map in the background so the universe auto-scan
        # has it ready when the scheduler kicks off. We don't await it here
        # because it can take a few seconds on cold start.
        _asyncio.create_task(_reg_init_cik())

        regulatory_inprocess = os.environ.get('REGULATORY_INPROCESS', '0') == '1'
        if regulatory_inprocess:
            from app.regulatory.services.monitor_service import (
                start_scheduler as _reg_start_scheduler,
                stop_scheduler as _reg_stop_scheduler,
            )
            await _reg_start_scheduler()
            app.state._reg_stop_scheduler = _reg_stop_scheduler
            import logging as _log
            _log.getLogger('app.regulatory').info(
                'regulatory scheduler running IN-PROCESS (REGULATORY_INPROCESS=1)'
            )
        else:
            app.state._reg_stop_scheduler = None
            import logging as _log
            _log.getLogger('app.regulatory').info(
                'regulatory scheduler decoupled — start `start_regulatory.{bat,sh}` '
                'separately to enable the SEC autoscan writer. Main app will read '
                'from `data/regulatory.db` only.'
            )
        # Signal-index refresher always runs (it just reads from SQLite — cheap).
        app.state._reg_signal_task = _asyncio.create_task(_signal_index_refresher())
    except Exception as exc:
        import logging as _log
        _log.getLogger('app.regulatory').exception('regulatory startup failed: %s', exc)
        app.state._reg_stop_scheduler = None
        app.state._reg_signal_task = None

    # Phase 26.10: ensure every backend boot publishes a fresh randomized
    # trycloudflare URL. If the launcher (start.sh/start.bat) already
    # spawned cloudflared, we'll detect the URL within the grace period
    # and do nothing. If not (e.g. direct uvicorn launch), we spawn one
    # ourselves so /api/public-url always has a working public link.
    try:
        from app.routes.public_url_admin import ensure_public_url_on_startup
        await ensure_public_url_on_startup()
    except Exception as exc:
        import logging as _log
        _log.getLogger('app.public_url').warning(
            'startup tunnel auto-refresh failed to schedule: %s', exc,
        )
    yield
    stop_warmer()
    try:
        if getattr(app.state, '_reg_stop_scheduler', None):
            await app.state._reg_stop_scheduler()
    except Exception:
        pass
    try:
        task = getattr(app.state, '_reg_signal_task', None)
        if task and not task.done():
            task.cancel()
    except Exception:
        pass
    try:
        # Phase 21: drain the shared regulatory http pool cleanly on shutdown.
        from app.regulatory.services.http_client import close_all as _reg_http_close
        await _reg_http_close()
    except Exception:
        pass
    try:
        # Phase 26.30: drain any in-memory quote-cache shards to disk so a
        # clean uvicorn shutdown never loses the most recent batch of
        # saved quotes (atexit also covers SIGTERM but explicit is safer).
        from app.services.quote_cache import flush_now as _qc_flush
        _qc_flush()
    except Exception:
        pass


app = FastAPI(title='Market Refinement Dashboard', lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_credentials=False, allow_methods=['*'], allow_headers=['*'])
# Phase 26.16 / Tier 1.2: gzip the snapshot + provider-status responses
# (anything > 4 KB). Empirically cuts the 6 MB snapshot payload to ~600 KB
# and shaves measurable CPU off `json.dumps` because the encoder writes
# into the pre-sized compressed buffer.
app.add_middleware(GZipMiddleware, minimum_size=4_000, compresslevel=5)
app.add_middleware(ForceHeadersMiddleware)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    return JSONResponse(status_code=500, content={'error': 'internal_server_error', 'path': str(request.url.path), 'detail': str(exc)})


@app.get('/')
def root_redirect():
    return RedirectResponse(url='/ui')


@app.get('/ui')
def ui_page():
    return FileResponse(FRONTEND_DIR / 'market-refinement-dashboard.html')


# Download the packaged source bundle (so users can grab the zip from a browser).
_DIST_DIR = Path(__file__).resolve().parent.parent / 'dist'
_PACKAGE_ZIP = _DIST_DIR / 'market-refinement-dashboard.zip'
# Phase 26.38: separate artifact for the leveraged-ETFs-only variant.
# Built by `scripts/build_zip_leveraged.sh`.  Same code, but ships with
# `app/data/variant.json` and `data/leveraged_universe.json` so the
# stocks universe is narrowed to ~270 curated leveraged/inverse ETFs
# and crypto is disabled.
_PACKAGE_ZIP_LEVERAGED = _DIST_DIR / 'market-refinement-dashboard-leveraged.zip'


def _serve_package_zip():
    if not _PACKAGE_ZIP.exists():
        return JSONResponse(status_code=404, content={'error': 'package_not_built'})
    return FileResponse(
        _PACKAGE_ZIP,
        media_type='application/zip',
        filename='market-refinement-dashboard.zip',
    )


def _serve_package_zip_leveraged():
    if not _PACKAGE_ZIP_LEVERAGED.exists():
        return JSONResponse(status_code=404, content={'error': 'leveraged_package_not_built'})
    return FileResponse(
        _PACKAGE_ZIP_LEVERAGED,
        media_type='application/zip',
        filename='market-refinement-dashboard-leveraged.zip',
    )


@app.get('/download')
def download_package():
    return _serve_package_zip()


# /api/* alias so the Emergent preview ingress (which only routes /api/* to
# this backend) can serve the zip directly to the user's browser.
@app.get('/api/download')
def download_package_api():
    return _serve_package_zip()


# Phase 26.38: leveraged-only variant download endpoints.  Mirror the
# same /download + /api/download pattern.  Same artifact whether the
# request hits the preview ingress (/api/...) or a direct local box.
@app.get('/download/leveraged')
def download_package_leveraged():
    return _serve_package_zip_leveraged()


@app.get('/api/download/leveraged')
def download_package_leveraged_api():
    return _serve_package_zip_leveraged()


# Phase 26.39: lightweight variant introspection endpoint.  The frontend
# uses this on boot to decide whether to flip on the live-tick mode
# (2 s detail refresh + faster snapshot poll).  Always returns a 200;
# main-app build returns `universe_mode: "full"` so the frontend can
# trust the response unconditionally.
@app.get('/api/system/variant')
def system_variant():
    from app.services.universe_service import (
        is_leveraged_variant,
        is_crypto_disabled,
        _load_variant_config,
    )
    cfg = _load_variant_config()
    leveraged = is_leveraged_variant()
    payload = {
        'universe_mode': cfg.get('universe_mode', 'full'),
        'disable_crypto': bool(is_crypto_disabled()),
        'build': cfg.get('build', 'market-refinement-dashboard'),
        # Frontend will turn on tick-mode iff this flag is true.
        'live_tick_enabled': bool(leveraged),
        'live_tick_interval_ms': 2000 if leveraged else 0,
        'live_tick_top_n': 10 if leveraged else 0,
    }
    if leveraged:
        # Telemetry for the priority lane (operator-visible)
        from app.services.top10_priority_service import get_status as _t10_status
        payload['top10_priority_lane'] = _t10_status()
    return payload


app.mount('/frontend', StaticFiles(directory=str(FRONTEND_DIR)), name='frontend')

app.include_router(health_router)
app.include_router(status_router)
# Phase 26.35: live thread-stack dump endpoints (/system/threads, /system/threads/summary).
# Used to debug wedges where the UI hangs and we need to see, in real
# time, which threads are blocked and on what.  Works even when the
# rest of the app is sluggish because the endpoint holds no locks.
try:
    from app.routes.threads_debug import router as threads_debug_router
    app.include_router(threads_debug_router)
except Exception:
    pass
app.include_router(search_router)
app.include_router(results_router)
app.include_router(detail_router)
app.include_router(active_scan_router)
app.include_router(guidebook_router)
# Phase 26.66: toggleable universe groups (per-exchange shards + crypto core/rest)
from app.routes.universes import router as universes_router
app.include_router(universes_router)
# Phase 5: manual refresh, user-added symbols, NASDAQ merge, backtest harness
from app.routes.phase5 import router as phase5_router
app.include_router(phase5_router)

# Phase 26.49: Future Mode manual deep-refresh + system reset (soft / hard).
try:
    from app.routes.future_mode import router as future_mode_router
    app.include_router(future_mode_router)
except Exception as exc:
    import logging as _log
    _log.getLogger('app.future_mode').exception('future_mode router failed to mount: %s', exc)

# Phase 26.61: Metrics Hub (algorithm catalog, cache health, weight tuner).
try:
    from app.routes.metrics_hub import router as metrics_hub_router
    app.include_router(metrics_hub_router)

    @app.get('/metrics-hub.html')
    def metrics_hub_page():
        return FileResponse(FRONTEND_DIR / 'metrics-hub.html')
except Exception as exc:
    import logging as _log
    _log.getLogger('app.metrics_hub').exception('metrics_hub router failed to mount: %s', exc)


# Phase 7: Regulatory Interest & Contract Monitor (insider filings, contract awards,
# auto-discovery, entity linking). Mounted under /api/regulatory/* so the Emergent
# preview ingress can reach it; also accessible via /regulatory.html in the frontend.
try:
    from app.regulatory.routes.regulatory_routes import router as regulatory_router
    app.include_router(regulatory_router)

    @app.get('/regulatory.html')
    def regulatory_page():
        return FileResponse(FRONTEND_DIR / 'regulatory.html')
except Exception as exc:
    import logging as _log
    _log.getLogger('app.regulatory').exception('regulatory router failed to mount: %s', exc)

# Phase 15: user-supplied API keys (Finnhub, Polygon, etc.) for unlocking
# additional providers. Mounted under /api/api-keys/*.
try:
    from app.routes.api_keys import router as api_keys_router
    app.include_router(api_keys_router)
except Exception as exc:
    import logging as _log
    _log.getLogger('app.api_keys').exception('api_keys router failed to mount: %s', exc)

# Phase 18: 10-day price-point prediction (aggregates every factor family
# into a single forward target + 95% CI). Mounted under /api/predict/*.
try:
    from app.routes.price_prediction import router as predict_router
    app.include_router(predict_router)
except Exception as exc:
    import logging as _log
    _log.getLogger('app.predict').exception('predict router failed to mount: %s', exc)

# Phase 22: user-saved prediction tracker.  Persists generated forecasts
# to a small SQLite DB (data/saved_predictions.db) and auto-evaluates
# them when their expiration date passes.  Mounted under /api/predictions/*.
try:
    from app.routes.prediction_tracker import router as prediction_tracker_router
    from app.services.prediction_tracker_service import (
        init_db as _pt_init_db,
        start_evaluator as _pt_start_evaluator,
    )
    _pt_init_db()
    _pt_start_evaluator()
    app.include_router(prediction_tracker_router)

    @app.get('/predictions.html')
    def predictions_page():
        return FileResponse(FRONTEND_DIR / 'predictions.html')

    @app.get('/predictions.js')
    def predictions_js():
        return FileResponse(FRONTEND_DIR / 'predictions.js', media_type='application/javascript')
except Exception as exc:
    import logging as _log
    _log.getLogger('app.prediction_tracker').exception(
        'prediction_tracker router failed to mount: %s', exc,
    )

# Phase 26: dedicated Data Providers Health page (/providers.html) and the
# /api/providers/status endpoint that powers its 10s auto-refresh.
try:
    from app.routes.providers import router as providers_router
    app.include_router(providers_router)

    @app.get('/providers.html')
    def providers_page():
        return FileResponse(FRONTEND_DIR / 'providers.html')

    @app.get('/providers.js')
    def providers_js():
        return FileResponse(FRONTEND_DIR / 'providers.js', media_type='application/javascript')
except Exception as exc:
    import logging as _log
    _log.getLogger('app.providers').exception('providers router failed to mount: %s', exc)

# Phase 26.5: surface the cloudflared / LAN public URL captured by the
# start.bat / start.sh launchers so the dashboard can show it under the title.
try:
    from app.routes.public_url import router as public_url_router
    app.include_router(public_url_router)
except Exception as exc:
    import logging as _log
    _log.getLogger('app.public_url').exception('public_url router failed to mount: %s', exc)

# Phase 26.6: backend-managed cloudflared tunnel (regenerate-on-demand) +
# long-run housekeeping (counter rotation + DB pruning).
try:
    from app.routes.public_url_admin import router as public_url_admin_router
    from app.routes.admin import router as admin_router
    from app.services.maintenance_service import start_maintenance_thread
    app.include_router(public_url_admin_router)
    app.include_router(admin_router)
    start_maintenance_thread()
except Exception as exc:
    import logging as _log
    _log.getLogger('app.maintenance').exception('maintenance/admin mount failed: %s', exc)

# Cache deduplication subsystem: admin/debug endpoints + non-blocking
# startup audit pass (reaction clustering / options chain / daily history).
try:
    from app.routes.cache_admin import router as cache_admin_router
    from app.services.cache_dedupe_service import start_startup_audit
    app.include_router(cache_admin_router)
    start_startup_audit()
except Exception as exc:
    import logging as _log
    _log.getLogger('app.cache_dedupe').exception('cache dedupe mount failed: %s', exc)

# Future Forecast Activator: per-row forecast execution route used by the
# scanner table's details-column button.
try:
    from app.routes.forecast_activator import router as forecast_activator_router
    app.include_router(forecast_activator_router)
except Exception as exc:
    import logging as _log
    _log.getLogger('app.forecast_activator').exception('forecast activator mount failed: %s', exc)

# Social-share landing route: serves an OG-tagged HTML card at
# /share/{symbol} that Facebook / LinkedIn / Twitter can scrape for link
# previews, then redirects real users into the dashboard deep-link.
try:
    from app.routes.social_share import router as social_share_router
    app.include_router(social_share_router)

    @app.get('/shared-analyses.html')
    def shared_analyses_page():
        return FileResponse(FRONTEND_DIR / 'shared-analyses.html')
except Exception as exc:
    import logging as _log
    _log.getLogger('app.social_share').exception('social share mount failed: %s', exc)

# Source download route: ships a self-contained zip of the project at
# /download/source.zip for local testing / porting into tagedin.com.
try:
    from app.routes.source_download import router as source_download_router
    app.include_router(source_download_router)
except Exception as exc:
    import logging as _log
    _log.getLogger('app.source_download').exception('source download mount failed: %s', exc)

# Cross-Market Squeeze Radar: scans stocks + crypto simultaneously
# and cross-ranks by SSP × PVI × cross-market-correlation.
try:
    from app.routes.cross_market_radar import router as cross_market_radar_router
    app.include_router(cross_market_radar_router)

    @app.get('/cross-market-squeeze.html')
    def cross_market_squeeze_page():
        return FileResponse(FRONTEND_DIR / 'cross-market-squeeze.html')
except Exception as exc:
    import logging as _log
    _log.getLogger('app.cross_market_radar').exception('cross-market radar mount failed: %s', exc)

# Phase 24: blacklist admin endpoints for the persistently-failing symbol
# audit page.  Read-only by default; the manual unblock endpoint is the
# only mutating route.
try:
    from app.routes.blacklist import router as blacklist_router
    app.include_router(blacklist_router)
except Exception as exc:
    import logging as _log
    _log.getLogger('app.blacklist').exception(
        'blacklist router failed to mount: %s', exc,
    )

# Phase 16: clear any stale failure-cooldowns for crypto -USD symbols so
# the new CryptoCompare daily-history fallback gets a chance to fill the
# ~90% warming gap on cryptocompare-sourced rows.
try:
    from app.services.daily_history_service import invalidate_failed_crypto
    invalidate_failed_crypto()
except Exception:
    pass

# Phase 12: server-side broadcast snapshot.  Mount the /api/scan/snapshot
# endpoint AND launch the single background scan loop so secondary devices
# (phones, tablets, other laptops) just mirror what the host already scored.
#
# In production (start.bat / start.sh) uvicorn runs without --reload, so
# this module is imported exactly once and `start_scan_loop()` spawns one
# scanner thread.  In dev with --reload uvicorn imports the module in both
# the watcher and the worker processes; each call to start_scan_loop()
# spawns its own thread, but the `_loop_started` flag inside the helper
# guarantees no double-spawn within the same interpreter.  The watcher's
# thread does nothing useful (no requests reach the watcher) but is also
# harmless.
try:
    from app.routes.snapshot import router as snapshot_router
    app.include_router(snapshot_router)
    from app.services.snapshot_store import start_scan_loop
    start_scan_loop()
except Exception as exc:
    import logging as _log
    _log.getLogger('app.snapshot').exception('snapshot loop failed to start: %s', exc)

# Phase 26.39: leveraged-variant-only top-10 priority lane.  Gated
# internally by `is_leveraged_variant()` — the main-app build is a
# no-op because the variant.json marker is not present.  Started after
# the main scan loop so the top-10 lane never wins a race during cold
# boot.
try:
    from app.services.top10_priority_service import start_top10_priority_lane
    started = start_top10_priority_lane(interval_seconds=2.0, top_n=10)
    if started:
        import logging as _log
        _log.getLogger('app.startup').info(
            'leveraged-variant: top-10 priority lane ACTIVE (2 s cadence)'
        )
except Exception as exc:
    import logging as _log
    _log.getLogger('app.startup').exception(
        'leveraged-variant: top-10 priority lane failed to start: %s', exc,
    )

# Phase 26.46: disk-based wedge watchdog. Writes
# data/wedge_watchdog.json every 30 s with full thread stacks + meta
# + process health.  When the backend wedges, the file's timestamp
# freezes and the contained stacks are the deadlock snapshot —
# readable from disk without needing the HTTP layer to be alive.
try:
    from app.services.wedge_watchdog import start_wedge_watchdog
    start_wedge_watchdog()
    import logging as _log
    _log.getLogger('app.startup').info(
        'wedge watchdog ACTIVE -> data/wedge_watchdog.json (30 s cadence)'
    )
except Exception as exc:
    import logging as _log
    _log.getLogger('app.startup').exception(
        'wedge watchdog failed to start: %s', exc,
    )

# Kick off background NASDAQ canonical-listing refresh on startup
try:
    from app.services.universe_extras import refresh_nasdaq_in_background
    refresh_nasdaq_in_background()
except Exception:
    pass

# Phase 28: Tiered Universe Architecture
# Initialise supporting services first, then start each scanner tier.
try:
    import logging as _log
    _tier_log = _log.getLogger('app.startup')

    # 1. GPU detection (must run before Tier 3 scanner so it knows the interval).
    from app.services.gpu_acceleration import initialize as _gpu_init
    _gpu_init()

    # 2. Tier cache store (starts disk-flush daemon thread).
    from app.services.tier_cache_store import initialize as _tc_init
    _tc_init()

    # 3. Tier manager (loads persisted state, starts flush thread).
    from app.services.tier_manager import initialize as _tm_init, seed_universe
    _tm_init()
    # Seed all universe symbols into Tier 3 on first start (no-op for symbols
    # that already have an assignment from the persisted state).
    for _mkt in ('stocks', 'crypto'):
        try:
            seed_universe(_mkt)
        except Exception as _seed_exc:  # noqa: BLE001
            _tier_log.warning('tier_manager: seed_universe(%s) failed: %s', _mkt, _seed_exc)

    # 4. Resilience watchdog.
    from app.services.tier_resilience import start_watchdog
    start_watchdog()

    # 5. Restore watchlist pins into tier_manager.
    try:
        from app.services.watchlist_service import restore_pins_to_tier_manager
        restore_pins_to_tier_manager()
    except Exception as _pin_exc:  # noqa: BLE001
        _tier_log.warning('watchlist_service: restore_pins failed: %s', _pin_exc)

    # 6. Start scanner tiers.
    from app.services.tier_1_active_scanner import start_tier1_scanner
    from app.services.tier_2_monitor_scanner import start_tier2_scanner
    from app.services.tier_3_background_scanner import start_tier3_scanner

    _t1_started = start_tier1_scanner()
    _t2_started = start_tier2_scanner()
    _t3_started = start_tier3_scanner()
    _tier_log.info(
        'tiered scanner: T1=%s T2=%s T3=%s',
        'ACTIVE' if _t1_started else 'skipped',
        'ACTIVE' if _t2_started else 'skipped',
        'ACTIVE' if _t3_started else 'skipped',
    )

    # 7. Mount tier-status and watchlist routes.
    from app.routes.tier_status import router as tier_status_router
    app.include_router(tier_status_router)
    from app.routes.watchlist import router as watchlist_router, symbol_router
    app.include_router(watchlist_router)
    app.include_router(symbol_router)
    _tier_log.info('tiered scanner: routes mounted (/api/tier-status, /api/watchlist, /api/symbol)')

except Exception as _tier_exc:
    import logging as _log
    _log.getLogger('app.startup').exception('tiered scanner init failed: %s', _tier_exc)
