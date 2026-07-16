"""
Public-tunnel URL surface for the dashboard.

When a user launches the app via start.bat / start.sh, the launcher spins up a
Cloudflare Quick Tunnel (`cloudflared tunnel --url http://localhost:8001`) so
the dashboard is reachable from any network. The tunnel URL is captured by
the launcher and persisted to `app/data/public_url.txt` so the frontend can
display it under the dashboard title - no need to copy/paste from a console
window that may be on a different monitor.

The launcher writes one URL per line in the file in priority order:
  line 1: the cloudflare public URL (https://*.trycloudflare.com)
  line 2: the primary LAN URL (http://<lan-ip>:<port>)
  ...    additional LAN URLs

Each URL is a bare origin (no /ui suffix). The route appends /ui itself when
constructing the "share with anyone" link. This file is wiped on each
launch so a stale URL from a previous session can't mislead users.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import APIRouter

log = logging.getLogger('app.routes.public_url')

router = APIRouter(prefix='/api', tags=['public-url'])

# Candidate locations the launcher might have written to, in priority order.
# The launcher writes the file relative to its own cwd (the unzip directory),
# but uvicorn may be launched from a different cwd. Probe each location and
# return the first that exists.
_HERE = Path(__file__).resolve()
# /app/app/routes/public_url.py -> /app
_REPO_ROOT = _HERE.parent.parent.parent
_CANDIDATES = [
    Path(os.environ.get('MRD_PUBLIC_URL_FILE')) if os.environ.get('MRD_PUBLIC_URL_FILE') else None,
    Path('app/data/public_url.txt'),                # launcher cwd-relative
    Path('data/public_url.txt'),                    # alternative launcher layout
    _REPO_ROOT / 'app' / 'data' / 'public_url.txt', # absolute repo path
    _REPO_ROOT / 'data' / 'public_url.txt',
]


def _pick_file() -> Path | None:
    """Return the first candidate file that exists, else None."""
    for cand in _CANDIDATES:
        if cand is None:
            continue
        try:
            if cand.exists() and cand.is_file():
                return cand
        except OSError:
            continue
    return None


def _read_lines() -> list[str]:
    """Read every legitimate URL line from the URL file.

    Filters out:
      - blank lines and comments
      - non-URL diagnostic output (e.g. "ECHO is off." from a buggy
        Windows launcher — see start.bat Phase 26.60 comment block)
      - anything that doesn't start with http:// or https://

    This is defense-in-depth: the launcher SHOULD only ever write real
    URLs, and the backend startup hook wipes obvious pollution, but the
    frontend banner must never surface garbage no matter what upstream
    bug produced it.
    """
    path = _pick_file()
    if not path:
        return []
    try:
        raw = path.read_text(encoding='utf-8', errors='replace')
    except OSError as exc:
        log.debug('public_url: cannot read %s: %s', path, exc)
        return []
    lines = []
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith('#'):
            continue
        # Strict URL prefix check.  Rejects any non-URL diagnostic text
        # that a buggy launcher may have leaked into the file.
        s_lower = s.lower()
        if not (s_lower.startswith('http://') or s_lower.startswith('https://')):
            log.debug('public_url: discarding non-URL line %r from %s', s, path)
            continue
        lines.append(s)
    return lines


def _classify(url: str) -> str:
    """Return a short label for the UI: 'public' | 'lan' | 'local' | 'other'."""
    lower = url.lower()
    if 'trycloudflare.com' in lower or 'ngrok' in lower or 'cloudflareaccess.com' in lower:
        return 'public'
    if 'localhost' in lower or '127.0.0.1' in lower:
        return 'local'
    if '192.168.' in lower or '10.' in lower or '172.16.' in lower or '172.17.' in lower:
        return 'lan'
    return 'other'


@router.get('/public-url')
def public_url():
    """Return the captured public tunnel URL (and LAN fallbacks).

    Shape:
        {
          'url': 'https://....trycloudflare.com',   # primary public URL or null
          'kind': 'public'|'lan'|'local'|null,
          'urls': [
            {'url': '...', 'kind': 'public'|'lan'|'local', 'label': '...'},
            ...
          ],
          'file': 'app/data/public_url.txt',
          'available': bool,
        }
    """
    lines = _read_lines()
    items = []
    primary_public = None
    for ln in lines:
        # Strip any trailing `/ui` so consumers can append their own path.
        bare = ln.rstrip('/').removesuffix('/ui').rstrip('/')
        kind = _classify(bare)
        label = {
            'public': 'Public (anyone with the link)',
            'lan': 'LAN (same network)',
            'local': 'This machine only',
            'other': 'Other',
        }.get(kind, 'Other')
        items.append({'url': bare, 'kind': kind, 'label': label})
        if kind == 'public' and primary_public is None:
            primary_public = bare
    return {
        'url': primary_public,
        'kind': 'public' if primary_public else (items[0]['kind'] if items else None),
        'urls': items,
        'file': str(_pick_file() or _CANDIDATES[1]),
        'available': bool(items),
    }
