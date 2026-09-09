
import os
from pydantic import BaseModel


def _env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


class Settings(BaseModel):
    app_name: str = "Market Refinement Dashboard"
    host: str = os.getenv("APPHOST", "127.0.0.1")
    port: int = int(os.getenv("APPPORT", "8010"))
    batch_size: int = int(os.getenv("BATCHSIZE", "25"))
    refresh_step_seconds: int = int(os.getenv("REFRESHSTEPSECONDS", "4"))
    provider_timeout_seconds: int = int(os.getenv("PROVIDERTIMEOUTSECONDS", "20"))
    max_search_results: int = int(os.getenv("MAXSEARCHRESULTS", "20"))
    coingecko_base_url: str = os.getenv("COINGECKOBASEURL", "https://api.coingecko.com/api/v3")
    coingecko_catalog_ttl_seconds: int = int(os.getenv("COINGECKOCATALOGTTLSECONDS", "21600"))
    coingecko_catalog_pages: int = int(os.getenv("COINGECKOCATALOGPAGES", "32"))
    coingecko_catalog_page_size: int = int(os.getenv("COINGECKOCATALOGPAGESIZE", "250"))
    provider_soft_fail_threshold: int = int(os.getenv("PROVIDERSOFTFAILTHRESHOLD", "3"))
    yfinance_chunk_size: int = int(os.getenv("YFINANCECHUNKSIZE", "10"))
    use_live_provider: bool = _env_bool("USELIVEPROVIDER", True)
    cache_ttl_seconds: int = int(os.getenv("CACHETTLSECONDS", "900"))
    cache_max_age_seconds: int = int(os.getenv("CACHEMAXAGESECONDS", "86400"))
    warmer_enabled: bool = _env_bool("WARMERENABLED", True)
    warmer_interval_seconds: int = int(os.getenv("WARMERINTERVALSECONDS", "20"))
    provider_min_request_gap_ms: int = int(os.getenv("PROVIDERMINREQUESTGAPMS", "120"))
    active_scan_limit: int = int(os.getenv("ACTIVESCANLIMIT", "100"))
    # Phase 27: automatic, unbiased prediction logging. The warmer loop
    # already cycles through the whole universe on a timer -- this piggybacks
    # on that so `accuracy_stats(source='auto_scan')` reflects the scanner's
    # real, systematic performance rather than only whichever picks a human
    # happened to click "Save" on (see prediction_tracker_service.py).
    auto_log_predictions_enabled: bool = _env_bool("AUTOLOGPREDICTIONSENABLED", True)
    auto_log_predictions_max_new_per_cycle: int = int(os.getenv("AUTOLOGPREDICTIONSMAXNEW", "10"))
    auto_log_predictions_forward_days: int = int(os.getenv("AUTOLOGPREDICTIONSFORWARDDAYS", "10"))

    # ---------------------------------------------------------------------------
    # Tiered Universe Architecture (Phase 28)
    # ---------------------------------------------------------------------------
    # Tier 1: Active tier — top symbols, full scoring, tight rescore loop.
    tier_1_size: int = int(os.getenv("TIER1_SIZE", "100"))
    tier_1_interval_seconds: float = float(os.getenv("TIER1_INTERVAL_SECONDS", "3.0"))

    # Tier 2: Monitor tier — next N symbols, lightweight scoring, medium cadence.
    tier_2_size: int = int(os.getenv("TIER2_SIZE", "1000"))
    tier_2_interval_seconds: float = float(os.getenv("TIER2_INTERVAL_SECONDS", "45.0"))

    # Tier 3: Background tier — remaining symbols, minimal scoring, hourly pass.
    # Without GPU falls back to once-daily scan.
    tier_3_interval_seconds: float = float(os.getenv("TIER3_INTERVAL_SECONDS", "3600.0"))
    tier_3_interval_no_gpu_seconds: float = float(os.getenv("TIER3_INTERVAL_NO_GPU_SECONDS", "86400.0"))

    # Promotion cooldown: a symbol can't be demoted back within this window.
    tier_promotion_cooldown_seconds: float = float(os.getenv("TIER_PROMOTION_COOLDOWN_SECONDS", "3600.0"))

    # Volume spike factor that triggers T3→T2 promotion.
    tier_volume_spike_factor: float = float(os.getenv("TIER_VOLUME_SPIKE_FACTOR", "5.0"))

    # Overnight price-gap percentage (absolute) that triggers T3→T2 promotion.
    tier_price_gap_pct: float = float(os.getenv("TIER_PRICE_GAP_PCT", "3.0"))

    # Inactivity window (seconds) after which a quiet symbol may be demoted.
    tier_inactivity_demotion_seconds: float = float(os.getenv("TIER_INACTIVITY_DEMOTION_SECONDS", "86400.0"))

    # Tier 3 disk-cache directory (relative to repo root if not absolute).
    tier_3_cache_dir: str = os.getenv("TIER3_CACHE_DIR", "data/tier3_cache")

    # GPU flags.
    gpu_enabled: bool = _env_bool("GPU_ENABLED", True)  # detect at startup; auto-set to False on no GPU

    # Provider quota — raised from 300 to 600 req/min.
    # Tier 1 gets 60 %, Tier 2 20 %, Tier 3 0 % (EOD only).
    provider_budget_per_minute: int = int(os.getenv("PROVIDERBUDGETPERMINUTE", "600"))
    provider_tier3_min_requests_per_minute: int = int(os.getenv("PROVIDER_TIER3_MIN_REQUESTS_PER_MINUTE", "12"))
    provider_circuit_breaker_seconds: float = float(os.getenv("PROVIDER_CIRCUIT_BREAKER_SECONDS", "30"))

    # Watchlist SQLite DB path (relative to repo root if not absolute).
    watchlist_db_path: str = os.getenv("WATCHLIST_DB_PATH", "data/watchlists.db")

    # Resilience: error threshold before user sees reset prompt.
    tier_error_threshold: int = int(os.getenv("TIER_ERROR_THRESHOLD", "5"))
    tier_error_window_seconds: float = float(os.getenv("TIER_ERROR_WINDOW_SECONDS", "600.0"))
    tier_watchdog_stall_seconds: float = float(os.getenv("TIER_WATCHDOG_STALL_SECONDS", "300.0"))

    # ---------------------------------------------------------------------------
    # Phase A — Externalized Tier State (Redis)
    # ---------------------------------------------------------------------------
    # Set USE_REDIS_STATE=1 to route tier/score reads-writes through Redis.
    # Default is 0 (in-memory only) to preserve single-process behaviour.
    use_redis_state: bool = _env_bool("USE_REDIS_STATE", False)
    redis_host: str = os.getenv("REDIS_HOST", "localhost")
    redis_port: int = int(os.getenv("REDIS_PORT", "6379"))
    redis_db: int = int(os.getenv("REDIS_DB", "0"))
    redis_password: str = os.getenv("REDIS_PASSWORD", "")
    # Namespace prefix for all tier-manager Redis keys.
    redis_tier_prefix: str = os.getenv("REDIS_TIER_PREFIX", "qs:tier")

    # ---------------------------------------------------------------------------
    # Phase B — Externalized Snapshot Store (Redis)
    # ---------------------------------------------------------------------------
    # Set USE_REDIS_SNAPSHOT=1 to route snapshot reads/writes through Redis.
    # Default is 0 (in-memory only) to preserve single-process behaviour.
    use_redis_snapshot: bool = _env_bool("USE_REDIS_SNAPSHOT", False)
    # Namespace prefix for all snapshot Redis keys.
    redis_snapshot_prefix: str = os.getenv("REDIS_SNAPSHOT_PREFIX", "qs:snap")

    # RAM-first cache is intentionally process-local.  In multi-worker
    # deployments this budget applies to each worker, not the host total.
    memory_cache_enabled: bool = _env_bool("MEMORY_CACHE_ENABLED", True)
    memory_cache_mode: str = os.getenv("MEMORY_CACHE_MODE", "enabled").strip().lower()
    memory_cache_namespace: str = os.getenv("MEMORY_CACHE_NAMESPACE", "v1")
    memory_cache_max_mb: int = int(os.getenv("MEMORY_CACHE_MAX_MB", "256"))
    memory_cache_max_entries_per_domain: int = int(os.getenv("MEMORY_CACHE_MAX_ENTRIES_PER_DOMAIN", "5000"))
    memory_cache_prune_interval_seconds: int = int(os.getenv("MEMORY_CACHE_PRUNE_INTERVAL_SECONDS", "60"))
    memory_cache_emergency_percent: float = float(os.getenv("MEMORY_CACHE_EMERGENCY_PERCENT", "0.90"))
    memory_cache_emergency_recovery_percent: float = float(os.getenv("MEMORY_CACHE_EMERGENCY_RECOVERY_PERCENT", "0.75"))
    cache_ttl_jitter_percent: float = float(os.getenv("CACHE_TTL_JITTER_PERCENT", "0.10"))
    memory_cache_singleflight_timeout_seconds: float = float(os.getenv("MEMORY_CACHE_SINGLEFLIGHT_TIMEOUT_SECONDS", "2.0"))
    memory_cache_warmup_enabled: bool = _env_bool("MEMORY_CACHE_WARMUP_ENABLED", True)
    memory_cache_warmup_jitter_seconds: int = int(os.getenv("MEMORY_CACHE_WARMUP_JITTER_SECONDS", "30"))
    cache_correctness_sample_rate: float = float(os.getenv("CACHE_CORRECTNESS_SAMPLE_RATE", "0.01"))

    # Domain flags and rollout modes permit incremental, immediate rollback.
    cache_enable_quotes: bool = _env_bool("CACHE_ENABLE_QUOTES", True)
    cache_enable_daily_history: bool = _env_bool("CACHE_ENABLE_DAILY_HISTORY", True)
    cache_enable_score_components: bool = _env_bool("CACHE_ENABLE_SCORE_COMPONENTS", False)
    cache_enable_composite_scores: bool = _env_bool("CACHE_ENABLE_COMPOSITE_SCORES", False)
    cache_enable_options_chains: bool = _env_bool("CACHE_ENABLE_OPTIONS_CHAINS", True)
    cache_enable_universe_metadata: bool = _env_bool("CACHE_ENABLE_UNIVERSE_METADATA", True)
    cache_enable_narratives: bool = _env_bool("CACHE_ENABLE_NARRATIVES", False)
    cache_enable_bayesian_priors: bool = _env_bool("CACHE_ENABLE_BAYESIAN_PRIORS", False)
    cache_mode_quotes: str = os.getenv("CACHE_MODE_QUOTES", os.getenv("MEMORY_CACHE_MODE", "enabled")).strip().lower()
    cache_mode_daily_history: str = os.getenv("CACHE_MODE_DAILY_HISTORY", os.getenv("MEMORY_CACHE_MODE", "enabled")).strip().lower()
    cache_mode_score_components: str = os.getenv("CACHE_MODE_SCORE_COMPONENTS", os.getenv("MEMORY_CACHE_MODE", "enabled")).strip().lower()
    cache_mode_composite_scores: str = os.getenv("CACHE_MODE_COMPOSITE_SCORES", os.getenv("MEMORY_CACHE_MODE", "enabled")).strip().lower()
    cache_mode_options_chains: str = os.getenv("CACHE_MODE_OPTIONS_CHAINS", os.getenv("MEMORY_CACHE_MODE", "enabled")).strip().lower()
    cache_mode_universe_metadata: str = os.getenv("CACHE_MODE_UNIVERSE_METADATA", os.getenv("MEMORY_CACHE_MODE", "enabled")).strip().lower()
    cache_mode_narratives: str = os.getenv("CACHE_MODE_NARRATIVES", os.getenv("MEMORY_CACHE_MODE", "enabled")).strip().lower()
    cache_mode_bayesian_priors: str = os.getenv("CACHE_MODE_BAYESIAN_PRIORS", os.getenv("MEMORY_CACHE_MODE", "enabled")).strip().lower()

    quote_cache_ttl_seconds: int = int(os.getenv("QUOTE_CACHE_TTL_SECONDS", "15"))
    tier_1_quote_max_age_seconds: int = int(os.getenv("TIER1_QUOTE_MAX_AGE_SECONDS", "15"))
    options_chain_cache_ttl_seconds: int = int(os.getenv("OPTIONS_CHAIN_CACHE_TTL_SECONDS", "120"))
    options_chain_tier1_refresh_seconds: int = int(os.getenv("OPTIONS_CHAIN_TIER1_REFRESH_SECONDS", "30"))
    universe_metadata_ttl_seconds: int = int(os.getenv("UNIVERSE_METADATA_TTL_SECONDS", "3600"))
    scanner_preset_cache_max_entries: int = int(os.getenv("SCANNER_PRESET_CACHE_MAX_ENTRIES", "128"))
    daily_history_cache_ttl_seconds: int = int(os.getenv("DAILY_HISTORY_CACHE_TTL_SECONDS", "3600"))
    score_component_cache_ttl_seconds: int = int(os.getenv("SCORE_COMPONENT_CACHE_TTL_SECONDS", "60"))
    composite_score_cache_ttl_seconds: int = int(os.getenv("COMPOSITE_SCORE_CACHE_TTL_SECONDS", "30"))
    narrative_cache_ttl_seconds: int = int(os.getenv("NARRATIVE_CACHE_TTL_SECONDS", "120"))
    bayesian_prior_ttl_seconds: int = int(os.getenv("BAYESIAN_PRIOR_TTL_SECONDS", "86400"))


settings = Settings()
