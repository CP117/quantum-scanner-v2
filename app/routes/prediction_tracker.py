"""
Prediction Tracker routes — save / list / delete / accuracy-stats endpoints
for the user-saved 10-day forecast history page.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.services import prediction_tracker_service as tracker

log = logging.getLogger('app.routes.prediction_tracker')

router = APIRouter(prefix='/api/predictions', tags=['predictions'])


class SavePredictionRequest(BaseModel):
    """Payload accepted by POST /api/predictions/save.

    `full_payload` is the entire prediction blob from /api/predict/{symbol}
    so the tracker page can render the same factor contributions / narrative
    without re-running the model.  Optional — when omitted the saved row
    just won't have the audit context.
    """
    symbol: str = Field(..., min_length=1, max_length=20)
    market: str = Field(default='stocks')
    anchor_price: float = Field(..., gt=0)
    target_price: float = Field(..., gt=0)
    direction: Optional[str] = None  # derived from sign if omitted
    confidence_pct: Optional[float] = None
    forward_days: int = Field(default=10, ge=1, le=60)
    notes: Optional[str] = Field(default=None, max_length=1000)
    full_payload: Optional[dict[str, Any]] = None


@router.post('/save')
async def save_prediction(req: SavePredictionRequest):
    """Persist a generated prediction to the tracker DB."""
    try:
        # Run the sync DB write off the event loop.
        loop = asyncio.get_running_loop()
        row = await loop.run_in_executor(None, tracker.save_prediction, req.model_dump())
        return row
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get('/list')
async def list_saved_predictions(
    market: Optional[str] = Query(None, pattern='^(stocks|crypto)$'),
    status: Optional[str] = Query(None, pattern='^(open|evaluated|unresolved)$'),
    source: Optional[str] = Query(None, pattern='^(user|auto_scan)$'),
    limit: int = Query(500, ge=1, le=2000),
):
    """Return up to `limit` saved predictions, newest first.

    `source='auto_scan'` restricts to systematically-logged scanner
    predictions (unbiased sample); `source='user'` restricts to
    manually-saved picks (selection-biased -- see `accuracy_stats`
    docstring). Omit for both.
    """
    loop = asyncio.get_running_loop()
    rows = await loop.run_in_executor(None, tracker.list_predictions, market, status, source, limit)
    return {
        'rows': rows,
        'total': len(rows),
        'filters': {'market': market, 'status': status, 'source': source, 'limit': limit},
    }


@router.delete('/{prediction_id}')
async def delete_saved_prediction(prediction_id: str):
    """Remove a saved prediction by ID."""
    loop = asyncio.get_running_loop()
    ok = await loop.run_in_executor(None, tracker.delete_prediction, prediction_id)
    if not ok:
        raise HTTPException(status_code=404, detail='prediction_not_found')
    return {'ok': True, 'id': prediction_id}


@router.get('/accuracy')
async def saved_prediction_accuracy(
    market: Optional[str] = Query(None, pattern='^(stocks|crypto)$'),
    source: Optional[str] = Query(None, pattern='^(user|auto_scan)$'),
):
    """Aggregate accuracy stats across every saved (and evaluated) prediction.

    Different from `/api/predict/accuracy` which tracks the rolling
    walk-forward backtest of the live scanner output.  This endpoint
    reflects the tracker DB's saved forecasts.

    IMPORTANT: manually-saved ('user') picks are selection-biased -- a
    person tends to save forecasts that already look promising, so
    their hit rate is not a fair read on the scanner's real accuracy.
    Only 'auto_scan' rows (logged automatically by the warmer loop for
    every symbol it scores, with no human curation) give an honest
    number. When `source` is omitted, the response's top-level stats
    are blended across both and should be treated as informational
    only -- use `by_source.auto_scan` for the real accuracy read, or
    pass `?source=auto_scan` to get it directly as the top-level stats.
    """
    loop = asyncio.get_running_loop()
    stats = await loop.run_in_executor(None, tracker.accuracy_stats, market, source)
    return stats


@router.post('/evaluate-now')
async def evaluate_now():
    """Manual trigger for the auto-evaluation pass.  Useful when the
    daily-history cache was empty when the scheduled tick fired.
    Returns the same stats the scheduled pass would have logged.
    """
    loop = asyncio.get_running_loop()
    stats = await loop.run_in_executor(None, tracker.evaluate_expired_predictions)
    return stats
