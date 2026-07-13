
from __future__ import annotations

from typing import Any
from pydantic import BaseModel, ConfigDict, Field


class StockResultRow(BaseModel):
    model_config = ConfigDict(extra='allow')

    symbol: str = ''
    name: str = ''
    exchange: str = 'unknown'
    final_score: float = 0.0
    tier: str = 'unranked'
    final_direction: str = 'neutral'
    resolution_label: str = '1D'
    factor_breakdown: dict[str, Any] = Field(default_factory=dict)
    as_of_utc: str | None = None
    age_seconds: int = 0
    freshness_label: str = 'unknown'
    stale: bool = True
    data_source: str = 'unknown'
    preview_only: bool = False
    state: str = 'ok'


class StockResultsEnvelope(BaseModel):
    model_config = ConfigDict(extra='allow')

    batch: int
    current_batch: int = 0
    total_batches: int = 1
    limit: int
    total: int
    scan_progress: dict[str, Any] = Field(default_factory=dict)
    filters: dict[str, Any] = Field(default_factory=dict)
    results: list[StockResultRow] = Field(default_factory=list)
    state: str = 'ok'
