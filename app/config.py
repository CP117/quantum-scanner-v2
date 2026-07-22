
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
    provider_budget_per_minute: int = int(os.getenv("PROVIDERBUDGETPERMINUTE", "300"))
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

    # Watchlist SQLite DB path (relative to repo root if not absolute).
    watchlist_db_path: str = os.getenv("WATCHLIST_DB_PATH", "data/watchlists.db")

    # Resilience: error threshold before user sees reset prompt.
    tier_error_threshold: int = int(os.getenv("TIER_ERROR_THRESHOLD", "5"))
    tier_error_window_seconds: float = float(os.getenv("TIER_ERROR_WINDOW_SECONDS", "600.0"))
    tier_watchdog_stall_seconds: float = float(os.getenv("TIER_WATCHDOG_STALL_SECONDS", "300.0"))


settings = Settings()
