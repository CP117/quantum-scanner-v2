"""
Prediction backtest + accuracy tracker.

Two complementary backtests:

1. **Forward persistence (live tracker)**: every `/api/predict/{symbol}`
   call appends to `data/prediction_history.jsonl`. A periodic
   resolver runs through pending predictions, fetches the actual price
   `forward_days` later, and stamps the realised hit / miss / MAE.
   The aggregate stats are exposed via `/api/predict/accuracy`.

2. **Walk-forward simulation**: applies a SIMPLIFIED version of the
   prediction model to historical daily bars. Uses ATR + composite-style
   directional aggregate (since we don't store historical factor scores)
   and reports raw hit-rate on direction, MAE on the % move, and per-
   confidence-bucket precision. This gives an immediate read on the
   model's edge without waiting weeks for live predictions to mature.

Forward persistence is the AUTHORITATIVE accuracy report; the walk-
forward is a sanity-check / cold-start estimator.
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

from app.services.daily_history_service import get_daily_history

log = logging.getLogger('app.prediction_backtest')

_DATA_DIR = Path(__file__).resolve().parent.parent.parent / 'data'
_HISTORY_FILE = _DATA_DIR / 'prediction_history.jsonl'
_LOCK = Lock()


# =====================================================================
# Forward-persistence layer
# =====================================================================

def record_prediction(prediction: dict) -> None:
    """Append a prediction to the JSONL history.  Called from
    `predict_price()` after the response has been built. Never raises.

    Each line stores the minimum needed to resolve the prediction
    later (symbol, target_price, expected_pct_move, direction, captured
    timestamp, current price at capture, forward_days, confidence). The
    full reasoning isn't persisted -- that's reproducible on demand.

    Phase 26.41: `forward_hours` is persisted for sub-daily predictions
    so the resolver knows whether to grade against an hourly bar close
    (intraday horizon) or a daily bar close (legacy horizon).
    """
    if not prediction or prediction.get('status') != 'ok':
        return
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        row = {
            'captured_at_utc': datetime.now(timezone.utc).isoformat(),
            'symbol': prediction.get('symbol'),
            'forward_days': prediction.get('forward_days'),
            'forward_hours': prediction.get('forward_hours'),
            'horizon_label': prediction.get('horizon_label'),
            'horizon_unit_label': prediction.get('horizon_unit_label'),
            'current_price': prediction.get('current_price'),
            'target_price': prediction.get('target_price'),
            'expected_pct_move': prediction.get('expected_pct_move'),
            'direction': prediction.get('direction'),
            'composite_direction': prediction.get('composite_direction'),
            'confidence': prediction.get('confidence'),
            'agreement_pct': prediction.get('agreement_pct'),
            'atr_pct': prediction.get('atr_pct'),
            'resolved': False,
        }
        with _LOCK:
            with _HISTORY_FILE.open('a', encoding='utf-8') as f:
                f.write(json.dumps(row) + '\n')
    except Exception as exc:  # noqa: BLE001
        log.debug('record_prediction failed: %s', exc)


def _load_history() -> list[dict]:
    if not _HISTORY_FILE.exists():
        return []
    rows: list[dict] = []
    with _LOCK:
        try:
            with _HISTORY_FILE.open('r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        pass
        except Exception:
            return []
    return rows


def _save_history(rows: list[dict]) -> None:
    if not rows:
        return
    tmp = _HISTORY_FILE.with_suffix('.jsonl.tmp')
    with _LOCK:
        with tmp.open('w', encoding='utf-8') as f:
            for r in rows:
                f.write(json.dumps(r) + '\n')
        os.replace(tmp, _HISTORY_FILE)


def resolve_pending(now_utc: datetime | None = None) -> dict[str, int]:
    """Walk through the prediction history and resolve any prediction
    whose `forward_days` window has now elapsed.

    Resolution = pull the latest cached daily close, compute the actual
    % move since capture, compare to the prediction. Stamps `resolved=true`
    + `actual_pct_move` + `direction_hit` + `abs_error_pct` on the row.

    Returns a small dict of counts: {`resolved_now`, `pending`, `total`}.
    Idempotent: re-running over the same history is a no-op for already-
    resolved rows.
    """
    rows = _load_history()
    if not rows:
        return {'resolved_now': 0, 'pending': 0, 'total': 0}
    now = now_utc or datetime.now(timezone.utc)
    resolved_now = 0
    pending = 0
    changed = False
    for row in rows:
        if row.get('resolved'):
            continue
        try:
            captured = datetime.fromisoformat(row['captured_at_utc'].replace('Z', '+00:00'))
        except Exception:
            row['resolved'] = True
            row['resolve_error'] = 'bad_timestamp'
            changed = True
            continue
        elapsed_days = (now - captured).total_seconds() / 86400.0
        # Phase 26.41: sub-daily horizon resolution.  When the row was
        # captured with `forward_hours`, grade it against the price
        # `forward_hours` trading-hours after capture.  We approximate
        # by walking the 1-hour-equivalent boundary on the most-recent
        # 5-day intraday history.  If the intraday history isn't
        # available yet (still inside the horizon, weekend, etc.) we
        # leave the row pending and retry on the next sweep.
        fwd_hours = row.get('forward_hours')
        if fwd_hours:
            fwd_hours = int(fwd_hours)
            # ~6.5 trading hours/day; require at least that many real
            # hours to have passed (calendar) BEFORE attempting
            # intraday resolution, so weekends don't trigger a false
            # "not enough history" loop.
            elapsed_hours_real = (now - captured).total_seconds() / 3600.0
            if elapsed_hours_real < float(fwd_hours):
                pending += 1
                continue
            sym = row.get('symbol') or ''
            try:
                import yfinance as yf  # noqa: WPS433
                hist = yf.Ticker(sym).history(period='5d', interval='5m', auto_adjust=False)
            except Exception:
                pending += 1
                continue
            if hist is None or getattr(hist, 'empty', True) or len(hist) < 12:
                pending += 1
                continue
            try:
                # Find first 5-min bar >= captured, then walk forward
                # `fwd_hours * 12` bars (12 five-min bars per hour).
                target_bar_offset = int(fwd_hours) * 12
                idx = hist.index
                start_pos = None
                for i, ts in enumerate(idx):
                    try:
                        ts_dt = ts.to_pydatetime() if hasattr(ts, 'to_pydatetime') else ts
                        # Some yfinance frames return tz-aware timestamps;
                        # normalise to UTC for the comparison.
                        if ts_dt.tzinfo is None:
                            ts_dt = ts_dt.replace(tzinfo=timezone.utc)
                    except Exception:
                        continue
                    if ts_dt >= captured:
                        start_pos = i
                        break
                if start_pos is None or start_pos + target_bar_offset >= len(idx):
                    pending += 1
                    continue
                actual_close = float(hist['Close'].iloc[start_pos + target_bar_offset])
                captured_price = float(row.get('current_price') or 0.0)
                if captured_price <= 0:
                    row['resolved'] = True
                    row['resolve_error'] = 'no_captured_price'
                    changed = True
                    continue
                actual_pct = (actual_close - captured_price) / captured_price * 100.0
                predicted_pct = float(row.get('expected_pct_move') or 0.0)
                row['actual_close'] = round(actual_close, 4)
                row['actual_pct_move'] = round(actual_pct, 3)
                row['abs_error_pct'] = round(abs(actual_pct - predicted_pct), 3)
                row['signed_error_pct'] = round(actual_pct - predicted_pct, 3)
                # Tighter direction threshold for intraday (matches
                # predict_price's neutral_threshold=0.15%).
                pred_dir = 1 if predicted_pct > 0.15 else (-1 if predicted_pct < -0.15 else 0)
                actual_dir = 1 if actual_pct > 0.15 else (-1 if actual_pct < -0.15 else 0)
                row['direction_hit'] = bool(pred_dir == actual_dir)
                row['direction_hit_loose'] = bool(pred_dir == actual_dir or pred_dir == 0 or actual_dir == 0)
                row['resolved'] = True
                row['resolved_at_utc'] = now.isoformat()
                resolved_now += 1
                changed = True
            except Exception as exc:  # noqa: BLE001
                log.debug('resolve_pending intraday %s failed: %s', row.get('symbol'), exc)
                pending += 1
            continue

        fwd = float(row.get('forward_days') or 10)
        # Use TRADING-day elapsed estimate (calendar * 5/7). If we
        # haven't hit the target window yet, leave pending.
        trading_elapsed = elapsed_days * (5.0 / 7.0)
        if trading_elapsed < fwd:
            pending += 1
            continue
        # Resolve.
        sym = row.get('symbol') or ''
        try:
            df = get_daily_history(sym, allow_fetch=False, blocking=False)
        except Exception:
            df = None
        if df is None or getattr(df, 'empty', True) or len(df) < 2:
            pending += 1
            continue
        # Pick the close that corresponds to `forward_days` AFTER capture.
        # We approximate by walking the daily-history index for the bar
        # whose timestamp is the first one ≥ captured + forward_days
        # trading sessions. If we don't have enough bars yet, keep
        # pending.
        try:
            idx = df.index
            # find the position where the bar time >= captured.
            from_pos = None
            for i, ts in enumerate(idx):
                try:
                    ts_dt = ts.to_pydatetime().replace(tzinfo=timezone.utc) if hasattr(ts, 'to_pydatetime') else ts
                except Exception:
                    continue
                if ts_dt >= captured:
                    from_pos = i
                    break
            if from_pos is None or from_pos + int(fwd) >= len(idx):
                pending += 1
                continue
            target_pos = from_pos + int(fwd)
            actual_close = float(df['Close'].iloc[target_pos])
            captured_price = float(row.get('current_price') or 0.0)
            if captured_price <= 0:
                row['resolved'] = True
                row['resolve_error'] = 'no_captured_price'
                changed = True
                continue
            actual_pct = (actual_close - captured_price) / captured_price * 100.0
            predicted_pct = float(row.get('expected_pct_move') or 0.0)
            row['actual_close'] = round(actual_close, 4)
            row['actual_pct_move'] = round(actual_pct, 3)
            row['abs_error_pct'] = round(abs(actual_pct - predicted_pct), 3)
            row['signed_error_pct'] = round(actual_pct - predicted_pct, 3)
            # Direction hit: same sign for predicted and actual,
            # treating |move| < 0.5% as "neutral" (loose tie band).
            pred_dir = 1 if predicted_pct > 0.5 else (-1 if predicted_pct < -0.5 else 0)
            actual_dir = 1 if actual_pct > 0.5 else (-1 if actual_pct < -0.5 else 0)
            row['direction_hit'] = bool(pred_dir == actual_dir)
            row['direction_hit_loose'] = bool(pred_dir == actual_dir or pred_dir == 0 or actual_dir == 0)
            row['resolved'] = True
            row['resolved_at_utc'] = now.isoformat()
            resolved_now += 1
            changed = True
        except Exception as exc:  # noqa: BLE001
            log.debug('resolve_pending %s failed: %s', sym, exc)
            pending += 1
            continue
    if changed:
        _save_history(rows)
    return {'resolved_now': resolved_now, 'pending': pending, 'total': len(rows)}


def accuracy_stats() -> dict[str, Any]:
    """Aggregate stats over every resolved prediction in the history.

    Returns hit rate, MAE, per-confidence-bucket precision, and a small
    sample of recent resolved predictions for the UI to show. Never
    raises.
    """
    rows = _load_history()
    resolved = [r for r in rows if r.get('resolved') and 'direction_hit' in r]
    if not resolved:
        return {
            'status': 'empty',
            'total_predictions': len(rows),
            'resolved_predictions': 0,
            'hint': 'Run /api/predict on a few symbols, wait 10+ trading days, then call /api/predict/accuracy.',
        }
    n = len(resolved)
    hits = sum(1 for r in resolved if r.get('direction_hit'))
    mae = sum(float(r.get('abs_error_pct') or 0) for r in resolved) / n
    me = sum(float(r.get('signed_error_pct') or 0) for r in resolved) / n
    # Confidence buckets
    buckets = {'low (<25%)': [], 'mid (25-50%)': [], 'high (50-75%)': [], 'very_high (75%+)': []}
    for r in resolved:
        c = float(r.get('confidence') or 0)
        key = 'low (<25%)' if c < 25 else ('mid (25-50%)' if c < 50 else ('high (50-75%)' if c < 75 else 'very_high (75%+)'))
        buckets[key].append(r)
    per_bucket: dict[str, dict] = {}
    for key, items in buckets.items():
        if not items:
            per_bucket[key] = {'count': 0, 'hit_rate': None, 'mae': None}
            continue
        b_hits = sum(1 for r in items if r.get('direction_hit'))
        b_mae = sum(float(r.get('abs_error_pct') or 0) for r in items) / len(items)
        per_bucket[key] = {
            'count': len(items),
            'hit_rate': round(b_hits / len(items), 4),
            'mae': round(b_mae, 3),
        }
    recent = sorted(resolved, key=lambda r: r.get('resolved_at_utc') or '', reverse=True)[:25]
    return {
        'status': 'ok',
        'total_predictions': len(rows),
        'resolved_predictions': n,
        'pending_predictions': len(rows) - n,
        'hit_rate': round(hits / n, 4),
        'mae_pct': round(mae, 3),
        'mean_error_pct': round(me, 3),  # signed - tells us if model is biased
        'per_confidence_bucket': per_bucket,
        'recent_resolved': [
            {
                'captured_at_utc': r.get('captured_at_utc'),
                'symbol': r.get('symbol'),
                'direction': r.get('direction'),
                'predicted_pct': r.get('expected_pct_move'),
                'actual_pct': r.get('actual_pct_move'),
                'abs_error': r.get('abs_error_pct'),
                'hit': r.get('direction_hit'),
                'confidence': r.get('confidence'),
            }
            for r in recent
        ],
    }


# =====================================================================
# Walk-forward simulation (cold-start estimator)
# =====================================================================

def walk_forward_estimate(symbol: str, lookback: int = 250, forward_days: int = 10) -> dict[str, Any]:
    """Simulate the prediction model on historical bars.

    NOTE: we don't have historical factor-family scores (those would
    require a full snapshot replay), so this is necessarily simplified.
    Approach: at each bar B in the lookback, derive a directional read
    from:
      - 5-day momentum (recent return)
      - 14-day ATR
      - distance to 20-day SMA
    Combine into a directional estimate, project 10 days forward
    using the same ATR-based drift formula. Compare to actual.

    Reports direction hit-rate + MAE + per-confidence-bucket stats.
    Treats this as a 'naive composite' baseline: any improvement the
    live factor-driven model has over this is genuine edge.
    """
    sym = (symbol or '').strip().upper()
    if not sym:
        return {'status': 'unavailable', 'reason': 'empty_symbol'}
    df = get_daily_history(sym, allow_fetch=False, blocking=False)
    if df is None or getattr(df, 'empty', True):
        return {'status': 'unavailable', 'reason': 'no_history', 'symbol': sym}
    df = df.tail(lookback)
    if len(df) < 30 + forward_days:
        return {
            'status': 'unavailable', 'reason': 'insufficient_bars',
            'symbol': sym, 'bars_available': len(df),
        }
    closes = df['Close'].astype(float).tolist()
    highs = df['High'].astype(float).tolist()
    lows = df['Low'].astype(float).tolist()
    n = len(closes)
    predictions: list[dict] = []
    hits = 0
    abs_err_sum = 0.0
    for i in range(20, n - forward_days):
        # Naive directional indicator
        ret_5d = (closes[i] - closes[i - 5]) / closes[i - 5] * 100.0
        sma_20 = sum(closes[i - 19:i + 1]) / 20.0
        dist_sma = (closes[i] - sma_20) / sma_20 * 100.0
        # Combined directional read (-1 to +1)
        direction_score = 0.6 * (ret_5d / 5.0) + 0.4 * (dist_sma / 5.0)
        direction_score = max(-1.0, min(1.0, direction_score))
        sign = 1 if direction_score > 0.1 else (-1 if direction_score < -0.1 else 0)
        # ATR estimate over last 14 bars
        trs = []
        for j in range(max(1, i - 13), i + 1):
            trs.append(max(highs[j] - lows[j], abs(highs[j] - closes[j - 1]), abs(lows[j] - closes[j - 1])))
        atr_pct = (sum(trs) / len(trs)) / closes[i] * 100.0
        # Projection
        predicted_pct = sign * abs(direction_score) * (0.5 * atr_pct) * forward_days
        predicted_pct = max(-25.0, min(25.0, predicted_pct))
        actual_close = closes[i + forward_days]
        actual_pct = (actual_close - closes[i]) / closes[i] * 100.0
        pred_dir = 1 if predicted_pct > 0.5 else (-1 if predicted_pct < -0.5 else 0)
        act_dir = 1 if actual_pct > 0.5 else (-1 if actual_pct < -0.5 else 0)
        hit = pred_dir == act_dir
        if hit:
            hits += 1
        abs_err_sum += abs(actual_pct - predicted_pct)
        predictions.append({
            'i': i, 'pred_pct': round(predicted_pct, 2),
            'act_pct': round(actual_pct, 2), 'hit': hit,
        })
    total = len(predictions)
    if total == 0:
        return {'status': 'unavailable', 'reason': 'no_walk_forward', 'symbol': sym}
    return {
        'status': 'ok',
        'symbol': sym,
        'lookback_bars': lookback,
        'forward_days': forward_days,
        'total_predictions': total,
        'hit_rate': round(hits / total, 4),
        'mae_pct': round(abs_err_sum / total, 3),
        'sample': predictions[-20:],  # last 20 predictions for inspection
        'note': (
            "Cold-start baseline using 5d momentum + 20d SMA distance "
            "in place of full factor breakdown. The LIVE forward-"
            "persistence model (/api/predict/accuracy) is the "
            "authoritative read once it has resolved predictions."
        ),
    }
