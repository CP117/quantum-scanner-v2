"""
Standalone Regulatory Monitor runner.

Phase 21 — decouples the regulatory subsystem from the FastAPI scanner
process.  Previously the universe auto-scan loop (which fires 7,000+ SEC
+ USAspending HTTP requests every 4 hours) ran inside the same asyncio
event loop as the FastAPI app.  Over time the event-loop contention
starved the snapshot scan thread of CPU and socket budget and caused
the scanner to stall around the 2,500-3,000 stock mark.

This runner spawns the regulatory scheduler as its OWN Python process
that talks to the same SQLite database (`/app/data/regulatory.db`).
The main FastAPI process just reads from SQLite via the in-process
signal index refresher (which is cheap — one SELECT over last-5-days
of filings every 60s) and never has to compete with the SEC autoscan
for sockets or event-loop time.

Usage
-----
Direct:   ``python -m app.regulatory.standalone_runner``
Launcher: ``start_regulatory.bat`` / ``start_regulatory.sh``

The runner:
  1. Initializes the SQLite schema (idempotent, safe to run alongside
     the main app).
  2. Pre-warms the ticker → CIK map.
  3. Starts the regulatory scheduler loop with all four sub-loops
     active (watchlist polling, auto-discovery, tracked-company
     re-scans, universe auto-scan).
  4. Logs each tick to stderr; press Ctrl+C to stop.
  5. Drains the shared httpx pool on exit.

Operational notes
-----------------
* SQLite tolerates concurrent readers + one writer at a time; the
  main FastAPI process only READS from this DB (signal index refresh
  + UI queries), the standalone runner is the only writer.  No
  collisions in practice.
* The runner reuses every existing rule (settings table, default
  intervals, request gap) — you can still tune everything from the
  main UI's Regulatory Settings panel without restarting either
  process.
* Toggling ``enable_universe_autoscan`` to "0" via the Settings panel
  pauses the runner's autoscan on the next tick (without stopping
  the process); flipping back to "1" resumes it.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time


log = logging.getLogger('app.regulatory.standalone')


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s %(message)s',
        stream=sys.stderr,
    )
    # Quiet yfinance / pandas noise just in case any path drags them in.
    logging.getLogger('yfinance').setLevel(logging.CRITICAL)
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)


async def _main() -> int:
    from app.regulatory.db.database import init_db
    from app.regulatory.services.monitor_service import (
        scheduler_loop,
        stop_scheduler,
    )
    from app.regulatory.services.cik_lookup_service import initialize as init_cik_map
    from app.regulatory.services.http_client import close_all as http_close_all

    # The monitor module uses a module-level `_scheduler_enabled` flag to
    # decide whether to keep looping.  Set it true (the scheduler_loop()
    # function does this on entry too, but starting it true also makes
    # signal handlers work cleanly).
    import app.regulatory.services.monitor_service as ms
    ms._scheduler_enabled = True

    started_at = time.monotonic()
    log.info('regulatory standalone runner starting (pid=%d)', os.getpid())

    # 1) DB schema.
    try:
        await init_db()
    except Exception as exc:
        log.exception('regulatory DB init failed: %s', exc)
        return 2

    # 2) Pre-warm the ticker → CIK map (cached on disk, fast after first run).
    try:
        size = await init_cik_map()
        log.info('regulatory standalone: CIK map ready (%d entries)', size)
    except Exception as exc:
        log.warning('regulatory standalone: CIK map prefetch failed: %s', exc)

    # 3) Wire up Ctrl+C / SIGTERM → request graceful shutdown.
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _stop(_signum: int = 0, _frame=None) -> None:
        log.info('regulatory standalone: shutdown signal received')
        stop_event.set()

    try:
        loop.add_signal_handler(signal.SIGINT, _stop)
        loop.add_signal_handler(signal.SIGTERM, _stop)
    except (NotImplementedError, RuntimeError):
        # Windows: signal handlers via loop.add_signal_handler aren't
        # supported.  Fall back to the synchronous signal module.
        signal.signal(signal.SIGINT, _stop)
        try:
            signal.signal(signal.SIGTERM, _stop)
        except (AttributeError, ValueError):
            pass

    # 4) Spawn the scheduler loop and wait for either it or the stop event.
    sched_task = asyncio.create_task(scheduler_loop(), name='reg-scheduler')
    stop_task = asyncio.create_task(stop_event.wait(), name='reg-stop-waiter')

    done, _pending = await asyncio.wait(
        {sched_task, stop_task},
        return_when=asyncio.FIRST_COMPLETED,
    )

    # 5) Graceful shutdown.
    log.info('regulatory standalone: stopping (uptime=%.1fs)', time.monotonic() - started_at)
    try:
        await stop_scheduler()
    except Exception:
        pass
    if not sched_task.done():
        sched_task.cancel()
        try:
            await sched_task
        except (asyncio.CancelledError, Exception):
            pass
    if not stop_task.done():
        stop_task.cancel()

    try:
        await http_close_all()
    except Exception:
        pass

    log.info('regulatory standalone runner stopped cleanly')
    return 0


def main() -> int:
    _configure_logging()
    try:
        return asyncio.run(_main())
    except KeyboardInterrupt:
        return 0


if __name__ == '__main__':
    sys.exit(main())
