
from pydantic import BaseModel, Field
from typing import Any


class ProviderStatus(BaseModel):
    provider_name: str = "yfinance"
    session_ready: bool = False
    crumb_present: bool = False
    degraded: bool = False
    failure_count: int = 0
    last_error: str | None = None
    last_warm_utc: str | None = None
    throttle_state: str = "normal"


class CacheStatus(BaseModel):
    cache_file_present: bool = False
    cache_entries: int = 0
    last_cache_write_utc: str | None = None
    last_cache_symbol: str | None = None
    # Phase 26.30: surface in-memory cache + flusher telemetry so operators
    # can see at a glance which serializer is in use, how many shards are
    # currently waiting for the debounced flusher, and the on-disk
    # footprint of the cache.
    cache_dir_present: bool = False
    cache_shards_present: int = 0
    cache_shard_total_bytes: int = 0
    cache_dirty_shards: int = 0
    cache_serializer: str | None = None
    legacy_migrated: bool = False


class WarmerStatus(BaseModel):
    enabled: bool = False
    running: bool = False
    interval_seconds: int = 0
    last_cycle_utc: str | None = None
    warmed_symbols: int = 0
    last_batch: int = 0


class DefaultedFieldsSnapshot(BaseModel):
    """Rolling counter of contract fields the normalizer had to default-fill.

    Exposed so operators can detect contract drift without reading stack traces.
    """
    rows_normalized: int = 0
    counts: dict[str, int] = Field(default_factory=dict)
    top: list[dict[str, Any]] = Field(default_factory=list)


class LastKnownGoodSummary(BaseModel):
    batches_cached: int = 0
    last_served_utc: str | None = None
    last_fallback_reason: str | None = None
    serves_total: int = 0


class SystemStatusEnvelope(BaseModel):
    backend_ok: bool = True
    current_batch: int = 0
    batch_size: int = 25
    refresh_step_seconds: int = 4
    degraded_mode: bool = False
    offline_mode: bool = False
    provider: ProviderStatus = Field(default_factory=ProviderStatus)
    cache: CacheStatus = Field(default_factory=CacheStatus)
    warmer: WarmerStatus = Field(default_factory=WarmerStatus)
    last_refresh_utc: str | None = None
    last_success_utc: str | None = None
    last_failure_utc: str | None = None
    recent_fetch_error_summary: str | None = None
    current_filters: dict = Field(default_factory=dict)
    defaulted_fields: DefaultedFieldsSnapshot = Field(default_factory=DefaultedFieldsSnapshot)
    last_known_good: LastKnownGoodSummary = Field(default_factory=LastKnownGoodSummary)
    failure_classes: dict[str, int] = Field(default_factory=dict)
    provider_stats: dict[str, dict] = Field(default_factory=dict)
    yf_batch_executor: dict[str, Any] = Field(default_factory=dict)
    stooq_diagnostics: dict = Field(default_factory=dict)
    options_chain_stats: dict[str, int] = Field(default_factory=dict)
    daily_history_stats: dict[str, Any] = Field(default_factory=dict)
    reaction_clustering_stats: dict[str, int] = Field(default_factory=dict)
    active_scan_pool: dict[str, int] = Field(default_factory=dict)
    # Phase 26.33: GC tuning + sweep-boundary collect telemetry so
    # operators can see (a) that long-haul GC tuning is active, (b)
    # how often the sweep-boundary collect has fired, and (c) the
    # last collect duration.
    gc_stats: dict[str, Any] = Field(default_factory=dict)
    state: str = "ok"
