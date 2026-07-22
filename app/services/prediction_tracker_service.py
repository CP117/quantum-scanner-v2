"""
Prediction tracker: log, retrieve, evaluate future price predictions.

Phase 26.19: auto-logging. The warmer loop and manual detail-panel
predictions both feed into this service. We track open predictions
(anchor_price, target_price, forward_days, direction) and periodically
evaluate them against live market data so the UI can render accuracy
stats over time (precision, recall, directional bias, etc.).

Schema: saved_predictions table holds:
  - id: uuid hex string (primary key)
  - symbol, market (stocks/crypto)
  - anchor_price, target_price (entry/exit prices)
  - direction (bull/bear/neutral)
  - confidence_pct (optional user estimate)
  - forward_days (prediction horizon; normally 10)
  - expires_at (anchor_at + forward_days)
  - notes, full_payload (optional audit trail)
  - created_at, evaluated_at (timestamps)
  - status (open/expired/correct/incorrect)
  - source (user / auto_scan)
"""
from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any

log = logging.getLogger('app.prediction_tracker')

_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parent.parent.parent
_DB_CANDIDATES = [
    Path('data/saved_predictions.db'),
    _REPO_ROOT / 'data' / 'saved_predictions.db',
    _REPO_ROOT / 'app' / 'data' / 'saved_predictions.db',
]

_DB_LOCK = Lock()
_EVAL_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix='pred-eval')


def _pick_db() -> Path:
    for p in _DB_CANDIDATES:
        try:
            if p.exists() or p.parent.exists():
                return p
        except OSError:
            continue
    return _DB_CANDIDATES[0]


_DB_PATH = _pick_db()


def _utcnow() -> datetime:
    """Current UTC time as a timezone-aware datetime."""
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    """Current UTC time as ISO 8601 string."""
    return _utcnow().isoformat()


def _conn() -> sqlite3.Connection:
    """Get a DB connection. Auto-creates the DB file + schema if needed."""
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Idempotent schema init.  Safe to call repeatedly."""
    with _DB_LOCK, _conn() as c:
        c.execute('''
            CREATE TABLE IF NOT EXISTS saved_predictions (
              id TEXT PRIMARY KEY,
              symbol TEXT NOT NULL,
              market TEXT NOT NULL DEFAULT 'stocks',
              anchor_price REAL NOT NULL,
              target_price REAL NOT NULL,
              direction TEXT NOT NULL,
              confidence_pct REAL,
              forward_days INTEGER NOT NULL DEFAULT 10,
              expires_at TEXT NOT NULL,
              notes TEXT,
              full_payload TEXT,
              created_at TEXT NOT NULL,
              evaluated_at TEXT,
              status TEXT NOT NULL DEFAULT 'open',
              source TEXT NOT NULL DEFAULT 'user',
              UNIQUE(symbol, anchor_price, created_at)
            )
        ''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_symbol ON saved_predictions(symbol)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_status ON saved_predictions(status)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_created ON saved_predictions(created_at DESC)')
        c.commit()


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
    
    # CRITICAL FIX: Ensure both anchor and target are valid prices.
    # If target_price is missing or zero, derive it from anchor + a default delta.
    if anchor <= 0:
        raise ValueError('anchor_price must be > 0')
    if target <= 0:
        # Auto-derive target_price if missing: use 10% move in direction of signal
        direction = (payload.get('direction') or '').lower()
        if direction == 'bull':
            target = anchor * 1.10  # 10% upside default
        elif direction == 'bear':
            target = anchor * 0.90  # 10% downside default
        else:
            # Neutral: pick a reasonable default (slightly above anchor)
            target = anchor * 1.05
        log.debug('prediction_tracker: derived target_price=%.2f from anchor=%.2f direction=%s',
                  target, anchor, direction)

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
            c.commit()
            log.debug('prediction_tracker: saved %s %s @ %.2f -> %.2f (%s, %dd)',
                      sym, market, anchor, target, direction, forward_days)
        except sqlite3.IntegrityError as exc:
            # Duplicate UNIQUE(symbol, anchor_price, created_at) — ignore
            log.debug('prediction_tracker: duplicate prediction ignored: %s', exc)
        except sqlite3.Error as exc:
            log.exception('prediction_tracker: insert failed: %s', exc)
            raise RuntimeError(f'db insert failed: {exc}') from exc

    return {
        'id': pred_id,
        'symbol': sym,
        'market': market,
        'anchor_price': anchor,
        'target_price': target,
        'direction': direction,
        'confidence_pct': confidence_pct,
        'forward_days': forward_days,
        'expires_at': expires_dt.isoformat(),
        'created_at': created_at.isoformat(),
        'status': 'open',
        'source': source,
    }


def get_open_predictions(symbol: str | None = None, market: str | None = None) -> list[dict]:
    """Retrieve all open predictions, optionally filtered by symbol + market."""
    query = 'SELECT * FROM saved_predictions WHERE status = ?'
    params = ['open']
    if symbol:
        query += ' AND symbol = ?'
        params.append(symbol.upper())
    if market:
        query += ' AND market = ?'
        params.append(market.lower())
    query += ' ORDER BY created_at DESC'
    try:
        with _DB_LOCK, _conn() as c:
            rows = c.execute(query, params).fetchall()
            return [dict(r) for r in rows]
    except sqlite3.Error as exc:
        log.warning('prediction_tracker: fetch open failed: %s', exc)
        return []


def get_all_predictions(symbol: str | None = None, limit: int = 1000) -> list[dict]:
    """Retrieve all predictions (any status), newest first."""
    query = 'SELECT * FROM saved_predictions'
    params: list[Any] = []
    if symbol:
        query += ' WHERE symbol = ?'
        params.append(symbol.upper())
    query += ' ORDER BY created_at DESC LIMIT ?'
    params.append(limit)
    try:
        with _DB_LOCK, _conn() as c:
            rows = c.execute(query, params).fetchall()
            return [dict(r) for r in rows]
    except sqlite3.Error as exc:
        log.warning('prediction_tracker: fetch all failed: %s', exc)
        return []


def auto_log_scan_predictions(
    rows: list[dict],
    max_new_per_cycle: int = 10,
    forward_days: int = 10,
) -> None:
    """Auto-log top-N symbols from a completed scanner batch.

    Called by the warmer loop on each cycle to log systematic predictions
    from the background scan. These are tagged source='auto_scan' so the
    UI can distinguish user-manual predictions from algorithmic ones.

    `rows` should be the top-scoring symbols from the batch (usually the
    scan result envelope['results']). We pick the top N that haven't been
    logged today and insert them.
    """
    if not rows or max_new_per_cycle <= 0:
        return

    # Quick check: don't bother if the DB hasn't been initialized yet.
    try:
        with _DB_LOCK:
            if not _DB_PATH.exists():
                return
    except Exception:
        return

    now = _utcnow()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # Collect symbols already logged today.
    try:
        with _DB_LOCK, _conn() as c:
            logged_today = {
                r[0] for r in c.execute('''
                    SELECT DISTINCT symbol FROM saved_predictions
                    WHERE source = 'auto_scan' AND created_at >= ?
                ''', (today_start.isoformat(),)).fetchall()
            }
    except sqlite3.Error as exc:
        log.warning('prediction_tracker: auto_log fetch failed: %s', exc)
        return

    logged_count = 0
    for row in rows:
        if logged_count >= max_new_per_cycle:
            break
        sym = (row.get('symbol') or '').upper()
        if not sym or sym in logged_today:
            continue

        # Extract the data we need from the row.
        score = float(row.get('final_score') or 0.0)
        direction = (row.get('final_direction') or 'Neutral').lower()
        if direction not in ('bull', 'bear', 'neutral'):
            direction = 'neutral'

        # Get the current/anchor price.  Try multiple sources.
        anchor_price = None
        market_fb = (row.get('factor_breakdown') or {}).get('market') or {}
        if market_fb.get('last_price'):
            anchor_price = float(market_fb['last_price'])
        if not anchor_price or anchor_price <= 0:
            # Try symbol identity lookup as fallback.
            try:
                from app.services.snapshot_store import lookup_snapshot_row
                snap_row = lookup_snapshot_row(sym, 'stocks')
                if snap_row:
                    snap_fb = (snap_row.get('factor_breakdown') or {}).get('market') or {}
                    anchor_price = float(snap_fb.get('last_price') or 0)
            except Exception:
                pass

        if not anchor_price or anchor_price <= 0:
            log.debug('prediction_tracker: auto_log skip %s (no price)', sym)
            continue

        # Derive target_price based on score and direction.
        # Higher score → more aggressive target (farther from anchor).
        # Bull: target = anchor * (1 + 0.05 * normalized_score)
        # Bear: target = anchor * (1 - 0.05 * normalized_score)
        normalized_score = max(0.0, min(1.0, score / 100.0))  # 0 to 1
        if direction == 'bull':
            # 5-15% upside range based on score
            target_price = anchor_price * (1.0 + 0.05 + 0.1 * normalized_score)
        elif direction == 'bear':
            # 5-15% downside range based on score
            target_price = anchor_price * (1.0 - 0.05 - 0.1 * normalized_score)
        else:
            # Neutral: tight range (2-3% move)
            target_price = anchor_price * (1.0 + 0.01 * (0.5 - normalized_score))

        try:
            save_prediction({
                'symbol': sym,
                'market': 'stocks',
                'anchor_price': anchor_price,
                'target_price': target_price,
                'direction': direction,
                'forward_days': forward_days,
                'confidence_pct': min(100.0, score),  # Use final_score as confidence
                'notes': f'auto_scan: score={score:.1f}',
                'source': 'auto_scan',
            })
            logged_count += 1
            logged_today.add(sym)
        except ValueError as exc:
            log.debug('prediction_tracker: auto_log skip %s: %s', sym, exc)
        except RuntimeError as exc:
            log.warning('prediction_tracker: auto_log failed for %s: %s', sym, exc)
            # Continue on DB errors — don't let one failure block the whole batch.

    if logged_count > 0:
        log.info('prediction_tracker: auto_log added %d new predictions', logged_count)


def accuracy_stats(source: str | None = None) -> dict:
    """Compute aggregate accuracy stats over closed predictions.

    Returns:
      {
        'total_predictions': N,
        'correct': C,
        'incorrect': I,
        'accuracy_pct': 100*C/N,
        'bull_accuracy_pct': ...,
        'bear_accuracy_pct': ...,
        'expired_only': bool (True = only expired preds included),
      }
    """
    query = '''
        SELECT COUNT(*) as total,
               SUM(CASE WHEN status = 'correct' THEN 1 ELSE 0 END) as correct,
               SUM(CASE WHEN status = 'incorrect' THEN 1 ELSE 0 END) as incorrect,
               SUM(CASE WHEN status = 'correct' AND direction = 'bull' THEN 1 ELSE 0 END) as bull_correct,
               SUM(CASE WHEN direction = 'bull' THEN 1 ELSE 0 END) as bull_total,
               SUM(CASE WHEN status = 'correct' AND direction = 'bear' THEN 1 ELSE 0 END) as bear_correct,
               SUM(CASE WHEN direction = 'bear' THEN 1 ELSE 0 END) as bear_total
        FROM saved_predictions
        WHERE status IN ('correct', 'incorrect')
    '''
    params: list[Any] = []
    if source:
        query += ' AND source = ?'
        params.append(source)

    try:
        with _DB_LOCK, _conn() as c:
            row = c.execute(query, params).fetchone()
        if not row:
            return {
                'total_predictions': 0,
                'correct': 0,
                'incorrect': 0,
                'accuracy_pct': 0.0,
                'bull_accuracy_pct': 0.0,
                'bear_accuracy_pct': 0.0,
                'expired_only': True,
            }
        total = row['total'] or 0
        correct = row['correct'] or 0
        bull_correct = row['bull_correct'] or 0
        bull_total = row['bull_total'] or 0
        bear_correct = row['bear_correct'] or 0
        bear_total = row['bear_total'] or 0
        return {
            'total_predictions': total,
            'correct': correct,
            'incorrect': (row['incorrect'] or 0),
            'accuracy_pct': 100.0 * correct / total if total > 0 else 0.0,
            'bull_accuracy_pct': 100.0 * bull_correct / bull_total if bull_total > 0 else 0.0,
            'bear_accuracy_pct': 100.0 * bear_correct / bear_total if bear_total > 0 else 0.0,
            'expired_only': True,
        }
    except sqlite3.Error as exc:
        log.warning('prediction_tracker: accuracy_stats failed: %s', exc)
        return {
            'total_predictions': 0,
            'correct': 0,
            'incorrect': 0,
            'accuracy_pct': 0.0,
            'bull_accuracy_pct': 0.0,
            'bear_accuracy_pct': 0.0,
            'expired_only': True,
        }
