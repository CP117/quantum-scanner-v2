"""Server-rendered share landing page for social platforms.

Facebook, LinkedIn and (to a lesser extent) Twitter cards render link
previews by scraping Open Graph / Twitter Card meta tags from the URL a
user pastes. Frontend intent URLs can't inject meta tags into the
target — that has to come from the server. So when the detail-panel
share widget wants to post to those platforms, it points the intent's
`url=` at this route.

Real human visitors get a small HTML card with the metric summary AND
an auto-redirect to the actual dashboard (`/ui?symbol=…`), so a click
from a shared LinkedIn post lands the reader on the live detail panel
for that symbol. Bots / crawlers just read the meta tags.
"""
from __future__ import annotations

import html
import os
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, Response

from app.services.og_image_service import get_share_og_image
from app.services.share_gallery_service import get_recent_shares, record_share
from app.services.snapshot_store import get_snapshot

router = APIRouter(tags=['share'])


def _num(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(default)


def _find_row(symbol: str) -> dict | None:
    """Look up the freshest scored row for a symbol across both
    markets.  Uses the O(1) snapshot lookup first, then broadcasts,
    then falls back to `get_symbol_detail` which will score the row
    on demand — this last step is what makes the share endpoint work
    for crypto symbols whose snapshot batch hasn't landed yet.
    """
    symbol = (symbol or '').upper().strip()
    if not symbol:
        return None
    try:
        from app.services.snapshot_store import lookup_snapshot_row
        for market in ('stocks', 'crypto'):
            row = lookup_snapshot_row(symbol, market)
            if row is not None and row.get('final_score'):
                return row
    except Exception:  # noqa: BLE001
        pass
    for market in ('stocks', 'crypto'):
        try:
            snap = get_snapshot(market, limit=5000, compact=False)
        except Exception:
            continue
        for row in (snap.get('results') or []):
            if str(row.get('symbol') or '').upper() == symbol:
                return row
    # Final fallback — on-demand score.  Crypto symbols (BTC-USD etc.)
    # take this path because their broadcast snapshot lags the batch
    # scoring loop; without this, the share landing page for a crypto
    # symbol would render the "no live snapshot" fallback card.
    try:
        from app.services.detail_service import get_symbol_detail
        market = 'crypto' if symbol.endswith('-USD') else 'stocks'
        payload = get_symbol_detail(symbol, force_live=False, market=market)
        if payload and payload.get('final_score'):
            return payload
    except Exception:  # noqa: BLE001
        pass
    return None


def _build_summary_lines(row: dict) -> list[str]:
    """Compact multi-line description used for meta og:description and
    the visible card body. Kept ≤ ~450 chars total since most social
    platforms truncate og:description around 300."""
    lines: list[str] = []
    sym = row.get('symbol') or ''
    lines.append(
        f"Tier {row.get('tier') or '?'} · {row.get('final_direction') or 'Neutral'} · "
        f"Score {_num(row.get('final_score')):.1f}"
    )
    fm_all = row.get('forward_metrics_garch') or row.get('forward_metrics') or {}
    blk = fm_all.get('forward_1d') or {}
    if blk:
        p_up = _num(blk.get('p_up_cf', blk.get('p_up', 0.5)), 0.5)
        p_ctx = blk.get('p_up_ctx')
        kelly = blk.get('effective_kelly_rank')
        parts = [f"1d P(up) {p_up * 100:.1f}%"]
        if p_ctx is not None:
            parts.append(f"ctx {_num(p_ctx) * 100:.1f}%")
        if kelly is not None:
            parts.append(f"Kelly {_num(kelly):.4f}")
        lines.append(' · '.join(parts))
    fc = fm_all.get('forecast_context') or {}
    tags = []
    sq = _num(fc.get('squeeze_probability'))
    ve = _num(fc.get('volatility_event_probability'))
    if sq >= 0.35:
        tags.append(f"Squeeze {sq * 100:.0f}%")
    if ve >= 0.45:
        tags.append(f"Vol-event {ve * 100:.0f}%")
    if tags:
        lines.append(' · '.join(tags))
    market = (row.get('factor_breakdown') or {}).get('market') or {}
    ssp = market.get('short_selling_pressure') or {}
    if ssp.get('score') is not None:
        lines.append(
            f"SSP {_num(ssp.get('score')):.0f} ({str(ssp.get('label') or 'neutral').replace('_', ' ')})"
        )
    pvi = market.get('predicted_volume_intensity') or {}
    if pvi.get('score') is not None:
        lines.append(
            f"PVI {_num(pvi.get('score')):.0f} ({pvi.get('bucket') or 'low'})"
        )
    oe = market.get('options_expiration') or {}
    if oe.get('nearest_expiration'):
        lines.append(
            f"Nearest expiry {oe.get('nearest_expiration')} "
            f"({oe.get('days_to_expiration')}d)"
        )
    return lines


def _public_base_url(request: Request) -> str:
    """Reconstruct the externally-visible base URL, honoring the
    forwarded headers set by the Emergent preview ingress / any
    upstream reverse proxy. Falls back to `request.base_url`."""
    fwd_host = request.headers.get('x-forwarded-host') or request.headers.get('host')
    fwd_proto = (request.headers.get('x-forwarded-proto')
                 or ('https' if request.url.scheme == 'https' else 'http'))
    if fwd_host and '127.0.0.1' not in fwd_host and 'localhost' not in fwd_host:
        return f"{fwd_proto}://{fwd_host}"
    return str(request.base_url).rstrip('/')


def _dashboard_url(request: Request, symbol: str, preset: str | None) -> str:
    """Absolute dashboard link the redirect points to."""
    base = _public_base_url(request)
    q = f"symbol={symbol}"
    if preset:
        q += f"&preset={preset}"
    return f"{base}/frontend/market-refinement-dashboard.html?{q}"


@router.get('/share/{symbol}', response_class=HTMLResponse)
def share_symbol(symbol: str, request: Request, preset: str | None = Query(None)) -> HTMLResponse:
    symbol = (symbol or '').upper().strip()
    row = _find_row(symbol)
    # Absolute URL for the OG image so scrapers on external hosts can fetch it.
    base = _public_base_url(request)
    og_image_url = f"{base}/share/{symbol}/og.png"
    canonical = f"{base}/share/{symbol}" + (f"?preset={preset}" if preset else '')
    # Log this share event (deduped internally). Use best-effort: never
    # let a persistence error break the share landing page.
    try:
        record_share(
            symbol=symbol,
            row=row,
            ua=request.headers.get('user-agent'),
            referer=request.headers.get('referer'),
            preset=preset,
        )
    except Exception:  # noqa: BLE001
        pass

    if row is None:
        # Bare minimum meta so social scrapers don't 404, plus a redirect.
        title = f"{symbol} — Quantum Market Scanner"
        desc = "Live multi-factor market scanner analysis. Open the dashboard to see the full breakdown."
        dashboard = _dashboard_url(request, symbol, preset)
        html_out = _render_share_html(
            symbol=symbol, title=title, description=desc,
            summary_lines=[desc], dashboard_url=dashboard,
            canonical_url=canonical, og_image_url=og_image_url,
        )
        return HTMLResponse(content=html_out, status_code=200)

    lines = _build_summary_lines(row)
    title = (
        f"{symbol} — {row.get('final_direction') or 'Neutral'} · "
        f"Tier {row.get('tier') or '?'} · Score {_num(row.get('final_score')):.1f}"
    )
    description = ' | '.join(lines)
    dashboard = _dashboard_url(request, symbol, preset)
    html_out = _render_share_html(
        symbol=symbol, title=title, description=description,
        summary_lines=lines, dashboard_url=dashboard,
        canonical_url=canonical, og_image_url=og_image_url,
    )
    return HTMLResponse(content=html_out, status_code=200)


@router.get('/share/{symbol}/og.png')
def share_symbol_og_image(symbol: str) -> Response:
    """1200x630 PNG card served as `og:image` for social link previews.
    Cached per-symbol for 5 minutes; content-type + cache headers set
    for both scraper and CDN correctness."""
    png = get_share_og_image(symbol)
    return Response(
        content=png,
        media_type='image/png',
        headers={
            'Cache-Control': 'public, max-age=300',
            'Content-Length': str(len(png)),
        },
    )


@router.get('/api/shares/recent')
def api_recent_shares(limit: int = Query(50, ge=1, le=200)) -> dict:
    """Feeds the Shared Analyses gallery page."""
    return {'items': get_recent_shares(limit=limit), 'limit': limit}


def _render_share_html(*, symbol: str, title: str, description: str,
                       summary_lines: list[str], dashboard_url: str,
                       canonical_url: str, og_image_url: str) -> str:
    site = os.environ.get('SHARE_SITE_NAME', 'Quantum Market Scanner')
    esc = html.escape
    body_items = ''.join(
        f'<li>{esc(line)}</li>' for line in summary_lines
    )
    # Twitter Card + Open Graph tags. `og:title` (title) + `og:description`
    # drive the FB/LinkedIn preview; `twitter:card=summary_large_image`
    # gets a large card on X even without an image.
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{esc(title)}</title>
  <meta name="description" content="{esc(description)}">
  <link rel="canonical" href="{esc(canonical_url)}">
  <!-- Open Graph -->
  <meta property="og:type" content="article">
  <meta property="og:site_name" content="{esc(site)}">
  <meta property="og:title" content="{esc(title)}">
  <meta property="og:description" content="{esc(description)}">
  <meta property="og:url" content="{esc(canonical_url)}">
  <meta property="og:image" content="{esc(og_image_url)}">
  <meta property="og:image:width" content="1200">
  <meta property="og:image:height" content="630">
  <meta property="og:image:alt" content="{esc(title)}">
  <!-- Twitter Card -->
  <meta name="twitter:card" content="summary_large_image">
  <meta name="twitter:title" content="{esc(title)}">
  <meta name="twitter:description" content="{esc(description)}">
  <meta name="twitter:image" content="{esc(og_image_url)}">
  <!-- 4-second auto-redirect for real users; bots keep the meta tags. -->
  <meta http-equiv="refresh" content="4; url={esc(dashboard_url)}">
  <style>
    body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#0d1117;color:#e6edf3;margin:0;padding:0;min-height:100vh;display:flex;align-items:center;justify-content:center}}
    .card{{max-width:640px;padding:32px;background:#141a24;border:1px solid rgba(255,255,255,.1);border-radius:14px;box-shadow:0 20px 60px rgba(0,0,0,.5)}}
    h1{{margin:0 0 12px;font-size:1.6rem;font-weight:700;letter-spacing:-.01em}}
    p.desc{{color:#c8d0dd;font-size:.95rem;line-height:1.45;margin:0 0 20px}}
    ul{{list-style:none;padding:0;margin:0 0 20px}}
    li{{padding:8px 12px;background:rgba(255,255,255,.03);border-left:3px solid #5eead4;margin-bottom:6px;font-size:.85rem;border-radius:0 6px 6px 0}}
    img.preview{{width:100%;border-radius:10px;margin-bottom:16px;border:1px solid rgba(255,255,255,.08)}}
    a.cta{{display:inline-block;background:linear-gradient(180deg,rgba(94,234,212,.24),rgba(94,234,212,.08));border:1px solid rgba(94,234,212,.5);color:#dffcf5;padding:10px 20px;border-radius:8px;text-decoration:none;font-weight:600;font-size:.9rem;letter-spacing:.02em}}
    a.cta:hover{{background:rgba(94,234,212,.28)}}
    .redirect{{color:rgba(255,255,255,.4);font-size:.72rem;margin-top:14px}}
    .brand{{color:#5eead4;font-size:.7rem;letter-spacing:.15em;text-transform:uppercase;margin-bottom:6px;font-weight:600}}
  </style>
</head>
<body>
  <div class="card">
    <div class="brand">{esc(site)}</div>
    <img class="preview" src="{esc(og_image_url)}" alt="{esc(title)}" loading="lazy">
    <h1>{esc(title)}</h1>
    <p class="desc">Live multi-factor analysis for <strong>${esc(symbol)}</strong> — short-selling pressure, predicted volume intensity, options expiration awareness and future forecast context.</p>
    <ul>{body_items}</ul>
    <a class="cta" href="{esc(dashboard_url)}">Open live dashboard \u2197</a>
    <div class="redirect">Auto-redirecting in 4 seconds…</div>
  </div>
</body>
</html>
"""
