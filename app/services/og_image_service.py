"""OG-image PNG generator for social share previews.

Renders a 1200x630 branded metric card for a given symbol using Pillow.
Cached in-memory per symbol for `_OG_TTL_SECONDS` to survive multiple
scraper hits (Facebook / LinkedIn hit the URL several times when a link
is first shared).
"""
from __future__ import annotations

import io
import logging
import threading
import time
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger('app.og_image')

_OG_TTL_SECONDS = 300  # 5 min — link scrapers dedupe within this window
_cache: dict[str, tuple[float, bytes]] = {}
_cache_lock = threading.Lock()

# Brand palette — matches the dashboard aesthetic (teal on near-black).
_BG_TOP = (18, 24, 34)         # gradient top
_BG_BOTTOM = (10, 14, 20)      # gradient bottom
_ACCENT = (94, 234, 212)       # #5eead4
_TEXT_PRIMARY = (230, 237, 243)
_TEXT_MUTED = (170, 180, 195)
_TEXT_DIM = (110, 125, 140)
_BULL = (94, 193, 154)
_BEAR = (230, 120, 138)
_CARD_BG = (20, 26, 36)
_CARD_BORDER = (50, 60, 76)

_FONT_CANDIDATES_BOLD = [
    '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf',
    '/usr/share/fonts/truetype/freefont/FreeSansBold.ttf',
]
_FONT_CANDIDATES_REGULAR = [
    '/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf',
    '/usr/share/fonts/truetype/freefont/FreeSans.ttf',
]


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    for path in (_FONT_CANDIDATES_BOLD if bold else _FONT_CANDIDATES_REGULAR):
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except Exception:  # noqa: BLE001
                continue
    return ImageFont.load_default()


def _num(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(default)


def _vgradient(img: Image.Image) -> None:
    """Vertical linear gradient onto the image in-place."""
    w, h = img.size
    top = _BG_TOP
    bot = _BG_BOTTOM
    drw = ImageDraw.Draw(img)
    for y in range(h):
        t = y / max(1, h - 1)
        r = int(top[0] + (bot[0] - top[0]) * t)
        g = int(top[1] + (bot[1] - top[1]) * t)
        b = int(top[2] + (bot[2] - top[2]) * t)
        drw.line([(0, y), (w, y)], fill=(r, g, b))


def _direction_color(direction: str) -> tuple[int, int, int]:
    d = (direction or '').lower()
    if d == 'bullish':
        return _BULL
    if d == 'bearish':
        return _BEAR
    return _TEXT_MUTED


def _extract_metrics(row: dict) -> dict[str, Any]:
    fm_all = row.get('forward_metrics_garch') or row.get('forward_metrics') or {}
    blk = fm_all.get('forward_1d') or {}
    fc = fm_all.get('forecast_context') or {}
    market = (row.get('factor_breakdown') or {}).get('market') or {}
    ssp = market.get('short_selling_pressure') or {}
    pvi = market.get('predicted_volume_intensity') or {}
    oe = market.get('options_expiration') or {}
    op = market.get('options_positioning') or {}
    return {
        'p_up': _num(blk.get('p_up_cf', blk.get('p_up', 0.5)), 0.5),
        'p_ctx': _num(blk.get('p_up_ctx'), None) if blk.get('p_up_ctx') is not None else None,
        'kelly': _num(blk.get('effective_kelly_rank'), None) if blk.get('effective_kelly_rank') is not None else None,
        'drift': _num(blk.get('drift_pct'), 0.0) + _num(blk.get('jump_drift_pct'), 0.0),
        'squeeze': _num(fc.get('squeeze_probability'), 0.0),
        'vol_event': _num(fc.get('volatility_event_probability'), 0.0),
        'ssp_score': _num(ssp.get('score'), None) if ssp.get('score') is not None else None,
        'ssp_label': str(ssp.get('label') or 'neutral').replace('_', ' '),
        'pvi_score': _num(pvi.get('score'), None) if pvi.get('score') is not None else None,
        'pvi_bucket': pvi.get('bucket') or 'low',
        'op_score': _num(op.get('pressure_score_adjusted', op.get('score')), None) if op.get('score') is not None else None,
        'op_bias': op.get('bias') or 'neutral',
        'dte': oe.get('days_to_expiration'),
        'nearest_exp': oe.get('nearest_expiration'),
    }


def _render_png(symbol: str, row: dict | None) -> bytes:
    W, H = 1200, 630
    img = Image.new('RGB', (W, H), _BG_BOTTOM)
    _vgradient(img)
    drw = ImageDraw.Draw(img)

    # Accent bar left side
    drw.rectangle([0, 0, 8, H], fill=_ACCENT)

    # Brand top-left
    drw.text((36, 30), 'QUANTUM MARKET SCANNER', font=_font(20, True), fill=_ACCENT)

    if row is None:
        drw.text((36, 200), symbol, font=_font(120, True), fill=_TEXT_PRIMARY)
        drw.text((36, 340), 'No live snapshot for this symbol yet.', font=_font(28), fill=_TEXT_MUTED)
        drw.text((36, 380), 'Open the dashboard for live analysis.', font=_font(24), fill=_TEXT_DIM)
        buf = io.BytesIO()
        img.save(buf, format='PNG', optimize=True)
        return buf.getvalue()

    direction = row.get('final_direction') or 'Neutral'
    tier = row.get('tier') or '?'
    score = _num(row.get('final_score'), 0.0)
    dir_color = _direction_color(direction)

    # Symbol — huge ticker on the left
    drw.text((36, 78), f'${symbol}', font=_font(112, True), fill=_TEXT_PRIMARY)

    # Tier + Direction chips under the symbol
    y_chips = 220
    drw.text((36, y_chips), f'Tier {tier}', font=_font(30, True), fill=_TEXT_MUTED)
    tier_w = drw.textlength(f'Tier {tier}', font=_font(30, True))
    drw.text((36 + tier_w + 20, y_chips), '·', font=_font(30), fill=_TEXT_DIM)
    drw.text((36 + tier_w + 44, y_chips), direction, font=_font(30, True), fill=dir_color)

    # Score huge on the right
    score_txt = f'{score:.1f}'
    score_font = _font(160, True)
    score_w = drw.textlength(score_txt, font=score_font)
    drw.text((W - score_w - 60, 78), score_txt, font=score_font, fill=_TEXT_PRIMARY)
    label_w = drw.textlength('SCORE', font=_font(22, True))
    drw.text((W - label_w - 60, 250), 'SCORE', font=_font(22, True), fill=_TEXT_DIM)

    m = _extract_metrics(row)

    # Metric card grid — 3 x 2 layout
    card_top = 300
    card_h = 130
    card_w = (W - 36 - 36 - 40) // 3  # 3 cards, 20px gap
    gap = 20
    cards: list[tuple[str, str, tuple[int, int, int]]] = []
    p_up_str = f'{m["p_up"] * 100:.1f}%'
    if m['p_ctx'] is not None:
        p_up_str = f'{m["p_up"] * 100:.1f}% \u2192 {m["p_ctx"] * 100:.1f}%'
    cards.append(('1-DAY P(UP) CF \u2192 CTX', p_up_str, _TEXT_PRIMARY))
    kelly_str = f'{m["kelly"]:.4f}' if m['kelly'] is not None else '—'
    cards.append(('EFFECTIVE KELLY', kelly_str, dir_color))
    drift_str = f'{m["drift"]:+.3f}%'
    cards.append(('1-DAY DRIFT', drift_str, _TEXT_PRIMARY))

    # Bottom row: SSP / PVI / Expiration
    ssp_str = f'{m["ssp_score"]:.0f} · {m["ssp_label"]}' if m['ssp_score'] is not None else '—'
    cards.append(('SHORT-SELLING PRESSURE', ssp_str, _TEXT_MUTED))
    pvi_str = f'{m["pvi_score"]:.0f} · {m["pvi_bucket"]}' if m['pvi_score'] is not None else '—'
    cards.append(('PREDICTED VOLUME', pvi_str, _TEXT_MUTED))
    exp_str = f'{m["nearest_exp"]} ({m["dte"]}d)' if m['nearest_exp'] else '—'
    cards.append(('NEAREST EXPIRY', exp_str, _TEXT_MUTED))

    for i, (label, value, color) in enumerate(cards):
        col = i % 3
        row_idx = i // 3
        x = 36 + col * (card_w + gap)
        y = card_top + row_idx * (card_h + gap)
        drw.rounded_rectangle([x, y, x + card_w, y + card_h], radius=10,
                              fill=_CARD_BG, outline=_CARD_BORDER, width=1)
        drw.text((x + 18, y + 16), label, font=_font(15, True), fill=_TEXT_DIM)
        # Auto-scale value font size if too long
        val_font = _font(30, True)
        max_w = card_w - 36
        if drw.textlength(value, font=val_font) > max_w:
            val_font = _font(24, True)
        if drw.textlength(value, font=val_font) > max_w:
            val_font = _font(20, True)
        drw.text((x + 18, y + 48), value, font=val_font, fill=color)

    # Context flags bottom-left
    tags = []
    if m['squeeze'] >= 0.35:
        tags.append(f'\u25B2 SQUEEZE {m["squeeze"] * 100:.0f}%')
    if m['vol_event'] >= 0.45:
        tags.append(f'\u26A1 VOL-EVENT {m["vol_event"] * 100:.0f}%')
    if tags:
        y_tag = H - 44
        x_tag = 36
        for t in tags:
            w = drw.textlength(t, font=_font(18, True))
            drw.rounded_rectangle([x_tag, y_tag - 4, x_tag + w + 24, y_tag + 22],
                                  radius=12, fill=(30, 40, 50), outline=_ACCENT, width=1)
            drw.text((x_tag + 12, y_tag), t, font=_font(18, True), fill=_ACCENT)
            x_tag += w + 40

    # Footer right — "open the dashboard"
    footer = 'Open live dashboard \u2197'
    fw = drw.textlength(footer, font=_font(18, True))
    drw.text((W - fw - 40, H - 40), footer, font=_font(18, True), fill=_ACCENT)

    buf = io.BytesIO()
    img.save(buf, format='PNG', optimize=True)
    return buf.getvalue()


def get_share_og_image(symbol: str) -> bytes:
    """Return cached PNG bytes for a symbol's OG card, generating on miss."""
    symbol = (symbol or '').upper().strip() or 'UNKNOWN'
    now = time.time()
    with _cache_lock:
        cached = _cache.get(symbol)
        if cached and (now - cached[0]) < _OG_TTL_SECONDS:
            return cached[1]

    # Fetch snapshot row OUTSIDE the lock (network / large iteration).
    from app.services.snapshot_store import get_snapshot, lookup_snapshot_row
    row = None
    # Try direct O(1) lookup first — works for symbols that landed in
    # the raw scored bucket but haven't been ranked into the broadcast
    # snapshot yet (common for crypto rows that don't go through the
    # priority lane's ranker).
    for market in ('stocks', 'crypto'):
        try:
            r = lookup_snapshot_row(symbol, market)
            if r is not None:
                row = r
                break
        except Exception:  # noqa: BLE001
            continue
    if row is None:
        # Last resort — score the row on demand.  This is what
        # unblocks the "share/OG image for a crypto symbol" path when
        # the snapshot bucket lags the batch scoring loop.
        try:
            from app.services.detail_service import get_symbol_detail
            m = 'crypto' if symbol.endswith('-USD') else 'stocks'
            payload = get_symbol_detail(symbol, force_live=False, market=m)
            if payload and payload.get('final_score'):
                row = payload
        except Exception:  # noqa: BLE001
            pass

    try:
        png = _render_png(symbol, row)
    except Exception as exc:  # noqa: BLE001
        log.warning('OG image render failed for %s: %s', symbol, exc)
        # Fall back to a minimal error card so scrapers don't 500.
        img = Image.new('RGB', (1200, 630), _BG_BOTTOM)
        ImageDraw.Draw(img).text((36, 260), f'${symbol}', font=_font(80, True), fill=_TEXT_PRIMARY)
        buf = io.BytesIO()
        img.save(buf, format='PNG', optimize=True)
        png = buf.getvalue()

    with _cache_lock:
        _cache[symbol] = (now, png)
    return png
