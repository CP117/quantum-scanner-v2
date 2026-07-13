"""
Live diagnostic endpoints for debugging wedge / lock-contention issues.

Phase 26.35: when the scanner appears frozen and the user reports "all
tabs spin forever", the root cause is almost always a thread blocked on
a non-interruptable I/O syscall (HTTP socket without a timeout) OR a
lock-acquisition deadlock somewhere.  Both look identical from the
outside — the process sits at 0 % CPU and HTTP handlers don't respond.

`/system/threads` dumps the current stack trace of EVERY Python thread
so the operator can see, in real time, which threads are blocked and
where.  This is the same data `py-spy dump` would produce — but
served by the app itself so it works in sandboxed environments where
py-spy can't attach (no ptrace).

This endpoint is read-only, holds no locks, and returns instantly even
when the rest of the app is wedged — because thread-state introspection
goes through `sys._current_frames()`, which only requires the GIL for
a few microseconds.
"""
from __future__ import annotations

import sys
import threading
import traceback
from fastapi import APIRouter

router = APIRouter()


@router.get('/system/threads')
def system_threads() -> dict:
    """Return a dict of {thread_name: stack_trace_lines} for every live
    Python thread.  Designed to work when the rest of the app is hung —
    holds no locks, uses only `sys._current_frames()` (cheap).
    """
    frames = sys._current_frames()
    threads_by_ident = {t.ident: t for t in threading.enumerate()}
    out: list[dict] = []
    for ident, frame in frames.items():
        t = threads_by_ident.get(ident)
        name = t.name if t else 'unknown'
        daemon = t.daemon if t else None
        is_alive = t.is_alive() if t else None
        try:
            stack = traceback.format_stack(frame, limit=25)
        except Exception as exc:  # noqa: BLE001
            stack = [f'<error formatting stack: {exc}>']
        out.append({
            'name': name,
            'ident': ident,
            'daemon': daemon,
            'is_alive': is_alive,
            'stack': stack,
        })
    # Sort: main thread first, then snap-workers, then everything else.
    def _sort_key(t):
        n = t.get('name') or ''
        if n == 'MainThread':
            return (0, n)
        if n.startswith('snap-worker'):
            return (1, n)
        if n.startswith('dh-'):
            return (2, n)
        return (3, n)
    out.sort(key=_sort_key)
    return {
        'thread_count': len(out),
        'threads': out,
    }


@router.get('/system/threads/summary')
def system_threads_summary() -> dict:
    """Compact version of /system/threads: just thread names + the
    top-of-stack frame.  Use this when you want a quick read on what's
    happening without scrolling through pages of frames.
    """
    full = system_threads()
    summary = []
    for t in full['threads']:
        stack = t.get('stack') or []
        # The deepest frame (where the thread is actually blocked).
        top = stack[-1].strip().splitlines()[0] if stack else '<empty stack>'
        summary.append({
            'name': t['name'],
            'daemon': t.get('daemon'),
            'is_alive': t.get('is_alive'),
            'top_frame': top,
        })
    return {
        'thread_count': len(summary),
        'threads': summary,
    }
