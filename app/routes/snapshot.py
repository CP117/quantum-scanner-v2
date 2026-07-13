"""
Snapshot endpoint - broadcasts the latest server-side scored rows to every
connected client.  This is what secondary devices (phones, tablets, other
laptops) hit instead of running their own per-batch sweep.

Phase 26.16 / Tier 2.5: serves an `ETag` header derived from the
snapshot's last-update fingerprint and honors `If-None-Match` with a 304
Not Modified response. Multiple browser tabs polling at 3-5s now skip the
re-thin + re-serialize cost entirely when nothing changed since their
last poll.
"""
import hashlib

from fastapi import APIRouter, Header, Query, Response
from fastapi.responses import JSONResponse

from app.services.snapshot_store import get_snapshot, get_snapshot_meta
from app.services.market_activity_service import stamp_active

router = APIRouter(prefix='/api/scan', tags=['snapshot'])


def _compute_snapshot_etag(market: str, limit: int, compact: bool, sort: str = 'score') -> str:
    """Build a weak ETag from the snapshot's update fingerprint.

    The tuple `(market, last_batch_at, rows_scored, evaluations_ever,
    current_batch_index, limit, compact)` changes whenever the snapshot
    bucket gains or loses rows, so it's sufficient to detect "anything
    the client cares about has changed".  We MD5 it to keep the header
    short.

    Marked as a weak ETag (`W/"…"`) because the payload includes
    monotonically-changing `age_seconds` fields that don't materially
    change the response semantics — strong-comparing them would defeat
    the cache for no benefit.
    """
    meta = get_snapshot_meta(market) or {}
    fingerprint = '|'.join(str(x) for x in (
        market,
        meta.get('last_batch_at') or '',
        meta.get('rows_scored') or 0,
        meta.get('evaluations_ever') or 0,
        meta.get('current_batch_index') or 0,
        meta.get('sweeps_completed') or 0,
        int(limit),
        bool(compact),
        sort,
    ))
    digest = hashlib.md5(fingerprint.encode('utf-8')).hexdigest()  # noqa: S324 - non-crypto use
    return f'W/"{digest}"'


@router.get('/snapshot')
def scan_snapshot(
    response: Response,
    market: str = Query('stocks'),
    limit: int = Query(1500, ge=1, le=10000),
    compact: bool = Query(True),
    sort: str = Query('score'),
    if_none_match: str | None = Header(None, alias='If-None-Match'),
):
    """Return scored rows from the background scanner for the requested
    market.

    Phase 25: hitting this endpoint with `market=crypto` is treated as a
    user signal that they're actively looking at the crypto tab.  The
    scan loop then opens up the live CoinGecko / CryptoCompare /
    CoinPaprika provider cascade for crypto.  Otherwise crypto stays in
    cache-only mode and the full HTTP budget goes to stocks.  See
    `market_activity_service.py` for the TTL window.

    Phase 26.16 / Tier 2.5: handles `If-None-Match` with a 304 response
    when the snapshot fingerprint hasn't advanced since the client's
    previous poll. ETag is `W/"<md5(market|last_batch|rows|batch_idx|
    sweeps|limit|compact)>"`. The frontend (`app.js`) is allowed to send
    this header on its polling cycle but is NOT required to — clients
    that omit it get the full payload as before.

    Phase 26.18.c: `market` accepts loose forms (`stock`, `equities`,
    `Crypto`, ...) via `normalize_market`. Anything unrecognized falls
    back to `stocks` (the most common case) instead of returning a 422.
    """
    from app.utils.input_tolerance import normalize_market
    market = normalize_market(market, default='stocks')
    stamp_active(market)
    sort = sort if sort in ('score', 'predicted_volume_intensity') else 'score'
    etag = _compute_snapshot_etag(market, limit, compact, sort)
    # Always advertise the current fingerprint so the client can echo it
    # back on the next poll.
    response.headers['ETag'] = etag
    # Phase 26.18 hotfix: default to `no-store` so that browsers (and the
    # vanilla `fetch()` calls in app.js) DO NOT auto-cache + auto-
    # revalidate the snapshot. Auto-revalidation was triggering 304
    # responses inside `fetchJson(...)` whose `if (!res.ok)` branch threw
    # an Error and surfaced as "Results unavailable, showing last good
    # snapshot". The ETag itself is still emitted so callers that
    # explicitly set `If-None-Match` (e.g. a future smart client) can
    # still benefit from the 304 short-circuit.
    response.headers['Cache-Control'] = 'no-store'

    # If-None-Match may carry multiple comma-separated tags; check each.
    if if_none_match:
        client_tags = {t.strip() for t in if_none_match.split(',') if t.strip()}
        if etag in client_tags or '*' in client_tags:
            # 304 — preserve ETag; no body.
            return Response(status_code=304, headers={
                'ETag': etag,
                'Cache-Control': 'no-store',
            })

    payload = get_snapshot(market, limit=limit, compact=compact, sort=sort)
    # Re-attach the ETag to the JSONResponse so it survives the route's
    # return path (FastAPI builds a fresh response object).
    return JSONResponse(content=payload, headers={
        'ETag': etag,
        'Cache-Control': 'no-store',
    })


@router.get('/snapshot/meta')
def scan_snapshot_meta(market: str | None = Query(None)):
    """Lightweight progress probe (no result rows).  Useful for the UI to
    poll cheaply while deciding whether to re-pull the full snapshot.

    Phase 26.18.c: market is normalized through `normalize_market`; an
    unknown / missing value falls through to `get_snapshot_meta(None)`
    which returns the both-markets summary.
    """
    if market is not None:
        from app.utils.input_tolerance import normalize_market
        market = normalize_market(market, default='stocks')
    return get_snapshot_meta(market)
