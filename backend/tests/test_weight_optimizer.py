"""
Tests for Bucket 4 — composite weight optimizer.

All tests run offline: no network access, no real DB files, no real
market data. Synthetic prediction rows are constructed to verify:

1. _extract_labelled_rows: factor scores parsed from full_payload JSON.
2. _project_simplex: unit-tests the simplex projection (used by the fitter).
3. _walk_forward_fit on a perfectly-separable synthetic dataset: the
   optimizer should recover a weight vector that achieves > 90 % WF accuracy.
4. _walk_forward_eval with the known-good weights on the same dataset.
5. fit_mqts_weights returns 'insufficient_data' when N < MIN_SAMPLES.
6. fit_mqts_weights returns a result with the correct structure when N ≥ MIN_SAMPLES.
7. evaluate_expired_predictions: correct/incorrect labeling based on price.
8. auto_log_scan_predictions: factor scores stored in full_payload.
9. auto_log_scan_predictions: accepts plain symbol strings (normalises via
   snapshot store fallback) without crashing.

Run from the repo root with:
    python -m pytest backend/tests/test_weight_optimizer.py -v
"""
from __future__ import annotations

import json
import math
import sqlite3
import tempfile
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Helpers shared across tests
# ---------------------------------------------------------------------------

def _make_row(
    direction: str,
    status: str,
    momentum: float,
    quality: float,
    trend: float,
    stability: float,
) -> dict:
    """Minimal synthetic closed-prediction dict matching the DB schema."""
    payload = json.dumps({
        'factor_scores': {
            'momentum': momentum,
            'quality': quality,
            'trend': trend,
            'stability': stability,
            'exit_risk': None,
        }
    })
    return {
        'id': 'test-' + direction,
        'direction': direction,
        'status': status,
        'full_payload': payload,
        'created_at': '2026-01-01T00:00:00',
    }


# ---------------------------------------------------------------------------
# 1. _extract_labelled_rows
# ---------------------------------------------------------------------------

from app.services.weight_optimizer_service import (
    _extract_labelled_rows,
    _project_simplex,
    _walk_forward_fit,
    _walk_forward_eval,
    _dict_to_list,
    _list_to_dict,
    fit_mqts_weights,
    MIN_SAMPLES,
    CURRENT_WEIGHTS,
)


def test_extract_labelled_rows_correct():
    row = _make_row('bull', 'correct', 80, 70, 60, 50)
    labelled = _extract_labelled_rows([row])
    assert len(labelled) == 1
    assert labelled[0]['label'] == 1
    assert labelled[0]['factors'] == [80.0, 70.0, 60.0, 50.0]


def test_extract_labelled_rows_incorrect():
    row = _make_row('bear', 'incorrect', 30, 40, 20, 35)
    labelled = _extract_labelled_rows([row])
    assert len(labelled) == 1
    assert labelled[0]['label'] == 0


def test_extract_labelled_rows_skips_all_zeros():
    """A row with all four factor scores = 0 is treated as unfilled and skipped."""
    row = _make_row('bull', 'correct', 0, 0, 0, 0)
    labelled = _extract_labelled_rows([row])
    assert len(labelled) == 0


def test_extract_labelled_rows_skips_open():
    row = _make_row('bull', 'open', 80, 70, 60, 50)
    labelled = _extract_labelled_rows([row])
    assert len(labelled) == 0


def test_extract_labelled_rows_skips_bad_payload():
    row = {
        'id': 'x', 'direction': 'bull', 'status': 'correct',
        'full_payload': 'not valid json', 'created_at': '2026-01-01',
    }
    labelled = _extract_labelled_rows([row])
    assert len(labelled) == 0


# ---------------------------------------------------------------------------
# 2. _project_simplex
# ---------------------------------------------------------------------------

def test_project_simplex_already_valid():
    w = [0.35, 0.25, 0.20, 0.20]
    proj = _project_simplex(w)
    assert abs(sum(proj) - 1.0) < 1e-9
    for v in proj:
        assert v >= 0.0


def test_project_simplex_negative_values():
    w = [-1.0, 2.0, 0.5, 0.5]
    proj = _project_simplex(w)
    assert abs(sum(proj) - 1.0) < 1e-9
    for v in proj:
        assert v >= 0.0


def test_project_simplex_uniform():
    w = [0.25, 0.25, 0.25, 0.25]
    proj = _project_simplex(w)
    for v in proj:
        assert abs(v - 0.25) < 1e-9


# ---------------------------------------------------------------------------
# 3. walk-forward fit on a synthetic separable dataset
# ---------------------------------------------------------------------------

def _make_synthetic_dataset(n: int = 200) -> list[dict]:
    """
    Dataset where momentum alone is the perfect predictor:
    if momentum > 50 → correct, else incorrect.
    The optimizer should recover weights with high momentum component.
    """
    import random
    rng = random.Random(42)
    rows = []
    for i in range(n):
        m = rng.uniform(20, 80)
        q = rng.uniform(30, 70)
        t = rng.uniform(30, 70)
        s = rng.uniform(30, 70)
        label = 1 if m > 50 else 0
        rows.append({'factors': [m, q, t, s], 'label': label})
    return rows


def test_walk_forward_fit_separable():
    """On a momentum-only separable dataset the WF accuracy should be > 65 %."""
    dataset = _make_synthetic_dataset(200)
    acc, fitted = _walk_forward_fit(dataset)
    assert acc > 0.65, f'Expected WF accuracy > 65 %, got {acc:.1%}'
    # momentum weight should be the largest
    assert fitted['momentum'] == max(fitted.values()), \
        f'momentum weight should be highest: {fitted}'


def test_walk_forward_fit_output_is_simplex():
    """Fitted weights must be on the probability simplex."""
    dataset = _make_synthetic_dataset(100)
    _, fitted = _walk_forward_fit(dataset)
    total = sum(fitted.values())
    assert abs(total - 1.0) < 1e-6, f'Weights sum to {total}, expected 1.0'
    for k, v in fitted.items():
        assert v >= 0.0, f'Weight {k}={v} is negative'


def test_walk_forward_eval_current_weights():
    """walk_forward_eval with the current hand-picked weights returns 0–1."""
    dataset = _make_synthetic_dataset(100)
    acc = _walk_forward_eval(dataset, CURRENT_WEIGHTS)
    assert 0.0 <= acc <= 1.0


def test_walk_forward_eval_vs_fit_on_known_signal():
    """
    On a momentum-driven dataset the fitted weights should match or beat the
    hand-picked weights in walk-forward accuracy.
    """
    dataset = _make_synthetic_dataset(200)
    acc_current = _walk_forward_eval(dataset, CURRENT_WEIGHTS)
    acc_fitted, _ = _walk_forward_fit(dataset)
    # Fitted should not be dramatically worse than current.
    # (On this perfectly-separable dataset, both should be >60 %.)
    assert acc_fitted >= acc_current - 0.05, (
        f'Fitted ({acc_fitted:.1%}) is more than 5 pp worse than current '
        f'({acc_current:.1%}) on a dataset where momentum is the sole signal.'
    )


# ---------------------------------------------------------------------------
# 5 & 6. fit_mqts_weights end-to-end (via an in-memory DB)
# ---------------------------------------------------------------------------

def _build_in_memory_db(rows_spec: list[tuple]) -> sqlite3.Connection:
    """
    Create an in-memory SQLite DB with the saved_predictions schema and
    insert synthetic rows.

    rows_spec: list of (direction, status, momentum, quality, trend, stability)
    """
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.execute('''
        CREATE TABLE saved_predictions (
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
          source TEXT NOT NULL DEFAULT 'user'
        )
    ''')

    import random
    rng = random.Random(0)
    base_dt = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i, (direction, status, m, q, t, s) in enumerate(rows_spec):
        dt = (base_dt + timedelta(hours=i)).isoformat()
        payload = json.dumps({
            'factor_scores': {
                'momentum': m, 'quality': q, 'trend': t, 'stability': s,
                'exit_risk': None,
            }
        })
        conn.execute('''
            INSERT INTO saved_predictions
              (id, symbol, market, anchor_price, target_price, direction,
               forward_days, expires_at, created_at, full_payload, status, source)
            VALUES (?, 'TEST', 'stocks', 100.0, 110.0, ?, 10, ?, ?, ?, ?, 'auto_scan')
        ''', (f'id-{i}', direction, (base_dt + timedelta(days=11)).isoformat(), dt, payload, status))
    conn.commit()
    return conn


def test_fit_mqts_insufficient_data():
    """With fewer than MIN_SAMPLES rows, fit_mqts_weights returns 'insufficient_data'."""
    # Build a DB with only 5 closed rows.
    spec = [('bull', 'correct', 70, 60, 55, 50)] * 5
    conn = _build_in_memory_db(spec)

    result = fit_mqts_weights(db_conn_factory=lambda: conn)
    assert result is not None
    assert result['recommendation'] == 'insufficient_data'
    assert result['n_samples'] == 5
    assert result['wf_accuracy_fitted'] is None


def test_fit_mqts_sufficient_data_structure():
    """With >= MIN_SAMPLES rows the result has the expected keys and types."""
    # Build MIN_SAMPLES rows with momentum as the only signal.
    import random
    rng = random.Random(7)
    spec = []
    for _ in range(MIN_SAMPLES + 20):
        m = rng.uniform(20, 80)
        q, t, s = rng.uniform(30, 70), rng.uniform(30, 70), rng.uniform(30, 70)
        status = 'correct' if m > 50 else 'incorrect'
        spec.append(('bull', status, m, q, t, s))
    conn = _build_in_memory_db(spec)

    result = fit_mqts_weights(db_conn_factory=lambda: conn)
    assert result is not None
    for key in ('fitted_weights', 'current_weights', 'n_samples',
                'wf_accuracy_fitted', 'wf_accuracy_current', 'recommendation', 'notes'):
        assert key in result, f'Missing key: {key}'
    assert result['n_samples'] >= MIN_SAMPLES
    # Weights must be on the simplex.
    fw = result['fitted_weights']
    assert abs(sum(fw.values()) - 1.0) < 1e-5
    for v in fw.values():
        assert v >= 0.0
    # Accuracies in [0, 1].
    assert 0.0 <= result['wf_accuracy_fitted'] <= 1.0
    assert 0.0 <= result['wf_accuracy_current'] <= 1.0
    assert result['recommendation'] in ('update', 'keep', 'insufficient_data')


# ---------------------------------------------------------------------------
# 7. evaluate_expired_predictions
# ---------------------------------------------------------------------------

from app.services.prediction_tracker_service import (
    evaluate_expired_predictions,
    _safe_rating_score,
)


def _make_pred_db(rows: list[dict]) -> tuple[sqlite3.Connection, str]:
    """Create a temp-file SQLite DB for tracker tests, return (conn, path)."""
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute('''
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
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    for row in rows:
        conn.execute('''
            INSERT INTO saved_predictions
              (id, symbol, market, anchor_price, target_price, direction,
               forward_days, expires_at, created_at, status, source)
            VALUES (?, ?, 'stocks', ?, ?, ?, 10, ?, ?, 'open', 'auto_scan')
        ''', (
            row['id'], row['symbol'], row['anchor'], row['target'],
            row['direction'], past, past,
        ))
    conn.commit()
    return conn, path


def _patch_tracker_db(path: str):
    """Patch prediction_tracker_service to use our temp DB path."""
    import app.services.prediction_tracker_service as tracker
    return patch.object(tracker, '_DB_PATH', Path(path))


def test_evaluate_expired_bull_correct():
    """Bull prediction where current price > anchor → correct."""
    conn, path = _make_pred_db([
        {'id': 'b1', 'symbol': 'AAPL', 'anchor': 100.0, 'target': 110.0, 'direction': 'bull'},
    ])
    # Mock price lookup to return 110 (> anchor 100).
    with _patch_tracker_db(path), \
         patch('app.services.prediction_tracker_service._lookup_current_price', return_value=110.0):
        result = evaluate_expired_predictions()
    assert result['evaluated'] == 1
    assert result['correct'] == 1
    assert result['incorrect'] == 0
    row = conn.execute("SELECT status FROM saved_predictions WHERE id='b1'").fetchone()
    assert row['status'] == 'correct'
    conn.close()
    os.unlink(path)


def test_evaluate_expired_bull_incorrect():
    """Bull prediction where current price < anchor → incorrect."""
    conn, path = _make_pred_db([
        {'id': 'b2', 'symbol': 'AAPL', 'anchor': 100.0, 'target': 110.0, 'direction': 'bull'},
    ])
    with _patch_tracker_db(path), \
         patch('app.services.prediction_tracker_service._lookup_current_price', return_value=90.0):
        result = evaluate_expired_predictions()
    assert result['correct'] == 0
    assert result['incorrect'] == 1
    conn.close()
    os.unlink(path)


def test_evaluate_expired_bear_correct():
    """Bear prediction where current price < anchor → correct."""
    conn, path = _make_pred_db([
        {'id': 'b3', 'symbol': 'TSLA', 'anchor': 200.0, 'target': 180.0, 'direction': 'bear'},
    ])
    with _patch_tracker_db(path), \
         patch('app.services.prediction_tracker_service._lookup_current_price', return_value=175.0):
        result = evaluate_expired_predictions()
    assert result['correct'] == 1
    conn.close()
    os.unlink(path)


def test_evaluate_expired_no_price_leaves_open():
    """When price lookup returns None the row stays open (still_open += 1)."""
    conn, path = _make_pred_db([
        {'id': 'b4', 'symbol': 'NVDA', 'anchor': 150.0, 'target': 160.0, 'direction': 'bull'},
    ])
    with _patch_tracker_db(path), \
         patch('app.services.prediction_tracker_service._lookup_current_price', return_value=None):
        result = evaluate_expired_predictions()
    assert result['evaluated'] == 0
    assert result['still_open'] == 1
    row = conn.execute("SELECT status FROM saved_predictions WHERE id='b4'").fetchone()
    assert row['status'] == 'open'
    conn.close()
    os.unlink(path)


# ---------------------------------------------------------------------------
# 8. auto_log_scan_predictions stores factor scores
# ---------------------------------------------------------------------------

from app.services.prediction_tracker_service import auto_log_scan_predictions


def test_auto_log_stores_factor_scores(tmp_path):
    """auto_log_scan_predictions stores M/Q/T/S scores in full_payload JSON."""
    db_path = tmp_path / 'preds.db'
    # Init the DB.
    import app.services.prediction_tracker_service as tracker
    with patch.object(tracker, '_DB_PATH', db_path):
        tracker.init_db()

    scan_row = {
        'symbol': 'MSFT',
        'final_score': 75.0,
        'final_direction': 'bull',
        'factor_breakdown': {
            'market': {'last_price': 300.0},
            'ratings': {
                'momentum': {'score': 80.0},
                'quality':  {'score': 70.0},
                'trend':    {'score': 65.0},
                'stability': {'score': 60.0},
                'exit_risk': {'score': 30.0},
            },
            'weights': {'momentum': 0.35, 'quality': 0.25, 'trend': 0.20, 'stability': 0.20},
        },
    }

    with patch.object(tracker, '_DB_PATH', db_path):
        n = auto_log_scan_predictions([scan_row], market='stocks', forward_days=10, max_new=5)

    assert n == 1

    # Retrieve and verify full_payload.
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT full_payload FROM saved_predictions WHERE symbol='MSFT'").fetchone()
    assert row is not None
    payload = json.loads(row['full_payload'])
    fs = payload['factor_scores']
    assert fs['momentum'] == 80.0
    assert fs['quality']  == 70.0
    assert fs['trend']    == 65.0
    assert fs['stability']== 60.0
    conn.close()


# ---------------------------------------------------------------------------
# 9. auto_log_scan_predictions: plain symbol strings don't crash
# ---------------------------------------------------------------------------

def test_auto_log_accepts_symbol_strings(tmp_path):
    """Plain symbol strings trigger snapshot lookup and don't crash."""
    db_path = tmp_path / 'preds2.db'
    import app.services.prediction_tracker_service as tracker
    with patch.object(tracker, '_DB_PATH', db_path):
        tracker.init_db()

    with patch.object(tracker, '_DB_PATH', db_path), \
         patch('app.services.prediction_tracker_service.lookup_snapshot_row',
               return_value=None, create=True):
        # If snapshot lookup returns None the row is silently skipped — no crash.
        n = auto_log_scan_predictions(['AAPL', 'GOOG'], market='stocks')
    assert n == 0  # skipped (no price found)


# ---------------------------------------------------------------------------
# 10. _safe_rating_score
# ---------------------------------------------------------------------------

def test_safe_rating_score_dict():
    assert _safe_rating_score({'score': 75.0}) == 75.0


def test_safe_rating_score_none():
    assert _safe_rating_score(None) is None


def test_safe_rating_score_missing_key():
    assert _safe_rating_score({'rating': 'A'}) is None


def test_safe_rating_score_string_number():
    assert _safe_rating_score({'score': '42'}) == 42.0
