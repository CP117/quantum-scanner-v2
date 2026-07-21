import yfinance as yf
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from app.config import settings
from app.services.universe_service import get_symbol_identity
from app.services.provider_session import mark_provider_failure
from app.services.quote_cache import get_cached_quote, cached_quote_is_usable, quote_age_seconds, save_quote
from app.services.scoring_service import score_from_prices, download_quotes, fetch_live_snapshot, fetch_intraday_shape
from app.services.crypto_provider_service import fetch_coingecko_snapshot
from app.utils.normalize import normalize_result_row
from app.utils.time import utcnow_iso

# Module-level pool reused across calls; sized small because each manual refresh
# only needs 3-4 parallel slots and we don't want to compete with the scanner.
_DETAIL_FETCH_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix='detail-fetch')

# Detail fetch timeout: prevent hung provider calls from blocking forever
_DETAIL_FETCH_TIMEOUT_S = 20.0


def _build_fundamentals(seed: dict) -> dict:
    return {
        'sector': seed.get('sector') or 'unknown',
        'industry': seed.get('industry') or 'unknown',
        'market_cap': seed.get('marketCap') or 0,
        'trailing_pe': seed.get('trailingPE') or 0,
        'forward_pe': seed.get('forwardPE') or 0,
        'profit_margin': seed.get('profitMargins') or 0,
        'return_on_equity': seed.get('returnOnEquity') or 0,
        'debt_to_equity': seed.get('debtToEquity') or 0,
        'current_ratio': seed.get('currentRatio') or 0,
        'revenue_growth': seed.get('revenueGrowth') or 0,
        'dividend_yield': seed.get('dividendYield') or 0,
    }


def _fundamentals_seed(identity: dict, info: dict) -> dict:
    return {
        'shortName': info.get('shortName') or identity.get('name') or '',
        'exchange': info.get('exchange') or identity.get('exchange') or 'unknown',
        'sector': info.get('sector') or 'unknown',
        'industry': info.get('industry') or 'unknown',
        'marketCap': info.get('marketCap') or 0,
        'trailingPE': info.get('trailingPE') or 0,
        'forwardPE': info.get('forwardPE') or 0,
        'profitMargins': info.get('profitMargins') or 0,
        'returnOnEquity': info.get('returnOnEquity') or 0,
        'debtToEquity': info.get('debtToEquity') or 0,
        'currentRatio': info.get('currentRatio') or 0,
        'revenueGrowth': info.get('revenueGrowth') or 0,
        'dividendYield': info.get('dividendYield') or 0,
        'previousClose': info.get('previousClose') or 0,
        'open': info.get('open') or 0,
        'dayLow': info.get('dayLow') or 0,
        'dayHigh': info.get('dayHigh') or 0,
        'currentPrice': info.get('currentPrice') or info.get('regularMarketPrice') or 0,
        'volume': info.get('volume') or info.get('regularMarketVolume') or 0,
        'averageVolume': info.get('averageVolume') or info.get('averageVolume10days') or 0,
        'bid': info.get('bid') or 0,
        'ask': info.get('ask') or 0,
    }


def _fetch_info(base_symbol: str) -> dict:
    if not settings.use_live_provider:
        return {}
    try:
        ticker = yf.Ticker(base_symbol)
        return ticker.info or {}
    except Exception as exc:
        mark_provider_failure(str(exc))
        return {}


def _refresh_snapshot_quote(snap_row: dict, base_symbol: str, market: str) -> dict:
    """Fresh-quote refresh WITHOUT losing the batch-attached forward /
    Lab / Strategy / Predictive metric blocks.  Used on the require_fresh
    path: we pull a new quote via the batched provider cache (cheap),
    update `last_price` / `previous_close` / `change_pct` / age + tag
    the row so the frontend sees the tick, but keep every downstream
    overlay the batch scorer already computed.  This is what fixes the
    2026-07-02 "detail panel Future Forecast + Lab Mode + Quantum
    sections vanished" regression: the previous require_fresh path
    replaced the row with a fresh score_from_prices() output that had
    none of those overlays.
    """
    try:
        from app.services.provider_service import download_quotes
        from app.services.quote_cache import save_quote, get_cached_quote, cached_quote_is_usable, quote_age_seconds
        from app.services.stateful_kit import utcnowiso
    except Exception:  # noqa: BLE001
        return snap_row
    quote = None
    try:
        quote = (download_quotes([base_symbol], market) or {}).get(base_symbol)
    except Exception:  # noqa: BLE001
        quote = None
    if quote:
        try:
            save_quote(base_symbol, quote)
        except Exception:  # noqa: BLE001
            pass
        try:
            lp = float(quote.get('last_price') or 0.0)
            pc = float(quote.get('previous_close') or 0.0)
        except (TypeError, ValueError):
            lp, pc = 0.0, 0.0
        market_fb = (snap_row.get('factor_breakdown') or {}).get('market') or {}
        if lp > 0 and pc > 0:
            market_fb['last_price'] = lp
            market_fb['previous_close'] = pc
            market_fb['change_pct'] = ((lp - pc) / pc) * 100.0 if pc > 0 else 0.0
            market_fb['age_seconds'] = 0
            market_fb['source'] = quote.get('provider') or 'yfinance'
            snap_row['factor_breakdown']['market'] = market_fb
            snap_row['as_of_utc'] = quote.get('captured_at_utc') or utcnowiso()
            snap_row['age_seconds'] = 0
            snap_row['freshness_label'] = 'fresh'
            snap_row['stale'] = False
    snap_row['score_revision_utc'] = utcnow_iso()
    snap_row['detail_source'] = 'snapshot_refreshed'
    snap_row['fresh_rescore'] = bool(quote)
    return snap_row


def _snapshot_is_incomplete(snap_row: dict) -> tuple[bool, str]:
    """Detect a snapshot row that shouldn't be trusted as the detail
    response.  Root cause of the 2026-07-02 "elevated stocks show
    incomplete metrics" bug — symbols pulled up by a filter chip from
    lower-ranked positions were mirrored from a batch cheap-pass row
    that never got a full factor breakdown.
    """
    if not snap_row:
        return True, 'missing'
    state = str(snap_row.get('state') or '').lower()
    if state in ('degraded', 'stale', 'unavailable', 'preview'):
        return True, f'state={state}'
    if snap_row.get('stale'):
        return True, 'stale_flag'
    src = str(snap_row.get('data_source') or '').lower()
    if src in ('unavailable', 'unknown', 'preview'):
        return True, f'source={src}'
    fb = snap_row.get('factor_breakdown') or {}
    market_fb = fb.get('market') or {}
    # Missing composite pieces = cheap-pass row.  Any of these being
    # absent means the batch never gave this symbol a full score.
    key_metric_fields = [
        'trend_volume_delta', 'institutional_confluence',
        'options_positioning', 'volume_sentiment',
    ]
    missing = [k for k in key_metric_fields if not market_fb.get(k)]
    if len(missing) >= 2:
        return True, f'missing_factors={",".join(missing[:3])}'
    # Age-based staleness: if the batch scored this row > 90s ago and no
    # live tick has refreshed it, prefer a rescore.
    try:
        age = float(snap_row.get('age_seconds') or 0)
        if age > 90:
            return True, f'age_seconds={age:.0f}'
    except (TypeError, ValueError):
        pass
    return False, ''


def get_symbol_detail(symbol: str, force_live: bool = False,
                      market: str = 'stocks',
                      require_fresh: bool = False) -> dict:
    """Symbol detail lookup with tiered freshness.

    * `require_fresh=False` (legacy default):  snapshot-mirror; falls
      back to a fetch+rescore if the row is missing OR looks incomplete
      (via `_snapshot_is_incomplete`).
    * `require_fresh=True` (new, sent by the frontend detail panel):
      always re-fetch the live quote (cheap via the batched provider
      cache) and rescore against the cached daily-hist + intraday
      shape.  This is what makes the 2-second live tick actually pull
      fresh data for the detail panel — even for filter-elevated
      symbols that the batch scanner hasn't touched recently.
    * `force_live=True` (manual Refresh Now button): full intraday +
      daily re-download.  Reserved for breaking a lockup / unblocking
      a rate-limited provider — NOT the primary refresh path.
    """
    identity = get_symbol_identity(symbol, market)
    base_symbol = identity.get('symbol', (symbol or '').upper())

    if not force_live:
        from app.services.snapshot_store import lookup_snapshot_row
        snap_row = lookup_snapshot_row(base_symbol, market)
        if snap_row is not None:
            incomplete, reason = _snapshot_is_incomplete(snap_row)
            if not incomplete:
                # Snapshot row is complete — return the mirror.  If the
                # caller asked for require_fresh we do a cheap quote-only
                # refresh IN PLACE that preserves every batch-attached
                # overlay (forward_metrics, forward_metrics_garch, lab,
                # strategy, predictive_expansion, forecast_context) but
                # updates the price/change/age from the fresh live quote.
                # This is the fix for the 2026-07-02 "Future Forecast +
                # Lab Mode + Quantum sections disappeared on refresh"
                # regression.
                fb = snap_row.get('factor_breakdown') or {}
                if not (fb.get('fundamentals') or {}).get('sector'):
                    fb['fundamentals'] = _build_fundamentals(_fundamentals_seed(identity, {}))
                    snap_row['factor_breakdown'] = fb
                if require_fresh:
                    snap_row = _refresh_snapshot_quote(snap_row, base_symbol, market)
                else:
                    snap_row['detail_source'] = 'snapshot_mirror'
                    snap_row['fresh_rescore'] = False
                return normalize_result_row(snap_row)
            # else: fall through to the rescore path below so filter-
            # elevated symbols don't get stuck at partial metrics.

    # -----------------------------------------------------------------
    # Original re-fetch + re-score path.  Hit when force_live=True
    # (user clicked Refresh) or when the symbol simply isn't in the
    # snapshot yet.  Parallelizes the 4 independent slow HTTP fetches
    # so manual-refresh latency goes from sequential 4*HTTP_RTT
    # (~30-60s) to max(HTTP_RTT) (~5-15s).
    # -----------------------------------------------------------------
    def _safe_intraday():
        try:
            return fetch_intraday_shape(base_symbol, market) or {}
        except Exception:
            return {}

    def _safe_daily():
        # Phase 22 fix: previously this returned None for crypto, which
        # left reaction_clustering / volume_sentiment / IOB stuck in the
        # "insufficient_history" warming state on every crypto manual
        # refresh.  yfinance returns 90d daily OHLCV for major crypto
        # tickers, and the daily_history_service has a CryptoCompare
        # fallback for the tail.  Run the same fetch for both markets.
        try:
            from app.services.daily_history_service import get_daily_history
            return get_daily_history(base_symbol, allow_fetch=True)
        except Exception:
            return None

    def _safe_live():
        if not settings.use_live_provider:
            return None
        try:
            if force_live:
                return fetch_live_snapshot(base_symbol, market)
            return download_quotes([base_symbol], market).get(base_symbol)
        except Exception as exc:
            mark_provider_failure(str(exc))
            return None

    def _safe_crypto_extra():
        if market != 'crypto':
            return {}
        try:
            return fetch_coingecko_snapshot(base_symbol) or {}
        except Exception:
            return {}

    # NOTE: we skip yf.Ticker.info on the manual-refresh path. It's the slowest
    # call (10-30s) and the data it provides (sector, industry, marketCap, PE
    # ratios) is also returned by the periodic scanner via the universe cache,
    # so the detail panel will inherit it from the row that's already in the
    # in-memory pool. The intraday shape + live snapshot cover everything we
    # need for fresh pricing & scoring.
    fut_shape = _DETAIL_FETCH_POOL.submit(_safe_intraday)
    fut_daily = _DETAIL_FETCH_POOL.submit(_safe_daily)
    fut_live = _DETAIL_FETCH_POOL.submit(_safe_live)
    fut_cg = _DETAIL_FETCH_POOL.submit(_safe_crypto_extra)
    
    # CRITICAL FIX: Add timeout to prevent forever-waits when providers hang.
    # Without this, a single hung provider blocks the entire request thread,
    # causing the UI to spin forever and the backend thread pool to saturate.
    try:
        shape = fut_shape.result(timeout=_DETAIL_FETCH_TIMEOUT_S)
    except (FuturesTimeoutError, Exception):
        shape = {}
    
    try:
        daily_hist = fut_daily.result(timeout=_DETAIL_FETCH_TIMEOUT_S)
    except (FuturesTimeoutError, Exception):
        daily_hist = None
    
    try:
        live_quote = fut_live.result(timeout=_DETAIL_FETCH_TIMEOUT_S)
    except (FuturesTimeoutError, Exception):
        live_quote = None
    
    try:
        cg = fut_cg.result(timeout=_DETAIL_FETCH_TIMEOUT_S)
    except (FuturesTimeoutError, Exception):
        cg = {}

    info = {}
    if shape:
        info = {**info, **{k: v for k, v in shape.items() if v not in (None, '', 0)}}
    if cg:
        info = {**info, **cg}
    fundamentals_seed = _fundamentals_seed(identity, info)
    row = None

    if live_quote:
        save_quote(base_symbol, live_quote)
        row = score_from_prices(identity, float(live_quote.get('last_price') or 0.0), float(live_quote.get('previous_close') or 0.0), 'yfinance-detail', 0, live_quote.get('captured_at_utc'), fundamentals_seed)
        row['score_revision_utc'] = utcnow_iso()
        row['fresh_rescore'] = True
        row['provider_outcome'] = live_quote.get('provider_outcome', 'live_success')
        row['preview_only'] = bool(live_quote.get('preview_only', False))
    elif not force_live:
        cached = get_cached_quote(base_symbol)
        if cached and cached_quote_is_usable(cached):
            age = quote_age_seconds(cached)
            row = score_from_prices(identity, float(cached.get('last_price') or 0.0), float(cached.get('previous_close') or 0.0), 'cache-detail', age, cached.get('captured_at_utc'), fundamentals_seed)
            row['score_revision_utc'] = utcnow_iso()
            row['fresh_rescore'] = False
            row['provider_outcome'] = 'cache_fallback'

    if row is None:
        cached = get_cached_quote(base_symbol)
        if cached and cached_quote_is_usable(cached):
            age = quote_age_seconds(cached)
            row = score_from_prices(identity, float(cached.get('last_price') or 0.0), float(cached.get('previous_close') or 0.0), 'cache-detail', age, cached.get('captured_at_utc'), fundamentals_seed)
            row['score_revision_utc'] = utcnow_iso()
            row['fresh_rescore'] = False
            row['provider_outcome'] = 'cache_after_live_failed' if force_live else 'cache_fallback'
            row['preview_only'] = False
            row['state'] = 'degraded' if force_live else row.get('state', 'ready')
        else:
            row = {
                'symbol': base_symbol,
                'name': fundamentals_seed.get('shortName') or identity.get('name') or base_symbol,
                'exchange': fundamentals_seed.get('exchange') or identity.get('exchange') or 'unknown',
                'final_score': 0.0,
                'tier': 'D',
                'final_direction': 'Neutral',
                'resolution_label': '1D',
                'factor_breakdown': {'fundamentals': _build_fundamentals(fundamentals_seed), 'market': {'last_price': 0, 'previous_close': 0, 'change_pct': 0, 'age_seconds': 0, 'source': 'unavailable'}},
                'as_of_utc': '',
                'age_seconds': 0,
                'freshness_label': 'stale',
                'stale': True,
                'data_source': 'unavailable',
                'preview_only': market == 'crypto',
                'state': 'degraded',
                'score_revision_utc': utcnow_iso(),
                'fresh_rescore': False,
                'provider_outcome': 'live_failed' if force_live else 'unavailable',
            }

    row['name'] = fundamentals_seed.get('shortName') or row.get('name')
    row['exchange'] = fundamentals_seed.get('exchange') or row.get('exchange')
    fb = row.get('factor_breakdown') or {}
    fb['fundamentals'] = _build_fundamentals(fundamentals_seed)
    row['factor_breakdown'] = fb
    ratings = (fb.get('ratings') or {})
    row['algorithm_ratings'] = {
        'momentum': ratings.get('momentum', {'score': 0, 'rating': 'Unknown'}),
        'quality': ratings.get('quality', {'score': 0, 'rating': 'Unknown'}),
        'trend': ratings.get('trend', {'score': 0, 'rating': 'Unknown'}),
        'stability': ratings.get('stability', {'score': 0, 'rating': 'Unknown'}),
    }
    if market == 'crypto' and row.get('provider_outcome') == 'live_success':
        market_fb = ((row.get('factor_breakdown') or {}).get('market') or {})
        if not market_fb.get('previous_close') or not market_fb.get('last_price'):
            row['provider_outcome'] = 'preview_fallback'
            row['preview_only'] = True

    # Phase 26.71 hotfix — attach the full forward/Lab/Strategy/Predictive
    # metric bundle so the detail panel's Future Forecast, Lab Mode,
    # Blended Ranking and Quantum sections aren't blank.  Without this,
    # a rescored row (triggered by require_fresh=True) is missing every
    # block downstream of `score_from_prices()` — those overlays are
    # normally added by the batch scoring loop and by the priority-
    # lane GARCH overlay pass.
    try:
        from app.services.future_mode_service import (
            attach_forward_metrics_fast, attach_forward_metrics_garch,
        )
        reg_sig = ((row.get('factor_breakdown') or {}).get('market') or {}).get('regulatory_signal')
        attach_forward_metrics_fast(row, regulatory_signal=reg_sig, market=market)
        # GARCH overlay is best-effort — silently skip if daily-history
        # is too short (the detail card already has forward_metrics from
        # the fast tier as its fallback).
        try:
            attach_forward_metrics_garch(row, base_symbol, market=market)
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        # Never fail the detail response over an overlay attach error.
        pass

    # Same for the scanner-context flat fields (SSP / PVI / expiration).
    # `score_from_prices` populates the nested `factor_breakdown.market`
    # families but leaves the flat row-root aliases (which the frontend
    # + presets read) None.  Populate them here so the detail card and
    # any subsequent filter re-application see the same values as the
    # batch snapshot mirror path.
    try:
        from app.services.scoring_service import _context_flat_fields  # noqa: PLC0415
        market_fb = (row.get('factor_breakdown') or {}).get('market') or {}
        ssp_dict = market_fb.get('short_selling_pressure') or {}
        pvi_dict = market_fb.get('predicted_volume_intensity') or {}
        exp_dict = market_fb.get('options_expiration') or {}
        row.update(_context_flat_fields(ssp_dict, pvi_dict, exp_dict))
    except Exception:  # noqa: BLE001
        pass

    # Preserve the forecast_context overlay (p_up_ctx, squeeze_probability,
    # etc.) that `apply_forecast_context` normally paints during batch.
    try:
        from app.services.forecast_context import apply_forecast_context
        apply_forecast_context(row, row)
    except Exception:  # noqa: BLE001
        pass

    return normalize_result_row(row)
