"""Options-expiration awareness for scanner analytics + forecasting.

Derives the nearest options expiration for each eligible symbol from the
real chain payload when available (CBOE / Yahoo summaries now carry
`expiration_dates`), otherwise estimates the nearest standard weekly /
monthly expiration so the scanner degrades gracefully instead of
nulling the whole context. Provenance is stamped in `source`
('chain' | 'estimated' | 'unavailable').
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

log = logging.getLogger('app.expiration')

HIGH_SENSITIVITY_DAYS = 5

UNAVAILABLE_EXPIRATION_CONTEXT: dict[str, Any] = {
    'nearest_expiration': None,
    'days_to_expiration': None,
    'expiration_type': None,
    'high_sensitivity_window': False,
    'risk_flag': False,
    'source': 'unavailable',
    'status': 'unavailable',
}


def _third_friday(year: int, month: int) -> date:
    d = date(year, month, 15)
    while d.weekday() != 4:
        d += timedelta(days=1)
    return d


def _next_friday(today: date) -> date:
    days_ahead = (4 - today.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return today + timedelta(days=days_ahead)


def classify_expiration_type(exp: date) -> str:
    tf = _third_friday(exp.year, exp.month)
    if exp == tf:
        return 'monthly'
    if exp.weekday() == 4:
        return 'weekly'
    return 'nonstandard'


def compute_expiration_context(
    symbol: str,
    options_payload: dict | None = None,
    *,
    pin_risk: str | None = None,
    market: str = 'stocks',
    today: date | None = None,
) -> dict[str, Any]:
    """Return the options_expiration context payload. Never raises."""
    today = today or datetime.now(timezone.utc).date()
    op = options_payload or {}
    source = 'unavailable'
    nearest: date | None = None

    try:
        dates = op.get('expiration_dates') or []
        parsed: list[date] = []
        for d in dates:
            try:
                parsed.append(datetime.fromisoformat(str(d)).date())
            except ValueError:
                continue
        parsed = sorted(x for x in parsed if x >= today)
        if parsed:
            nearest = parsed[0]
            source = 'chain'
    except Exception:  # noqa: BLE001
        pass

    if nearest is None:
        if market == 'crypto':
            return dict(UNAVAILABLE_EXPIRATION_CONTEXT)
        # Estimate: symbols with a summarized chain but no explicit dates
        # get the standard weekly Friday; symbols with no chain data at
        # all stay unavailable (no liquid options assumption).
        if op.get('expirations_used') or op.get('provenance') in ('real_chain', 'cboe_chain'):
            nearest = _next_friday(today)
            source = 'estimated'
        else:
            return dict(UNAVAILABLE_EXPIRATION_CONTEXT)

    days = (nearest - today).days
    exp_type = classify_expiration_type(nearest)
    high_window = days <= HIGH_SENSITIVITY_DAYS
    pin = str(pin_risk or op.get('pin_risk') or 'low')
    risk_flag = bool(high_window and (source == 'chain' or pin in ('high', 'moderate')))

    return {
        'nearest_expiration': nearest.isoformat(),
        'days_to_expiration': int(days),
        'expiration_type': exp_type,
        'high_sensitivity_window': bool(high_window),
        'risk_flag': risk_flag,
        'source': source,
        'status': 'implemented',
    }
