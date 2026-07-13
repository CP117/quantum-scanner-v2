"""Phase 26.46 — Disk-based wedge watchdog.

When the backend HTTP layer freezes (provider thread holding a lock
that HTTP handlers need), no `/system/threads/summary` endpoint can
help — the request never reaches a handler.  This service writes the
same information to disk on a fixed cadence so the user can grab the
file from a wedged install via the OS file browser.

Output: `/app/data/wedge_watchdog.json`, overwritten every 30 seconds
when healthy, frozen at the moment of the wedge.  Includes:
    * Wall-clock timestamp (proves whether the file is fresh or stale)
    * Snap-worker progress counters (`rows_scored`, `current_batch_index`)
    * Priority-lane state (`monitor_only`, `consecutive_slow`)
    * Full Python stack trace of every live thread
    * Approximate memory + open-file-descriptor count (process health)

If the file's `timestamp_utc` is more than ~60 seconds old, you're
looking at the state captured the moment the backend stopped
scheduling its threads — which IS the deadlock snapshot.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger('app.wedge_watchdog')

_DUMP_PATH = Path(__file__).resolve().parent.parent.parent / 'data' / 'wedge_watchdog.json'
_INTERVAL_SECONDS = 30.0
_thread: threading.Thread | None = None
_stop = threading.Event()


def _snapshot_threads() -> list[dict]:
    """One-shot snapshot of every live thread's identity + top-30 stack frames."""
    out = []
    frames = sys._current_frames()
    for tid, frame in frames.items():
        # Find the matching Thread object so we can pull its name.
        name = '<unknown>'
        daemon = False
        alive = False
        for t in threading.enumerate():
            if t.ident == tid:
                name = t.name
                daemon = t.daemon
                alive = t.is_alive()
                break
        stack_lines: list[str] = []
        try:
            stack_lines = [line.rstrip() for line in traceback.format_stack(frame, limit=30)]
        except Exception as exc:  # noqa: BLE001
            stack_lines = [f'<failed to format stack: {exc}>']
        out.append({
            'tid': tid,
            'name': name,
            'daemon': daemon,
            'alive': alive,
            'stack': stack_lines,
        })
    return out


def _snapshot_process_health() -> dict:
    """Best-effort RSS / FD count.  No-op when psutil isn't around."""
    try:
        import psutil  # noqa: WPS433
        p = psutil.Process(os.getpid())
        return {
            'rss_mb': round(p.memory_info().rss / 1024 / 1024, 1),
            'open_fds': p.num_fds() if hasattr(p, 'num_fds') else None,
            'thread_count': p.num_threads(),
            'cpu_percent': p.cpu_percent(interval=0.0),
        }
    except Exception:  # noqa: BLE001
        return {}


def _dump_once() -> None:
    """Write a single watchdog frame to disk.  Never raises."""
    try:
        from app.services.snapshot_store import get_snapshot_meta
        meta = get_snapshot_meta('stocks')
    except Exception as exc:  # noqa: BLE001
        meta = {'error': f'meta_fetch_failed: {type(exc).__name__}: {exc}'}

    try:
        from app.services.top10_priority_service import get_status as _t10
        t10 = _t10()
    except Exception as exc:  # noqa: BLE001
        t10 = {'error': f'priority_lane_status_failed: {type(exc).__name__}: {exc}'}

    payload = {
        'timestamp_utc': datetime.now(timezone.utc).isoformat(),
        'monotonic_seconds': time.monotonic(),
        'process_health': _snapshot_process_health(),
        'snapshot_meta_stocks': meta,
        'top10_priority_lane': t10,
        'threads': _snapshot_threads(),
        'note': (
            'If timestamp_utc is more than ~60 s behind real time, the '
            'process was wedged at this moment.  Look for threads with '
            "identical stacks across multiple watchdog dumps — they're "
            'the deadlocked ones.'
        ),
    }
    try:
        _DUMP_PATH.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp file then rename for atomicity (so the user
        # never reads a half-written dump).
        tmp = _DUMP_PATH.with_suffix('.json.tmp')
        with tmp.open('w', encoding='utf-8') as f:
            json.dump(payload, f, indent=2, default=str)
        tmp.replace(_DUMP_PATH)
    except Exception as exc:  # noqa: BLE001
        log.warning('wedge_watchdog: failed to write %s: %s', _DUMP_PATH, exc)


def _loop() -> None:
    log.info('wedge_watchdog: started (interval=%.1fs, path=%s)', _INTERVAL_SECONDS, _DUMP_PATH)
    while not _stop.is_set():
        _dump_once()
        _stop.wait(_INTERVAL_SECONDS)
    log.info('wedge_watchdog: stopped')


def start_wedge_watchdog() -> bool:
    """Idempotent. Safe to call from main.py at module import time."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return True
    _stop.clear()
    _thread = threading.Thread(target=_loop, name='wedge-watchdog', daemon=True)
    _thread.start()
    return True


def get_last_dump_age_seconds() -> float | None:
    """Used by /system/status to surface 'watchdog last dumped X s ago'."""
    try:
        return time.time() - _DUMP_PATH.stat().st_mtime
    except Exception:  # noqa: BLE001
        return None
