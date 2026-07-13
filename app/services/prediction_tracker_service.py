"""
Prediction Tracker — user-saved prediction history + auto-evaluation.

Phase 22: user can press "Save prediction" on the detail panel after a
10-day forecast is generated.  Each saved prediction is persisted with:
  - symbol + market
  - anchor_price       (price at time of save)
  - target_price       (forecast 10-day target)
  - direction          ('bull' / 'bear' / 'neutral')
  - confidence_pct     (band width inverted; 100% = no uncertainty)
  - expires_at         (anchor + N trading days)
  - notes              (free-form user input, optional)
  - created_at         (UTC timestamp)
  - full_payload       (JSON snapshot of the prediction details for audit)

When `expires_at` passes, an auto-evaluation pass fetches the actual
close price on/after the expiration date and computes:
  - actual_close       (close on expiration date)
  - error_pct          ((actual - target) / anchor)
  - directional_hit    (direction matched the realized sign of return)
  - magnitude_hit      (actual fell inside the 95% confidence band)
  - status             ('open' -> 'evaluated' -> 'unresolved' if no data)

Storage
-------
Single SQLite DB at `data/saved_predictions.db` (separate from the
regulatory db so deleting one doesn't affect the other).  WAL mode + a
UNIQUE(symbol, created_at) index so the user can save the same symbol
multiple times without collisions but each row is uniquely keyed.

Public API (all sync; called from FastAPI route handlers via run_in_executor)
-------------------------------------------------------------------------
  save_prediction(payload) -> dict   (the freshly inserted row)
  list_predictions(market=None, status=None, limit=500) -> list[dict]
  delete_prediction(prediction_id) -> bool
  evaluate_expired_predictions() -> dict[stats]
  accuracy_stats(market=None) -> dict[summary]
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger('app.prediction_tracker')

_DB_PATH = Path(__file__).resolve().parent.parent.parent / 'data' / 'saved_predictions.db'
_DB_LOCK = threading.RLock()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().isoformat()


def _conn() -> sqlite3.Connection:
    """Return a fresh SQLite connection.  Always close in a `with` or finally.

    WAL mode lets multiple readers (the UI page polling for fresh
    evaluation results) coexist with the single writer (the auto-eval
    background task) without blocking.
    """
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # Phase 26.8: explicit busy_timeout pragma in addition to the
    # connect-time `timeout` so concurrent readers / Windows AV scanners
    # never trigger a hard "database is locked" failure on a write.
    conn.execute('PRAGMA busy_timeout = 10000')
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    return conn


def init_db() -> None:
    """Idempotent schema init.  Safe to call on every startup."""
    with _DB_LOCK, _conn() as c:
        c.execute('''
            CREATE TABLE IF NOT EXISTS saved_predictions (
                id              TEXT PRIMARY KEY,
                symbol          TEXT NOT NULL,
                market          TEXT NOT NULL,
                anchor_price    REAL NOT NULL,
                target_price    REAL NOT NULL,
                direction       TEXT NOT NULL,
                confidence_pct  REAL,
                forward_days    INTEGER NOT NULL DEFAULT 10,
                expires_at      TEXT NOT NULL,
                notes           TEXT,
                created_at      TEXT NOT NULL,
                full_payload    TEXT,
                status          TEXT NOT NULL DEFAULT 'open',
                actual_close    REAL,
                error_pct       REAL,
                directional_hit INTEGER,
                magnitude_hit   INTEGER,
                evaluated_at    TEXT,
                source          TEXT NOT NULL DEFAULT 'user'
            )
        ''')
        # Migration for DBs created before the `source` column existed --
        # CREATE TABLE IF NOT EXISTS above won't add it to an existing
        # table, so add it explicitly and tolerate "duplicate column"
        # if it's already there.
        try:
            c.execute("ALTER TABLE saved_predictions ADD COLUMN source TEXT NOT NULL DEFAULT 'user'")
        except sqlite3.OperationalError as exc:
            if 'duplicate column' not in str(exc).lower():
                raise
        c.execute(
            'CREATE INDEX IF NOT EXISTS ix_pred_symbol ON saved_predictions(symbol)'
        )
        c.execute(
            'CREATE INDEX IF NOT EXISTS ix_pred_expires ON saved_predictions(expires_at)'
        )
        c.execute(
            'CREATE INDEX IF NOT EXISTS ix_pred_status ON saved_predictions(status)'
        )
        c.execute(
            'CREATE INDEX IF NOT EXISTS ix_pred_source ON saved_predictions(source)'
        )
    log.info('prediction_tracker: schema ready at %s', _DB_PATH)


def save_prediction(payload: dict[str, Any]) -> dict[str, Any]:
    """Insert a new saved prediction.

    `payload` must include `symbol`, `anchor_price`, `target_price`,
    `direction`, `forward_days`.  Other fields are optional; we fill
    sensible defaults.  Returns the inserted row as a dict.

    Raises ValueError on malformed input so the route handler can return
    400.  Never raises sqlite3 errors — those are caught + logged and
    re-raised as RuntimeError for consistent client handling.
    """
    if not payload:
        raise ValueError('empty payload')
    sym = (payload.get('symbol') or '').strip().upper()
    if not sym:
        raise ValueError('symbol required')
    market = (payload.get('market') or 'stocks').lower()
    if market not in ('stocks', 'crypto'):
        market = 'stocks'

    try:
        anchor = float(payload.get('anchor_price') or payload.get('current_price') or 0)
        target = float(payload.get('target_price') or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'invalid price field: {exc}') from exc
    if anchor <= 0 or target <= 0:
        raise ValueError('anchor_price and target_price must both be > 0')

    direction = (payload.get('direction') or '').lower()
    if direction not in ('bull', 'bear', 'neutral'):
        # Derive from sign of (target - anchor) if not explicit.
        delta = target - anchor
        direction = 'bull' if delta > 0 else ('bear' if delta < 0 else 'neutral')

    forward_days = int(payload.get('forward_days') or 10)
    confidence_pct = payload.get('confidence_pct')
    confidence_pct = float(confidence_pct) if confidence_pct is not None else None

    created_at = _utcnow()
    expires_dt = created_at + timedelta(days=forward_days)
    pred_id = uuid.uuid4().hex

    # Persist the full prediction payload so the tracker page can show
    # full audit context (factor contributions, narrative, etc.) without
    # re-running the model.
    full_payload_json = ''
    try:
        full_payload_json = json.dumps(
            payload.get('full_payload') or payload,
            default=str,
        )
    except Exception as exc:
        log.debug('prediction_tracker: payload json failed: %s', exc)
        full_payload_json = '{}'

    notes = (payload.get('notes') or '').strip()[:1000]  # cap to 1k chars
    source = (payload.get('source') or 'user').strip().lower()
    if source not in ('user', 'auto_scan'):
        source = 'user'

    with _DB_LOCK, _conn() as c:
        try:
            c.execute('''
                INSERT INTO saved_predictions
                  (id, symbol, market, anchor_price, target_price, direction,
                   confidence_pct, forward_days, expires_at, notes,
                   created_at, full_payload, status, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
            ''', (
                pred_id, sym, market, anchor, target, direction,
                confidence_pct, forward_days, expires_dt.isoformat(), notes,
                created_at.isoformat(), full_payload_json, source,
            ))
        except sqlite3.Error as exc:
            log.exception('prediction_tracker: insert failed: %s', exc)
            raise RuntimeError(f'db insert failed: {exc}') from exc

    row = _get_row_by_id(pred_id)
    return row or {}


def _get_row_by_id(pred_id: str) -> Optional[dict[str, Any]]:
    with _DB_LOCK, _conn() as c:
        row = c.execute(
            'SELECT * FROM saved_predictions WHERE id = ?', (pred_id,),
        ).fetchone()
    return _row_to_dict(row) if row else None


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    if row is None:
        return {}
    d = dict(row)
    # Decode the audit payload back into structured JSON for the UI.
    if d.get('full_payload'):
        try:
            d['full_payload'] = json.loads(d['full_payload'])
        except Exception:
            d['full_payload'] = None
    # Booleanise SQLite ints for clarity in the JSON API.
    for k in ('directional_hit', 'magnitude_hit'):
        if d.get(k) is not None:
            d[k] = bool(d[k])
    return d


def list_predictions(
    market: Optional[str] = None,
    status: Optional[str] = None,
    source: Optional[str] = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Return up to `limit` rows ordered by most recent first.

    Optional filters: `market` ('stocks'|'crypto'), `status`
    ('open'|'evaluated'|'unresolved'), `source` ('user'|'auto_scan').
    """
    clauses = []
    params: list[Any] = []
    if market in ('stocks', 'crypto'):
        clauses.append('market = ?')
        params.append(market)
    if status in ('open', 'evaluated', 'unresolved'):
        clauses.append('status = ?')
        params.append(status)
    if source in ('user', 'auto_scan'):
        clauses.append('source = ?')
        params.append(source)
    where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
    sql = f'SELECT * FROM saved_predictions{where} ORDER BY created_at DESC LIMIT ?'
    params.append(int(limit or 500))
    with _DB_LOCK, _conn() as c:
        rows = c.execute(sql, tuple(params)).fetchall()
    return [_row_to_dict(r) for r in rows]


def delete_prediction(pred_id: str) -> bool:
    if not pred_id:
        return False
    with _DB_LOCK, _conn() as c:
        cur = c.execute(
            'DELETE FROM saved_predictions WHERE id = ?', (pred_id,),
        )
        return cur.rowcount > 0


def evaluate_expired_predictions() -> dict[str, int]:
    """Fetch actual close prices for every prediction whose `expires_at`
    is in the past and `status='open'`, then compute the hit/miss
    classification and store it back.

    Uses `app.services.daily_history_service.get_daily_history` so we
    reuse the cached OHLCV the rest of the scanner already pulled.  If
    daily history isn't available for the symbol (data void / delisted /
    crypto provider gap), the row is marked `status='unresolved'` and
    can be retried automatically on the next scheduler tick.

    Returns counts: {'evaluated': N, 'unresolved': N, 'still_open': N}.
    """
    from app.services.daily_history_service import get_daily_history

    stats = {'evaluated': 0, 'unresolved': 0, 'still_open': 0}
    now_iso = _utcnow_iso()

    with _DB_LOCK, _conn() as c:
        rows = c.execute(
            "SELECT * FROM saved_predictions WHERE status = 'open' AND expires_at <= ? LIMIT 500",
            (now_iso,),
        ).fetchall()

    for row in rows:
        d = dict(row)
        sym = d['symbol']
        anchor = float(d['anchor_price'] or 0)
        target = float(d['target_price'] or 0)
        direction = d['direction']
        confidence = d.get('confidence_pct')
        expires_at = d['expires_at']
        try:
            expires_dt = datetime.fromisoformat(expires_at)
        except ValueError:
            stats['unresolved'] += 1
            continue

        # Fetch the actual close at-or-after the expiration date.
        actual = None
        try:
            df = get_daily_history(sym, allow_fetch=True)
            if df is not None and not getattr(df, 'empty', True):
                # Pick the FIRST trading day whose date >= expires_at.
                # On weekends/holidays this gracefully advances to the
                # next session.  pandas DatetimeIndex tz handling can be
                # quirky so we compare ISO strings to be safe.
                target_date_str = expires_dt.date().isoformat()
                hit_idx = None
                for ts in df.index:
                    try:
                        ts_str = ts.isoformat() if hasattr(ts, 'isoformat') else str(ts)
                        if ts_str[:10] >= target_date_str:
                            hit_idx = ts
                            break
                    except Exception:
                        continue
                if hit_idx is not None:
                    actual = float(df.loc[hit_idx, 'Close'])
        except Exception as exc:
            log.debug('prediction_tracker: history fetch failed for %s: %s', sym, exc)

        if actual is None or actual <= 0:
            stats['unresolved'] += 1
            continue

        # ---- Compute hit/miss metrics ----
        # error_pct: signed % off from target (relative to anchor — gives a
        # more meaningful magnitude than (actual-target)/target when the
        # forecast is far from anchor).
        error_pct = ((actual - target) / anchor) * 100.0 if anchor > 0 else 0.0
        actual_return = ((actual - anchor) / anchor) * 100.0 if anchor > 0 else 0.0

        # Directional hit: did the actual return match the predicted sign?
        if direction == 'bull':
            directional_hit = actual_return > 0
        elif direction == 'bear':
            directional_hit = actual_return < 0
        else:  # neutral
            directional_hit = abs(actual_return) < 2.0  # ±2% window for neutral calls

        # Magnitude hit: did the actual land inside the 95% confidence band?
        # We don't store the band directly — derive it from confidence_pct,
        # which is `100 - 2σ_pct` per the predict_price formula.  When
        # confidence is unknown, fall back to a generous ±10% window.
        if confidence is not None and confidence > 0:
            two_sigma_pct = max(0.5, 100.0 - float(confidence))
        else:
            two_sigma_pct = 10.0
        magnitude_hit = abs(error_pct) <= two_sigma_pct

        with _DB_LOCK, _conn() as c:
            c.execute('''
                UPDATE saved_predictions
                   SET status = 'evaluated',
                       actual_close = ?,
                       error_pct = ?,
                       directional_hit = ?,
                       magnitude_hit = ?,
                       evaluated_at = ?
                 WHERE id = ?
            ''', (
                round(actual, 6),
                round(error_pct, 4),
                1 if directional_hit else 0,
                1 if magnitude_hit else 0,
                _utcnow_iso(),
                d['id'],
            ))
        stats['evaluated'] += 1

    # Anything still 'open' that wasn't due yet stays open.
    with _DB_LOCK, _conn() as c:
        still_open = c.execute(
            "SELECT COUNT(*) FROM saved_predictions WHERE status = 'open'",
        ).fetchone()[0]
    stats['still_open'] = int(still_open or 0)
    return stats


def auto_log_scan_predictions(symbols: list[str], market: str = 'stocks',
                               forward_days: int = 10, max_new: int = 10) -> dict[str, int]:
    """Systematically log scanner-generated predictions with
    source='auto_scan', so `accuracy_stats(source='auto_scan')` measures
    the scanner's real, unbiased performance instead of only whichever
    picks a human happened to click "Save" on.

    This is the piece that was missing: `save_prediction` was only ever
    reachable from the manual "Save prediction" button in the UI, so
    every accuracy number this system could produce was selection-biased
    by definition -- a user tends to save picks that already look good,
    and the sample size is whatever a human bothered to click, not the
    full scanned universe.

    Intended to be called periodically with a batch of symbols the
    scanner just refreshed (e.g. from `warmer_service`'s background
    loop, which already cycles the whole universe on a timer with no
    user involvement). For each symbol:
      1. Skip it if an 'auto_scan' prediction for that symbol is
         already OPEN -- a fast warmer tick shouldn't spam a new row
         for a symbol whose prior forecast hasn't resolved yet. A new
         one is logged only after the old one expires/evaluates.
      2. Otherwise call `predict_price` (reads from the warm snapshot
         cache, so this is cheap) and log the result verbatim via
         `save_prediction(..., source='auto_scan')`.

    `max_new` caps how many NEW predict_price calls happen per
    invocation, since predict_price recomputes a full factor blend --
    this bounds the added cost per warmer tick regardless of batch size.

    Returns {'logged': N, 'skipped_existing_open': N, 'skipped_unavailable': N}.
    """
    from app.services.price_prediction_service import predict_price

    seen: set[str] = set()
    syms: list[str] = []
    for s in symbols:
        sym = (s or '').strip().upper()
        if sym and sym not in seen:
            seen.add(sym)
            syms.append(sym)

    result = {'logged': 0, 'skipped_existing_open': 0, 'skipped_unavailable': 0}
    if not syms:
        return result

    with _DB_LOCK, _conn() as c:
        placeholders = ','.join('?' * len(syms))
        rows = c.execute(
            f"SELECT DISTINCT symbol FROM saved_predictions "
            f"WHERE source = 'auto_scan' AND status = 'open' AND symbol IN ({placeholders})",
            tuple(syms),
        ).fetchall()
    already_open = {r['symbol'] for r in rows}

    for sym in syms:
        if sym in already_open:
            result['skipped_existing_open'] += 1
            continue
        if result['logged'] >= max_new:
            break
        try:
            pred = predict_price(sym, forward_days=forward_days, market=market)
        except Exception as exc:
            log.debug('auto_log_scan_predictions: predict_price failed for %s: %s', sym, exc)
            result['skipped_unavailable'] += 1
            continue
        if not pred or pred.get('status') != 'ok':
            result['skipped_unavailable'] += 1
            continue

        label = str(pred.get('composite_direction') or pred.get('direction') or '').lower()
        if 'bull' in label:
            direction = 'bull'
        elif 'bear' in label:
            direction = 'bear'
        else:
            direction = 'neutral'

        payload = {
            'symbol': sym,
            'market': market,
            'anchor_price': pred.get('current_price'),
            'target_price': pred.get('target_price'),
            'direction': direction,
            'confidence_pct': pred.get('confidence'),
            'forward_days': pred.get('forward_days') or forward_days,
            'notes': '',
            'full_payload': pred,
            'source': 'auto_scan',
        }
        try:
            save_prediction(payload)
            result['logged'] += 1
        except Exception as exc:
            log.debug('auto_log_scan_predictions: save_prediction failed for %s: %s', sym, exc)
            result['skipped_unavailable'] += 1

    return result


def accuracy_stats(market: Optional[str] = None, source: Optional[str] = None) -> dict[str, Any]:
    """Aggregate accuracy across all evaluated saved predictions.

    `source` optionally restricts to 'user' or 'auto_scan'. IMPORTANT:
    'user' rows are selection-biased (a person chose what to save,
    which tends to skew toward picks that already felt confident) --
    only 'auto_scan' rows (logged automatically and systematically by
    `auto_log_scan_predictions`, not cherry-picked) give an honest read
    on how the scanner's scoring actually performs. When `source` is
    omitted we still report the two breakdowns separately (`by_source`)
    rather than blending them into one number, since a blended number
    would hide exactly which one you're looking at.

    Returns: {
      total_saved, open, evaluated, unresolved,
      directional_accuracy_pct, magnitude_accuracy_pct,
      mean_abs_error_pct, median_abs_error_pct,
      by_direction: {bull: {...}, bear: {...}, neutral: {...}},
      by_source: {user: {...}, auto_scan: {...}}   (each a mini version
                                                     of the top-level stats)
    }
    """
    where_clauses = []
    params: list[Any] = []
    if market in ('stocks', 'crypto'):
        where_clauses.append('market = ?')
        params.append(market)
    if source in ('user', 'auto_scan'):
        where_clauses.append('source = ?')
        params.append(source)
    where = (' WHERE ' + ' AND '.join(where_clauses)) if where_clauses else ''

    with _DB_LOCK, _conn() as c:
        rows = c.execute(
            f'SELECT direction, status, error_pct, directional_hit, magnitude_hit, source '
            f'FROM saved_predictions{where}',
            tuple(params),
        ).fetchall()

    def _summarize(subset: list[sqlite3.Row]) -> dict[str, Any]:
        out = {
            'total_saved': len(subset),
            'open': 0,
            'evaluated': 0,
            'unresolved': 0,
            'directional_accuracy_pct': None,
            'magnitude_accuracy_pct': None,
            'mean_abs_error_pct': None,
            'median_abs_error_pct': None,
            'by_direction': {
                'bull': {'total': 0, 'directional_hits': 0, 'magnitude_hits': 0},
                'bear': {'total': 0, 'directional_hits': 0, 'magnitude_hits': 0},
                'neutral': {'total': 0, 'directional_hits': 0, 'magnitude_hits': 0},
            },
        }
        if not subset:
            return out
        errors_abs: list[float] = []
        dir_hits = 0
        mag_hits = 0
        evaluated_count = 0
        for r in subset:
            status = r['status']
            if status == 'open':
                out['open'] += 1
            elif status == 'unresolved':
                out['unresolved'] += 1
            elif status == 'evaluated':
                evaluated_count += 1
                out['evaluated'] += 1
                err = r['error_pct']
                if err is not None:
                    errors_abs.append(abs(float(err)))
                d_hit = int(r['directional_hit'] or 0)
                m_hit = int(r['magnitude_hit'] or 0)
                dir_hits += d_hit
                mag_hits += m_hit
                direction = (r['direction'] or 'neutral').lower()
                bd = out['by_direction'].get(direction)
                if bd is not None:
                    bd['total'] += 1
                    bd['directional_hits'] += d_hit
                    bd['magnitude_hits'] += m_hit
        if evaluated_count > 0:
            out['directional_accuracy_pct'] = round(100.0 * dir_hits / evaluated_count, 2)
            out['magnitude_accuracy_pct'] = round(100.0 * mag_hits / evaluated_count, 2)
        if errors_abs:
            out['mean_abs_error_pct'] = round(sum(errors_abs) / len(errors_abs), 3)
            srt = sorted(errors_abs)
            mid = len(srt) // 2
            out['median_abs_error_pct'] = round(
                (srt[mid] if len(srt) % 2 == 1 else (srt[mid - 1] + srt[mid]) / 2.0),
                3,
            )
        # Per-direction accuracy %s.
        # BUGFIX (found 2026-07 while wiring up auto_log_scan_predictions):
        # this block used to sit AFTER a `return out` at the end of the
        # outer function, which made it unreachable -- `by_direction`
        # entries always had `total`/`directional_hits`/`magnitude_hits`
        # but never the derived `*_accuracy_pct` fields. Moved inside
        # `_summarize` itself so it runs for the top-level stats AND for
        # each `by_source` breakdown, not just once at the very end.
        for k, bd in out['by_direction'].items():
            if bd['total'] > 0:
                bd['directional_accuracy_pct'] = round(100.0 * bd['directional_hits'] / bd['total'], 2)
                bd['magnitude_accuracy_pct'] = round(100.0 * bd['magnitude_hits'] / bd['total'], 2)
            else:
                bd['directional_accuracy_pct'] = None
                bd['magnitude_accuracy_pct'] = None
        return out

    out = _summarize(rows)
    if source is None:
        out['by_source'] = {
            'user': _summarize([r for r in rows if r['source'] == 'user']),
            'auto_scan': _summarize([r for r in rows if r['source'] == 'auto_scan']),
        }
    return out


# ---------------------------------------------------------------------------
# Background evaluator
# ---------------------------------------------------------------------------
_evaluator_thread: Optional[threading.Thread] = None
_evaluator_stop = threading.Event()
_EVAL_INTERVAL_SECONDS = float(__import__('os').environ.get('PREDICTION_EVAL_INTERVAL', '3600.0'))


def _evaluator_loop():
    log.info('prediction_tracker: evaluator started (interval=%.0fs)', _EVAL_INTERVAL_SECONDS)
    # Initial 30s grace so the daily-history cache + scoring loop have a
    # chance to warm before we start hammering them with hit/miss queries.
    time.sleep(30.0)
    while not _evaluator_stop.is_set():
        try:
            stats = evaluate_expired_predictions()
            if stats['evaluated'] or stats['unresolved']:
                log.info('prediction_tracker: evaluator pass %s', stats)
        except Exception as exc:
            log.exception('prediction_tracker: evaluator pass failed: %s', exc)
        # Sleep with periodic wake so shutdown is responsive.
        if _evaluator_stop.wait(_EVAL_INTERVAL_SECONDS):
            break
    log.info('prediction_tracker: evaluator stopped')


def start_evaluator() -> None:
    """Spawn the background auto-evaluation loop.  Idempotent."""
    global _evaluator_thread
    if _evaluator_thread and _evaluator_thread.is_alive():
        return
    _evaluator_stop.clear()
    _evaluator_thread = threading.Thread(
        target=_evaluator_loop, name='pred-tracker-eval', daemon=True,
    )
    _evaluator_thread.start()


def stop_evaluator() -> None:
    _evaluator_stop.set()


# Don't auto-init on import — main.py lifespan calls init_db() and
# start_evaluator() so we control startup ordering.
