
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


settings = Settings()
