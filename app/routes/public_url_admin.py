"""
Backend-managed cloudflared tunnel.

This module lets the backend regenerate the Cloudflare Quick Tunnel URL
on demand via POST /api/public-url/regenerate, without restarting the
whole app or relying on the launcher script to do it.

How it works:

  1. Locate the cloudflared binary. We search:
       a) `cwd/cloudflared` (Linux/macOS) or `cwd/cloudflared.exe` (Windows)
          -- this is where start.sh/start.bat downloads it.
       b) PATH lookup as a fallback.
  2. Kill any running cloudflared process spawned by THIS backend
     (we track the PID we started). We deliberately do NOT kill
     unrelated cloudflared processes the user may have running.
  3. Spawn a fresh `cloudflared tunnel --url http://localhost:<PORT>`
     with stdout redirected to /tmp/mrd_cloudflared_backend.log.
  4. In a background asyncio task, tail the log for the
     `https://<random>.trycloudflare.com` URL (up to 60 s).
  5. When found, rewrite `app/data/public_url.txt` with the new URL
     followed by the existing LAN URLs.

Idempotent: calling regenerate() while a regenerate is already in flight
returns the in-flight task's status without spawning another cloudflared.

Returns immediately with a status payload; the frontend polls
/api/public-url every 30 s (and on demand after a regenerate click) to
pick up the new URL.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from threading import Lock
from typing import Any

from fastapi import APIRouter
from app.utils.time import utcnow_iso

log = logging.getLogger('app.routes.public_url_admin')

router = APIRouter(prefix='/api/public-url', tags=['public-url'])


# ---------------------------------------------------------------------------
# Resolve paths the same way app/routes/public_url.py does so the file we
# write is the one the GET /api/public-url endpoint reads.
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parent.parent.parent  # /app/app/routes -> /app
_URL_FILE_CANDIDATES = [
    Path('app/data/public_url.txt'),
    _REPO_ROOT / 'app' / 'data' / 'public_url.txt',
    _REPO_ROOT / 'data' / 'public_url.txt',
]


def _pick_url_file() -> Path:
    for cand in _URL_FILE_CANDIDATES:
        try:
            if cand.exists():
                return cand
        except OSError:
            continue
    # Default to the canonical write location if nothing exists yet.
    target = _URL_FILE_CANDIDATES[1]  # absolute /app/app/data/public_url.txt
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def _find_cloudflared() -> str | None:
    """Locate the cloudflared binary.

    1. ./cloudflared.exe (Windows, alongside start.bat)
    2. ./cloudflared     (Linux/macOS, alongside start.sh)
    3. PATH lookup
    """
    is_windows = sys.platform.startswith('win') or os.name == 'nt'
    here_exe = Path('cloudflared.exe' if is_windows else 'cloudflared')
    if here_exe.exists():
        return str(here_exe.resolve())
    # Repo-root alongside the project layout
    proj_exe = _REPO_ROOT / ('cloudflared.exe' if is_windows else 'cloudflared')
    if proj_exe.exists():
        return str(proj_exe)
    return shutil.which('cloudflared.exe' if is_windows else 'cloudflared')


# ---------------------------------------------------------------------------
# State of the in-progress regenerate (single-flight)
# ---------------------------------------------------------------------------
_state_lock = Lock()
_state: dict[str, Any] = {
    'status': 'idle',            # idle | spawning | watching | success | error
    'message': None,
    'started_at_utc': None,
    'finished_at_utc': None,
    'tunnel_pid': None,           # PID we last spawned
    'log_path': None,             # path to the cloudflared log we're tailing
    'last_url': None,             # last URL successfully captured
    'last_url_captured_utc': None,
    'regenerate_count': 0,        # how many times the backend has spawned cloudflared
}


def _set(**fields: Any) -> None:
    with _state_lock:
        _state.update(fields)


def regenerate_status() -> dict:
    with _state_lock:
        return dict(_state)


# ---------------------------------------------------------------------------
# Process management
# ---------------------------------------------------------------------------

def _is_running(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        if sys.platform.startswith('win'):
            # On Windows, sending signal 0 isn't supported; use tasklist.
            out = subprocess.run(
                ['tasklist', '/FI', f'PID eq {pid}'],
                capture_output=True, text=True, timeout=5,
            )
            return str(pid) in (out.stdout or '')
        os.kill(pid, 0)
        return True
    except (OSError, subprocess.SubprocessError, ValueError):
        return False


def _kill(pid: int | None) -> None:
    if not pid:
        return
    try:
        if sys.platform.startswith('win'):
            subprocess.run(
                ['taskkill', '/F', '/PID', str(pid)],
                capture_output=True, timeout=5,
            )
        else:
            try:
                os.kill(pid, signal.SIGTERM)
                # Give it 2 seconds to drain, then SIGKILL.
                for _ in range(20):
                    if not _is_running(pid):
                        return
                    time.sleep(0.1)
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    except Exception as exc:  # noqa: BLE001
        log.warning('cloudflared kill(pid=%s) failed: %s', pid, exc)


# ---------------------------------------------------------------------------
# LAN-IP discovery (cross-platform best-effort)
# ---------------------------------------------------------------------------

def _discover_lan_ips() -> list[str]:
    """Return non-loopback IPv4 addresses. Best-effort - empty list is fine."""
    ips: list[str] = []
    try:
        import socket
        hostname = socket.gethostname()
        # Some systems return only the loopback for gethostbyname; fall back
        # to addrinfo on hostname which usually exposes the LAN IP.
        try:
            for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
                ip = info[4][0]
                if ip and not ip.startswith('127.') and not ip.startswith('169.254.'):
                    if ip not in ips:
                        ips.append(ip)
        except socket.gaierror:
            pass
        # Connect-to-internet trick: opens a UDP socket that doesn't actually
        # send anything but lets the OS pick the egress interface IP.
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(('1.1.1.1', 80))
            egress_ip = s.getsockname()[0]
            if egress_ip and not egress_ip.startswith('127.') and egress_ip not in ips:
                ips.append(egress_ip)
        finally:
            s.close()
    except Exception as exc:  # noqa: BLE001
        log.debug('LAN-IP discovery failed: %s', exc)
    return ips


def _backend_port() -> int:
    """Best-effort: figure out which port the backend is listening on so we
    can tunnel to ourselves. Default to 8001 to match start.bat/start.sh.
    """
    try:
        return int(os.environ.get('PORT') or os.environ.get('BACKEND_PORT') or 8001)
    except (ValueError, TypeError):
        return 8001


def _write_url_file(public_url: str | None, port: int) -> None:
    """Write the URL file: public URL first (if any), then LAN, then localhost."""
    path = _pick_url_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    if public_url:
        lines.append(public_url)
    for ip in _discover_lan_ips():
        lines.append(f'http://{ip}:{port}')
    lines.append(f'http://localhost:{port}')
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')


# Pattern matches ONLY real URLs we're willing to publish.  Anything
# else read out of public_url.txt (whitespace, "ECHO is off." garbage
# from a buggy launcher, comments, or stray shell diagnostics) is
# rejected so the frontend banner never surfaces junk.
_URL_LINE_RE = re.compile(r'^\s*https?://[^\s]+', re.IGNORECASE)
_URL_RE = re.compile(r'https://[a-z0-9-]+\.trycloudflare\.com', re.IGNORECASE)


# ---------------------------------------------------------------------------
# The actual regenerate worker. Runs in a worker thread (subprocess + sleep
# loop) so the HTTP request can return immediately.
# ---------------------------------------------------------------------------

_in_flight = threading.Lock()


def _do_regenerate_in_thread(port: int) -> None:
    """Synchronous implementation - intended for asyncio.to_thread."""
    binary = _find_cloudflared()
    if not binary:
        _set(
            status='error',
            message='cloudflared binary not found. Run start.bat/start.sh once first to download it.',
            finished_at_utc=utcnow_iso(),
        )
        return

    # Kill any cloudflared we previously spawned. We don't kill unrelated
    # cloudflared processes the launcher may have started - those are owned
    # by the launcher and will be cleaned up on its own exit.
    prev_pid = _state.get('tunnel_pid')
    if _is_running(prev_pid):
        _kill(prev_pid)

    log_path = Path('/tmp') / f'mrd_cloudflared_backend_{port}.log'
    try:
        log_path.write_text('', encoding='utf-8')  # truncate
    except OSError:
        pass

    _set(
        status='spawning',
        message=f'Launching cloudflared -> http://localhost:{port}',
        started_at_utc=utcnow_iso(),
        finished_at_utc=None,
        log_path=str(log_path),
    )

    try:
        # stdout + stderr to the same log; cloudflared writes the URL on
        # stderr by default, so capturing both is mandatory.
        proc = subprocess.Popen(
            [
                binary, '--no-autoupdate', 'tunnel',
                '--url', f'http://localhost:{port}',
            ],
            stdout=open(log_path, 'a', encoding='utf-8'),
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            close_fds=(os.name != 'nt'),
        )
    except Exception as exc:  # noqa: BLE001
        _set(
            status='error',
            message=f'Failed to launch cloudflared: {exc}',
            finished_at_utc=utcnow_iso(),
        )
        return

    _set(
        tunnel_pid=proc.pid,
        status='watching',
        message='Waiting for trycloudflare.com URL (up to 60s)...',
        regenerate_count=int(_state.get('regenerate_count') or 0) + 1,
    )

    # Pre-write the URL file with LAN+localhost so the banner can still
    # render *something* while we wait for the tunnel URL.
    _write_url_file(None, port)

    deadline = time.monotonic() + 60.0
    captured: str | None = None
    while time.monotonic() < deadline:
        try:
            content = log_path.read_text(encoding='utf-8', errors='replace')
        except OSError:
            content = ''
        m = _URL_RE.search(content)
        if m:
            captured = m.group(0)
            break
        # Also bail early if cloudflared exited.
        if proc.poll() is not None:
            break
        time.sleep(0.5)

    if captured:
        _write_url_file(captured, port)
        _set(
            status='success',
            message=f'Captured {captured}',
            last_url=captured,
            last_url_captured_utc=utcnow_iso(),
            finished_at_utc=utcnow_iso(),
        )
        log.info('cloudflared regenerate succeeded: %s', captured)
    else:
        # Don't kill the proc - it may still be negotiating beyond our 60 s
        # window. The frontend's polling will pick it up when ready.
        _set(
            status='watching',
            message='Tunnel still negotiating beyond 60s window. The URL '
                    'will appear in the banner once cloudflared finishes.',
            finished_at_utc=utcnow_iso(),
        )
        log.warning('cloudflared regenerate timed out waiting for URL')


# ---------------------------------------------------------------------------
# HTTP routes
# ---------------------------------------------------------------------------

@router.post('/regenerate')
async def regenerate_tunnel():
    """Spawn a fresh Cloudflare Quick Tunnel and update the URL file.

    Returns immediately with the in-progress status. The frontend polls
    /api/public-url to see the new URL once cloudflared finishes.
    """
    if not _in_flight.acquire(blocking=False):
        return {
            'ok': True,
            'started': False,
            'reason': 'regenerate already in flight',
            'state': regenerate_status(),
        }
    port = _backend_port()
    # Release the in-flight lock once the worker finishes (success/error).
    async def _runner():
        try:
            await asyncio.to_thread(_do_regenerate_in_thread, port)
        finally:
            try:
                _in_flight.release()
            except RuntimeError:
                pass
    asyncio.create_task(_runner())
    return {
        'ok': True,
        'started': True,
        'port': port,
        'state': regenerate_status(),
    }


@router.get('/regenerate-status')
def get_regenerate_status():
    """Lightweight polling endpoint for the frontend to show progress."""
    state = regenerate_status()
    state['pid_alive'] = _is_running(state.get('tunnel_pid'))
    return state


# ---------------------------------------------------------------------------
# Phase 26.10: Auto-refresh on backend startup
# ---------------------------------------------------------------------------
#
# We want EVERY backend startup to surface a fresh, randomized
# `https://*.trycloudflare.com` URL. There are two real launch flows:
#
#   1) Via start.sh / start.bat (the normal flow for end users): the
#      launcher already spawns cloudflared and writes public_url.txt with
#      a brand-new random URL. The backend should NOT spawn a second
#      cloudflared in this case - that would waste resources and confuse
#      the tunnel-cleanup logic on Ctrl+C.
#
#   2) Direct uvicorn launch (dev / supervisor / cloud preview): the
#      launcher never runs, so public_url.txt is empty AND no cloudflared
#      is alive. In this case we want the backend itself to spawn the
#      tunnel.
#
# Strategy: wait up to `MRD_TUNNEL_GRACE_SECONDS` (default 18s) for the
# launcher's URL to land in the file. If it doesn't, treat ourselves as
# the source of truth and call the same regenerate worker the HTTP
# endpoint uses.
# ---------------------------------------------------------------------------

def _public_url_file_has_trycloudflare() -> bool:
    """True iff one of our candidate files contains a trycloudflare URL."""
    for cand in _URL_FILE_CANDIDATES:
        try:
            if not cand.exists():
                continue
            txt = cand.read_text(encoding='utf-8', errors='replace')
            if _URL_RE.search(txt):
                return True
        except OSError:
            continue
    return False


def _find_stale_public_url_file(max_age_seconds: float) -> Path | None:
    """Return the first public_url.txt that either
       (a) contains a trycloudflare URL but whose mtime is older than
           `max_age_seconds` (left over from a previous launch), OR
       (b) contains any non-blank, non-comment line that is NOT a valid
           URL — indicating pollution from a buggy launcher (e.g. the
           literal string "ECHO is off." leaked by start.bat when
           delayed-expansion capture failed pre-Phase-26.60).

    Both conditions warrant wiping the file so the launcher (or backend
    fallback) can publish a fresh URL cleanly.  Files that are entirely
    blank or entirely valid URLs are LEFT ALONE regardless of age (the
    valid content is either fresh enough to keep or truly empty and
    harmless).
    """
    import time as _time
    now = _time.time()
    for cand in _URL_FILE_CANDIDATES:
        try:
            if not cand.exists():
                continue
            txt = cand.read_text(encoding='utf-8', errors='replace')
            stripped = txt.strip()
            if not stripped:
                # Empty file — not stale, not polluted.
                continue
            # Check for pollution: any non-blank, non-comment line that
            # isn't a URL.
            polluted = False
            for line in txt.splitlines():
                s = line.strip()
                if not s or s.startswith('#'):
                    continue
                if not _URL_LINE_RE.match(s):
                    polluted = True
                    break
            if polluted:
                log.info(
                    'startup: public_url.txt at %s contains non-URL '
                    'lines (launcher pollution); will wipe',
                    cand,
                )
                return cand
            # Not polluted — apply mtime-based staleness only if it
            # holds a tunnel URL that could be from a prior session.
            if _URL_RE.search(txt):
                mtime = cand.stat().st_mtime
                if (now - mtime) > max_age_seconds:
                    return cand
        except OSError:
            continue
    return None


async def ensure_public_url_on_startup() -> None:
    """Schedule a one-shot background task that guarantees a fresh public
    URL is published shortly after backend boot.

    Returns immediately - the actual work runs as an asyncio background task
    so it can't delay the FastAPI lifespan startup. Safe to call multiple
    times (single-flight lock prevents duplicate cloudflared spawns).
    """
    grace_seconds = float(os.environ.get('MRD_TUNNEL_GRACE_SECONDS') or 18.0)
    # Any pre-existing public_url.txt whose mtime is older than this
    # (default 5 min) is treated as "left over from a prior run / shipped
    # inside the zip" and gets wiped so the launcher (or backend fallback)
    # can publish a fresh URL.
    stale_threshold = float(os.environ.get('MRD_TUNNEL_STALE_SECONDS') or 300.0)

    # Detect & wipe stale URL file before the grace period begins. This
    # prevents the "shipped a fake URL in the zip" failure mode where the
    # backend skips spawning because it sees an old trycloudflare URL.
    stale = _find_stale_public_url_file(stale_threshold)
    if stale is not None:
        try:
            stale.write_text('', encoding='utf-8')
            log.info(
                'startup: wiped stale public_url.txt (%s, mtime > %ds old)',
                stale, int(stale_threshold),
            )
        except OSError as exc:
            log.warning('startup: could not wipe stale %s: %s', stale, exc)

    async def _worker():
        try:
            # Phase 1: give the launcher up to grace_seconds to publish.
            # Poll every 1s instead of one big sleep so we can bail early
            # the moment a URL appears.
            deadline = time.monotonic() + grace_seconds
            while time.monotonic() < deadline:
                if _public_url_file_has_trycloudflare():
                    log.info(
                        'startup: launcher-supplied public URL detected; '
                        'skipping backend tunnel spawn'
                    )
                    return
                await asyncio.sleep(1.0)

            # Phase 2: launcher didn't publish - spawn one ourselves.
            log.info(
                'startup: no launcher-supplied public URL after %.0fs; '
                'attempting to spawn a fresh cloudflared tunnel from the backend',
                grace_seconds,
            )
            # Friendly preflight log: tell the operator whether we can even
            # find the binary BEFORE trying to spawn it. This is the single
            # most common failure mode (cloudflared never downloaded).
            binary = _find_cloudflared()
            if not binary:
                log.warning(
                    'startup: cloudflared binary not found on disk or PATH. '
                    'Run start.bat / start.sh (or place a cloudflared binary '
                    'next to the app dir) to enable the public tunnel. LAN '
                    'and localhost URLs are still served via /api/public-url.'
                )
                # Fall through to _do_regenerate_in_thread anyway so it
                # records the error state for the UI.
            if not _in_flight.acquire(blocking=False):
                log.info('startup: regenerate already in flight, skipping')
                return
            port = _backend_port()
            try:
                await asyncio.to_thread(_do_regenerate_in_thread, port)
            finally:
                try:
                    _in_flight.release()
                except RuntimeError:
                    pass
        except Exception as exc:  # noqa: BLE001
            log.warning('startup tunnel ensure failed: %s', exc)

    # Fire-and-forget. If the user has disabled the auto-refresh via env
    # var, we skip entirely.
    if os.environ.get('MRD_AUTO_TUNNEL_ON_STARTUP', '1') == '0':
        log.info('startup: MRD_AUTO_TUNNEL_ON_STARTUP=0 set; skipping')
        return
    asyncio.create_task(_worker())
