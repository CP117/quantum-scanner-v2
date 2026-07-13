"""
yfinance provider adapter — wraps the existing scoring_service yfinance code
path as a chain-compatible fetcher.  Calling it from the multi-provider chain
keeps the existing behavior (warm session, budget enforcement, fast_info + 1m
history fallback) while making it composable with the other providers.
"""
from __future__ import annotations

import logging
from typing import Iterable

from app.config import settings

log = logging.getLogger('app.providers.yfinance')


def fetch(symbols: list[str], market: str) -> dict[str, dict]:
    if not symbols:
        return {}
    # Lazy import to avoid circular dependency at module load.
    from app.services.scoring_service import download_quotes_yfinance_only
    return download_quotes_yfinance_only(symbols, market)
