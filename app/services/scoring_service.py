from __future__ import annotations
import logging
import math
import os as _os
import threading as _threading
from concurrent.futures import (
    ThreadPoolExecutor as _ScoringTpe,
    TimeoutError as _ScoringFTimeout,
)
from typing import Dict, List
import yfinance as yf
from app.config import settings
from app.services.provider_session import mark_provider_failure, clear_provider_failure, provider_budget_allowance, warm_provider_session
from app.services.quote_cache import save_quote, get_cached_quote, cached_quote_is_usable, quote_age_seconds
from app.services.universe_service import filter_supported_provider_rows
from app.services.crypto_provider_service import fetch_coingecko_snapshot, fetch_coingecko_snapshots
from app.utils.time import utcnowiso, freshness_label_from_age

# Module-level logger.  Used by failure-path code (the `except` branches in
# score_symbol_rows / extended-composite blend) that previously referenced an
# undefined `log` name -> tripping a NameError and aborting the whole batch
# the very first time a yfinance request threw inside the parallel fetcher.
log = logging.getLogger('app.scoring')

# ---------------------------------------------------------------------------
# Phase 26.34: hard-timeout wrapper for yf.download()
# ---------------------------------------------------------------------------
# The root cause of the user-reported "pass-2 wedge at 0 % CPU" was here.
# `yf.download(tickers=..., threads=True, timeout=N)` accepts a `timeout`
# parameter — but that only applies to *individual* HTTP requests inside
# yfinance's internal thread pool.  The OVERALL call has no ceiling.  Once
# Yahoo starts rate-limiting (which happens reliably after the first
# universal pass builds penalty state), yfinance retries the same hung
# sockets internally and `yf.download` can block for tens of minutes
# while every snap-worker waits on it at 0 % CPU.
#
# Fix: route every `yf.download` through a dedicated executor with a
# wall-clock ceiling.  The hung yfinance internal threads keep running
# in the background (they're daemon threads — they'll exit when their
# sockets time out at the OS layer or when the process dies), but the
# snap-worker is freed at the timeout deadline and falls through to the
# next provider in the cascade (yahoo-chart → stooq → finnhub).
#
# 32 workers: shared by yf.download (called by 2 snap-workers, one per
# batch) AND yf.Ticker.history() (called by up to 10 dh-prefetch workers
# concurrently — see daily_history_service.get_daily_history).  When
# Yahoo hangs, we need enough headroom to absorb the in-flight calls
# without queueing the next caller behind a dead one.  Each idle
# thread is ~8 KB of stack — cheap.
_YF_BATCH_EXECUTOR_LOCK = _threading.Lock()
_YF_BATCH_EXECUTOR = _ScoringTpe(
    max_workers=32, thread_name_prefix='yf-batch-timeout',
)
# 30 s is generous (a healthy batch of 100 symbols completes in 2-6 s)
# and matches our outer provider-cascade budget.  Overridable via env
# for operators who hit a faster/slower link.
_YF_BATCH_TIMEOUT_SECONDS = float(_os.environ.get('YF_BATCH_TIMEOUT_SECONDS', '30.0'))
_YF_BATCH_STATS = {
    'submits': 0, 'completions': 0, 'timeouts': 0, 'errors': 0,
}


def yf_batch_executor_stats() -> dict:
    """Operator telemetry for the yfinance batch-download timeout
    executor.  Surfaced under
    `/system/status.provider_stats.yfinance.batch_timeout_executor.*`.
    """
    with _YF_BATCH_EXECUTOR_LOCK:
        return dict(_YF_BATCH_STATS)


def _yf_download_with_timeout(**kwargs):
    """Wrapper around `yf.download(**kwargs)` with a hard wall-clock
    ceiling.  Returns whatever `yf.download` returned, or None on
    timeout / error.  The hung internal thread is abandoned (daemon —
    will exit when its socket eventually times out).
    """
    # Pop wrapper-only kwargs that yfinance doesn't accept.
    market_hint = kwargs.pop('_market_hint', 'unknown')
    with _YF_BATCH_EXECUTOR_LOCK:
        _YF_BATCH_STATS['submits'] += 1
        executor = _YF_BATCH_EXECUTOR
    try:
        fut = executor.submit(yf.download, **kwargs)
    except RuntimeError:
        # Executor is dead — should never happen, but degrade gracefully.
        with _YF_BATCH_EXECUTOR_LOCK:
            _YF_BATCH_STATS['errors'] += 1
        return None
    try:
        out = fut.result(timeout=_YF_BATCH_TIMEOUT_SECONDS)
        with _YF_BATCH_EXECUTOR_LOCK:
            _YF_BATCH_STATS['completions'] += 1
        return out
    except _ScoringFTimeout:
        with _YF_BATCH_EXECUTOR_LOCK:
            _YF_BATCH_STATS['timeouts'] += 1
        log.warning(
            'yf.download timed out after %.0fs (%d symbols, market=%s); '
            'abandoning hung internal threads and falling through to '
            'the next cascade provider',
            _YF_BATCH_TIMEOUT_SECONDS,
            len(kwargs.get('tickers', '').split()) if isinstance(kwargs.get('tickers'), str) else 0,
            market_hint,
        )
        return None
    except Exception as exc:  # noqa: BLE001
        with _YF_BATCH_EXECUTOR_LOCK:
            _YF_BATCH_STATS['errors'] += 1
        log.debug('yf.download raised: %s: %s', exc.__class__.__name__, exc)
        return None

def chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]

def safe_float(value, default=0.0):
    try:
        if value is None:
            return default
        if isinstance(value, float) and math.isnan(value):
            return default
        return float(value)
    except Exception:
        return default

def score_to_rating(value: float) -> str:
    if value >= 85:
        return 'Excellent'
    if value >= 70:
        return 'Strong'
    if value >= 55:
        return 'Moderate'
    if value >= 40:
        return 'Weak'
    return 'Poor'

def classify_tier(score: float) -> str:
    if score >= 80:
        return 'A'
    if score >= 60:
        return 'B'
    if score >= 40:
        return 'C'
    return 'D'

def classify_direction(change_pct: float) -> str:
    if change_pct >= 1.0:
        return 'Bullish'
    if change_pct <= -1.0:
        return 'Bearish'
    return 'Neutral'

def clamp_score(value: float) -> float:
    return max(0.0, min(100.0, value))

def session_extension_score(open_price: float, prev_close: float, last_price: float) -> float:
    if open_price <= 0 or prev_close <= 0 or last_price <= 0:
        return 50.0
    overnight_gap = ((open_price - prev_close) / prev_close) * 100.0
    intraday_follow = ((last_price - open_price) / open_price) * 100.0
    aligned = 1 if overnight_gap == 0 or intraday_follow == 0 else (1 if overnight_gap * intraday_follow > 0 else -1)
    score = 55.0 + overnight_gap * 8.0 + intraday_follow * 10.0 + aligned * 8.0
    return clamp_score(score)

def location_in_range_score(day_low: float, day_high: float, last_price: float, bullish: bool = True) -> float:
    if day_high <= day_low or last_price <= 0:
        return 50.0
    pct = (last_price - day_low) / (day_high - day_low)
    pct = max(0.0, min(1.0, pct))
    return clamp_score(100.0 * pct if bullish else 100.0 * (1.0 - pct))

def volume_pressure_score(volume: float, avg_volume: float) -> float:
    if volume <= 0 or avg_volume <= 0:
        return 50.0
    ratio = volume / avg_volume
    return clamp_score(40.0 + min(60.0, ratio * 18.0))

def turnover_score(volume: float, market_cap: float, last_price: float) -> float:
    if volume <= 0 or market_cap <= 0 or last_price <= 0:
        return 50.0
    traded_value = volume * last_price
    ratio = traded_value / market_cap
    return clamp_score(35.0 + min(65.0, ratio * 4000.0))

def trend_volume_delta_pct(change_pct: float, volume: float, avg_volume: float) -> float:
    if volume <= 0:
        return 0.0
    rel_volume = volume / avg_volume if avg_volume > 0 else 1.0
    directional_share = max(-1.0, min(1.0, change_pct / 5.0))
    return directional_share * max(20.0, min(120.0, rel_volume * 100.0))

def trend_volume_delta_bucket(delta_pct: float) -> str:
    if delta_pct >= 20.0:
        return 'strong_bullish'
    if delta_pct > 0.0:
        return 'bullish_neutral'
    if delta_pct <= -20.0:
        return 'strong_bearish'
    if delta_pct < 0.0:
        return 'bearish_neutral'
    return 'neutral'

def trend_volume_delta_score(delta_pct: float) -> float:
    bucket = trend_volume_delta_bucket(delta_pct)
    if bucket == 'strong_bullish':
        return clamp_score(80.0 + min(20.0, (delta_pct - 20.0) * 0.5))
    if bucket == 'bullish_neutral':
        return clamp_score(55.0 + min(20.0, delta_pct))
    if bucket == 'strong_bearish':
        return clamp_score(20.0 - min(20.0, abs(delta_pct) - 20.0) * 0.5)
    if bucket == 'bearish_neutral':
        return clamp_score(45.0 - min(20.0, abs(delta_pct)))
    return 50.0

def institutional_order_block_factor(last_price: float, day_low: float, day_high: float, volume: float, avg_volume: float, hist=None) -> dict:
    if last_price <= 0:
        return {'score': 50.0, 'bias': 'neutral', 'state': 'unavailable'}
    rangescore = 0.0
    volscore = volume_pressure_score(volume, avg_volume)
    displacement = 0.0
    freshness_bars = None
    touch_count = 0
    respect_rate = 0.5
    zone_low = day_low
    zone_high = (day_low + day_high) / 2 if day_high > day_low else last_price
    if hist is not None and len(hist) >= 20:
        try:
            recent = hist.tail(40).copy()
            recent['range'] = (recent['High'] - recent['Low']).clip(lower=0)
            recent['body'] = (recent['Close'] - recent['Open']).abs()
            base = recent.tail(6)
            zone_low = float(base['Low'].min())
            zone_high = float(base['High'].max())
            avg_range = float(recent['range'].tail(20).mean() or 0)
            base_range = float(base['range'].mean() or 0)
            rangescore = max(0.0, min(100.0, 100.0 - ((base_range / avg_range) * 100.0))) if avg_range > 0 else 50.0
            move = float(recent['Close'].iloc[-1] - base['Close'].mean())
            atr_like = avg_range if avg_range > 0 else max(last_price * 0.01, 0.01)
            displacement = move / atr_like
            hits = recent[(recent['Low'] <= zone_high) & (recent['High'] >= zone_low)]
            touch_count = int(len(hits))
            defended = hits[((hits['Close'] >= zone_low) & (hits['Close'] <= recent['High']))] if move >= 0 else hits[((hits['Close'] <= zone_high) & (hits['Close'] >= recent['Low']))]
            respect_rate = min(1.0, max(0.0, len(defended) / len(hits))) if len(hits) else 0.5
            freshness_bars = int(len(recent) - 6)
        except Exception:
            pass
    midpoint = (zone_low + zone_high) / 2 if zone_high >= zone_low else last_price
    distance_pct = ((last_price - midpoint) / last_price) * 100.0 if last_price else 0.0
    base_quality = rangescore
    displacement_strength = max(0.0, min(100.0, 50.0 + displacement * 15.0))
    retest_score = max(0.0, min(100.0, respect_rate * 100.0))
    freshness_score = max(20.0, 100.0 - float(freshness_bars or 20) * 2.0)
    proximity_score = max(0.0, 100.0 - min(100.0, abs(distance_pct) * 15.0))
    score = clamp_score(base_quality * 0.2 + volscore * 0.2 + displacement_strength * 0.25 + retest_score * 0.25 + freshness_score * 0.05 + proximity_score * 0.05)
    bullish = last_price >= midpoint and displacement >= 0
    bias = 'bullish' if score >= 55 and bullish else 'bearish' if score <= 45 and not bullish else 'neutral'
    state = 'holding' if zone_low <= last_price <= zone_high else 'tested' if abs(distance_pct) <= 2.0 else 'fresh' if (freshness_bars or 99) <= 10 else 'stale'
    return {
        'score': round(score, 2),
        'bias': bias,
        'state': state,
        'zone_low': round(zone_low, 2),
        'zone_high': round(zone_high, 2),
        'midpoint': round(midpoint, 2),
        'distance_from_price_pct': round(distance_pct, 2),
        'touch_count': touch_count,
        'respect_rate': round(retest_score, 2),
        'displacement_strength': round(displacement_strength, 2),
        'volume_confirmation': round(volscore, 2),
        'freshness_bars': freshness_bars,
        'confidence': round((base_quality * 0.35 + displacement_strength * 0.35 + retest_score * 0.30), 2)
    }

def dark_pool_proxy_factor(last_price: float, open_price: float, prev_close: float, day_low: float, day_high: float, volume: float, avg_volume: float, market_cap: float = 0.0, hist=None) -> dict:
    if last_price <= 0:
        return {'score': 50.0, 'bias': 'neutral', 'status': 'unavailable'}
    turnover = turnover_score(volume, market_cap, last_price)
    travel_pct = ((day_high - day_low) / last_price) * 100.0 if last_price else 0.0
    body_pct = abs(last_price - open_price) / last_price * 100.0 if last_price else 0.0
    close_vs_prev = abs(last_price - prev_close) / last_price * 100.0 if last_price else 0.0
    absorption_score = clamp_score(turnover * 0.45 + max(0.0, 100.0 - travel_pct * 18.0) * 0.35 + max(0.0, 100.0 - body_pct * 30.0) * 0.20)
    compression_score = clamp_score(max(0.0, 100.0 - travel_pct * 20.0))
    nearest = last_price
    memory_reaction_score = 50.0
    zone_density = 1
    if hist is not None and len(hist) >= 25:
        try:
            recent = hist.tail(60).copy()
            recent['turnover_proxy'] = recent['Volume'].fillna(0) * recent['Close'].fillna(0)
            cutoff = float(recent['turnover_proxy'].quantile(0.8))
            prints = recent[recent['turnover_proxy'] >= cutoff].copy()
            if len(prints):
                prints['print_level'] = ((prints['Open'] + prints['High'] + prints['Low'] + prints['Close']) / 4).round(2)
                levels = prints['print_level'].value_counts().sort_values(ascending=False)
                zone_density = int(min(10, len(levels)))
                nearest = min(levels.index.tolist(), key=lambda x: abs(float(x) - last_price))
                reacts = recent[(recent['Low'] <= nearest) & (recent['High'] >= nearest)]
                memory_reaction_score = clamp_score(40.0 + min(30.0, len(reacts) * 4.0) + min(30.0, zone_density * 3.0))
        except Exception:
            pass
    distance_pct = ((last_price - nearest) / last_price) * 100.0 if last_price else 0.0
    pinning_effect = 'high' if abs(distance_pct) <= 1.0 and absorption_score >= 60 else 'moderate' if abs(distance_pct) <= 2.5 else 'low'
    score = clamp_score(absorption_score * 0.4 + compression_score * 0.2 + turnover * 0.15 + memory_reaction_score * 0.2 + max(0.0, 100.0 - abs(distance_pct) * 12.0) * 0.05)
    bias = 'bullish' if last_price >= nearest and score >= 55 else 'bearish' if last_price < nearest and score >= 55 else 'neutral'
    return {
        'score': round(score, 2),
        'bias': bias,
        'nearest_print_level': round(nearest, 2),
        'distance_to_print_pct': round(distance_pct, 2),
        'absorption_score': round(absorption_score, 2),
        'compression_score': round(compression_score, 2),
        'turnover_score': round(turnover, 2),
        'memory_reaction_score': round(memory_reaction_score, 2),
        'zone_density': zone_density,
        'pinning_effect': pinning_effect,
        'confidence': round((absorption_score * 0.45 + memory_reaction_score * 0.35 + compression_score * 0.20), 2),
        'status': 'implemented'
    }


# ---------------------------------------------------------------------------
# Phase 26.16 / Tier 2.1 — vectorized indicator primitives
# ---------------------------------------------------------------------------
# The legacy helpers (`_rolling_mean`, `_rolling_std`, `_percent_rank`,
# `_ema_series`) are kept as scalar/legacy fallbacks. The hot path used by
# `institutional_confluence_factor` now goes through NumPy-backed variants
# so a 60-bar EMA-of-EMA pass that was O(N²) in pure Python collapses to
# O(N) in vectorized C code.
#
# Parity: the vectorized versions are checked against the legacy ones in
# /app/tests/test_scoring_vectorization.py and produce identical scalars
# to ~1e-9 absolute error (float math reordering is the only source of
# divergence, well below the 2-decimal rounding the scoring layer applies).
try:
    import numpy as _np  # type: ignore
    _NP_AVAILABLE = True
except Exception:  # pragma: no cover - numpy is a hard dependency anyway
    _NP_AVAILABLE = False
    _np = None  # type: ignore


def _to_float_array(values):
    """Convert an iterable of price/volume-like values into a float64 ndarray,
    silently filtering Nones (matches the legacy `safe_float` semantics).
    Returns an empty array if numpy isn't available or the input is empty.
    """
    if not _NP_AVAILABLE:
        return None
    # Numpy arrays come in directly (e.g. the output of _np_ema_full); the
    # generic `not values` truthiness check below would raise on them.
    if isinstance(values, _np.ndarray):
        if values.size == 0:
            return values.astype(_np.float64, copy=False)
        return values.astype(_np.float64, copy=False)
    if values is None:
        return _np.empty(0, dtype=_np.float64)
    if not values:
        return _np.empty(0, dtype=_np.float64)
    vals = [v for v in values if v is not None]
    if not vals:
        return _np.empty(0, dtype=_np.float64)
    try:
        arr = _np.asarray(vals, dtype=_np.float64)
    except Exception:
        # Fallback for mixed types — convert one-by-one via safe_float
        arr = _np.fromiter((safe_float(v) for v in vals), dtype=_np.float64, count=len(vals))
    return arr


def _np_rolling_mean(values) -> float:
    """Vectorized mean of the entire array (matches legacy `_rolling_mean`)."""
    arr = _to_float_array(values)
    if arr is None:
        return _rolling_mean(values)
    if arr.size == 0:
        return 0.0
    return float(arr.mean())


def _np_rolling_std(values) -> float:
    """Vectorized population stdev (matches legacy `_rolling_std`)."""
    arr = _to_float_array(values)
    if arr is None:
        return _rolling_std(values)
    if arr.size < 2:
        return 0.0
    return float(arr.std())  # numpy default ddof=0 == population std (matches legacy)


def _np_percent_rank(value: float, values) -> float:
    """Vectorized percent rank (% of values <= `value`)."""
    arr = _to_float_array(values)
    if arr is None:
        return _percent_rank(value, values)
    if arr.size == 0:
        return 50.0
    count = int((arr <= value).sum())
    return 100.0 * count / arr.size


def _np_ema_scalar(values, length: int) -> float:
    """Vectorized EMA returning the FINAL scalar (matches legacy `_ema_series`).

    Uses the standard EMA recursion `ema[i] = α*v[i] + (1-α)*ema[i-1]`
    seeded with `values[0]`. Implemented as a Python loop over a NumPy
    array — it's already O(N) and the per-step work is negligible; the big
    win is `_np_ema_full` below, which the rs_ratio call site uses.
    """
    arr = _to_float_array(values)
    if arr is None:
        return _ema_series(values, length)
    if arr.size == 0:
        return 0.0
    alpha = 2.0 / (length + 1.0)
    ema = float(arr[0])
    # `arr[1:]` is contiguous; the loop is tight but still Python — fine
    # for the lengths we use (<=100).
    for v in arr[1:]:
        ema = alpha * float(v) + (1.0 - alpha) * ema
    return ema


def _np_ema_full(values, length: int):
    """Return the FULL EMA series as a NumPy array (one EMA value per input
    bar).

    This is the key optimization for the rs_ratio call site, which
    previously computed an *expanding-window* EMA via:

        [_ema_series(rs[:i+1], 14) for i in range(len(rs))]

    That listcomp is O(N²) in Python. But the expanding-window EMA at
    position `i` is mathematically identical to the cumulative EMA series
    at position `i` (because EMA only depends on the previous EMA value,
    not the window length per se). So we can compute the entire series in
    one O(N) pass.
    """
    arr = _to_float_array(values)
    if arr is None or arr.size == 0:
        return _np.empty(0, dtype=_np.float64) if _NP_AVAILABLE else []
    alpha = 2.0 / (length + 1.0)
    one_minus = 1.0 - alpha
    out = _np.empty(arr.size, dtype=_np.float64)
    ema = float(arr[0])
    out[0] = ema
    for i in range(1, arr.size):
        ema = alpha * float(arr[i]) + one_minus * ema
        out[i] = ema
    return out


def _rolling_mean(values: list[float]) -> float:
    vals = [safe_float(v) for v in values if v is not None]
    return sum(vals) / len(vals) if vals else 0.0

def _rolling_std(values: list[float]) -> float:
    vals = [safe_float(v) for v in values if v is not None]
    if len(vals) < 2:
        return 0.0
    m = sum(vals) / len(vals)
    return (sum((v - m) ** 2 for v in vals) / len(vals)) ** 0.5

def _percent_rank(value: float, values: list[float]) -> float:
    vals = [safe_float(v) for v in values if v is not None]
    if not vals:
        return 50.0
    count = sum(1 for v in vals if v <= value)
    return 100.0 * count / len(vals)

def _ema_series(values: list[float], length: int) -> float:
    vals = [safe_float(v) for v in values if v is not None]
    if not vals:
        return 0.0
    alpha = 2.0 / (length + 1.0)
    ema = vals[0]
    for v in vals[1:]:
        ema = alpha * v + (1 - alpha) * ema
    return ema

def _roc(current: float, past: float) -> float:
    return ((current - past) / past) * 100.0 if past not in (0, None) else 0.0



def _safe_expirations(ticker) -> list[str]:
    try:
        exps = list(getattr(ticker, 'options', []) or [])
        return [e for e in exps if e]
    except Exception:
        return []

def _parse_expiry_days(expiry: str) -> int:
    from datetime import datetime, timezone
    try:
        dt = datetime.strptime(expiry, '%Y-%m-%d').replace(tzinfo=timezone.utc)
        return max(0, (dt.date() - datetime.now(timezone.utc).date()).days)
    except Exception:
        return 9999

def _bucket_expiry(days: int) -> str:
    if days <= 14:
        return 'near_term'
    if days <= 45:
        return 'monthly'
    return 'longer_dated'

def _weighted_strike(records: list[dict]) -> float | None:
    total = sum(max(0.0, r.get('weight', 0.0)) for r in records)
    if total <= 0:
        return None
    return sum(r['strike'] * r['weight'] for r in records) / total

def options_positioning_factor(symbol: str, last_price: float) -> dict:
    if not symbol or last_price <= 0:
        return {'score': 50.0, 'bias': 'neutral', 'status': 'symbol_unavailable'}
    try:
        tk = yf.Ticker(symbol)
        expirations = _safe_expirations(tk)[:6]
    except Exception:
        return {'score': 50.0, 'bias': 'neutral', 'status': 'options_unavailable'}
    if not expirations:
        return {'score': 50.0, 'bias': 'neutral', 'status': 'no_expirations'}

    buckets = {'near_term': [], 'monthly': [], 'longer_dated': []}
    total_call_w = 0.0
    total_put_w = 0.0
    all_records = []

    for exp in expirations:
        days = _parse_expiry_days(exp)
        bucket = _bucket_expiry(days)
        try:
            chain = tk.option_chain(exp)
        except Exception:
            continue
        for side_name, df in [('call', getattr(chain, 'calls', None)), ('put', getattr(chain, 'puts', None))]:
            if df is None or len(df) == 0:
                continue
            cols = set(df.columns)
            for _, row in df.iterrows():
                strike = safe_float(row['strike']) if 'strike' in cols else 0.0
                oi = safe_float(row['openInterest']) if 'openInterest' in cols else 0.0
                vol = safe_float(row['volume']) if 'volume' in cols else 0.0
                premium = safe_float(row['lastPrice']) if 'lastPrice' in cols else 0.0
                iv = safe_float(row['impliedVolatility']) if 'impliedVolatility' in cols else 0.0
                if strike <= 0 or (oi <= 0 and vol <= 0):
                    continue
                weight = max(0.0, oi * 0.65 + vol * 0.35) * max(0.01, premium)
                rec = {'expiry': exp, 'days': days, 'bucket': bucket, 'side': side_name, 'strike': strike, 'oi': oi, 'vol': vol, 'premium': premium, 'iv': iv, 'weight': weight}
                buckets[bucket].append(rec)
                all_records.append(rec)
                if side_name == 'call':
                    total_call_w += weight
                else:
                    total_put_w += weight

    def summarize(records: list[dict]) -> dict:
        if not records:
            return {'target_price': None, 'call_wall': None, 'put_wall': None, 'bias': 'neutral', 'pressure_score': 50.0}
        calls = [r for r in records if r['side'] == 'call']
        puts = [r for r in records if r['side'] == 'put']
        call_target = _weighted_strike(calls)
        put_target = _weighted_strike(puts)
        call_wall = max(calls, key=lambda r: r['weight'])['strike'] if calls else None
        put_wall = max(puts, key=lambda r: r['weight'])['strike'] if puts else None
        net_call = sum(r['weight'] for r in calls)
        net_put = sum(r['weight'] for r in puts)
        total = net_call + net_put
        directional = ((net_call - net_put) / total) if total > 0 else 0.0
        weighted_target = _weighted_strike(records)
        dist = ((weighted_target - last_price) / last_price) * 100.0 if weighted_target else 0.0
        score = clamp_score(50.0 + directional * 30.0 + max(-20.0, min(20.0, dist)))
        bias = 'bullish' if score >= 60 else 'bearish' if score <= 40 else 'neutral'
        return {
            'target_price': round(weighted_target, 2) if weighted_target else None,
            'call_wall': round(call_wall, 2) if call_wall else None,
            'put_wall': round(put_wall, 2) if put_wall else None,
            'call_target': round(call_target, 2) if call_target else None,
            'put_target': round(put_target, 2) if put_target else None,
            'bias': bias,
            'pressure_score': round(score, 2),
            'contracts_considered': len(records)
        }

    near_term = summarize(buckets['near_term'])
    monthly = summarize(buckets['monthly'])
    composite = summarize(all_records)
    total = total_call_w + total_put_w
    call_put_ratio = (total_call_w / total_put_w) if total_put_w > 0 else None
    pin_risk = 'high' if composite.get('call_wall') and composite.get('put_wall') and abs(composite['call_wall'] - composite['put_wall']) / last_price < 0.03 else 'moderate' if all_records else 'low'
    gamma_level = composite.get('target_price')
    final_score = round((near_term.get('pressure_score', 50.0) * 0.4 + monthly.get('pressure_score', 50.0) * 0.35 + composite.get('pressure_score', 50.0) * 0.25), 2)
    final_bias = 'bullish' if final_score >= 60 else 'bearish' if final_score <= 40 else 'neutral'
    return {
        'score': final_score,
        'bias': final_bias,
        'status': 'implemented',
        'near_term': near_term,
        'monthly': monthly,
        'composite': composite,
        'call_put_premium_ratio': round(call_put_ratio, 2) if call_put_ratio else None,
        'pin_risk': pin_risk,
        'gamma_level': gamma_level,
        'expirations_used': len(expirations)
    }
def institutional_confluence_factor(symbol: str, info: dict, hist) -> dict:
    try:
        closes = hist['Close'].dropna().astype(float).tolist() if hist is not None and 'Close' in hist else []
        highs = hist['High'].dropna().astype(float).tolist() if hist is not None and 'High' in hist else []
        lows = hist['Low'].dropna().astype(float).tolist() if hist is not None and 'Low' in hist else []
        opens = hist['Open'].dropna().astype(float).tolist() if hist is not None and 'Open' in hist else []
        vols = hist['Volume'].dropna().astype(float).tolist() if hist is not None and 'Volume' in hist else []
    except Exception:
        closes, highs, lows, opens, vols = [], [], [], [], []

    if len(closes) < 25:
        return {
            'score': 50.0, 'bias': 'neutral', 'status': 'insufficient_history',
            'rrg': {'score': 50.0, 'quadrant': 'NEUTRAL'},
            'flow': {'score': 50.0, 'bias': 'NEUTRAL', 'unusual_volume': False},
            'regime': {'score': 50.0, 'state': 'RANGING'},
            'liquidity': {'score': 50.0, 'signal': 'NONE', 'zones': 0},
            'session': {'score': 50.0, 'state': 'OFF_HOURS'}
        }

    close_now = safe_float(closes[-1])
    open_now = safe_float(opens[-1] if opens else info.get('open'))
    high_now = safe_float(highs[-1] if highs else info.get('dayHigh'))
    low_now = safe_float(lows[-1] if lows else info.get('dayLow'))
    volume_now = safe_float(vols[-1] if vols else info.get('volume'))

    rs = closes[-60:]
    # Phase 26.16 / Tier 2.1: vectorized expanding-EMA collapses the
    # previous O(N²) listcomp into a single O(N) pass. For len(rs)=60
    # this is ~10x faster on CPython.
    if rs:
        rs_ema_full = _np_ema_full(rs, 14)
        rs_ratio = _np_ema_scalar(rs_ema_full, 14) if (rs_ema_full is not None and getattr(rs_ema_full, 'size', 0) > 0) else 100.0
    else:
        rs_ratio = 100.0
    rs_base = rs[:-10] if len(rs) > 10 else rs
    if rs_base:
        rs_base_ema_full = _np_ema_full(rs_base, 14)
        rs_ratio_past = _np_ema_scalar(rs_base_ema_full, 14) if (rs_base_ema_full is not None and getattr(rs_base_ema_full, 'size', 0) > 0) else rs_ratio
    else:
        rs_ratio_past = rs_ratio
    rs_mom = _roc(rs_ratio, rs_ratio_past)
    quadrant = 'LEADING' if rs_ratio >= 100 and rs_mom >= 0 else 'WEAKENING' if rs_ratio >= 100 else 'IMPROVING' if rs_mom >= 0 else 'LAGGING'
    rrg_score = 75.0 if quadrant == 'LEADING' else 60.0 if quadrant == 'IMPROVING' else 40.0 if quadrant == 'WEAKENING' else 25.0

    flow_lookback = min(50, len(vols))
    avg_vol = _np_rolling_mean(vols[-flow_lookback:]) if vols else safe_float(info.get('averageVolume'), 0.0)
    vol_std = _np_rolling_std(vols[-flow_lookback:]) if vols else 0.0
    vol_z = (volume_now - avg_vol) / vol_std if vol_std else 0.0
    unusual_vol = vol_z >= 2.0
    range_now = max(0.0, high_now - low_now)
    buy_pressure = ((close_now - low_now) / range_now) if range_now else 0.5
    flow_score = clamp_score(50.0 + (buy_pressure - 0.5) * 40.0 + vol_z * 5.0)
    flow_bias = 'BULLISH' if flow_score >= 60 else 'BEARISH' if flow_score <= 40 else 'NEUTRAL'

    # Phase 26.16 / Tier 2.1: vectorize the True-Range loop. Builds three
    # sliced arrays (highs[1:], lows[1:], closes[:-1]) and does the
    # elementwise max in C rather than a Python `for i in range(...)`.
    max_n = min(len(closes), len(highs), len(lows))
    if max_n > 1 and _NP_AVAILABLE:
        h = _np.asarray(highs[1:max_n], dtype=_np.float64)
        l = _np.asarray(lows[1:max_n], dtype=_np.float64)
        pc = _np.asarray(closes[0:max_n - 1], dtype=_np.float64)
        # element-wise max of (h-l, |h-pc|, |l-pc|)
        tr_arr = _np.maximum(_np.maximum(h - l, _np.abs(h - pc)), _np.abs(l - pc))
        trs = tr_arr.tolist()
    else:
        trs = []
        for i in range(1, max_n):
            prev_close = closes[i - 1]
            tr = max(highs[i] - lows[i], abs(highs[i] - prev_close), abs(lows[i] - prev_close))
            trs.append(tr)
    atr = _np_rolling_mean(trs[-14:]) if trs else 0.0
    atr_rank = _np_percent_rank(atr, trs[-100:] if trs else [])
    ema20 = _np_ema_scalar(closes[-60:], 20)
    ema50 = _np_ema_scalar(closes[-100:], 50)
    regime = 'COMPRESSION' if atr_rank < 20 else 'EXPANSION' if atr_rank > 80 else 'TRENDING_BULL' if ema20 > ema50 else 'TRENDING_BEAR' if ema20 < ema50 else 'RANGING'
    regime_score = 70.0 if regime == 'TRENDING_BULL' else 30.0 if regime == 'TRENDING_BEAR' else 50.0

    recent_high = max(highs[-21:-1]) if len(highs) > 20 else high_now
    recent_low = min(lows[-21:-1]) if len(lows) > 20 else low_now
    sweep_high = high_now > recent_high and close_now < recent_high and close_now < open_now
    sweep_low = low_now < recent_low and close_now > recent_low and close_now > open_now
    liq_score = clamp_score(50.0 + (20.0 if sweep_low else 0.0) - (20.0 if sweep_high else 0.0))
    liquidity_signal = 'BULL_GRAB' if sweep_low else 'BEAR_GRAB' if sweep_high else 'NONE'
    liq_zones = min(10, max(0, len(highs) // 20 + len(lows) // 20))

    session_state = 'NEW_YORK'
    session_score = 60.0

    confluence = round((rrg_score + flow_score + regime_score + liq_score + session_score) / 5.0, 2)
    bias = 'STRONG_BULL' if confluence >= 65 else 'BULLISH' if confluence >= 55 else 'STRONG_BEAR' if confluence <= 35 else 'BEARISH' if confluence <= 45 else 'NEUTRAL'
    return {
        'score': confluence,
        'bias': bias,
        'status': 'implemented_from_icm',
        'rrg': {'score': round(rrg_score, 2), 'quadrant': quadrant, 'rs_ratio': round(rs_ratio, 2), 'rs_momentum': round(rs_mom, 2)},
        'flow': {'score': round(flow_score, 2), 'bias': flow_bias, 'buy_pressure': round(buy_pressure, 3), 'volume_zscore': round(vol_z, 2), 'unusual_volume': unusual_vol},
        'regime': {'score': round(regime_score, 2), 'state': regime, 'atr_rank_pct': round(atr_rank, 2)},
        'liquidity': {'score': round(liq_score, 2), 'signal': liquidity_signal, 'zones': liq_zones, 'sweep_high': sweep_high, 'sweep_low': sweep_low},
        'session': {'score': session_score, 'state': session_state}
    }

def gap_efficiency_score(open_price: float, prev_close: float, last_price: float) -> float:
    if open_price <= 0 or prev_close <= 0 or last_price <= 0:
        return 50.0
    gap_pct = ((open_price - prev_close) / prev_close) * 100.0
    close_from_open = ((last_price - open_price) / open_price) * 100.0
    if abs(gap_pct) < 0.15:
        return clamp_score(55.0 + close_from_open * 6.0)
    follow_ratio = close_from_open / gap_pct if gap_pct else 0.0
    return clamp_score(55.0 + follow_ratio * 35.0)

def intraday_volatility_score(day_low: float, day_high: float, last_price: float) -> float:
    if day_low <= 0 or day_high <= 0 or last_price <= 0 or day_high <= day_low:
        return 50.0
    range_pct = ((day_high - day_low) / last_price) * 100.0
    return clamp_score(95.0 - min(75.0, abs(range_pct - 3.0) * 10.0))

def movement_penalty(change_pct: float) -> float:
    return min(45.0, max(0.0, abs(change_pct) - 2.5) * 4.0)

def retracement_score(open_price: float, day_low: float, day_high: float, last_price: float, change_pct: float) -> float:
    if day_high <= day_low or open_price <= 0 or last_price <= 0:
        return 50.0
    session_range = day_high - day_low
    if session_range <= 0:
        return 50.0
    bullish = change_pct >= 0
    if bullish:
        adverse_move = max(0.0, day_high - last_price)
    else:
        adverse_move = max(0.0, last_price - day_low)
    retracement_pct = adverse_move / session_range
    score = 95.0 - retracement_pct * 120.0
    return clamp_score(score)

def open_hold_score(open_price: float, last_price: float, change_pct: float) -> float:
    if open_price <= 0 or last_price <= 0:
        return 50.0
    move_from_open = ((last_price - open_price) / open_price) * 100.0
    if change_pct >= 0:
        return clamp_score(55.0 + move_from_open * 18.0)
    return clamp_score(55.0 - move_from_open * 18.0)

def directional_consistency_score(prev_close: float, open_price: float, last_price: float, change_pct: float) -> float:
    if prev_close <= 0 or open_price <= 0 or last_price <= 0:
        return 50.0
    gap_pct = ((open_price - prev_close) / prev_close) * 100.0
    intraday_pct = ((last_price - open_price) / open_price) * 100.0
    if abs(change_pct) < 0.5:
        return 55.0
    if change_pct > 0:
        aligned = 1 if intraday_pct >= 0 else -1
        continuation_bonus = min(20.0, max(0.0, intraday_pct) * 8.0)
    else:
        aligned = 1 if intraday_pct <= 0 else -1
        continuation_bonus = min(20.0, max(0.0, -intraday_pct) * 8.0)
    gap_alignment = 6.0 if gap_pct == 0 or gap_pct * change_pct > 0 else -6.0
    return clamp_score(55.0 + aligned * 12.0 + continuation_bonus + gap_alignment)

def build_stability_breakdown(px: float, prev_close: float, info: dict | None = None) -> dict:
    info = info or {}
    open_price = safe_float(info.get('open'), 0.0)
    day_low = safe_float(info.get('dayLow'), 0.0)
    day_high = safe_float(info.get('dayHigh'), 0.0)
    change_pct = 0.0 if not prev_close else ((px - prev_close) / prev_close) * 100.0

    base_penalty = movement_penalty(change_pct)
    retracement = retracement_score(open_price, day_low, day_high, px, change_pct)
    hold_open = open_hold_score(open_price, px, change_pct)
    range_hold = location_in_range_score(day_low, day_high, px, bullish=(change_pct >= 0))
    consistency = directional_consistency_score(prev_close, open_price, px, change_pct)

    raw_components = {
        'retracement_control': round(retracement, 2),
        'open_hold': round(hold_open, 2),
        'range_hold': round(range_hold, 2),
        'directional_consistency': round(consistency, 2),
        'large_move_penalty': round(max(0.0, 100.0 - base_penalty * 2.0), 2),
    }
    weights = {
        'retracement_control': 0.28,
        'open_hold': 0.22,
        'range_hold': 0.18,
        'directional_consistency': 0.20,
        'large_move_penalty': 0.12,
    }
    score_before_penalty = sum(raw_components[name] * weights[name] for name in weights)
    final_stability = clamp_score(score_before_penalty - base_penalty)
    return {
        'score': round(final_stability, 2),
        'rating': score_to_rating(final_stability),
        'weights': weights,
        'components': raw_components,
        'movement_penalty_points': round(base_penalty, 2),
        'inputs': {
            'open': open_price,
            'day_low': day_low,
            'day_high': day_high,
            'last_price': px,
            'previous_close': prev_close,
            'change_pct': round(change_pct, 4),
        },
    }

def build_quality_breakdown(info: dict | None = None) -> dict:
    info = info or {}
    prev_close = safe_float(info.get('previousClose'), 0.0)
    open_price = safe_float(info.get('open'), 0.0)
    day_low = safe_float(info.get('dayLow'), 0.0)
    day_high = safe_float(info.get('dayHigh'), 0.0)
    last_price = safe_float(info.get('currentPrice') or info.get('regularMarketPrice'), 0.0)
    volume = safe_float(info.get('volume') or info.get('regularMarketVolume'), 0.0)
    avg_volume = safe_float(info.get('averageVolume') or info.get('averageVolume10days'), 0.0)
    market_cap = safe_float(info.get('marketCap'), 0.0)
    bid = safe_float(info.get('bid'), 0.0)
    ask = safe_float(info.get('ask'), 0.0)

    spread_pct = ((ask - bid) / last_price) * 100.0 if ask > 0 and bid > 0 and last_price > 0 and ask >= bid else 0.0
    spread_score = clamp_score(95.0 - min(80.0, spread_pct * 140.0)) if spread_pct > 0 else 55.0

    components = {
        'relative_volume': round(volume_pressure_score(volume, avg_volume), 2),
        'turnover': round(turnover_score(volume, market_cap, last_price), 2),
        'session_extension': round(session_extension_score(open_price, prev_close, last_price), 2),
        'range_position': round(location_in_range_score(day_low, day_high, last_price, bullish=True), 2),
        'gap_efficiency': round(gap_efficiency_score(open_price, prev_close, last_price), 2),
        'intraday_volatility': round(intraday_volatility_score(day_low, day_high, last_price), 2),
        'spread_quality': round(spread_score, 2),
    }
    weights = {
        'relative_volume': 0.22,
        'turnover': 0.12,
        'session_extension': 0.18,
        'range_position': 0.16,
        'gap_efficiency': 0.12,
        'intraday_volatility': 0.12,
        'spread_quality': 0.08,
    }
    quality_score = round(sum(components[name] * weights[name] for name in weights), 2)
    intraday = {
        'previous_close': prev_close,
        'open': open_price,
        'day_low': day_low,
        'day_high': day_high,
        'last_price': last_price,
        'volume': volume,
        'average_volume': avg_volume,
        'market_cap': market_cap,
        'bid': bid,
        'ask': ask,
        'spread_pct': round(spread_pct, 4),
    }
    return {
        'score': quality_score,
        'rating': score_to_rating(quality_score),
        'weights': weights,
        'components': components,
        'intraday_inputs': intraday,
    }

def build_exit_risk_breakdown(px: float, prev_close: float, info: dict | None = None) -> dict:
    info = info or {}
    open_price = safe_float(info.get('open'), 0.0)
    day_low = safe_float(info.get('dayLow') or info.get('day_low'), 0.0)
    day_high = safe_float(info.get('dayHigh') or info.get('day_high'), 0.0)
    volume = safe_float(info.get('volume') or info.get('regularMarketVolume'), 0.0)
    avg_volume = safe_float(info.get('averageVolume') or info.get('average_volume') or info.get('averageVolume10days'), 0.0)
    change_pct = 0.0 if not prev_close else ((px - prev_close) / prev_close) * 100.0
    has_shape = open_price > 0 and day_low > 0 and day_high > 0 and day_high > day_low and px > 0 and prev_close > 0
    if not has_shape:
        return {
            'score': None,
            'rating': 'Unknown',
            'exit_flag': 'unknown',
            'components': {
                'trap_risk': None,
                'retracement_risk': None,
                'exhaustion_risk': None,
                'upper_wick_pct': None,
                'lower_wick_pct': None,
                'relative_volume': round(volume / avg_volume, 2) if avg_volume > 0 else None,
            },
            'inputs': {
                'open': open_price,
                'day_low': day_low,
                'day_high': day_high,
                'last_price': px,
                'previous_close': prev_close,
                'change_pct': round(change_pct, 4),
            },
            'data_ready': False,
        }
    bar_range = max(day_high - day_low, 0.01)
    upper_wick_pct = clamp_score(((day_high - max(open_price, px)) / bar_range) * 100.0)
    lower_wick_pct = clamp_score(((min(open_price, px) - day_low) / bar_range) * 100.0)
    retracement_from_high = clamp_score(((day_high - px) / bar_range) * 100.0)
    extension_from_open = abs(((px - open_price) / open_price) * 100.0) if open_price > 0 else 0.0
    rel_volume = volume / avg_volume if avg_volume > 0 else 1.0
    trap_risk = upper_wick_pct if change_pct >= 0 else lower_wick_pct
    exhaustion_risk = clamp_score(35.0 + extension_from_open * 18.0 + max(0.0, rel_volume - 1.0) * 15.0)
    reversal_risk = clamp_score(0.45 * trap_risk + 0.35 * retracement_from_high + 0.20 * exhaustion_risk)
    exit_flag = 'hold'
    if reversal_risk >= 75:
        exit_flag = 'exit'
    elif reversal_risk >= 60:
        exit_flag = 'caution'
    return {
        'score': round(reversal_risk, 2),
        'rating': score_to_rating(100.0 - reversal_risk),
        'exit_flag': exit_flag,
        'components': {
            'trap_risk': round(trap_risk, 2),
            'retracement_risk': round(retracement_from_high, 2),
            'exhaustion_risk': round(exhaustion_risk, 2),
            'upper_wick_pct': round(upper_wick_pct, 2),
            'lower_wick_pct': round(lower_wick_pct, 2),
            'relative_volume': round(rel_volume, 2),
        },
        'inputs': {
            'open': open_price,
            'day_low': day_low,
            'day_high': day_high,
            'last_price': px,
            'previous_close': prev_close,
            'change_pct': round(change_pct, 4),
        },
        'data_ready': True,
    }



def _reaction_state_from_score(score: float) -> str:
    if score >= 60:
        return 'continuation'
    if score <= 40:
        return 'reversal'
    return 'choppy'

def _directional_sentiment(change_pct: float, tvd_score: float, flow_bias: str, exit_risk: float) -> dict:
    directional = 0.0
    directional += max(-20.0, min(20.0, change_pct * 2.5))
    directional += (tvd_score - 50.0) * 0.45
    if flow_bias == 'bullish':
        directional += 8.0
    elif flow_bias == 'bearish':
        directional -= 8.0
    directional -= max(0.0, exit_risk - 50.0) * 0.18
    sentiment_score = clamp_score(50.0 + directional)
    sentiment_bias = 'bullish' if sentiment_score >= 55 else 'bearish' if sentiment_score <= 45 else 'neutral'
    return {'score': round(sentiment_score, 2), 'bias': sentiment_bias, 'directional_edge': round(directional, 2)}

def _target_reaction_model(last_price: float, target_price: float | None, sentiment_bias: str, sentiment_score: float, volume_ratio: float, exit_risk: float, bucket_bias: str | None = None, confluence_score: float = 50.0) -> dict:
    if not target_price or last_price <= 0:
        return {'state': 'unavailable', 'continuation_score': 50.0, 'reversal_score': 50.0, 'chop_score': 50.0}
    distance_pct = ((target_price - last_price) / last_price) * 100.0
    approaching = abs(distance_pct) <= 3.0
    same_side = (sentiment_bias == 'bullish' and target_price >= last_price) or (sentiment_bias == 'bearish' and target_price <= last_price)
    continuation = 50.0
    continuation += 12.0 if same_side else -12.0
    continuation += (sentiment_score - 50.0) * 0.55
    continuation += max(-10.0, min(10.0, (volume_ratio - 1.0) * 12.0))
    continuation += (confluence_score - 50.0) * 0.15
    if bucket_bias == 'bullish':
        continuation += 6.0
    elif bucket_bias == 'bearish':
        continuation -= 6.0
    continuation -= max(0.0, exit_risk - 55.0) * 0.25
    if approaching and abs(sentiment_score - 50.0) < 8.0:
        continuation -= 8.0
    continuation = clamp_score(continuation)
    reversal = clamp_score(100.0 - continuation + max(0.0, exit_risk - 55.0) * 0.2 + (8.0 if approaching and not same_side else 0.0))
    chop = clamp_score(100.0 - abs(continuation - 50.0) * 1.6 + (10.0 if approaching else 0.0) - abs(volume_ratio - 1.0) * 8.0)
    state = max([('continuation', continuation), ('reversal', reversal), ('choppy', chop)], key=lambda x: x[1])[0]
    return {
        'state': state,
        'distance_to_target_pct': round(distance_pct, 2),
        'continuation_score': round(continuation, 2),
        'reversal_score': round(reversal, 2),
        'chop_score': round(chop, 2),
        'sentiment_bias': sentiment_bias,
        'target_price': round(target_price, 2)
    }

def build_reaction_map(last_price: float, change_pct: float, volume: float, avg_volume: float, trend_volume_delta: dict, institutional_confluence: dict, options_positioning: dict, institutional_order_block: dict, exit_model: dict) -> dict:
    volume_ratio = (volume / avg_volume) if avg_volume > 0 else 1.0
    tvd_score = safe_float(trend_volume_delta.get('score'), 50.0)
    flow_bias = institutional_confluence.get('flow', {}).get('bias', institutional_confluence.get('bias', 'neutral'))
    confluence_score = safe_float(institutional_confluence.get('score'), 50.0)
    exit_risk = safe_float(exit_model.get('score'), 50.0)
    sentiment = _directional_sentiment(change_pct, tvd_score, flow_bias, exit_risk)
    near_term = options_positioning.get('near_term', {})
    monthly = options_positioning.get('monthly', {})
    composite = options_positioning.get('composite', {})
    options_reaction = {
        'near_term': _target_reaction_model(last_price, near_term.get('target_price'), sentiment['bias'], sentiment['score'], volume_ratio, exit_risk, near_term.get('bias'), confluence_score),
        'monthly': _target_reaction_model(last_price, monthly.get('target_price'), sentiment['bias'], sentiment['score'], volume_ratio, exit_risk, monthly.get('bias'), confluence_score),
        'composite': _target_reaction_model(last_price, composite.get('target_price'), sentiment['bias'], sentiment['score'], volume_ratio, exit_risk, composite.get('bias'), confluence_score),
    }
    iob_target = institutional_order_block.get('midpoint')
    iob_reaction = _target_reaction_model(last_price, iob_target, sentiment['bias'], sentiment['score'], volume_ratio, exit_risk, institutional_order_block.get('bias'), confluence_score)
    iob_reaction['zone_low'] = institutional_order_block.get('zone_low')
    iob_reaction['zone_high'] = institutional_order_block.get('zone_high')
    iob_reaction['order_block_state'] = institutional_order_block.get('state')
    options_composite_score = round((options_reaction['near_term'].get('continuation_score', 50.0) + options_reaction['monthly'].get('continuation_score', 50.0) + options_reaction['composite'].get('continuation_score', 50.0)) / 3.0, 2)
    return {
        'volume_sentiment': sentiment,
        'options_reaction': {
            'near_term': options_reaction['near_term'],
            'monthly': options_reaction['monthly'],
            'composite': options_reaction['composite'],
            'dominant_state': _reaction_state_from_score(options_composite_score),
            'continuation_bias_score': options_composite_score,
        },
        'order_block_reaction': {
            **iob_reaction,
            'dominant_state': iob_reaction.get('state', 'unavailable')
        }
    }
def _predictive_consensus_modifier(families: dict) -> tuple[float, list[str]]:
    """Boost or depreciate the composite based on whether the predictive
    factor families (reaction_clustering, volume_sentiment) agree with the
    directional consensus of the other five families.

    Rules per the user's spec:
      - If a predictive family flags STRONGLY in the same direction as
        the consensus → boost the composite (acknowledge the signal).
      - If a predictive family flags STRONGLY in a direction that
        contradicts the consensus → depreciate confidence (small penalty
        + a note that surfaces in the detail panel).
      - Weak / neutral predictive signals contribute nothing.

    Returns ``(modifier, notes)`` where modifier is in roughly [-7, +5]
    and notes is a list of human-readable strings.
    """
    notes: list[str] = []
    if not isinstance(families, dict):
        return 0.0, notes

    # --- consensus from the 5 non-predictive families ---
    consensus_keys = [
        'trend_volume_delta', 'institutional_confluence',
        'options_positioning', 'institutional_order_block',
        'dark_pool_proxy',
    ]
    bull = bear = 0
    for k in consensus_keys:
        fam = families.get(k) or {}
        bias = str(fam.get('bias') or fam.get('attraction_state') or '').lower()
        if 'bull' in bias or bias == 'attracting':
            bull += 1
        elif 'bear' in bias or bias == 'repelling':
            bear += 1
    consensus = 'bullish' if bull > bear else ('bearish' if bear > bull else 'neutral')
    strength = abs(bull - bear)  # 0..5

    modifier = 0.0

    # --- reaction_clustering signal ---
    rc = families.get('reaction_clustering') or {}
    rc_class = str(rc.get('classification') or 'NEUTRAL').upper()
    rc_strong = float(rc.get('dominant_probability') or 0) > 0.55 \
                and rc_class in ('PROPEL', 'REJECT')
    rc_dir = ('bullish' if rc_class == 'PROPEL'
              else 'bearish' if rc_class == 'REJECT'
              else 'neutral')
    if rc_strong:
        if rc_dir == consensus and strength >= 2:
            modifier += 3.0
            notes.append(f'reaction_clustering {rc_class} agrees with {strength}-way {consensus} consensus (+3.0)')
        elif rc_dir != consensus and strength >= 2 and consensus != 'neutral':
            modifier -= 4.0
            notes.append(f'reaction_clustering {rc_class} contradicts {strength}-way {consensus} consensus — confidence depreciated (-4.0)')
        else:
            notes.append(f'reaction_clustering {rc_class} noted, consensus too weak to amplify')

    # --- volume_sentiment signal ---
    vs = families.get('volume_sentiment') or {}
    vs_bias = str(vs.get('bias') or 'neutral').lower()
    vs_conviction = float(vs.get('conviction_score') or 0)
    vs_strong = vs_conviction >= 60.0 and ('bull' in vs_bias or 'bear' in vs_bias)
    vs_dir = 'bullish' if 'bull' in vs_bias else ('bearish' if 'bear' in vs_bias else 'neutral')
    if vs_strong:
        if vs_dir == consensus and strength >= 2:
            modifier += 2.0
            notes.append(f'volume_sentiment {vs_dir} (conv {vs_conviction:.0f}) agrees with {consensus} consensus (+2.0)')
        elif vs_dir != consensus and strength >= 2 and consensus != 'neutral':
            modifier -= 3.0
            notes.append(f'volume_sentiment {vs_dir} (conv {vs_conviction:.0f}) contradicts {consensus} consensus — depreciated (-3.0)')

    # Clamp modifier so it can never single-handedly shift a row a whole tier.
    modifier = max(-7.0, min(5.0, modifier))
    return modifier, notes


def _confidence_audit(px: float, prev_close: float, quality_inputs: dict) -> dict:
    """Detect which algorithm inputs are missing so the sanity rule can clamp
    final_score and tell the user *why* the row is low-confidence.

    Returns:
      {
        'live_count': int,   # how many of momentum/quality/trend/stability had real inputs
        'missing':   list,   # human labels for the missing inputs
      }
    """
    quality_inputs = quality_inputs or {}
    has_quote = bool(px and prev_close and px > 0 and prev_close > 0)
    has_open = safe_float(quality_inputs.get('open')) > 0
    has_range = safe_float(quality_inputs.get('day_low')) > 0 and \
                safe_float(quality_inputs.get('day_high')) > 0 and \
                safe_float(quality_inputs.get('day_high')) > safe_float(quality_inputs.get('day_low'))
    has_volume = safe_float(quality_inputs.get('volume')) > 0
    has_avg_volume = safe_float(quality_inputs.get('average_volume')) > 0
    # Each rating needs a different mix of inputs to be considered "live"
    momentum_live = has_quote
    trend_live = has_quote
    stability_live = has_quote and has_open and has_range
    quality_live = (has_quote and has_open and has_range) or has_volume
    live_count = sum(int(b) for b in (momentum_live, quality_live, trend_live, stability_live))
    missing: list[str] = []
    if not has_quote:
        missing.append('live_quote (last price / previous close)')
    if not has_open:
        missing.append('session_open_price')
    if not has_range:
        missing.append('intraday_day_range')
    if not has_volume:
        missing.append('intraday_volume')
    if not has_avg_volume:
        missing.append('average_volume')
    return {
        'live_count': live_count,
        'missing': missing,
        'inputs_seen': {
            'has_quote': has_quote, 'has_open': has_open, 'has_range': has_range,
            'has_volume': has_volume, 'has_avg_volume': has_avg_volume,
        },
    }


def build_algorithm_breakdown(px: float, prev_close: float, source: str, age_seconds: int, provider_note: str | None = None, quality_snapshot: dict | None = None):
    change_pct = 0.0 if not prev_close else ((px - prev_close) / prev_close) * 100.0
    momentum = clamp_score(50.0 + change_pct * 5.0)
    quality_bundle = quality_snapshot or build_quality_breakdown({})
    quality = quality_bundle.get('score', 55.0)
    trend = clamp_score(50.0 + change_pct * 4.0)
    stability_bundle = build_stability_breakdown(px, prev_close, quality_bundle.get('intraday_inputs', {}))
    stability = stability_bundle.get('score', 50.0)
    exit_bundle = build_exit_risk_breakdown(px, prev_close, quality_bundle.get('intraday_inputs', {}))
    exit_risk = exit_bundle.get('score')
    exit_penalty = max(0.0, ((exit_risk if exit_risk is not None else 50.0) - 50.0) * 0.22)
    final_score = round(momentum * 0.35 + quality * 0.25 + trend * 0.20 + stability * 0.20 - exit_penalty, 2)

    # ---- Low-confidence sanity rule -------------------------------------
    # When 2 or more of the four algorithm ratings are dead (score == 0 or
    # marked degraded), the composite is misleading.  Clamp the final
    # score to no more than the highest "live" rating, force D tier, and
    # surface a human-readable reason on the row.
    rating_scores = {'momentum': momentum, 'quality': quality, 'trend': trend, 'stability': stability}
    degraded = [name for name, s in rating_scores.items() if safe_float(s) <= 5.0]
    audit = _confidence_audit(px, prev_close, quality_bundle.get('intraday_inputs', {}))
    score_explanation = None
    if degraded and len(degraded) >= 2:
        live_scores = [s for name, s in rating_scores.items() if name not in degraded]
        live_cap = max(live_scores) if live_scores else 0.0
        # Clamp composite to at most the strongest live rating, but never higher
        # than 30 so it visibly sits in D-tier territory.
        clamped = min(safe_float(final_score), live_cap, 30.0)
        score_explanation = (
            f"Low-confidence composite: {len(degraded)} of 4 algorithm ratings "
            f"have no usable input ({', '.join(degraded)}). "
            f"Missing market shape: {', '.join(audit['missing']) or 'unknown'}. "
            f"Final score capped at {clamped:.2f} and tier forced to D until "
            f"a full intraday snapshot is available."
        )
        final_score = round(clamped, 2)
        forced_tier = 'D'
    else:
        forced_tier = None
    return {
        'weights': {'momentum': 0.35, 'quality': 0.25, 'trend': 0.20, 'stability': 0.20},
        'ratings': {
            'momentum': {'score': round(momentum, 2), 'rating': score_to_rating(momentum)},
            'quality': {'score': round(quality, 2), 'rating': score_to_rating(quality), 'components': quality_bundle.get('components', {}), 'weights': quality_bundle.get('weights', {})},
            'trend': {'score': round(trend, 2), 'rating': score_to_rating(trend)},
            'stability': {'score': round(stability, 2), 'rating': score_to_rating(stability), 'components': stability_bundle.get('components', {}), 'weights': stability_bundle.get('weights', {}), 'movement_penalty_points': stability_bundle.get('movement_penalty_points', 0.0)},
            'exit_risk': {'score': round(exit_risk, 2) if exit_risk is not None else None, 'rating': exit_bundle.get('rating', 'Unknown'), 'exit_flag': exit_bundle.get('exit_flag', 'unknown'), 'components': exit_bundle.get('components', {}), 'data_ready': exit_bundle.get('data_ready', False)},
        },
        'market': {
            'last_price': round(px, 4),
            'previous_close': round(prev_close, 4),
            'change_pct': round(change_pct, 4),
            'age_seconds': age_seconds,
            'source': source,
            'provider_note': provider_note or ''
        },
        'intraday_quality': quality_bundle.get('intraday_inputs', {}),
        'stability_model': {
            'components': stability_bundle.get('components', {}),
            'weights': stability_bundle.get('weights', {}),
            'movement_penalty_points': stability_bundle.get('movement_penalty_points', 0.0),
            'inputs': stability_bundle.get('inputs', {}),
        },
        'quality_model': {
            'components': quality_bundle.get('components', {}),
            'weights': quality_bundle.get('weights', {}),
        },
        'exit_model': exit_bundle,
        'exit_penalty': round(exit_penalty, 2),
        'final_score': final_score,
        'tier': forced_tier or classify_tier(final_score),
        'direction': classify_direction(change_pct),
        'confidence_audit': audit,
        'score_explanation': score_explanation,
    }

def _compute_context_families(symbol: str, px: float, prev_close: float,
                              fundamentals_info: dict | None, daily_hist,
                              options_payload: dict | None, market_kind: str):
    """Short selling pressure + predicted volume intensity + expiration context."""
    from app.services.short_pressure_service import (
        compute_short_selling_pressure, UNAVAILABLE_SHORT_PRESSURE,
    )
    from app.services.volume_intensity_service import (
        compute_predicted_volume_intensity, UNAVAILABLE_VOLUME_INTENSITY,
    )
    from app.services.expiration_service import (
        compute_expiration_context, UNAVAILABLE_EXPIRATION_CONTEXT,
    )
    try:
        exp_ctx = compute_expiration_context(symbol, options_payload, market=market_kind)
    except Exception:  # noqa: BLE001
        exp_ctx = dict(UNAVAILABLE_EXPIRATION_CONTEXT)
    try:
        ssp = compute_short_selling_pressure(symbol, px, prev_close,
                                             fundamentals_info, daily_hist, options_payload)
    except Exception:  # noqa: BLE001
        ssp = dict(UNAVAILABLE_SHORT_PRESSURE)
    try:
        pvi = compute_predicted_volume_intensity(symbol, px, fundamentals_info,
                                                 daily_hist, options_payload,
                                                 exp_ctx.get('days_to_expiration'))
    except Exception:  # noqa: BLE001
        pvi = dict(UNAVAILABLE_VOLUME_INTENSITY)
    return ssp, pvi, exp_ctx


def _context_flat_fields(ssp: dict, pvi: dict, exp_ctx: dict) -> dict:
    """First-class serialized scanner-context fields (additive contract)."""
    return {
        'short_selling_pressure_score': ssp.get('score', 50.0),
        'short_selling_pressure_label': ssp.get('label', 'neutral'),
        'short_selling_pressure_source': ssp.get('source', 'unavailable'),
        'predicted_volume_intensity_score': pvi.get('score', 0.0),
        'predicted_volume_intensity_bucket': pvi.get('bucket', 'low'),
        'predicted_volume_event_flag': bool(pvi.get('event_flag')),
        'nearest_options_expiration': exp_ctx.get('nearest_expiration'),
        'days_to_options_expiration': exp_ctx.get('days_to_expiration'),
        'expiration_risk_flag': bool(exp_ctx.get('risk_flag')),
        'future_forecast_ready': False,
        'future_forecast_summary': None,
    }


def score_from_prices(row: dict, px: float, prev_close: float, source: str, age_seconds: int, as_of_utc: str, provider_note: str | None = None, fundamentals_info: dict | None = None, use_real_options: bool = False, hist=None, daily_hist=None, reg_index_snapshot: dict | None = None, score_depth: str = 'full') -> dict:
    """Score a single symbol from price + auxiliary data.

    Phase 26.18 / Tier 3.3: `score_depth` gates the expensive secondary
    factor families.

      * `'full'` (default): compute the full 7-family extended composite,
        factor narratives, and predictive-consensus modifier — the
        original behavior.
      * `'cheap'`: skip `compute_extended_factors`, the 7-family blend,
        the predictive modifier, and `build_factor_narratives`. The
        returned row still has a final_score / tier / direction (derived
        from the core M/Q/T/S composite) and is correctly shaped for
        ranking, but its `factor_breakdown.market` carries only the
        cheaply-computable bits. Used as Pass 1 of the two-pass scoring
        loop in `score_symbol_rows` — symbols that don't make the top
        cut never pay the full extended-factor cost.

    The regulatory-signal nudge runs in BOTH paths so cheap rows still
    benefit from insider activity bumps when present.
    """
    from app.services.extended_factors import compute_extended_factors
    # Phase 10: backfill averageVolume from daily_hist when the live provider
    # snapshot doesn't carry one (notably crypto, where yfinance.fast_info
    # exposes no avg-vol and CoinGecko is rate-limited).  Using the 30-day
    # rolling daily average is the standard interpretation of "average
    # volume" and lets the quality model + confidence audit stop flagging
    # crypto rows as missing inputs.
    fundamentals_info = dict(fundamentals_info or {})
    if safe_float(fundamentals_info.get('averageVolume')) <= 0 and daily_hist is not None:
        try:
            vols = daily_hist['Volume'].dropna().astype(float) if 'Volume' in daily_hist else None
            if vols is not None and len(vols) > 0:
                window = min(30, len(vols))
                avg_vol = float(vols.tail(window).mean())
                if avg_vol > 0:
                    fundamentals_info['averageVolume'] = avg_vol
                    if not fundamentals_info.get('averageVolume10days'):
                        fundamentals_info['averageVolume10days'] = float(vols.tail(min(10, len(vols))).mean())
        except Exception:
            pass
    breakdown = build_algorithm_breakdown(px, prev_close, source, age_seconds, provider_note, build_quality_breakdown(fundamentals_info or {}))
    # Phase 26.18 / Tier 3.3: short-circuit the expensive secondary-factor
    # pipeline when running Pass 1 of the two-pass loop. We still emit a
    # well-shaped row (with the core M/Q/T/S composite, market metadata,
    # tier/direction) so the caller can rank by `final_score` and decide
    # whether the symbol earns a Pass 2 (full-depth) re-score. The factor
    # families and narratives that Pass 1 skips are added back when Pass
    # 2 re-invokes this function with score_depth='full'.
    symbol = row.get('symbol', '') or ''
    market_kind = 'crypto' if str(symbol).upper().endswith('-USD') else 'stocks'

    if score_depth == 'cheap':
        # No extended_factors / narratives / 7-family blend / predictive
        # modifier. The core composite IS the final_score on this path.
        # `factor_breakdown.market` gets only the cheap fields the
        # algorithm-breakdown already produced (source, prices, etc.) so
        # downstream renderers don't NPE.
        market_block = breakdown.get('market') or {}
        market_block.setdefault('depth', 'cheap')
        # Phase 26.20: trend_volume_delta is genuinely cheap to compute
        # (just change_pct + volume + avg_volume — values that are already
        # in the quote dict, no daily history required). Computing it
        # inline here means cheap-pass rows carry a real TVD score and
        # don't get counted as "trend_volume_delta warming" by the
        # defaulted-fields telemetry. This is what drives the "factor
        # coverage" reading on the main dashboard: previously most rows
        # spent their entire Pass 1 lifetime with TVD defaulted, dragging
        # coverage down to ~30%. With this fix the worst-defaulted family
        # becomes one of the genuinely-extended ones (institutional /
        # options) and coverage climbs into the 80%+ range.
        try:
            _info = fundamentals_info or {}
            _vol = safe_float(_info.get('volume'))
            _avgvol = safe_float(_info.get('averageVolume'))
            _chg_pct = ((px - prev_close) / prev_close * 100.0) if prev_close else 0.0
            _tvd_pct = trend_volume_delta_pct(_chg_pct, _vol, _avgvol)
            market_block['trend_volume_delta'] = {
                'score': round(safe_float(trend_volume_delta_score(_tvd_pct), 50.0), 2),
                'bias': 'bullish' if _tvd_pct > 0 else 'bearish' if _tvd_pct < 0 else 'neutral',
                'bucket': trend_volume_delta_bucket(_tvd_pct),
                'delta_pct': round(_tvd_pct, 2),
                'status': 'implemented',
                'provenance': 'derived',
            }
        except Exception:
            # If anything goes sideways the row still passes through
            # ensure_nested_payloads, which will default the family —
            # exactly the previous behavior. No log line.
            pass
        breakdown['market'] = market_block
        breakdown.setdefault('secondary_composite', {
            'extended_avg': None,
            'core_final': float(breakdown.get('final_score', 0.0) or 0.0),
            'blended_pre_modifier': None,
            'predictive_modifier': 0.0,
            'blended_final': float(breakdown.get('final_score', 0.0) or 0.0),
            'weight': 0.0,
            'family_scores': {},
            'modifier_notes': ['pass-1 cheap scoring; full extended families deferred'],
            'depth': 'cheap',
        })
        # Apply regulatory delta even on cheap pass — it's just a dict
        # lookup + small final_score nudge and reflects high-confidence
        # insider activity the user wants to see in the ranking.
        try:
            if reg_index_snapshot is not None:
                from app.regulatory.services.signal_service import get_signal_sync_from as _reg_get_signal_from
                reg_sig = _reg_get_signal_from(reg_index_snapshot, symbol)
            else:
                from app.regulatory.services.signal_service import get_signal_sync as _reg_get_signal
                reg_sig = _reg_get_signal(symbol)
            if reg_sig and reg_sig.get('weight', 0) > 0 and abs(reg_sig.get('score_delta', 0)) > 0.05:
                delta = float(reg_sig['score_delta']) * float(reg_sig['weight'])
                old_score = float(breakdown['final_score'])
                new_score = max(0.0, min(100.0, old_score + delta))
                breakdown['final_score'] = round(new_score, 2)
                breakdown['tier'] = classify_tier(breakdown['final_score'])
                # Stash a minimal regulatory payload so the bucket-eviction
                # path can still protect this row from cap-driven eviction
                # before Pass 2 runs.
                breakdown.setdefault('market', {})['regulatory_signal'] = {
                    'applied_delta': round(delta, 2),
                    'direction': 'up' if delta > 0 else 'down' if delta < 0 else 'flat',
                    'event_count': reg_sig.get('event_count', 0),
                    'top_role_weight': reg_sig.get('top_role_weight', 0.0),
                }
        except Exception:
            pass
        # Scanner-context families (proxy-grade on the cheap pass so every
        # row carries sortable/filterable fields from Pass 1 onward).
        try:
            _ssp_c, _pvi_c, _exp_c = _compute_context_families(
                symbol, px, prev_close, fundamentals_info, daily_hist, None, market_kind)
            market_block['short_selling_pressure'] = _ssp_c
            market_block['predicted_volume_intensity'] = _pvi_c
            market_block['options_expiration'] = _exp_c
            _ctx_flat_cheap = _context_flat_fields(_ssp_c, _pvi_c, _exp_c)
        except Exception:  # noqa: BLE001
            _ctx_flat_cheap = {}
        return {
            **_ctx_flat_cheap,
            'symbol': symbol,
            'name': row.get('name', ''),
            'exchange': row.get('exchange', ''),
            'final_score': breakdown['final_score'],
            'tier': breakdown.get('tier') or classify_tier(float(breakdown['final_score'])),
            'final_direction': breakdown['direction'],
            'resolution_label': '1D',
            'factor_breakdown': breakdown,
            'as_of_utc': as_of_utc,
            'age_seconds': age_seconds,
            'freshness_label': freshness_label_from_age(age_seconds),
            'stale': age_seconds > settings.cache_ttl_seconds,
            'data_source': source,
            'preview_only': False,
            'state': 'ready_pass1',
            'score_explanation': breakdown.get('score_explanation'),
            'confidence_audit': breakdown.get('confidence_audit'),
            '_score_depth': 'cheap',
        }

    # ---- score_depth == 'full' (original code path below) ----
    families = compute_extended_factors(
        symbol=symbol,
        last_price=px,
        prev_close=prev_close,
        info=fundamentals_info or {},
        market=market_kind,
        use_real_options=use_real_options,
        hist=hist,
        daily_hist=daily_hist,
    )
    market_block = breakdown.get('market') or {}
    # Extract the reaction_map family (it isn't keyed as a normal family in
    # `families` — it lives under `market.reaction_map`).  Add it as a
    # synthetic family entry so the equal-blend math treats it like a peer.
    families_7 = dict(families)  # shallow copy
    rmap = families.get('reaction_map') or {}
    if isinstance(rmap, dict):
        # Translate the reaction-clustering classification into a 0-100 score
        # so it can blend with the others.
        rc_class = (rmap.get('classification') or 'NEUTRAL').upper()
        propel_p = float(rmap.get('propel_probability', 0) or 0)
        reject_p = float(rmap.get('reject_probability', 0) or 0)
        chop_p   = float(rmap.get('chop_probability', 0) or 0)
        if rc_class == 'PROPEL':
            rc_score = 50.0 + propel_p * 50.0    # 50-100
        elif rc_class == 'REJECT':
            rc_score = 50.0 - reject_p * 50.0    # 0-50
        elif rc_class == 'CHOP':
            rc_score = 50.0 - chop_p * 10.0      # mild penalty for chop
        else:
            rc_score = 50.0
        # Replace `reaction_map` entry with a normalized 'reaction_clustering' family.
        families_7.pop('reaction_map', None)
        families_7['reaction_clustering'] = {
            'score': round(rc_score, 2),
            'status': rmap.get('status') or 'unavailable',
            'classification': rc_class,
            'bias': ('bullish' if rc_class == 'PROPEL' else
                     'bearish' if rc_class == 'REJECT' else 'neutral'),
            'dominant_probability': max(propel_p, reject_p, chop_p),
        }
    market_block.update(families)  # write all original family payloads back
    market_block['reaction_clustering'] = families_7.get('reaction_clustering', {})
    breakdown['market'] = market_block

    # Phase 15: build per-factor narratives.  Each family payload gets
    # `cell_text` (1-line popover for the table pills) and `detail_text` +
    # `prediction` (multi-line description for the detail panel).  Done
    # AFTER the family payloads are merged into market_block so the
    # narrative generator sees the final shape including any modulation
    # fields like `expected_reaction`.
    try:
        from app.services.factor_narratives import build_factor_narratives
        narratives = build_factor_narratives(families_7)
        for fam_key, narr in narratives.items():
            fam = market_block.get(fam_key)
            if isinstance(fam, dict):
                fam['narrative'] = narr
        breakdown['factor_narratives'] = narratives
    except Exception as exc:  # noqa: BLE001
        log.debug('factor narratives failed: %s', exc)

    # ---------- 7-family equal-weight composite blend ----------
    # All seven factor families contribute equally to extended_avg.  The
    # core M/Q/T/S composite still carries 80% weight; the extended blend
    # carries 20%.  Then a predictive-consensus modifier (capped at +/-5)
    # acknowledges/depreciates predictive families when they agree or
    # contradict the rest of the consensus.
    try:
        family_order = [
            'trend_volume_delta', 'institutional_confluence',
            'options_positioning', 'institutional_order_block',
            'dark_pool_proxy', 'volume_sentiment', 'reaction_clustering',
        ]
        extended_scores = []
        family_score_breakdown = {}
        for name in family_order:
            fam = families_7.get(name) or {}
            s = float(fam.get('score', 50.0) or 50.0)
            extended_scores.append(s)
            family_score_breakdown[name] = round(s, 2)
        extended_avg = sum(extended_scores) / len(extended_scores) if extended_scores else 50.0
        core_final = float(breakdown.get('final_score', 0.0))
        blended = round(core_final * 0.80 + extended_avg * 0.20, 2)

        # ---- predictive-consensus modifier ----
        mod_value, mod_notes = _predictive_consensus_modifier(families_7)
        blended_with_mod = blended + mod_value

        if breakdown.get('score_explanation'):
            # Low-confidence row: never let the blend push above the cap.
            blended_with_mod = min(blended_with_mod, core_final)
        # Final guardrail clamp to [0, 100].
        blended_with_mod = max(0.0, min(100.0, blended_with_mod))

        breakdown['final_score'] = round(blended_with_mod, 2)
        # Re-classify tier from the new composite UNLESS the sanity rule
        # already forced D.
        if not breakdown.get('score_explanation'):
            breakdown['tier'] = classify_tier(blended_with_mod)
        breakdown['secondary_composite'] = {
            'extended_avg': round(extended_avg, 2),
            'core_final': round(core_final, 2),
            'blended_pre_modifier': blended,
            'predictive_modifier': round(mod_value, 2),
            'blended_final': round(blended_with_mod, 2),
            'weight': 0.20,
            'family_scores': family_score_breakdown,
            'modifier_notes': mod_notes,
        }
    except Exception as exc:  # noqa: BLE001
        log.debug('extended composite blend failed: %s', exc)

    # ---------- Extended sanity-rule: flag missing factor families ----------
    # When any of the 7 families is unavailable / insufficient_history /
    # symbol_unavailable, append a note to score_explanation so the user
    # knows the composite is incomplete.  This does not by itself force
    # a tier change — the core rule already covers the most severe case.
    try:
        pending_statuses = {
            'insufficient_history', 'unavailable', 'symbol_unavailable',
            'no_expirations', 'options_unavailable',
        }
        # Phase 25: families that are STRUCTURALLY n/a for this market type
        # (e.g. options_positioning for spot crypto, volume_sentiment for
        # crypto since there's no consolidated tape) are tracked
        # separately.  They never count as "warming" and never trigger
        # the low-confidence clamp, but we DO mention them in the score
        # explanation so the user understands why the composite uses
        # fewer families for crypto rows.
        warming: list[str] = []
        not_applicable: list[str] = []
        for name in (
            'trend_volume_delta', 'institutional_confluence',
            'options_positioning', 'institutional_order_block',
            'dark_pool_proxy', 'volume_sentiment', 'reaction_clustering',
        ):
            fam = families_7.get(name) or {}
            st = str(fam.get('status') or '').lower()
            if st == 'not_applicable':
                not_applicable.append(name)
            elif st in pending_statuses:
                warming.append(name)
        notes: list[str] = []
        if warming:
            notes.append(
                f'{len(warming)} of 7 factor families still warming '
                f'({", ".join(warming)}); composite reflects only the live families.'
            )
        if not_applicable:
            notes.append(
                f'{len(not_applicable)} factor families not applicable to this market '
                f'({", ".join(not_applicable)}); composite reweights across the remaining live families.'
            )
        if notes:
            existing = breakdown.get('score_explanation') or ''
            warm_note = ' '.join(notes)
            breakdown['score_explanation'] = (existing + ' ' + warm_note).strip() if existing else warm_note
            # If 4+ families missing AND they're warming (not just n/a),
            # hard-clamp tier to C/D so the row cannot mislead the user
            # with a high composite.
            if len(warming) >= 4 and not breakdown.get('score_explanation', '').startswith('Low-confidence composite'):
                composite_now = float(breakdown.get('final_score', 0.0))
                if composite_now > 60.0:
                    breakdown['final_score'] = round(min(composite_now, 60.0), 2)
                    breakdown['tier'] = classify_tier(breakdown['final_score'])
    except Exception:  # noqa: BLE001
        pass

    # -----------------------------------------------------------------------
    # Scanner-context integration: short selling pressure, predicted volume
    # intensity and options-expiration proximity.  Weighted rank integration:
    # PVI is a mild lift toward likely high-volume names, short pressure is
    # directional (squeeze-bullish vs bearish-dominant) scaled by confidence,
    # and expiration proximity acts as a contextual amplifier — never a
    # standalone score driver.  Total effect clamped to ±3 pts so no single
    # context factor can dominate the broader composite alignment.
    # -----------------------------------------------------------------------
    _ctx_flat: dict = {}
    try:
        ssp, pvi, exp_ctx = _compute_context_families(
            symbol, px, prev_close, fundamentals_info, daily_hist,
            families.get('options_positioning'), market_kind)
        market_block['short_selling_pressure'] = ssp
        market_block['predicted_volume_intensity'] = pvi
        market_block['options_expiration'] = exp_ctx
        _ctx_flat = _context_flat_fields(ssp, pvi, exp_ctx)
        pvi_adj = max(-1.0, min(2.0, (float(pvi.get('score', 0.0) or 0.0) - 50.0) / 50.0 * 2.0))
        conf_w = {'high': 1.0, 'medium': 0.7, 'low': 0.4}.get(ssp.get('confidence'), 0.4)
        ssp_label = ssp.get('label')
        if ssp_label in ('squeeze_risk_bullish', 'elevated_squeeze_watch'):
            ssp_adj = 1.0 * conf_w
        elif ssp_label == 'bearish_pressure':
            ssp_adj = -1.0 * conf_w
        elif ssp_label == 'elevated':
            ssp_adj = -0.4 * conf_w
        else:
            ssp_adj = 0.0
        ctx_adj = pvi_adj + ssp_adj
        if exp_ctx.get('high_sensitivity_window'):
            ctx_adj *= 1.25
        ctx_adj = max(-3.0, min(3.0, ctx_adj))
        if abs(ctx_adj) >= 0.05 and not breakdown.get('score_explanation'):
            new_final = max(0.0, min(100.0, float(breakdown['final_score']) + ctx_adj))
            breakdown['final_score'] = round(new_final, 2)
            breakdown['tier'] = classify_tier(new_final)
        sc_block = breakdown.get('secondary_composite')
        if isinstance(sc_block, dict):
            sc_block['context_adjustment'] = round(ctx_adj, 2)
            sc_block['context_components'] = {
                'predicted_volume_intensity': round(pvi_adj, 2),
                'short_selling_pressure': round(ssp_adj, 2),
                'expiration_amplifier': bool(exp_ctx.get('high_sensitivity_window')),
            }
    except Exception as exc:  # noqa: BLE001
        log.debug('scanner context metrics failed for %s: %s', symbol, exc)

    # -----------------------------------------------------------------------
    # Regulatory signal nudge — see /app/app/regulatory/services/signal_service.py.
    # Per spec: large insider BUY → small positive bump; insider SELL → small
    # negative bump; staleness >5 days disregarded; signal halved if no fresh
    # confirming activity within decay window.
    # We apply at most ±signal_max_boost points (default ±8) to the composite,
    # weighted by the per-signal confidence (0..1).
    # -----------------------------------------------------------------------
    try:
        # Phase 26.16 / Tier 2.2: prefer the caller-supplied index snapshot
        # to avoid forcing the GIL to bounce on every per-symbol lookup
        # inside a scoring batch. Falls back to the global lookup when the
        # caller didn't pass one (manual single-symbol refreshes etc.).
        if reg_index_snapshot is not None:
            from app.regulatory.services.signal_service import get_signal_sync_from as _reg_get_signal_from
            reg_sig = _reg_get_signal_from(reg_index_snapshot, row.get('symbol', ''))
        else:
            from app.regulatory.services.signal_service import get_signal_sync as _reg_get_signal
            reg_sig = _reg_get_signal(row.get('symbol', ''))
        if reg_sig and reg_sig.get('weight', 0) > 0 and abs(reg_sig.get('score_delta', 0)) > 0.05:
            delta = float(reg_sig['score_delta']) * float(reg_sig['weight'])
            old_score = float(breakdown['final_score'])
            new_score = max(0.0, min(100.0, old_score + delta))
            breakdown['final_score'] = round(new_score, 2)
            breakdown['tier'] = classify_tier(breakdown['final_score'])
            # Annotate the breakdown so the UI can show the user WHY the
            # composite moved.
            breakdown.setdefault('market', {})['regulatory_signal'] = {
                'applied_delta': round(delta, 2),
                'raw_score_delta': reg_sig['score_delta'],
                'weight': reg_sig['weight'],
                'reason': reg_sig.get('reason'),
                'staleness_days': reg_sig.get('staleness_days'),
                'event_count': reg_sig.get('event_count', 0),
                'insider_event_count': reg_sig.get('insider_event_count', 0),
                'award_event_count': reg_sig.get('award_event_count', 0),
                'cluster_bonus': reg_sig.get('cluster_bonus', 0.0),
                'bull_cluster_count': reg_sig.get('bull_cluster_count', 0),
                'bear_cluster_count': reg_sig.get('bear_cluster_count', 0),
                'top_role_weight': reg_sig.get('top_role_weight', 0.0),
                # Phase 26.13: aggregate cluster-window notional so the UI
                # can render the FULL dollar exposure, not just the most-
                # recent row's number.
                'aggregate_notional': reg_sig.get('aggregate_notional', 0.0),
                'signed_aggregate_notional': reg_sig.get('signed_aggregate_notional', 0.0),
                'cluster_event_count': reg_sig.get('cluster_event_count', 0),
                'direction': 'up' if delta > 0 else 'down' if delta < 0 else 'flat',
            }
            # Surface a one-liner in the score_explanation so users see it
            # without having to open the factor panel.
            direction_word = 'boosted' if delta > 0 else 'dampened'
            note = (
                f"Regulatory signal {direction_word} score by {delta:+.2f} pts "
                f"({reg_sig.get('reason', '')})."
            )
            existing = breakdown.get('score_explanation') or ''
            breakdown['score_explanation'] = (existing + ' ' + note).strip() if existing else note
    except Exception:  # noqa: BLE001
        # Scoring MUST NEVER break because of the regulatory subsystem.
        pass

    return {
        **_ctx_flat,
        'symbol': row.get('symbol', ''),
        'name': row.get('name', ''),
        'exchange': row.get('exchange', ''),
        'final_score': breakdown['final_score'],
        'tier': breakdown.get('tier') or classify_tier(float(breakdown['final_score'])),
        'final_direction': breakdown['direction'],
        'resolution_label': '1D',
        'factor_breakdown': breakdown,
        'as_of_utc': as_of_utc,
        'age_seconds': age_seconds,
        'freshness_label': freshness_label_from_age(age_seconds),
        'stale': age_seconds > settings.cache_ttl_seconds,
        'data_source': source,
        'preview_only': False,
        'state': 'ready',
        'score_explanation': breakdown.get('score_explanation'),
        'confidence_audit': breakdown.get('confidence_audit'),
        '_score_depth': 'full',
    }

def merge_market_shape(primary: dict | None = None, secondary: dict | None = None, tertiary: dict | None = None) -> dict:
    primary = primary or {}
    secondary = secondary or {}
    tertiary = tertiary or {}
    merged = {}
    for key in ['shortName', 'exchange', 'previousClose', 'open', 'dayLow', 'dayHigh', 'currentPrice', 'regularMarketPrice', 'volume', 'regularMarketVolume', 'averageVolume', 'averageVolume10days', 'marketCap', 'bid', 'ask']:
        for source in (primary, secondary, tertiary):
            value = source.get(key)
            if value not in (None, '', 0):
                merged[key] = value
                break
            if key not in merged:
                merged[key] = value or 0
    return merged


def fetch_intraday_shape(symbol: str, market: str = 'stocks') -> dict:
    """Quick intraday snapshot used by the scoring batch.

    Returns open/high/low/close/volume/marketCap shape ONLY.  Heavy `.info`
    metadata (sector, industry, PE, dividends, etc.) is intentionally NOT
    fetched here — it's slow (1-3 s round-trip), redundant for scoring,
    and is fetched on-demand by the detail view via `fetch_fundamentals`.

    Provider cascade (crypto):
        1) yfinance fast_info        — primary, lowest-latency
        2) CoinGecko snapshot        — supplies OHLC + total_volume + market_cap
        3) yfinance 1d/5m bars       — last-resort for open/high/low
        4) CryptoCompare pricemultifull — Phase 11 hardening: covers the
            ~rank-500+ tail that yfinance has no data for
        5) 90-day daily-history cache last bar — final shape fallback so the
            row leaves the scanner with state=ready instead of degraded

    For stocks the same call still goes yfinance-only; the additional crypto
    fallbacks are gated on `market == 'crypto'`.
    """
    if not symbol:
        return {}
    shape = {}
    try:
        if provider_budget_allowance(1):
            warm_provider_session()
            ticker = yf.Ticker(symbol)
            fast = getattr(ticker, 'fast_info', None) or {}
            shape.update({
                'currentPrice': safe_float(getattr(fast, 'last_price', None) if hasattr(fast, 'last_price') else fast.get('lastPrice') if isinstance(fast, dict) else None),
                'previousClose': safe_float(getattr(fast, 'previous_close', None) if hasattr(fast, 'previous_close') else fast.get('previousClose') if isinstance(fast, dict) else None),
                'open': safe_float(getattr(fast, 'open', None) if hasattr(fast, 'open') else fast.get('open') if isinstance(fast, dict) else None),
                'dayLow': safe_float(getattr(fast, 'day_low', None) if hasattr(fast, 'day_low') else fast.get('dayLow') if isinstance(fast, dict) else None),
                'dayHigh': safe_float(getattr(fast, 'day_high', None) if hasattr(fast, 'day_high') else fast.get('dayHigh') if isinstance(fast, dict) else None),
                'volume': safe_float(getattr(fast, 'last_volume', None) if hasattr(fast, 'last_volume') else fast.get('lastVolume') if isinstance(fast, dict) else None),
                'marketCap': safe_float(getattr(fast, 'market_cap', None) if hasattr(fast, 'market_cap') else fast.get('marketCap') if isinstance(fast, dict) else None),
            })
    except Exception as exc:
        mark_provider_failure(str(exc))
    # Crypto path still needs the CoinGecko snapshot for OHLC etc.
    crypto_info = fetch_coingecko_snapshot(symbol) if market == 'crypto' else {}
    hist_shape = {}
    # Only pull 1d/5m bars if we still don't have open/dayLow/dayHigh from
    # fast_info — saves a Yahoo round-trip on the happy path.
    needs_hist = not (shape.get('open') and shape.get('dayLow') and shape.get('dayHigh'))
    if needs_hist:
        try:
            if provider_budget_allowance(1):
                hist = yf.Ticker(symbol).history(period='1d', interval='5m', auto_adjust=False)
                if hist is not None and not getattr(hist, 'empty', True):
                    opens = hist['Open'].dropna() if 'Open' in hist else []
                    highs = hist['High'].dropna() if 'High' in hist else []
                    lows = hist['Low'].dropna() if 'Low' in hist else []
                    closes = hist['Close'].dropna() if 'Close' in hist else []
                    vols = hist['Volume'].dropna() if 'Volume' in hist else []
                    hist_shape = {
                        'open': safe_float(opens.iloc[0]) if len(opens) else 0,
                        'dayHigh': safe_float(highs.max()) if len(highs) else 0,
                        'dayLow': safe_float(lows.min()) if len(lows) else 0,
                        'currentPrice': safe_float(closes.iloc[-1]) if len(closes) else 0,
                        'previousClose': safe_float(closes.iloc[0]) if len(closes) else 0,
                        'volume': safe_float(vols.iloc[-1]) if len(vols) else 0,
                    }
        except Exception as exc:
            mark_provider_failure(str(exc))
    # `info` is no longer fetched here — pass an empty dict to keep the
    # downstream merge contract intact.  Detail view re-runs the full
    # fetch_fundamentals call on-demand.
    merged = merge_market_shape(shape, crypto_info, {})
    merged = merged | hist_shape if hist_shape else merged

    # ---- Phase 11: crypto provider hardening ----------------------------
    # The merged shape above is built from yfinance fast_info + CoinGecko +
    # yfinance 1d/5m bars.  For the ~rank-500+ tail (low-cap coins) Yahoo
    # often has no listing at all and CoinGecko's free tier is usually
    # rate-limited mid-batch, leaving `last_price = 0` and the row will be
    # rejected by the scoring path as `state=degraded`.  Try CryptoCompare
    # (no auth, generous limits) and then the cached daily history as final
    # fallbacks so every coin in the universe ends the scan with real data.
    if market == 'crypto' and safe_float(merged.get('currentPrice')) <= 0:
        try:
            from app.services.providers.cryptocompare_provider import fetch as _cc_fetch
            cc = _cc_fetch([symbol], 'crypto') or {}
            row = cc.get(symbol) or {}
            if safe_float(row.get('last_price')) > 0:
                # Translate CryptoCompare's snake_case row into the camelCase
                # `merge_market_shape` schema so downstream code sees a normal
                # provider response.
                cc_shape = {
                    'currentPrice': safe_float(row.get('last_price')),
                    'regularMarketPrice': safe_float(row.get('last_price')),
                    'previousClose': safe_float(row.get('previous_close') or row.get('open') or row.get('last_price')),
                    'open': safe_float(row.get('open') or row.get('previous_close') or row.get('last_price')),
                    'dayLow': safe_float(row.get('day_low') or row.get('low_24h') or row.get('last_price')),
                    'dayHigh': safe_float(row.get('day_high') or row.get('high_24h') or row.get('last_price')),
                    'volume': safe_float(row.get('volume')),
                    'regularMarketVolume': safe_float(row.get('volume')),
                    'averageVolume': safe_float(row.get('volume')),
                    'marketCap': safe_float(row.get('market_cap')),
                }
                # Only overlay non-zero values so we don't clobber an earlier
                # partial CoinGecko/yfinance hit.
                for k, v in cc_shape.items():
                    if v and not safe_float(merged.get(k)):
                        merged[k] = v
        except Exception:
            pass

    # Final fallback: synthesise an intraday shape from the cached daily
    # history.  We already pull 90 daily bars per scanned symbol so this is
    # zero additional network cost.  Using yesterday's close as last_price
    # is obviously approximate, but it lets the row score as `stale-ok`
    # instead of being dropped as degraded with empty factor families.
    if market == 'crypto' and safe_float(merged.get('currentPrice')) <= 0:
        try:
            from app.services.daily_history_service import get_daily_history
            dh = get_daily_history(symbol, allow_fetch=False)
            if dh is not None and not getattr(dh, 'empty', True):
                last = dh.iloc[-1]
                prev = dh.iloc[-2] if len(dh) > 1 else last
                vols = dh['Volume'].dropna().astype(float) if 'Volume' in dh else None
                avg_vol = float(vols.tail(30).mean()) if vols is not None and len(vols) > 0 else 0.0
                merged.update({
                    'currentPrice': safe_float(last.get('Close')),
                    'regularMarketPrice': safe_float(last.get('Close')),
                    'previousClose': safe_float(prev.get('Close')),
                    'open': safe_float(last.get('Open')),
                    'dayLow': safe_float(last.get('Low')),
                    'dayHigh': safe_float(last.get('High')),
                    'volume': safe_float(last.get('Volume')),
                    'regularMarketVolume': safe_float(last.get('Volume')),
                    'averageVolume': avg_vol,
                })
        except Exception:
            pass

    return merged

def fetch_live_snapshot(symbol: str, market: str = 'stocks') -> dict:
    """Force-live single-symbol quote fetch.

    Phase 22 critical fix: previously this function ONLY tried yfinance
    fast_info + 1m history.  That works OK for stocks but crypto symbols
    (DOGE-USD, BTC-USD, ...) frequently return zero/None from yfinance,
    leaving the manual-refresh button stuck on the failure path even
    though the CoinGecko / CryptoCompare / CoinPaprika cascade would
    serve the quote immediately.

    Now uses the same `download_quotes()` multi-provider cascade as the
    batch scoring path, then falls back to direct yfinance only when
    the cascade returns nothing.  Honors the provider budget and
    captures provenance on the returned shape.
    """
    if not symbol:
        return {}
    captured_at = utcnowiso()
    if not provider_budget_allowance(1):
        return {}

    # First attempt: the canonical cascade for the requested market.
    # For crypto:  yfinance/coingecko -> cryptocompare -> coinpaprika
    # For stocks:  yfinance -> yahoo-chart -> finnhub -> stooq
    try:
        cascade_result = download_quotes([symbol], market)
        live = cascade_result.get(symbol) if cascade_result else None
        if live and safe_float(live.get('last_price')) > 0:
            # Stamp provenance so the UI badge is honest.
            return {
                **live,
                'captured_at_utc': live.get('captured_at_utc') or captured_at,
                'source': live.get('source')
                          or ('coingecko' if market == 'crypto' else 'yfinance'),
                'provider_outcome': 'live_success',
                'preview_only': False,
            }
    except Exception as exc:
        mark_provider_failure(str(exc))

    # Fallback A: yfinance fast_info (kept from the original implementation
    # so single-symbol Yahoo direct hits still work when the cascade is
    # rate-limited).
    try:
        warm_provider_session()
        ticker = yf.Ticker(symbol)
        fast = getattr(ticker, 'fast_info', None) or {}
        last_price = safe_float(getattr(fast, 'last_price', None) if hasattr(fast, 'last_price') else fast.get('lastPrice') if isinstance(fast, dict) else None)
        prev_close = safe_float(getattr(fast, 'previous_close', None) if hasattr(fast, 'previous_close') else fast.get('previousClose') if isinstance(fast, dict) else None)
        open_price = safe_float(getattr(fast, 'open', None) if hasattr(fast, 'open') else fast.get('open') if isinstance(fast, dict) else None)
        day_low = safe_float(getattr(fast, 'day_low', None) if hasattr(fast, 'day_low') else fast.get('dayLow') if isinstance(fast, dict) else None)
        day_high = safe_float(getattr(fast, 'day_high', None) if hasattr(fast, 'day_high') else fast.get('dayHigh') if isinstance(fast, dict) else None)
        volume = safe_float(getattr(fast, 'last_volume', None) if hasattr(fast, 'last_volume') else fast.get('lastVolume') if isinstance(fast, dict) else None)
        market_cap = safe_float(getattr(fast, 'market_cap', None) if hasattr(fast, 'market_cap') else fast.get('marketCap') if isinstance(fast, dict) else None)
        if last_price > 0 and prev_close > 0:
            return {
                'last_price': last_price,
                'previous_close': prev_close,
                'open': open_price,
                'day_low': day_low,
                'day_high': day_high,
                'volume': volume,
                'market_cap': market_cap,
                'captured_at_utc': captured_at,
                'source': 'yfinance-fast',
                'provider_outcome': 'live_success',
                'preview_only': False,
            }
    except Exception as exc:
        mark_provider_failure(str(exc))

    # Fallback B: yfinance 1m intraday history.
    try:
        hist = yf.Ticker(symbol).history(period='1d', interval='1m', auto_adjust=False)
        if hist is not None and not getattr(hist, 'empty', True):
            close_series = hist['Close'].dropna()
            if len(close_series) >= 1:
                last_price = safe_float(close_series.iloc[-1])
                prev_close = safe_float(close_series.iloc[0])
                inferred_prev = prev_close if prev_close > 0 else last_price
                preview_only = market == 'crypto' and (prev_close <= 0 or inferred_prev == last_price)
                return {
                    'last_price': last_price,
                    'previous_close': inferred_prev,
                    'open': safe_float(hist['Open'].dropna().iloc[0]) if 'Open' in hist and len(hist['Open'].dropna()) else 0,
                    'day_low': safe_float(hist['Low'].dropna().min()) if 'Low' in hist else 0,
                    'day_high': safe_float(hist['High'].dropna().max()) if 'High' in hist else 0,
                    'volume': safe_float(hist['Volume'].dropna().iloc[-1]) if 'Volume' in hist and len(hist['Volume'].dropna()) else 0,
                    'market_cap': 0,
                    'captured_at_utc': captured_at,
                    'source': 'yfinance-1m',
                    'provider_outcome': 'preview_fallback' if preview_only else 'live_success',
                    'preview_only': preview_only,
                }
    except Exception as exc:
        mark_provider_failure(str(exc))

    # Fallback C: for crypto only — direct CoinGecko snapshot.  Used when
    # the cascade returned empty AND yfinance had nothing.  Last line of
    # defence for the major coins (BTC, ETH, DOGE, ...) before we give up.
    if market == 'crypto':
        try:
            cg = fetch_coingecko_snapshot(symbol)
            cg_px = safe_float(cg.get('currentPrice') or cg.get('regularMarketPrice'))
            cg_prev = safe_float(cg.get('previousClose'))
            if cg_px > 0:
                return {
                    'last_price': cg_px,
                    'previous_close': cg_prev if cg_prev > 0 else cg_px,
                    'open': safe_float(cg.get('open')) or cg_prev or cg_px,
                    'day_low': safe_float(cg.get('dayLow')),
                    'day_high': safe_float(cg.get('dayHigh')),
                    'volume': safe_float(cg.get('volume')) or safe_float(cg.get('averageVolume')),
                    'market_cap': safe_float(cg.get('marketCap')),
                    'captured_at_utc': captured_at,
                    'source': 'coingecko-direct',
                    'provider_outcome': 'live_success' if cg_prev > 0 else 'preview_fallback',
                    'preview_only': cg_prev <= 0,
                }
        except Exception as exc:
            mark_provider_failure(str(exc))

    return {}

def download_quotes_yfinance_only(symbols: List[str], market: str = 'stocks') -> Dict[str, dict]:
    """Internal: legacy yfinance-only batch downloader used as one provider
    inside the multi-provider chain.  Crypto rows are served via coingecko."""
    result = {}
    if not symbols:
        return result
    if market == 'crypto':
        snapshots = fetch_coingecko_snapshots(symbols)
        captured_at = utcnowiso()
        for sym, cg in snapshots.items():
            if cg.get('currentPrice'):
                result[sym] = {
                    'last_price': safe_float(cg.get('currentPrice')),
                    'previous_close': safe_float(cg.get('previousClose') or cg.get('currentPrice')),
                    'open': safe_float(cg.get('open') or cg.get('previousClose') or cg.get('currentPrice')),
                    'day_low': safe_float(cg.get('dayLow')),
                    'day_high': safe_float(cg.get('dayHigh')),
                    'volume': safe_float(cg.get('volume') or cg.get('regularMarketVolume')),
                    'market_cap': safe_float(cg.get('marketCap')),
                    'captured_at_utc': captured_at,
                    'source': 'coingecko',
                    'provider_outcome': 'live_success',
                    'preview_only': False,
                }
        if result:
            return result
    if not provider_budget_allowance(max(1, min(len(symbols), 5))):
        return result
    warm_provider_session()
    joined = ' '.join(symbols)
    # Phase 26.34: hard wall-clock ceiling around yf.download to keep
    # yfinance's internal thread pool from wedging the entire snap-worker
    # at 0 % CPU when Yahoo rate-limits.  See comment block above
    # `_yf_download_with_timeout`.
    data = _yf_download_with_timeout(
        tickers=joined,
        period='5d',
        interval='1d',
        group_by='ticker',
        auto_adjust=False,
        progress=False,
        threads=True,
        timeout=settings.provider_timeout_seconds,
        _market_hint=market,
    )
    if data is None:
        # The wrapper already logged the timeout / error.  Mark a
        # provider failure so the upstream cascade can record it.
        mark_provider_failure('yf.download_timeout_or_error')
        return result
    if data is None or getattr(data, 'empty', True):
        return result
    captured_at = utcnowiso()
    if len(symbols) == 1:
        sym = symbols[0]
        try:
            close_series = data['Close'].dropna()
            open_series = data.get('Open', pd.Series(dtype=float)).dropna()
            high_series = data.get('High', pd.Series(dtype=float)).dropna()
            low_series = data.get('Low', pd.Series(dtype=float)).dropna()
            vol_series = data.get('Volume', pd.Series(dtype=float)).dropna()
            if len(close_series) >= 2:
                last_price = safe_float(close_series.iloc[-1])
                previous_close = safe_float(close_series.iloc[-2])
                result[sym] = {
                    'last_price': last_price,
                    'previous_close': previous_close if previous_close > 0 else last_price,
                    'open': safe_float(open_series.iloc[-1]) if len(open_series) else 0,
                    'day_high': safe_float(high_series.iloc[-1]) if len(high_series) else 0,
                    'day_low': safe_float(low_series.iloc[-1]) if len(low_series) else 0,
                    'volume': safe_float(vol_series.iloc[-1]) if len(vol_series) else 0,
                    'captured_at_utc': captured_at, 'source': 'yfinance',
                    'provider_outcome': 'live_success', 'preview_only': False,
                }
        except Exception:
            pass
        return result
    for sym in symbols:
        try:
            frame = data[sym]
            close_series = frame['Close'].dropna()
            if len(close_series) >= 2:
                last_price = safe_float(close_series.iloc[-1])
                previous_close = safe_float(close_series.iloc[-2])
                # Pull OHLCV for today's bar straight from the batched download
                # so we don't need a follow-up per-symbol fetch_intraday_shape call.
                try:
                    open_val = safe_float(frame['Open'].dropna().iloc[-1])
                    high_val = safe_float(frame['High'].dropna().iloc[-1])
                    low_val = safe_float(frame['Low'].dropna().iloc[-1])
                    vol_val = safe_float(frame['Volume'].dropna().iloc[-1])
                except Exception:
                    open_val = high_val = low_val = vol_val = 0
                result[sym] = {
                    'last_price': last_price,
                    'previous_close': previous_close if previous_close > 0 else last_price,
                    'open': open_val, 'day_high': high_val, 'day_low': low_val, 'volume': vol_val,
                    'captured_at_utc': captured_at, 'source': 'yfinance',
                    'provider_outcome': 'live_success', 'preview_only': False,
                }
        except Exception:
            continue
    return result


def download_quotes(symbols: List[str], market: str = 'stocks') -> Dict[str, dict]:
    """Multi-provider cascade for batch quote downloads.

    Cascade order:
      stocks:  yfinance -> yahoo-chart -> stooq
      crypto:  yfinance/coingecko -> cryptocompare

    Each layer only tries the symbols the previous layers failed to resolve.
    Provenance is preserved per-row via the `source` field.
    """
    if not symbols:
        return {}
    from app.services.providers.base import run_quote_chain
    from app.services.providers import yfinance_provider, yahoo_chart_provider, stooq_provider, cryptocompare_provider, coinpaprika_provider, finnhub_provider
    if market == 'crypto':
        chain = [
            ('yfinance+coingecko', yfinance_provider.fetch),
            ('cryptocompare', cryptocompare_provider.fetch),
            # Phase 15: CoinPaprika tail layer for the rank-1000+ coins that
            # CoinGecko free-tier rate-limits and CryptoCompare doesn't carry.
            ('coinpaprika', coinpaprika_provider.fetch),
        ]
    else:
        chain = [
            ('yfinance', yfinance_provider.fetch),
            ('yahoo-chart', yahoo_chart_provider.fetch),
        ]
        # Phase 26.19: only insert Finnhub into the cascade when a key is
        # actually configured. Without a key, fetch() short-circuits to {}
        # but the chain still records the call/miss, producing a noisy
        # "IDLE with 600+ calls" row on the providers dashboard. Skipping
        # the registration keeps telemetry clean. When the user adds a
        # Finnhub key via Settings, it slots in ahead of Stooq.
        from app.services import api_keys as _api_keys
        if _api_keys.get('finnhub'):
            chain.append(('finnhub', finnhub_provider.fetch))
        # Phase 26.19: only insert Stooq when its dedicated circuit-breaker
        # is closed. When Stooq's host is unreachable from the runtime
        # network, its CB stays open for up to 1h between probes — calling
        # the (no-op) fetch() during that window still records call+miss
        # in the chain, dragging the aggregate cascade hit rate down by
        # ~25 points without recovering any actual quotes. Skipping the
        # registration entirely while the CB is open keeps the hit-rate
        # metric honest. The CB auto-half-opens after its cooldown so
        # Stooq slots back in automatically when the network recovers.
        try:
            if not stooq_provider.stats_snapshot().get('circuit_open'):
                chain.append(('stooq', stooq_provider.fetch))
        except Exception:
            # Snapshot failure should not break the cascade.
            chain.append(('stooq', stooq_provider.fetch))
    return run_quote_chain(symbols, market, chain)

def fetch_fundamentals(symbol: str, market: str = 'stocks') -> dict:
    try:
        if not provider_budget_allowance(1):
            return {}
        info = yf.Ticker(symbol).info or {}
        base = {
            'shortName': info.get('shortName') or '',
            'exchange': info.get('exchange') or '',
            'previousClose': info.get('previousClose') or 0,
            'open': info.get('open') or 0,
            'dayLow': info.get('dayLow') or 0,
            'dayHigh': info.get('dayHigh') or 0,
            'currentPrice': info.get('currentPrice') or info.get('regularMarketPrice') or 0,
            'volume': info.get('volume') or info.get('regularMarketVolume') or 0,
            'averageVolume': info.get('averageVolume') or info.get('averageVolume10days') or 0,
            'marketCap': info.get('marketCap') or 0,
            'bid': info.get('bid') or 0,
            'ask': info.get('ask') or 0,
        }
        if market == 'crypto':
            cg = fetch_coingecko_snapshot(symbol)
            return merge_market_shape(cg, base)
        return base
    except Exception as exc:
        mark_provider_failure(str(exc))
        return {}

def score_symbol_rows(rows: List[dict], force_full_pass2: bool = False) -> List[dict]:
    """Run the full scanner scoring pipeline on a list of seed rows.

    Phase 26.39 — `force_full_pass2`:
        When True, the Pass-2 candidate set is the entire input batch
        (not just the top-30 %).  This is what the leveraged-variant
        top-10 priority lane uses: it only feeds the function 10
        symbols at a time AND those 10 symbols are the leaderboard
        leaders — they MUST be fully scored every time or the detail
        panel's extended-factor cards (institutional confluence,
        options positioning, IOB, reaction clustering, volume sentiment,
        etc.) blank out the moment the priority lane upserts a cheap
        Pass-1-only row on top of a previously fully-scored row.
    """
    if not rows:
        return []
    # Determine which symbols qualify for real options-chain pulls in this pass.
    # Active-scan pool symbols (rotating top names) get the real chain; others
    # keep the inferred heuristic.  We honor the options_chain_service throttle
    # and cache so this is safe to call broadly.
    try:
        from app.services.active_scan_pool import active_scan_symbols
        real_options_set = set(active_scan_symbols() or [])
    except Exception:
        real_options_set = set()
    # Phase 26.16 / Tier 2.2: snapshot the regulatory index ONCE for this
    # whole batch. Every score_from_prices() call below reads its
    # regulatory signal from this snapshot instead of touching the
    # module-level global, which (1) eliminates cross-thread GIL bounces
    # on the global dict read and (2) guarantees the entire batch sees a
    # consistent view of the index even if a background refresh lands
    # mid-batch.
    try:
        from app.regulatory.services.signal_service import get_signal_index_snapshot
        reg_index_snapshot = get_signal_index_snapshot()
    except Exception:
        reg_index_snapshot = None

    # Phase 26.18 / Tier 3.3: two-pass scoring.
    #
    # Pass 1 runs `score_from_prices(..., score_depth='cheap')` on EVERY
    # symbol — this skips the expensive `compute_extended_factors` blob
    # plus narratives, the 7-family blend, and the predictive-consensus
    # modifier. The cheap path still yields a usable `final_score` from
    # the core M/Q/T/S composite, so we can rank.
    #
    # After Pass 1, we compute the Pass 2 set: top `MRD_FULL_SCORE_TOP_PCT`
    # symbols by score, UNION every active_scan_pool symbol (they need
    # the real options chain), UNION every row with a regulatory signal
    # (the user explicitly wants those highlighted with full detail).
    # Pass 2 re-runs full-depth scoring only on this set and replaces
    # the pass-1 rows in `output`.
    #
    # Symbols that don't make Pass 2 keep their pass-1 row but with
    # `state='ready'` and a stub `secondary_composite`; the dashboard
    # renderer treats them identically. The user only sees ~top 30% of
    # the universe on the leaderboard at any given time anyway, so the
    # missing factor families on the lower 70% are invisible.
    _TOP_PCT = max(0.0, min(100.0, float(_os.environ.get('MRD_FULL_SCORE_TOP_PCT', '30'))))
    # When the top-pct env var is set to 100 (or higher), the gate is
    # effectively disabled — every row runs Pass 2 and behavior matches
    # the legacy single-pass code.
    _TWO_PASS_ACTIVE = _TOP_PCT < 100.0 and _os.environ.get('MRD_TWO_PASS_SCORING', '1') != '0'

    output = []
    # Stash per-symbol inputs from Pass 1 so Pass 2 can re-call
    # score_from_prices without re-fetching quotes / intraday / daily
    # history.  Key = symbol; value = dict of kwargs to splat back into
    # `score_from_prices(score_depth='full', **kwargs)`.
    pass2_inputs: dict[str, dict] = {}
    pass2_indices: dict[str, int] = {}  # symbol -> index in `output`
    provider_rows = filter_supported_provider_rows(rows)
    provider_symbols = {row.get('symbol', '') for row in provider_rows if row.get('symbol')}
    try:
        symbol_to_quote = {}
        symbols = [row.get('symbol', '') for row in provider_rows if row.get('symbol')]
        stock_symbols = [s for s in symbols if not str(s).upper().endswith('-USD')]
        crypto_symbols = [s for s in symbols if str(s).upper().endswith('-USD')]
        for group in chunks(stock_symbols, settings.yfinance_chunk_size):
            symbol_to_quote.update(download_quotes(group, 'stocks'))
        for group in chunks(crypto_symbols, settings.yfinance_chunk_size):
            symbol_to_quote.update(download_quotes(group, 'crypto'))
        if symbol_to_quote:
            clear_provider_failure()
    except Exception as exc:
        mark_provider_failure(str(exc))
        symbol_to_quote = {}
    intraday_by_symbol: dict[str, dict] = {}
    # Decide which symbols actually need a separate `fetch_intraday_shape`
    # call.  download_quotes() now extracts open/high/low/volume from the
    # batched yfinance result, so if a symbol already has those fields we
    # can skip the per-symbol Yahoo round-trip entirely — that's the
    # dominant cost in the batch path.
    needs_intraday: list[str] = []
    for r in provider_rows:
        sym = r.get('symbol', '')
        if not sym:
            continue
        q = symbol_to_quote.get(sym) or {}
        if (q.get('open') and q.get('day_low') and q.get('day_high')):
            # Synthesize an intraday_by_symbol entry from the batched quote.
            intraday_by_symbol[sym] = {
                'currentPrice': q.get('last_price') or 0,
                'previousClose': q.get('previous_close') or 0,
                'open': q.get('open') or 0,
                'dayLow': q.get('day_low') or 0,
                'dayHigh': q.get('day_high') or 0,
                'volume': q.get('volume') or 0,
                'marketCap': 0,
            }
        else:
            needs_intraday.append(sym)
    # Parallel fetch only the symbols whose batched quote was incomplete.
    if needs_intraday:
        try:
            # Phase 26.32: manual pool + shutdown(wait=False) so a hung
            # provider socket cannot block the entire scoring batch
            # past as_completed's 25s deadline.
            from concurrent.futures import (
                ThreadPoolExecutor,
                as_completed,
                TimeoutError as _FuturesTimeoutError,
            )
            _intraday_pool = ThreadPoolExecutor(
                max_workers=12, thread_name_prefix='intraday',
            )
            try:
                futures = {
                    _intraday_pool.submit(
                        fetch_intraday_shape,
                        sym,
                        'crypto' if str(sym).upper().endswith('-USD') else 'stocks',
                    ): sym
                    for sym in needs_intraday
                }
                try:
                    for fut in as_completed(futures, timeout=25):
                        sym = futures[fut]
                        try:
                            intraday_by_symbol[sym] = fut.result() or {}
                        except Exception:
                            intraday_by_symbol[sym] = {}
                except _FuturesTimeoutError:
                    log.debug(
                        'intraday parallel fetch: as_completed timed out at '
                        '25s; %d/%d done', len(intraday_by_symbol),
                        len(needs_intraday),
                    )
                    # Fill any unfinished symbols with empty dicts so the
                    # rest of scoring sees a uniform shape.
                    for sym in needs_intraday:
                        intraday_by_symbol.setdefault(sym, {})
            finally:
                _intraday_pool.shutdown(wait=False)
        except Exception as exc:  # noqa: BLE001
            log.warning('intraday parallel fetch failed (%s); falling back to sequential', exc)
            for sym in needs_intraday:
                intraday_by_symbol[sym] = fetch_intraday_shape(
                    sym,
                    'crypto' if str(sym).upper().endswith('-USD') else 'stocks',
                )
    # Phase 5b: kick off daily-history prefetch for every symbol in this
    # batch up-front so the *next* time the scanner visits these tickers
    # the institutional confluence / IOB / volume sentiment / reaction
    # clustering all compute against real bars.  Calls return immediately
    # for symbols already cached; only the truly cold ones hit the network.
    #
    # Phase 10: crypto symbols (e.g. BTC-USD, ETH-USD) are ALSO included —
    # yfinance returns full 90d daily OHLCV for them, so the same factor
    # families that work for stocks now compute properly for crypto instead
    # of being stuck in the "warming / insufficient_history" fallback.
    #
    # Phase 14: BLOCK on the prefetch pool draining for THIS batch's
    # symbols before we start scoring.  Previously the prefetch was
    # fire-and-forget — by the time `score_from_prices` reads back the
    # daily_hist on the next line it would still be None for cold symbols
    # and `institutional_confluence` would return its
    # `insufficient_history` warming fallback.  That's the empty INST
    # column + low-confidence flag the user saw on every scored row.
    # With await_prefetch_for_batch the worst case is ~3 s per batch
    # (10 workers * 0.08 s throttle, 25 symbols, mostly warm) but every
    # row gets real factor scores.
    try:
        from app.services.daily_history_service import await_prefetch_for_batch
        batch_symbols = [row.get('symbol', '') for row in provider_rows if row.get('symbol')]
        await_prefetch_for_batch(batch_symbols, timeout=8.0)
    except Exception:
        pass

    # Phase 26.18 / Tier 2.3: parallel options-chain pre-warm.
    #
    # Symbols in `real_options_set` (the active_scan_pool top names) need
    # the real options chain during scoring. Without prefetch, each
    # symbol's chain is fetched SEQUENTIALLY inside the sync scoring loop
    # — that's the single biggest blocking cost per-symbol for active
    # pool tickers (CBOE: ~500-800 ms each, Yahoo fallback: up to 8 s).
    #
    # The prefetch runs N HTTP calls in parallel via a ThreadPoolExecutor
    # and warms the existing `options_chain_service._cache`. By the time
    # the scoring loop reads `get_real_options_positioning` for each
    # active symbol, the cache is populated and the call is a noop.
    #
    # Only the active_scan_pool subset triggers prefetch; all other
    # symbols use the (much cheaper) inferred heuristic path.
    if real_options_set:
        try:
            from app.services.options_chain_service import prefetch_options_chains
            sp_pairs: list[tuple[str, float]] = []
            for r in provider_rows:
                sym = (r.get('symbol') or '').upper()
                if not sym or sym not in real_options_set:
                    continue
                # Prefer the intraday currentPrice (most recent quote we have);
                # fall back to previousClose, then to fundamental hints.
                intraday = intraday_by_symbol.get(sym, {}) if isinstance(intraday_by_symbol, dict) else {}
                px = (
                    safe_float(intraday.get('currentPrice'))
                    or safe_float(intraday.get('regularMarketPrice'))
                    or safe_float(intraday.get('previousClose'))
                    or safe_float(r.get('last_price'))
                )
                if px > 0:
                    sp_pairs.append((sym, px))
            if sp_pairs:
                outcomes = prefetch_options_chains(sp_pairs)
                if outcomes:
                    hits = sum(1 for v in outcomes.values() if v == 'hit')
                    log.debug(
                        'tier 2.3 prefetch: %d/%d options chains warmed (hits=%d, cached=%d, miss/err=%d)',
                        len(outcomes), len(sp_pairs), hits,
                        sum(1 for v in outcomes.values() if v == 'cached'),
                        sum(1 for v in outcomes.values() if v in ('miss', 'error', 'timeout', 'cooldown')),
                    )
        except Exception as exc:  # noqa: BLE001
            log.debug('tier 2.3 options prefetch failed (non-fatal): %s', exc)

    for row in rows:
        symbol = row.get('symbol', '')
        intraday = intraday_by_symbol.get(symbol) or {}
        live = symbol_to_quote.get(symbol)
        use_real_options = symbol in real_options_set
        # Daily history is required for reaction clustering, volume sentiment,
        # and the IOB heuristic.  Per user requirement: every scanned stock
        # should have these metrics computed and persisted; the scanner does
        # not need to re-fetch unless the symbol is re-processed by the
        # automatic scanner OR the user clicks the manual refresh button.
        #
        # We read from the persistent cache synchronously (24h TTL, disk-
        # backed).  If the symbol has never been fetched, we kick off a
        # background prefetch so the *next* time the scanner visits this
        # symbol the history is already there — without blocking this batch.
        daily_hist = None
        sym_upper = str(symbol).upper()
        # Phase 10: pull daily history for crypto too (yfinance supports
        # BTC-USD / ETH-USD / etc.) so institutional_confluence,
        # volume_sentiment and reaction_clustering compute against real bars
        # instead of returning the "insufficient_history" warming fallback.
        if symbol:
            try:
                from app.services.daily_history_service import get_daily_history, prefetch_daily_history
                daily_hist = get_daily_history(symbol, allow_fetch=False)
                if daily_hist is None:
                    prefetch_daily_history(symbol)
            except Exception:
                daily_hist = None
        if live:
            save_quote(symbol, live)
            live_source = live.get('source') or ('coingecko' if str(symbol).upper().endswith('-USD') else 'yfinance')
            _live_as_of = live.get('captured_at_utc') or utcnowiso()
            _depth = 'cheap' if _TWO_PASS_ACTIVE else 'full'
            scored = score_from_prices(row, live.get('last_price', 0.0), live.get('previous_close', 0.0), live_source, 0, _live_as_of, fundamentals_info=intraday, use_real_options=use_real_options, daily_hist=daily_hist, reg_index_snapshot=reg_index_snapshot, score_depth=_depth)
            if intraday.get('shortName'):
                scored['name'] = intraday.get('shortName') or scored.get('name')
            if intraday.get('exchange'):
                scored['exchange'] = intraday.get('exchange') or scored.get('exchange')
            output.append(scored)
            if _TWO_PASS_ACTIVE and symbol:
                pass2_inputs[symbol] = dict(
                    row=row, px=live.get('last_price', 0.0),
                    prev_close=live.get('previous_close', 0.0),
                    source=live_source, age_seconds=0, as_of_utc=_live_as_of,
                    provider_note=None,
                    fundamentals_info=intraday, use_real_options=use_real_options,
                    daily_hist=daily_hist, reg_index_snapshot=reg_index_snapshot,
                )
                pass2_indices[symbol] = len(output) - 1
            continue
        cached = get_cached_quote(symbol)
        if cached and cached_quote_is_usable(cached):
            age = quote_age_seconds(cached)
            _cached_as_of = cached.get('captured_at_utc') or utcnowiso()
            _depth = 'cheap' if _TWO_PASS_ACTIVE else 'full'
            scored = score_from_prices(row, safe_float(cached.get('last_price')), safe_float(cached.get('previous_close')), 'cache', age, _cached_as_of, 'cached last-good quote', fundamentals_info=intraday, use_real_options=use_real_options, daily_hist=daily_hist, reg_index_snapshot=reg_index_snapshot, score_depth=_depth)
            if intraday.get('shortName'):
                scored['name'] = intraday.get('shortName') or scored.get('name')
            if intraday.get('exchange'):
                scored['exchange'] = intraday.get('exchange') or scored.get('exchange')
            output.append(scored)
            if _TWO_PASS_ACTIVE and symbol:
                pass2_inputs[symbol] = dict(
                    row=row, px=safe_float(cached.get('last_price')),
                    prev_close=safe_float(cached.get('previous_close')),
                    source='cache', age_seconds=age, as_of_utc=_cached_as_of,
                    provider_note='cached last-good quote',
                    fundamentals_info=intraday, use_real_options=use_real_options,
                    daily_hist=daily_hist, reg_index_snapshot=reg_index_snapshot,
                )
                pass2_indices[symbol] = len(output) - 1
            continue

        # Phase 11: synthesize a "stale-ok" quote from intraday/daily_hist when
        # no live provider responded and no cache exists.  This is the path that
        # rescues the crypto rank-500+ tail: yfinance has no listing,
        # CoinGecko/CryptoCompare are rate-limited, but fetch_intraday_shape's
        # CryptoCompare + daily_hist fallbacks have populated `intraday` with
        # real OHLC.  Without this branch the row would emit state=degraded
        # with empty factor families even though we have everything we need
        # to score it.
        synth_px = safe_float(intraday.get('currentPrice') or intraday.get('regularMarketPrice'))
        synth_prev = safe_float(intraday.get('previousClose'))
        if synth_px > 0 and synth_prev > 0:
            synth_source = 'cryptocompare' if str(symbol).upper().endswith('-USD') else 'yfinance'
            _synth_as_of = utcnowiso()
            _depth = 'cheap' if _TWO_PASS_ACTIVE else 'full'
            scored = score_from_prices(
                row, synth_px, synth_prev, synth_source,
                settings.cache_ttl_seconds,  # mark as stale-ish so the UI badge is honest
                _synth_as_of,
                'derived from secondary provider / daily-history fallback',
                fundamentals_info=intraday, use_real_options=use_real_options, daily_hist=daily_hist,
                reg_index_snapshot=reg_index_snapshot,
                score_depth=_depth,
            )
            scored['stale'] = True
            scored['freshness_label'] = 'stale-ok'
            scored['data_source'] = synth_source
            if intraday.get('shortName'):
                scored['name'] = intraday.get('shortName') or scored.get('name')
            if intraday.get('exchange'):
                scored['exchange'] = intraday.get('exchange') or scored.get('exchange')
            output.append(scored)
            if _TWO_PASS_ACTIVE and symbol:
                pass2_inputs[symbol] = dict(
                    row=row, px=synth_px, prev_close=synth_prev,
                    source=synth_source, age_seconds=settings.cache_ttl_seconds,
                    as_of_utc=_synth_as_of,
                    provider_note='derived from secondary provider / daily-history fallback',
                    fundamentals_info=intraday, use_real_options=use_real_options,
                    daily_hist=daily_hist, reg_index_snapshot=reg_index_snapshot,
                )
                pass2_indices[symbol] = len(output) - 1
            continue

        fallback_note = 'provider symbol filtered' if symbol not in provider_symbols else 'live quote unavailable; preview fallback'
        output.append({
            'symbol': row.get('symbol', ''),
            'name': intraday.get('shortName') or row.get('name', ''),
            'exchange': intraday.get('exchange') or row.get('exchange', 'unknown'),
            'final_score': safe_float(row.get('final_score'), 50.0) or 50.0,
            'tier': row.get('tier') or 'C',
            'final_direction': row.get('final_direction') or 'Neutral',
            'resolution_label': '1D',
            'factor_breakdown': {
                'market': {
                    'last_price': 0,
                    'previous_close': 0,
                    'change_pct': 0,
                    'age_seconds': settings.cache_max_age_seconds,
                    'source': 'preview_fallback',
                    'provider_note': fallback_note,
                },
                'fundamentals': build_quality_breakdown(intraday).get('inputs', {}) if intraday else {},
            },
            'as_of_utc': '',
            'age_seconds': settings.cache_max_age_seconds,
            'freshness_label': 'stale',
            'stale': True,
            'data_source': 'preview_fallback',
            'preview_only': True,
            'state': 'degraded',
        })

    # Phase 26.18 / Tier 3.3 — Pass 2: promote the top-K to full depth.
    #
    # Selection rules (UNION):
    #   * Top `_TOP_PCT`% of pass-1 rows by `final_score`.
    #   * Every active_scan_pool symbol — they need the real options chain.
    #   * Every row carrying a regulatory signal (any non-trivial delta).
    #     A pass-1 cheap row attaches a minimal `regulatory_signal` to its
    #     factor_breakdown.market so we can detect this without re-doing
    #     the regulatory lookup here.
    if _TWO_PASS_ACTIVE and pass2_inputs:
        # Build the candidate set.
        candidates: set[str] = set()
        # 0) Phase 26.39: when the caller explicitly asks for full-depth
        # scoring on every input (priority-lane callers do this), seed
        # the candidate set with every pass-1 symbol BEFORE applying the
        # top-K + active-scan + regulatory rules.  Those rules can still
        # add more, but they can't remove anything we've already pinned.
        if force_full_pass2:
            candidates.update(pass2_inputs.keys())
        # 1) Top-K by score.
        ranked = sorted(
            ((r.get('symbol') or '', float(r.get('final_score') or 0)) for r in output if r.get('symbol')),
            key=lambda kv: kv[1],
            reverse=True,
        )
        k = max(1, int(round(len(ranked) * _TOP_PCT / 100.0)))
        for sym, _ in ranked[:k]:
            candidates.add(sym)
        # 2) Active-scan pool — they ALWAYS get the real options chain.
        for sym in real_options_set:
            if sym in pass2_inputs:
                candidates.add(sym)
        # 3) Regulatory-flagged rows.
        for r in output:
            sym = r.get('symbol')
            if not sym or sym not in pass2_inputs:
                continue
            reg = (r.get('factor_breakdown') or {}).get('market', {}).get('regulatory_signal')
            if isinstance(reg, dict) and abs(float(reg.get('applied_delta') or 0)) >= 0.5:
                candidates.add(sym)
        # Run Pass 2 for the promoted set, replacing pass-1 rows in place.
        promoted = 0
        # Phase 26.21: identify the top-N by Pass-1 score in THIS batch.
        # Only these tickers get `use_real_options=True` forced on — that
        # keeps the real options-chain volume bounded (≤ N per batch)
        # while still guaranteeing the cascade is exercised on the rows
        # the user actually cares about, even when the historical
        # active_scan_pool doesn't intersect with the current sweep
        # universe. Larger Pass-2 promotions (top-30% by score) still
        # happen as before, but they keep their original
        # `use_real_options` flag (True only if they're in the pool) so
        # we don't hammer CBOE with low-quality small-caps that lack
        # listings.
        _MAX_FORCED_OPTIONS_PER_BATCH = int(_os.environ.get('MRD_FORCED_OPTIONS_PER_BATCH', '5'))
        top_n_force_options: set[str] = set(
            sym for sym, _ in ranked[:_MAX_FORCED_OPTIONS_PER_BATCH]
        )
        for sym in candidates:
            kwargs = pass2_inputs.get(sym)
            idx = pass2_indices.get(sym)
            if kwargs is None or idx is None:
                continue
            if sym in top_n_force_options or sym in real_options_set:
                kwargs = dict(kwargs)
                kwargs['use_real_options'] = True
            try:
                full_row = score_from_prices(score_depth='full', **kwargs)
                # Preserve the cosmetic name/exchange enrichments the
                # pass-1 row received.
                old = output[idx]
                if old.get('name'):
                    full_row.setdefault('name', old.get('name'))
                if old.get('exchange'):
                    full_row.setdefault('exchange', old.get('exchange'))
                # Preserve any stale/freshness overrides applied above.
                if old.get('stale'):
                    full_row['stale'] = True
                if old.get('freshness_label') == 'stale-ok':
                    full_row['freshness_label'] = 'stale-ok'
                output[idx] = full_row
                promoted += 1
            except Exception as exc:  # noqa: BLE001
                # Pass 2 failure is non-fatal — keep the pass-1 cheap row.
                log.warning('pass-2 score_from_prices failed for %s: %s', sym, exc)
        if promoted:
            log.debug(
                'tier 3.3 two-pass: %d/%d rows promoted to full depth (top_pct=%.1f%%)',
                promoted, len(output), _TOP_PCT,
            )

    # Phase 26.47 — Future Mode: attach the fast-tier forward-metrics
    # block (5 horizons: 1h, 5h, 1d, 5d, 20d) + the advanced-math
    # overlay (Cornish-Fisher p_up, VaR/CVaR, jump-drift, Hurst regime
    # weight, effective_kelly_rank) to every row that has enough
    # factor depth.  The per-symbol advanced bundle is memoised so
    # this stays cheap on repeated re-scores.
    #
    # Failures are non-fatal — Future Mode is purely additive on top
    # of the existing scoring pipeline.  If the attach call raises for
    # any reason, the row is returned with `forward_metrics=None` and
    # the scanner keeps working in classic mode.
    try:
        from app.services.future_mode_service import attach_forward_metrics_fast
        for r in output:
            try:
                reg_sig = (r.get('factor_breakdown') or {}).get('market', {}).get('regulatory_signal')
                # Infer market from the -USD suffix so crypto rows get
                # BTC-USD as their LCC driver instead of SPY.
                _sym = str(r.get('symbol') or '').upper()
                _mkt = 'crypto' if _sym.endswith('-USD') else 'stocks'
                attach_forward_metrics_fast(r, regulatory_signal=reg_sig, market=_mkt)
            except Exception as exc:  # noqa: BLE001
                log.debug('forward_metrics_fast attach failed for %s: %s', r.get('symbol'), exc)
                r['forward_metrics'] = None
    except Exception as exc:  # noqa: BLE001
        log.warning('future_mode_service unavailable (non-fatal): %s', exc)

    return output
