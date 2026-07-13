"""
Shared `httpx.AsyncClient` pool for the regulatory monitor.

Why this exists
---------------
Phase 21 critical: the regulatory subsystem previously instantiated a new
`async with httpx.AsyncClient(...)` for every single HTTP request to SEC /
USAspending / SEC ticker maps.  Over a full 7,000-ticker auto-scan sweep
that's ~14,000 client instantiations, each negotiating a fresh TLS
handshake and opening a brand-new connection.  Over hours of sustained
operation this leaks file descriptors, exhausts the OS socket budget,
and saturates the asyncio event loop with churn that the snapshot scan
loop (running in a separate thread) cannot help with.

This module exposes a single keyed cache of long-lived `AsyncClient`
instances with proper connection pooling.  Two clients are pre-configured:
  - ``"sec"`` for SEC.gov endpoints (carries the required User-Agent).
  - ``"usaspending"`` for USAspending.gov endpoints.

Both reuse TLS sessions and keep up to ``max_keepalive_connections``
sockets alive so the typical autoscan sweep negotiates TLS once per
remote host instead of once per request.

Lifecycle
---------
Clients are created lazily on first use.  ``close_all()`` is called on
app shutdown to drain the connection pools cleanly.  The clients are
safe to share across coroutines (httpx async clients are designed for
concurrent use).
"""
from __future__ import annotations

import logging

import httpx

log = logging.getLogger('app.regulatory.http')

# Conservative limits — we don't need a huge pool but we DO need to
# keep connections alive long enough to reuse the TLS session across
# the typical autoscan cadence (~8 req/sec).  20 keepalive sockets per
# remote host is plenty.
_DEFAULT_LIMITS = httpx.Limits(
    max_keepalive_connections=20,
    max_connections=40,
    keepalive_expiry=60.0,
)

# Standard SEC User-Agent (required by SEC's fair-use policy).
_SEC_HEADERS = {
    'User-Agent': 'MarketRefinementDashboard/1.0 admin@localhost',
    'Accept-Encoding': 'gzip, deflate',
}

# Module-level cache keyed by logical name.
_clients: dict[str, httpx.AsyncClient] = {}


def _build_client(name: str) -> httpx.AsyncClient:
    """Construct an `httpx.AsyncClient` configured for the given logical name."""
    headers: dict[str, str] = {}
    if name == 'sec':
        headers = dict(_SEC_HEADERS)
    return httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, connect=10.0),
        headers=headers,
        follow_redirects=True,
        limits=_DEFAULT_LIMITS,
        http2=False,
    )


def get_client(name: str = 'sec') -> httpx.AsyncClient:
    """Return the shared `AsyncClient` for the given logical name.

    Valid names: ``"sec"`` (default), ``"usaspending"``.  Unknown names get
    a vanilla client without baked-in headers.
    """
    client = _clients.get(name)
    if client is not None and not client.is_closed:
        return client
    client = _build_client(name)
    _clients[name] = client
    log.debug('regulatory http: created shared client name=%s', name)
    return client


async def close_all() -> None:
    """Close every client in the pool.  Safe to call repeatedly."""
    for name, client in list(_clients.items()):
        try:
            await client.aclose()
        except Exception as exc:  # noqa: BLE001
            log.debug('regulatory http: aclose %s failed: %s', name, exc)
    _clients.clear()


def stats() -> dict:
    """Diagnostic snapshot of the pool (number of cached clients).

    httpx doesn't expose live socket counts in a stable way; this is just
    enough to confirm pooling is in effect (i.e. the count stays low).
    """
    return {
        'clients_open': sum(1 for c in _clients.values() if not c.is_closed),
        'clients_total': len(_clients),
        'names': sorted(_clients.keys()),
    }
