"""
Bucket 4 — composite weight optimizer.

Fits the M/Q/T/S blending weights
    final_score = w_m·momentum + w_q·quality + w_t·trend + w_s·stability

against the historical record of closed auto_scan predictions using a
walk-forward, cross-validated logistic regression.  The optimizer is
intentionally conservative:

* It returns silence (current hand-picked defaults) when the closed-
  prediction history is too small (N < MIN_SAMPLES).
* It uses walk-forward validation — training only on strictly earlier
  predictions, scoring on later ones — so it never looks ahead.
* It reports the walk-forward accuracy of the *fitted* weights vs the
  *current* hand-picked weights as a sanity check.  If the fitted weights
  don't beat the current ones, the optimizer still reports the comparison
  but flags which set is stronger.
* It does NOT automatically write the fitted weights back into the scorer.
  The purpose of this first pass is to accumulate enough outcome data to
  make a confident recommendation; the weights in scoring_service.py can
  then be updated manually once the recommendation is stable across
  multiple runs.

Math summary
------------
We model the probability that a bull prediction is "correct" given the
logged factor scores:

    p(correct | x) = σ(x·w / T)

where x = [momentum, quality, trend, stability] (all on 0–100 scale),
w = weights on a probability simplex (wᵢ ≥ 0, Σwᵢ = 1), T = temperature
(fixed at 50 for the 0–100 scale so the logit sits in ±2 range), and
σ is the logistic function.  The simplex constraint means the model is
really fitting a *weighted average* of the four factor scores, which is
exactly what scoring_service already computes — we're only refining the
weights.

Walk-forward protocol
---------------------
Predictions are sorted by created_at.  We use 60 % of rows for the
initial training burn-in, then slide forward 1 step at a time, always
training on all past rows and predicting the next one.  The final
reported accuracy is the fraction of held-out steps where the model
predicted the correct outcome.

Reality-breaker weights
-----------------------
The reality_breaker_multiplier uses a similar linear combination:
    raw = 0.30·z_lcc + 0.25·z_qpii − 0.25·z_llve + 0.20·z_trs

A separate function `fit_reality_breaker_weights` handles those, but the
methodology is identical.  Because the sub-factor z-scores are NOT yet
stored in the prediction log, that function currently returns None until
the logging gap for reality-breaker inputs is closed in a future pass.
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
from threading import Lock
from typing import Any

log = logging.getLogger('app.weight_optimizer')

# Minimum number of closed auto_scan predictions (with factor scores) needed
# before we attempt a fit.  Below this the sample is too small to distinguish
# signal from noise across 4 degrees of freedom.
MIN_SAMPLES: int = 50

# Minimum walk-forward accuracy improvement over the current hand-picked
# weights before we log a "recommendation to update" message.
# Set conservatively — we need a real improvement, not rounding noise.
MIN_ACCURACY_DELTA: float = 0.02  # 2 percentage points

# Current hand-picked weights from scoring_service.py (kept in sync manually).
CURRENT_WEIGHTS: dict[str, float] = {
    'momentum': 0.35,
    'quality':  0.25,
    'trend':    0.20,
    'stability': 0.20,
}

_FACTORS = ('momentum', 'quality', 'trend', 'stability')

_lock = Lock()
_last_result: dict | None = None  # cache of most recent fit


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fit_mqts_weights(db_conn_factory=None) -> dict | None:
    """Fit M/Q/T/S composite weights against closed auto_scan predictions.

    Parameters
    ----------
    db_conn_factory : callable | None
        A zero-argument callable that returns a sqlite3.Connection with
        row_factory=sqlite3.Row set.  If None, the prediction tracker's
        own _conn() is used.

    Returns
    -------
    dict with keys:
        fitted_weights   – simplex weights as {'momentum':…, 'quality':…, …}
        current_weights  – the hand-picked weights for comparison
        n_samples        – number of closed predictions used
        wf_accuracy_fitted   – walk-forward accuracy of fitted weights
        wf_accuracy_current  – walk-forward accuracy of current weights
        recommendation   – 'update' | 'keep' | 'insufficient_data'
        notes            – human-readable explanation
    or None if the DB is unreachable.
    """
    rows = _load_closed_rows(db_conn_factory)
    if rows is None:
        return None  # DB error — caller should retry later

    labelled = _extract_labelled_rows(rows)
    n = len(labelled)
    if n < MIN_SAMPLES:
        log.info(
            'weight_optimizer: only %d labelled rows (need %d); returning silence.',
            n, MIN_SAMPLES,
        )
        result = {
            'fitted_weights': CURRENT_WEIGHTS.copy(),
            'current_weights': CURRENT_WEIGHTS.copy(),
            'n_samples': n,
            'wf_accuracy_fitted': None,
            'wf_accuracy_current': None,
            'recommendation': 'insufficient_data',
            'notes': (
                f'Need {MIN_SAMPLES} closed auto_scan predictions with factor '
                f'scores; have {n}.  Run the scanner for at least '
                f'{MIN_SAMPLES - n} more prediction cycles before refitting.'
            ),
        }
        with _lock:
            global _last_result
            _last_result = result
        return result

    acc_fitted, fitted_w = _walk_forward_fit(labelled)
    acc_current = _walk_forward_eval(labelled, CURRENT_WEIGHTS)

    delta = acc_fitted - acc_current
    if delta >= MIN_ACCURACY_DELTA:
        recommendation = 'update'
        notes = (
            f'Fitted weights improve walk-forward accuracy by {delta:.1%} '
            f'({acc_current:.1%} → {acc_fitted:.1%}) over {n} predictions. '
            f'Recommended: update scoring_service.py CURRENT_WEIGHTS.'
        )
        log.warning(
            'weight_optimizer: fitted weights outperform current by %.1f pp '
            'over %d samples. Fitted: %s  Current: %s',
            delta * 100, n, _fmt(fitted_w), _fmt(CURRENT_WEIGHTS),
        )
    elif delta < -MIN_ACCURACY_DELTA:
        recommendation = 'keep'
        notes = (
            f'Current hand-picked weights outperform fitted weights by '
            f'{-delta:.1%} ({acc_fitted:.1%} fitted vs {acc_current:.1%} current) '
            f'over {n} predictions. Keep current weights.'
        )
        log.info('weight_optimizer: current weights better by %.1f pp — no change.', -delta * 100)
    else:
        recommendation = 'keep'
        notes = (
            f'Fitted and current weights perform within {abs(delta):.1%} of each '
            f'other ({acc_fitted:.1%} vs {acc_current:.1%}) over {n} predictions. '
            f'Difference is below the {MIN_ACCURACY_DELTA:.0%} threshold — keep '
            f'current weights until more data accumulates.'
        )
        log.info('weight_optimizer: weights within threshold — no change needed.')

    result: dict = {
        'fitted_weights': fitted_w,
        'current_weights': CURRENT_WEIGHTS.copy(),
        'n_samples': n,
        'wf_accuracy_fitted': round(acc_fitted, 4),
        'wf_accuracy_current': round(acc_current, 4),
        'recommendation': recommendation,
        'notes': notes,
    }
    with _lock:
        _last_result = result
    return result


def last_fit_result() -> dict | None:
    """Return the most recent fit result (None if no fit has been run yet)."""
    with _lock:
        return _last_result


def fit_reality_breaker_weights() -> dict | None:
    """Placeholder for reality_breaker composite weight fitting.

    The reality_breaker_multiplier combines:
        raw = 0.30·z(LCC) + 0.25·z(QPII) − 0.25·z(LLVE) + 0.20·z(TRS)

    Fitting these weights requires that the four sub-factor z-scores (LCC,
    QPII, LLVE, TRS) are stored in the prediction log alongside the outcome
    label.  They are NOT yet stored (as of this pass).  This function
    returns None until the logging gap for reality-breaker inputs is closed.
    """
    log.info(
        'weight_optimizer: reality_breaker sub-factor scores not yet logged — '
        'fit_reality_breaker_weights() returns None until logging gap is closed.'
    )
    return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_closed_rows(db_conn_factory=None) -> list[dict] | None:
    """Pull closed auto_scan predictions from the prediction tracker DB.

    Returns None on DB error; empty list if the table exists but has no rows.
    """
    try:
        if db_conn_factory is not None:
            conn = db_conn_factory()
        else:
            from app.services.prediction_tracker_service import _conn, _DB_LOCK
            with _DB_LOCK:
                conn = _conn()
        with conn:
            rows = conn.execute('''
                SELECT id, direction, status, full_payload, created_at
                FROM saved_predictions
                WHERE source = 'auto_scan'
                  AND status IN ('correct', 'incorrect')
                ORDER BY created_at ASC
            ''').fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError as exc:
        log.debug('weight_optimizer: DB not ready: %s', exc)
        return None
    except Exception as exc:
        log.warning('weight_optimizer: load_closed_rows failed: %s', exc)
        return None


def _extract_labelled_rows(rows: list[dict]) -> list[dict]:
    """Filter to rows that have all four M/Q/T/S scores and a binary label.

    Returns a list of dicts: {factors: [m,q,t,s], label: 0|1}.
    """
    out = []
    for row in rows:
        status = row.get('status', '')
        if status not in ('correct', 'incorrect'):
            continue
        label = 1 if status == 'correct' else 0

        payload_str = row.get('full_payload') or '{}'
        try:
            payload = json.loads(payload_str) if isinstance(payload_str, str) else (payload_str or {})
        except (json.JSONDecodeError, TypeError):
            payload = {}

        fs = payload.get('factor_scores') or {}
        try:
            m = float(fs.get('momentum') or 0)
            q = float(fs.get('quality') or 0)
            t = float(fs.get('trend') or 0)
            s = float(fs.get('stability') or 0)
        except (TypeError, ValueError):
            continue  # incomplete — skip

        # Skip rows where all four scores are exactly 0 (unfilled defaults).
        if m == 0 and q == 0 and t == 0 and s == 0:
            continue

        out.append({'factors': [m, q, t, s], 'label': label})
    return out


def _sigmoid(x: float) -> float:
    """Numerically stable sigmoid."""
    if x >= 0:
        e = math.exp(-x)
        return 1.0 / (1.0 + e)
    else:
        e = math.exp(x)
        return e / (1.0 + e)


_TEMPERATURE = 50.0  # divides the raw weighted average (0–100 scale) into logit space


def _predict(factors: list[float], weights: list[float]) -> float:
    """Return p(correct) for a factor vector given weights."""
    dot = sum(f * w for f, w in zip(factors, weights))
    return _sigmoid((dot - _TEMPERATURE) / _TEMPERATURE)


def _cross_entropy(labelled: list[dict], weights: list[float]) -> float:
    """Binary cross-entropy loss."""
    eps = 1e-9
    loss = 0.0
    for row in labelled:
        p = _predict(row['factors'], weights)
        y = row['label']
        loss -= y * math.log(p + eps) + (1 - y) * math.log(1 - p + eps)
    return loss / len(labelled)


def _fit_simplex_sgd(
    labelled: list[dict],
    lr: float = 0.005,
    n_epochs: int = 200,
    seed_weights: list[float] | None = None,
) -> list[float]:
    """Fit simplex weights via projected gradient descent.

    The simplex constraint (wᵢ ≥ 0, Σwᵢ = 1) is enforced after each
    step by projecting back onto the standard simplex.

    Gradient of the cross-entropy loss w.r.t. wⱼ:
        ∂L/∂wⱼ = (1/N) Σᵢ (p̂ᵢ − yᵢ) · fᵢⱼ / T
    where fᵢⱼ is the j-th factor score of sample i and T = TEMPERATURE.
    """
    n_factors = 4
    w = seed_weights[:] if seed_weights else [0.25] * n_factors
    # Project initial weights onto the simplex.
    w = _project_simplex(w)

    for _ in range(n_epochs):
        grad = [0.0] * n_factors
        n = len(labelled)
        for row in labelled:
            p = _predict(row['factors'], w)
            residual = (p - row['label']) / (n * _TEMPERATURE)
            for j in range(n_factors):
                grad[j] += residual * row['factors'][j]

        # Gradient step.
        for j in range(n_factors):
            w[j] -= lr * grad[j]

        # Project back onto the simplex.
        w = _project_simplex(w)

    return w


def _project_simplex(v: list[float]) -> list[float]:
    """Euclidean projection onto the standard probability simplex.

    Uses the O(n log n) algorithm: sort descending, find the largest k
    such that (sum_k v_j - 1) / k < v_k, then shift and clip.
    """
    n = len(v)
    u = sorted(v, reverse=True)
    css = 0.0
    rho = 0
    for j in range(n):
        css += u[j]
        if u[j] - (css - 1.0) / (j + 1) > 0:
            rho = j
    theta = (sum(u[:rho + 1]) - 1.0) / (rho + 1)
    return [max(0.0, x - theta) for x in v]


def _dict_to_list(weights: dict[str, float]) -> list[float]:
    return [weights.get(k, 0.25) for k in _FACTORS]


def _list_to_dict(weights: list[float]) -> dict[str, float]:
    return {k: round(weights[i], 4) for i, k in enumerate(_FACTORS)}


def _walk_forward_fit(labelled: list[dict]) -> tuple[float, dict[str, float]]:
    """Walk-forward weight fitting.

    Train on the first 60% of rows (burn-in), then slide forward
    one step at a time.  At each step:
      1. Fit weights on all rows seen so far (train set).
      2. Predict the *next* row's label.
      3. Record whether the prediction is correct.

    Returns (accuracy, fitted_weights_from_full_data).
    """
    n = len(labelled)
    burn_in = max(10, int(n * 0.60))
    if burn_in >= n:
        # Not enough data for a meaningful walk-forward — fit on all.
        w_list = _fit_simplex_sgd(labelled)
        return 0.0, _list_to_dict(w_list)

    correct = 0
    steps = 0
    w_list: list[float] = [0.25] * 4  # initial

    for split in range(burn_in, n):
        train = labelled[:split]
        test_row = labelled[split]

        w_list = _fit_simplex_sgd(train, seed_weights=w_list)
        p = _predict(test_row['factors'], w_list)
        pred_label = 1 if p >= 0.5 else 0
        if pred_label == test_row['label']:
            correct += 1
        steps += 1

    acc = correct / steps if steps > 0 else 0.0
    # Fit on the full dataset for the returned recommended weights.
    w_final = _fit_simplex_sgd(labelled, seed_weights=w_list)
    return acc, _list_to_dict(w_final)


def _walk_forward_eval(labelled: list[dict], weights: dict[str, float]) -> float:
    """Walk-forward accuracy of a fixed weight set (no fitting).

    Uses the same burn_in split as _walk_forward_fit so results are
    comparable.
    """
    n = len(labelled)
    burn_in = max(10, int(n * 0.60))
    if burn_in >= n:
        return 0.0

    w_list = _dict_to_list(weights)
    correct = 0
    steps = 0
    for split in range(burn_in, n):
        test_row = labelled[split]
        p = _predict(test_row['factors'], w_list)
        pred_label = 1 if p >= 0.5 else 0
        if pred_label == test_row['label']:
            correct += 1
        steps += 1

    return correct / steps if steps > 0 else 0.0


def _fmt(w: dict[str, float]) -> str:
    return ' '.join(f'{k}={v:.3f}' for k, v in w.items())
