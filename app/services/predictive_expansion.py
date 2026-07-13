"""Phase 26.60 — Predictive Expansion Pack.

This module is the THIRD experimental signal suite (after Lab Mode
and Strategy Tier).  It implements the 10 standard metrics + 4
"reality-breaker" advanced overlays from the integration spec:

Standard (always computed; per-toggle blended into ranking)
-----------------------------------------------------------
1.  msm_drift_premium            — expected drift of the current
                                   hidden Markov regime vs the long-run mean.
2.  ts_nonlinear_dependence      — forecast lift of a nonlinear model
                                   over a linear AR baseline (0..1).
3.  trend_curvature_pct          — local SSA-trend quadratic
                                   coefficient (% per period^2).
4.  lead_lag_influence           — predictive lift over an AR-only
                                   baseline when index/sector drivers
                                   are included (0..1).
5.  volofvol_regime_score        — probability the volatility process
                                   itself is in a high-vol-of-vol regime.
6.  multiscale_consistency       — directional agreement across
                                   1h, 5h, 1d, 5d, 20d horizons (-1..1).
7.  entropy_regime_stability     — 1 - normalised entropy of the
                                   rolling regime label sequence (0..1).
8.  drawdown_memory_score        — mean forward-return difference
                                   after deep drawdowns vs calm days.
9.  liq_adjusted_signal          — predictability score × liquidity
                                   score, both normalised to [0, 1].
10. ml_residual_edge             — z-scored residual of a tiny ridge-
                                   regression baseline vs a saturating
                                   nonlinear blend.

Reality-breaker overlays (opt-in, behind Advanced Experimental Mode)
--------------------------------------------------------------------
11. local_causal_cone_signal             (LCC)  — coherent upstream
                                                  driver field, σ-scaled.
12. quantum_path_interference_index      (QPII, field name kept for
                                                  compatibility) — now a
                                                  cross-model forecast
                                                  CONSENSUS score (see
                                                  `_model_consensus_score`).
                                                  Combines GARCH, regime-
                                                  switching, Hurst-tilt, and
                                                  block-bootstrap forecasts
                                                  via inverse-variance
                                                  weighting + Cochran's Q/I²
                                                  heterogeneity. The original
                                                  "complex-amplitude Monte-
                                                  Carlo interference" version
                                                  resampled a single Gaussian
                                                  model and was retired
                                                  2026-07 -- it could not,
                                                  even in principle, detect
                                                  anything about whether that
                                                  one model was correct.
13. local_lyapunov_volatility_exponent   (LLVE) — local nonlinear
                                                  trajectory divergence.
14. temporal_renormalization_score       (TRS)  — sign-flipped slope of
                                                  drift-to-vol vs log
                                                  horizon (small magnitude
                                                  = stable scale flow).

Composite multipliers
---------------------
* strategy_v2_rank_multiplier   ∈ [0.6, 1.4]
* regime_risk_multiplier        ∈ [0.6, 1.4]
* ml_rank_multiplier            ∈ [0.8, 1.2]
* liq_kelly_factor              ∈ [0.7, 1.3]
* reality_breaker_multiplier    ∈ [0.5, 1.5]  (DEFAULT OFF)

All formulas follow the PDF spec.  Where the spec demands inputs we
don't natively have (full cross-asset graph, transfer-entropy
network, ETF basket beta), we use **soft, transparent proxies
derived from cached data**:
  * Driver basket   → demeaned cumulative return as a market proxy.
  * Influence graph → AR(1) cross-correlations across the bucket
                      top-N symbols (filled in by the caller).
  * Markov regimes  → 2-state HMM on returns (already in Lab signals
                      indirectly; we recompute lean for this module).
The functions degrade to NEUTRAL outputs when data is missing — they
NEVER raise.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Sequence

import numpy as np

from app.services.garch_volatility import garch_forecast


_MIN_HISTORY = 40
_EPS = 1e-9


# =========================================================================
# Shared: walk-forward (expanding-window) out-of-sample R²
# =========================================================================
#
# Bucket-2 rework (2026-07): `ts_nonlinear_dependence` and
# `lead_lag_influence` used to compare two OLS fits on IN-SAMPLE R².
# That comparison is structurally biased: the "richer" model (more
# columns in X) can only ever match or beat the simpler model's
# in-sample fit, even when the extra columns are pure noise, because
# OLS in-sample R² is monotonic non-decreasing in the number of
# regressors. So the old "lift" was guaranteed to look like evidence
# of nonlinearity / cross-asset influence whether or not any existed.
#
# The fix: score both models on data they never saw when they were
# fit. `_forward_chain_r2` walks forward through the series in
# contiguous blocks -- for each block it fits on every observation
# strictly BEFORE the block (an expanding window, i.e. only the past)
# and scores the fit against the block's actual values. This is
# strictly causal (no shuffling, no peeking at t+1 to score t) and
# gives an honest, comparable R² for each model against a "predict
# the training-window mean" naive baseline.
def _forward_chain_r2(X: np.ndarray, y: np.ndarray, min_train: int, n_folds: int = 6) -> float | None:
    """Walk-forward, strictly-causal out-of-sample R² for a linear
    model with design matrix `X` (include an intercept column
    yourself if you want one) against target `y`.

    Splits the tail of the series (everything after `min_train`) into
    up to `n_folds` contiguous test blocks. Each block is scored using
    a model fit ONLY on rows that occur earlier in the series -- never
    on the block itself or anything after it. Squared errors are
    pooled across all folds before computing R², so a single easy or
    hard fold can't dominate the estimate.

    Returns None (not 0.0) when there isn't enough data to do this
    honestly -- callers should treat None as "no evidence either way",
    not "no dependence".
    """
    n = len(y)
    usable = n - min_train
    if usable < n_folds or usable < 2:
        return None
    fold_size = max(1, usable // n_folds)
    sse_model = 0.0
    sse_naive = 0.0
    n_scored = 0
    start = min_train
    while start < n:
        end = min(n, start + fold_size)
        if end <= start:
            break
        X_train, y_train = X[:start], y[:start]
        X_test, y_test = X[start:end], y[start:end]
        # Need strictly more training rows than free parameters, with
        # a little headroom, or the fit is unreliable / degenerate.
        if X_train.shape[0] < X_train.shape[1] + 3:
            start = end
            continue
        try:
            beta, *_ = np.linalg.lstsq(X_train, y_train, rcond=None)
            yhat = X_test @ beta
        except Exception:
            start = end
            continue
        if not np.all(np.isfinite(yhat)):
            start = end
            continue
        naive_pred = float(np.mean(y_train))
        sse_model += float(np.sum((y_test - yhat) ** 2))
        sse_naive += float(np.sum((y_test - naive_pred) ** 2))
        n_scored += y_test.size
        start = end
    if n_scored == 0 or sse_naive <= _EPS:
        return None
    return 1.0 - sse_model / (sse_naive + _EPS)


# =========================================================================
# Dataclass — per-symbol Phase 26.60 bundle
# =========================================================================
@dataclass
class PredictiveExpansionSignals:
    """Per-symbol Phase 26.60 bundle.  Safe defaults when history is
    insufficient — caller checks `available`."""
    available: bool = False
    n_obs: int = 0
    # Phase 26.61d — sentinel marking whether the 4 reality_breaker
    # overlays were actually computed (vs. left at default 0.0).  The
    # cache uses this to decide whether to recompute when a caller
    # asks for reality_breakers but the cached bundle didn't compute
    # them.
    reality_breakers_computed: bool = False

    # 10 standard metrics (in spec order)
    msm_drift_premium: float = 0.0
    ts_nonlinear_dependence: float = 0.0
    trend_curvature_pct: float = 0.0
    lead_lag_influence: float = 0.0
    volofvol_regime_score: float = 0.0
    multiscale_consistency: float = 0.0
    entropy_regime_stability: float = 0.0
    drawdown_memory_score: float = 0.0
    liq_adjusted_signal: float = 0.0
    ml_residual_edge: float = 0.0

    # 4 reality-breaker overlays (DEFAULT NEUTRAL — computed only
    # when the caller wants them; the dataclass carries values so the
    # snapshot row can show them even when the blend is OFF).
    local_causal_cone_signal: float = 0.0
    quantum_path_interference_index: float = 0.0
    local_lyapunov_volatility_exponent: float = 0.0
    temporal_renormalization_score: float = 0.0

    # Auxiliary diagnostics
    liquidity_score_norm: float = 0.5
    predictability_score_norm: float = 0.5
    notes: list = field(default_factory=list)


# =========================================================================
# Numerical helpers — all degrade to neutral on bad input
# =========================================================================
def _safe_returns(closes: Sequence[float]) -> np.ndarray:
    closes = np.asarray(closes, dtype=float)
    closes = closes[np.isfinite(closes) & (closes > 0)]
    if closes.size < 2:
        return np.empty(0, dtype=float)
    return np.diff(np.log(closes))


def _clamp(x: float, lo: float, hi: float) -> float:
    if not np.isfinite(x):
        return (lo + hi) / 2.0
    return max(lo, min(hi, float(x)))


# =========================================================================
# (1) msm_drift_premium  —  2-state HMM on returns; weighted regime drift
# =========================================================================
def _msm_drift_premium(returns: np.ndarray) -> float:
    """Lightweight 2-state Markov-switching mean approximation.

    Fits an EM-style 2-Gaussian mixture on returns (no transition
    matrix) and reports:
        sum_k p_T,k * mu_k  -  mean(returns)
    The simplification is intentional: we don't need exact filtered
    state probabilities for ranking — only a stable per-symbol signed
    "current regime drift premium" that the spec defines.  Returns a
    %-shaped daily-drift premium (i.e. multiplied by 100 in caller).
    """
    if returns.size < 30:
        return 0.0
    r = returns
    mu_global = float(np.mean(r))
    # Initialise: split by median
    med = float(np.median(r))
    a = r[r <= med]
    b = r[r >  med]
    if a.size < 5 or b.size < 5:
        return 0.0
    mu_a, mu_b = float(np.mean(a)), float(np.mean(b))
    sd_a = float(np.std(a)) + _EPS
    sd_b = float(np.std(b)) + _EPS
    pi_a, pi_b = a.size / r.size, b.size / r.size
    # Two EM iterations are enough for this purpose (we don't need
    # convergence — just a softly-weighted regime split).
    for _ in range(2):
        # E-step (Gaussian responsibilities)
        ra = pi_a * np.exp(-0.5 * ((r - mu_a) / sd_a) ** 2) / (sd_a + _EPS)
        rb = pi_b * np.exp(-0.5 * ((r - mu_b) / sd_b) ** 2) / (sd_b + _EPS)
        total = ra + rb + _EPS
        wa = ra / total
        wb = rb / total
        # M-step
        wsum_a = wa.sum() + _EPS
        wsum_b = wb.sum() + _EPS
        mu_a = float((wa * r).sum() / wsum_a)
        mu_b = float((wb * r).sum() / wsum_b)
        sd_a = float(math.sqrt(((wa * (r - mu_a) ** 2).sum() / wsum_a))) + _EPS
        sd_b = float(math.sqrt(((wb * (r - mu_b) ** 2).sum() / wsum_b))) + _EPS
        pi_a = float(wsum_a / r.size)
        pi_b = float(wsum_b / r.size)
    # Current regime probabilities — use the LAST observation's
    # responsibilities to pin "current" drift.
    last = float(r[-1])
    la = pi_a * math.exp(-0.5 * ((last - mu_a) / sd_a) ** 2) / (sd_a + _EPS)
    lb = pi_b * math.exp(-0.5 * ((last - mu_b) / sd_b) ** 2) / (sd_b + _EPS)
    total = la + lb + _EPS
    p_now_a, p_now_b = la / total, lb / total
    premium = (p_now_a * mu_a + p_now_b * mu_b) - mu_global
    # Return as PCT (the spec leaves units to the caller; we use %)
    return _clamp(premium * 100.0, -5.0, 5.0)


# =========================================================================
# (2) ts_nonlinear_dependence  —  AR(p) vs threshold-AR R² lift
# =========================================================================
def _ts_nonlinear_dependence(returns: np.ndarray) -> float:
    """Out-of-sample forecast lift of a *threshold*-AR(2) (different
    slopes when r_{t-1} >= 0 vs r_{t-1} < 0) over a linear AR(2),
    measured by strictly-causal walk-forward validation.

    FIX (Bucket-2 rework, 2026-07): the previous version compared
    IN-SAMPLE R² of the two fits. The threshold-AR has 5 free
    parameters vs the linear AR's 3, so it could only ever match or
    beat the linear model's in-sample fit -- even on pure noise. That
    guaranteed a "lift" whether or not the asset's returns had any
    real nonlinear structure, which made the factor closer to a
    parameter-count detector than a nonlinearity detector.

    Now both models are fit on an expanding window of *past* data only
    and scored on the *next* block of returns they never saw. A lift
    can only appear here if the threshold split genuinely improves
    forecast accuracy on data the model wasn't fit to -- the honest
    definition of "this asset behaves nonlinearly."

    Returns the out-of-sample R² lift, clamped to [0, 1] (a negative
    lift means the extra parameters overfit rather than helped, which
    we report as "no evidence of nonlinearity," i.e. 0.0).
    """
    if returns.size < 40:
        return 0.0
    r = returns
    # Design matrix for AR(2)
    X = np.column_stack([r[1:-1], r[:-2]])   # cols = lag-1, lag-2
    y = r[2:]
    if X.shape[0] < 30:
        return 0.0
    Xb = np.column_stack([np.ones(len(y)), X])
    # Threshold AR: separate slopes for sign of r_{t-1}
    sign1 = (X[:, 0] >= 0).astype(float)
    X_tar = np.column_stack([np.ones(len(y)),
                             X[:, 0] * sign1, X[:, 0] * (1 - sign1),
                             X[:, 1] * sign1, X[:, 1] * (1 - sign1)])
    min_train = max(15, X.shape[0] // 2)
    r2_lin_oos = _forward_chain_r2(Xb, y, min_train)
    r2_tar_oos = _forward_chain_r2(X_tar, y, min_train)
    if r2_lin_oos is None or r2_tar_oos is None:
        return 0.0
    lift = r2_tar_oos - r2_lin_oos
    return _clamp(lift, 0.0, 1.0)


# =========================================================================
# (3) trend_curvature_pct  —  Local quadratic on smoothed log-price tail
# =========================================================================
def _trend_curvature_pct(closes: np.ndarray, window: int = 20) -> float:
    """Fit  y_{t-i} = a + b·i + c·i²  on the last `window` log-prices.

    Returns  100 · c  (so units are "% curvature per step²").  Smoothed
    log-price uses a centred 5-point moving average — defensive against
    single-tick spikes.
    """
    if closes.size < window + 5:
        return 0.0
    arr = np.asarray(closes, dtype=float)
    arr = arr[np.isfinite(arr) & (arr > 0)]
    if arr.size < window + 5:
        return 0.0
    log_p = np.log(arr[-(window + 4):])
    # 5-pt centred MA (preserves edges via 'valid')
    kernel = np.ones(5) / 5.0
    smoothed = np.convolve(log_p, kernel, mode='valid')
    if smoothed.size < window:
        return 0.0
    y = smoothed[-window:]
    i = np.arange(window, dtype=float)
    # Fit  y = a + b i + c i²
    try:
        coef = np.polyfit(i, y, 2)
    except Exception:
        return 0.0
    c = float(coef[0])
    return _clamp(c * 100.0, -2.0, 2.0)


# =========================================================================
# (4) lead_lag_influence  —  AR vs AR+driver R² lift
# =========================================================================
def _lead_lag_influence(returns: np.ndarray, driver_returns: np.ndarray | None) -> float:
    """Out-of-sample forecast lift of AR(2) + 2 lagged driver returns
    over AR(2) alone, via the same walk-forward validation as
    `_ts_nonlinear_dependence`. Driver is typically a market/sector
    proxy. When no driver is supplied, returns 0.0 (neutral).

    FIX (Bucket-2 rework, 2026-07): same structural bug as
    `_ts_nonlinear_dependence` -- comparing in-sample R² meant adding
    the 2 driver columns could only help (or do nothing), never hurt,
    regardless of whether the driver actually leads this symbol. Two
    unrelated, uncorrelated random walks would still show a positive
    "lead-lag influence" under the old formula. Walk-forward
    validation removes that guarantee: the driver columns only earn a
    positive score if they improve forecasts on returns the model
    hadn't seen yet.
    """
    if driver_returns is None or returns.size < 40:
        return 0.0
    n_pairs = min(returns.size, driver_returns.size)
    if n_pairs < 40:
        return 0.0
    r = returns[-n_pairs:]
    d = driver_returns[-n_pairs:]
    X_ar = np.column_stack([r[1:-1], r[:-2]])
    y = r[2:]
    if X_ar.shape[0] < 30:
        return 0.0
    Xb = np.column_stack([np.ones(len(y)), X_ar])
    Xd = np.column_stack([Xb, d[1:-1], d[:-2]])
    min_train = max(15, X_ar.shape[0] // 2)
    r2_ar_oos = _forward_chain_r2(Xb, y, min_train)
    r2_d_oos = _forward_chain_r2(Xd, y, min_train)
    if r2_ar_oos is None or r2_d_oos is None:
        return 0.0
    lift = r2_d_oos - r2_ar_oos
    return _clamp(lift, 0.0, 1.0)


# =========================================================================
# (5) volofvol_regime_score  —  Heuristic HMM on |Δσ|
# =========================================================================
def _volofvol_regime_score(returns: np.ndarray) -> float:
    """Compute rolling 10-day realised vol, then a 2-state HMM-like
    probability that the LAST observation belongs to the high-vol-of-
    vol regime (= top quartile of |Δσ| historically).

    Simplification: instead of a full Viterbi/forward pass we report
        sigmoid( (|Δσ_now| - median) / scale )
    where `scale` = MAD of |Δσ|.  This is monotone in vol-of-vol and
    bounded in [0, 1], which is what the spec asks for.
    """
    if returns.size < 30:
        return 0.0
    r = returns
    # 10-day rolling std
    n_win = 10
    sigmas = np.array([
        float(np.std(r[max(0, i - n_win): i])) for i in range(n_win, r.size + 1)
    ])
    if sigmas.size < 5:
        return 0.0
    dsig = np.abs(np.diff(sigmas))
    if dsig.size < 5:
        return 0.0
    med = float(np.median(dsig))
    mad = float(np.median(np.abs(dsig - med))) + _EPS
    last = float(dsig[-1])
    z = (last - med) / (1.4826 * mad)
    return _clamp(1.0 / (1.0 + math.exp(-z)), 0.0, 1.0)


# =========================================================================
# (6) multiscale_consistency  —  Sign agreement across horizons
# =========================================================================
def _multiscale_consistency(per_horizon_drifts_pct: dict[str, float]) -> float:
    """Average signed agreement across horizons.

    Returns the weighted mean of sign(d_h) with weights matching the
    PDF's "heavier on longer horizons" guidance:
        1h:0.5  5h:0.7  1d:1.0  5d:1.3  20d:1.5

    Accepts horizon keys in either the trading-style form
    ('1h_hold', '5h_hold', ...) or the forward-block form
    ('forward_1h', 'forward_5h', ...) — auto-normalised below.
    """
    weights = {
        '1h_hold':  0.5,
        '5h_hold':  0.7,
        '1d_hold':  1.0,
        '5d_hold':  1.3,
        '20d_hold': 1.5,
    }
    # Phase 26.61d — auto-normalise alternative key form.
    _alias = {
        'forward_1h':  '1h_hold',
        'forward_5h':  '5h_hold',
        'forward_1d':  '1d_hold',
        'forward_5d':  '5d_hold',
        'forward_20d': '20d_hold',
    }
    normalised = {}
    for k, v in (per_horizon_drifts_pct or {}).items():
        canon = _alias.get(k, k)
        normalised[canon] = v
    num = 0.0
    den = 0.0
    for k, w in weights.items():
        v = normalised.get(k)
        if v is None or not np.isfinite(v):
            continue
        a = 1.0 if v > 0 else (-1.0 if v < 0 else 0.0)
        num += w * a
        den += abs(w)
    if den < _EPS:
        return 0.0
    return _clamp(num / den, -1.0, 1.0)


# =========================================================================
# (7) entropy_regime_stability  —  Inverse entropy of recent regime labels
# =========================================================================
def _entropy_regime_stability(returns: np.ndarray) -> float:
    """Encode the last 60 days into "bull / bear / flat" labels using
    a 5-day rolling-mean sign of returns, then report:
        1 - H/log(K)
    where K = 3 labels.
    """
    if returns.size < 30:
        return 0.5
    r = returns[-60:]
    if r.size < 10:
        return 0.5
    win = 5
    means = np.array([
        float(np.mean(r[max(0, i - win): i])) for i in range(win, r.size + 1)
    ])
    threshold = float(np.std(r)) * 0.25
    labels = np.where(means > threshold, 1,
                     np.where(means < -threshold, -1, 0))
    if labels.size < 5:
        return 0.5
    # Empirical PMF
    _, counts = np.unique(labels, return_counts=True)
    p = counts / counts.sum()
    K = 3
    h = -float(np.sum(p * np.log(p + _EPS)))
    return _clamp(1.0 - h / math.log(K), 0.0, 1.0)


# =========================================================================
# (8) drawdown_memory_score  —  Mean post-drawdown forward return
# =========================================================================
def _drawdown_memory_score(closes: np.ndarray, h: int = 5) -> float:
    """E[R_{t..t+h} | DD_t <= -θ]  -  E[R_{t..t+h} | |DD_t| < θ/2]

    θ is chosen as the 25th percentile of historical drawdowns (so
    "large drawdown" tightens automatically for low-vol names).
    Returns the difference in mean forward returns, as a daily-pct
    figure clamped to ±5%.
    """
    if closes.size < 60:
        return 0.0
    arr = np.asarray(closes, dtype=float)
    arr = arr[np.isfinite(arr) & (arr > 0)]
    if arr.size < 60:
        return 0.0
    # Rolling 20-day high
    n_win = 20
    rolling_max = np.maximum.accumulate(arr[-(arr.size - n_win):])  # crude proxy
    if rolling_max.size < arr.size - n_win:
        return 0.0
    # Use simple drawdown = price / running 20-day high - 1
    high = np.array([float(np.max(arr[max(0, i - n_win): i + 1])) for i in range(arr.size)])
    dd = arr / (high + _EPS) - 1.0
    # Forward h-day log return
    log_p = np.log(arr + _EPS)
    fwd = np.empty(arr.size)
    fwd[:] = np.nan
    if arr.size <= h:
        return 0.0
    fwd[:-h] = log_p[h:] - log_p[:-h]
    # Threshold = 25th percentile of dd (most negative)
    valid_dd = dd[np.isfinite(dd)]
    if valid_dd.size < 20:
        return 0.0
    theta = float(np.percentile(valid_dd, 10))  # ~10th percentile = "deep" drawdown
    half_theta = abs(theta) / 2.0
    # Population masks (drop NaN forward returns)
    finite_fwd = np.isfinite(fwd)
    mask_deep = finite_fwd & (dd <= theta)
    mask_calm = finite_fwd & (np.abs(dd) < half_theta)
    if mask_deep.sum() < 3 or mask_calm.sum() < 3:
        return 0.0
    diff_pct = (float(np.mean(fwd[mask_deep])) - float(np.mean(fwd[mask_calm]))) * 100.0
    return _clamp(diff_pct, -5.0, 5.0)


# =========================================================================
# (9) liq_adjusted_signal  —  pred_score × liq_score
# =========================================================================
def _liq_adjusted_signal(predictability_norm: float, liq_norm: float) -> float:
    pred = _clamp(predictability_norm, 0.0, 1.0)
    liq = _clamp(liq_norm, 0.0, 1.0)
    return _clamp(pred * liq, 0.0, 1.0)


def _predictability_norm(returns: np.ndarray) -> float:
    """Convert RQA/MI/spec/LZ-style predictability into a [0,1] score.

    We use compact proxies derived from `returns`:
      * |AR(1)|             — autocorrelation strength
      * |Hurst - 0.5|·2     — long-memory strength
      * 1 - normalised-entropy on quantised returns (= structure)
    """
    if returns.size < 20:
        return 0.5
    r = returns
    # AR(1)
    try:
        c0 = float(np.dot(r[:-1], r[:-1])) + _EPS
        ar1 = float(np.dot(r[:-1], r[1:]) / c0)
    except Exception:
        ar1 = 0.0
    ar1_term = min(1.0, abs(ar1) * 5.0)
    # Cheap Hurst proxy via variance ratio of 1d vs 5d returns
    if r.size >= 30:
        var1 = float(np.var(r))
        r5 = np.add.reduceat(r, np.arange(0, r.size, 5))[: r.size // 5]
        var5 = float(np.var(r5)) if r5.size >= 3 else var1
        ratio = var5 / (5.0 * var1 + _EPS)
        h_term = min(1.0, abs(math.log(max(ratio, _EPS)) / math.log(5.0) + 0.5 - 0.5) * 2.0)
    else:
        h_term = 0.0
    # Entropy on sign-quantised returns
    signs = np.sign(r)
    pos = float(np.mean(signs > 0))
    neg = float(np.mean(signs < 0))
    flat = max(0.0, 1.0 - pos - neg)
    p = np.array([pos, neg, flat])
    p = p[p > 0]
    h = -float(np.sum(p * np.log(p + _EPS)))
    h_norm = h / math.log(3) if p.size > 1 else 0.0
    entropy_term = 1.0 - h_norm
    return _clamp((ar1_term + h_term + entropy_term) / 3.0, 0.0, 1.0)


def _liquidity_norm_from_row(row: dict | None) -> float:
    """Best-effort liquidity score from the existing row payload.

    Tries (in order):
      * row.get('liquidity_score') / 100         (if present, expects 0..100)
      * factor_breakdown.market.dark_pool_proxy  (0..100)
      * fallback 0.5 (neutral)
    """
    if not row:
        return 0.5
    direct = row.get('liquidity_score')
    if isinstance(direct, (int, float)) and direct > 0:
        return _clamp(direct / 100.0, 0.0, 1.0)
    fb = row.get('factor_breakdown') or {}
    market = fb.get('market') or {}
    dp = market.get('dark_pool_proxy') or market.get('dark_pool_attraction') or {}
    if isinstance(dp, dict):
        s = dp.get('score')
        if isinstance(s, (int, float)) and s > 0:
            return _clamp(s / 100.0, 0.0, 1.0)
    return 0.5


# =========================================================================
# (10) ml_residual_edge  —  tiny ridge baseline vs saturating blend
# =========================================================================
def _ml_residual_edge(returns: np.ndarray) -> float:
    """Train a tiny in-sample ridge regression of r_{t+1} on
    [r_t, r_{t-1}, r_{t-4}] vs a saturating nonlinear blend
    (clipped lag-1 + sign-of-lag-2) and report (ML - linear) / σ.

    No external ML libs — pure numpy, deterministic.  Soft-fails to 0
    when history is too short or any matrix is singular.
    """
    if returns.size < 40:
        return 0.0
    r = returns
    X = np.column_stack([r[3:-1], r[2:-2], r[:-4]])
    y = r[4:]
    if X.shape[0] < 20 or X.shape[0] != y.size:
        return 0.0
    # Ridge baseline (small λ)
    lam = 1e-3
    try:
        Xb = np.column_stack([np.ones(len(y)), X])
        XtX = Xb.T @ Xb + lam * np.eye(Xb.shape[1])
        beta_lin = np.linalg.solve(XtX, Xb.T @ y)
        yhat_lin_last = float(beta_lin[0] + np.dot(X[-1], beta_lin[1:]))
    except Exception:
        return 0.0
    # Saturating "ML" blend: clipped lag-1 + sign-of-lag-2 + lag-4 dampened
    yhat_ml_last = (
        np.clip(r[-1], -0.03, 0.03) * 0.5
        + float(np.sign(r[-2])) * 0.5 * abs(r[-2])
        + r[-4] * 0.2
    )
    sigma = float(np.std(r)) + _EPS
    edge = (yhat_ml_last - yhat_lin_last) / sigma
    return _clamp(edge, -3.0, 3.0)


# =========================================================================
# 11–14: Reality-breaker overlays — all degrade to NEUTRAL (0.0) by default
# =========================================================================
def validate_model_consensus_score(closes: Sequence[float], horizon: int = 5,
                                    min_train: int = 100, step: int = 5,
                                    driver_closes: Sequence[float] | None = None,
                                    row: dict | None = None) -> dict:
    """Walk-forward validation of `_model_consensus_score` against
    REALIZED forward returns it never saw.

    This is the check the old QPII never got: a factor being built
    from legitimate individual techniques doesn't mean the COMBINED
    number actually predicts anything. At each evaluation point we
    compute the consensus score using only closes (and driver closes,
    if supplied) up to that point -- nothing later -- then compare it
    to the actual `horizon`-day forward return once that data would
    have become available.

    `driver_closes` (a market/sector proxy's own close series, same
    length/alignment as `closes`) and `row` (for the GEX regime sign)
    are optional -- without at least one of them, `_model_consensus_score`
    now correctly returns 0.0 for every fold (no independent
    confirmation available), which will show up here as
    `n_nonzero_scores: 0` rather than a misleadingly "clean" result.

    Returns
    -------
    dict with:
      n_scored                     -- how many (score, realized) pairs were evaluated
      n_nonzero_scores             -- how many of those actually had an
                                       independent view available (nonzero score)
      correlation                  -- Pearson r between score and realized return
      directional_hit_rate         -- accuracy of sign(score) vs sign(realized),
                                       restricted to |score| >= 0.1 (near-zero
                                       scores make no directional claim)
      directional_hit_rate_confident -- same, restricted to |score| >= 1.0
      baseline_hit_rate            -- fraction of realized returns that were
                                       positive (the naive "always guess up"
                                       or "always guess down" rate, whichever
                                       is larger)
    Returns {'available': False} if there isn't enough history to test.
    """
    closes_arr = np.asarray(list(closes), dtype=float)
    closes_arr = closes_arr[np.isfinite(closes_arr) & (closes_arr > 0)]
    n = closes_arr.size
    if n < min_train + horizon + 20:
        return {'available': False, 'reason': 'insufficient_history', 'n_obs': int(n)}

    driver_closes_arr = None
    if driver_closes is not None:
        driver_closes_arr = np.asarray(list(driver_closes), dtype=float)
        driver_closes_arr = driver_closes_arr[np.isfinite(driver_closes_arr) & (driver_closes_arr > 0)]
        if driver_closes_arr.size != n:
            # Must stay aligned with `closes` at each fold cutoff -- if
            # lengths don't match we can't safely slice both the same
            # way, so drop the driver rather than risk misaligned data.
            driver_closes_arr = None

    scores, realized = [], []
    t = min_train
    while t + horizon < n:
        train_closes = closes_arr[:t + 1]
        train_rets = np.diff(np.log(train_closes))
        train_driver_rets = None
        if driver_closes_arr is not None:
            train_driver_closes = driver_closes_arr[:t + 1]
            train_driver_rets = np.diff(np.log(train_driver_closes))
        try:
            s = _model_consensus_score(train_rets, train_closes, horizon=horizon,
                                        driver_returns=train_driver_rets, row=row)
        except Exception:
            t += step
            continue
        fwd_ret = float(np.log(closes_arr[t + horizon] / closes_arr[t]))
        scores.append(s)
        realized.append(fwd_ret)
        t += step

    n_scored = len(scores)
    if n_scored < 10:
        return {'available': False, 'reason': 'too_few_folds', 'n_scored': n_scored}

    scores_arr = np.array(scores)
    realized_arr = np.array(realized)
    n_nonzero = int(np.sum(scores_arr != 0.0))
    if np.std(scores_arr) <= _EPS or np.std(realized_arr) <= _EPS:
        corr = 0.0
    else:
        try:
            corr = float(np.corrcoef(scores_arr, realized_arr)[0, 1])
            if not np.isfinite(corr):
                corr = 0.0
        except Exception:
            corr = 0.0

    def _hit_rate(min_abs_score: float) -> float | None:
        mask = np.abs(scores_arr) >= min_abs_score
        if not np.any(mask):
            return None
        agree = np.sign(scores_arr[mask]) == np.sign(realized_arr[mask])
        return float(np.mean(agree))

    pos_frac = float(np.mean(realized_arr > 0))
    return {
        'available': True,
        'n_scored': n_scored,
        'n_nonzero_scores': n_nonzero,
        'correlation': round(corr, 4),
        'directional_hit_rate': _hit_rate(0.1),
        'directional_hit_rate_confident': _hit_rate(1.0),
        'baseline_hit_rate': round(max(pos_frac, 1.0 - pos_frac), 4),
    }


def _local_causal_cone_signal(returns: np.ndarray, driver_returns: np.ndarray | None) -> float:
    """Cone-weighted directional driver-field signal.

    Without a true cross-asset graph, we approximate the causal cone
    with the SIGN-CONFIRMED lagged contribution of the driver basket
    over the last 5 lags:
        F = sum_{k=1..5} α_k · driver_return_{t-k} · sign(corr(driver, sym))
    """
    if driver_returns is None or returns.size < 10 or driver_returns.size < 10:
        return 0.0
    n = min(returns.size, driver_returns.size)
    r = returns[-n:]
    d = driver_returns[-n:]
    if n < 20:
        return 0.0
    sigma = float(np.std(r)) + _EPS
    # Sign of contemporaneous correlation
    try:
        rho = float(np.corrcoef(r, d)[0, 1])
        if not np.isfinite(rho):
            rho = 0.0
    except Exception:
        rho = 0.0
    sgn = 1.0 if rho >= 0 else -1.0
    alphas = np.array([0.5, 0.3, 0.15, 0.08, 0.04])
    lags = min(len(alphas), n - 1)
    contribution = float(np.sum(alphas[:lags] * d[-(lags + 1):-1][::-1])) * sgn
    return _clamp(contribution / sigma, -3.0, 3.0)


def _local_hurst_rs(returns: np.ndarray, window: int = 60) -> float:
    """Rescaled-range (R/S) Hurst exponent over the trailing `window`
    returns (Hurst 1951 / Mandelbrot & Wallis 1969).

    H > 0.5  → trending / positively autocorrelated series
    H ≈ 0.5  → no persistence (random walk)
    H < 0.5  → mean-reverting / anti-persistent series

    This is a real, well-known statistic (not the invented QPII
    "phase"). Used here purely to decide whether recent momentum
    should be extrapolated or faded -- it earns its role in the
    consensus by being one of several genuinely different views, not
    by being declared correct.
    """
    r = returns[-window:] if returns.size > window else returns
    n = r.size
    if n < 20:
        return 0.5
    mean = float(np.mean(r))
    dev = r - mean
    cum = np.cumsum(dev)
    R = float(np.max(cum) - np.min(cum))
    S = float(np.std(r))
    if S <= _EPS or R <= _EPS:
        return 0.5
    rs = R / S
    try:
        h = math.log(rs) / math.log(n)
    except (ValueError, ZeroDivisionError):
        return 0.5
    return float(min(1.0, max(0.0, h)))


def _block_bootstrap_forecast(returns: np.ndarray, horizon: int,
                               n_resamples: int = 1000, block: int = 5) -> tuple[float, float]:
    """Empirical block-bootstrap forecast of the `horizon`-step
    cumulative log return.

    Unlike a GBM Monte Carlo (which assumes Gaussian i.i.d. shocks),
    this resamples contiguous BLOCKS of the asset's own historical
    returns with replacement -- so fat tails, skew, and short-range
    autocorrelation in the real data all carry through into the
    forecast distribution, instead of being assumed away.

    Returns (mean, std) of the simulated horizon-return distribution.
    Deterministic given the input series (seeded from its own tail),
    matching this module's existing determinism convention.
    """
    n = returns.size
    if n < block * 4:
        return 0.0, float(np.std(returns)) * math.sqrt(max(1, horizon)) + _EPS
    seed = int(abs(hash(tuple(np.round(returns[-10:], 6).tolist()))) % (2**31))
    rng = np.random.RandomState(seed)
    n_blocks_needed = int(math.ceil(horizon / block))
    max_start = n - block
    totals = np.empty(n_resamples, dtype=float)
    for i in range(n_resamples):
        starts = rng.randint(0, max_start + 1, size=n_blocks_needed)
        path = np.concatenate([returns[s:s + block] for s in starts])[:horizon]
        totals[i] = float(np.sum(path))
    return float(np.mean(totals)), float(np.std(totals)) + _EPS


def _gex_regime_sign_from_row(row: dict | None) -> int:
    """Read dealer-gamma-exposure regime sign from a snapshot row, if
    present.  +1 = positive gamma (dealers hedge by fading moves →
    mean-reverting).  -1 = negative gamma (dealers hedge by chasing
    moves → momentum-amplifying).  0 = no options data / no material
    exposure.

    This is genuinely INDEPENDENT data: it comes from the options
    market's positioning, not from a transform of the symbol's own
    historical price series -- which is exactly what the other views
    in this ensemble were missing (see `_model_consensus_score`).
    """
    if not row:
        return 0
    gamma = row.get('options_gamma') or {}
    sign = gamma.get('regime_sign')
    if sign in (1, -1, 0):
        return int(sign)
    label = str(gamma.get('regime') or gamma.get('level_label') or '').lower()
    if 'positive' in label or 'mean-rever' in label:
        return 1
    if 'negative' in label or 'amplif' in label or 'squeeze' in label:
        return -1
    return 0


def _ar_driver_point_forecast(returns: np.ndarray, driver_returns: np.ndarray,
                               horizon: int) -> tuple[float, float] | None:
    """Fit AR(2) + 2 lagged driver-return terms on the full available
    history and return a (drift, sigma) forecast for the next
    `horizon` periods. Returns None if there isn't enough paired
    history to fit reliably.
    """
    n_pairs = min(returns.size, driver_returns.size)
    if n_pairs < 40:
        return None
    r = returns[-n_pairs:]
    d = driver_returns[-n_pairs:]
    X = np.column_stack([r[1:-1], r[:-2]])
    y = r[2:]
    if X.shape[0] < 30:
        return None
    Xd = np.column_stack([np.ones(len(y)), X[:, 0], X[:, 1], d[1:-1], d[:-2]])
    try:
        beta, *_ = np.linalg.lstsq(Xd, y, rcond=None)
        resid = y - Xd @ beta
    except Exception:
        return None
    x_last = np.array([1.0, r[-1], r[-2], d[-1], d[-2]])
    one_step = float(x_last @ beta)
    if not np.isfinite(one_step):
        return None
    drift = one_step * horizon
    sigma = float(np.std(resid)) * math.sqrt(horizon) + _EPS
    return drift, sigma


def _model_consensus_score(returns: np.ndarray, closes: np.ndarray, horizon: int = 5,
                            driver_returns: np.ndarray | None = None,
                            row: dict | None = None) -> float:
    """Cross-model forecast-consensus score.

    Replaces the old `quantum_path_interference_index` ("Mock QPII"),
    which resampled ONE model (a single Gaussian GBM fit) 30 times and
    dressed the dispersion of that single model's own noise up as
    complex-amplitude "interference."

    HISTORY: the first rework (2026-07) replaced the fake physics with
    four legitimate techniques (GARCH, regime-switching, Hurst tilt,
    block bootstrap) combined via inverse-variance weighting + Cochran's
    Q/I² heterogeneity. That was real math, but `validate_model_consensus_score`
    caught a real problem with it: all four views were transforms of the
    SAME short window of the SAME price series, so they could spuriously
    "agree" just from sharing sampling noise -- not from independently
    confirming a real signal. Walk-forward testing showed it did not
    reliably beat a naive baseline even when genuine drift was present.

    This version fixes that by requiring at least one view built from
    data that is NOT derived from the symbol's own price history:

      * Cross-asset driver view -- AR(2) + lagged driver-return
        regression. Only used if `lead_lag_influence`'s own
        walk-forward validation (see Bucket-2 fix) shows this
        driver relationship actually holds up out-of-sample for THIS
        symbol; a driver that doesn't pass that test is not used.
      * Dealer-gamma-exposure (GEX) regime view -- options-market
        positioning (independent of price history) decides whether to
        extrapolate or fade recent momentum, with the Hurst exponent
        as a secondary confirmation.

    GARCH and an empirical block bootstrap (both price-derived) are
    still included as supporting views when at least one independent
    view is present, since volatility-clustering and fat-tail shape
    are still useful information -- they're just not trustworthy
    enough ALONE to certify a consensus, which is exactly what the
    validator demonstrated.

    If NEITHER independent view is available for this symbol (no
    driver series, or no options data), this returns 0.0 rather than
    compute a consensus from price-only views -- per the validator's
    finding, that consensus is not reliable enough to ship as signal.
    The honest answer when we don't have independent confirmation is
    "no signal," not a number that looks confident anyway.

    Returns a signed score clamped to [-2, 2].
    """
    if returns.size < 40:
        return 0.0
    r = returns[-120:] if returns.size > 120 else returns

    independent_drifts: list[float] = []
    independent_sigmas: list[float] = []

    # --- Independent view: cross-asset driver regression ------------
    if driver_returns is not None and driver_returns.size >= 40:
        lift = _lead_lag_influence(r, driver_returns)  # walk-forward validated (Bucket 2)
        if lift > 0.05:  # driver info must have shown real out-of-sample value for THIS symbol
            fc = _ar_driver_point_forecast(r, driver_returns, horizon)
            if fc is not None:
                independent_drifts.append(fc[0])
                independent_sigmas.append(fc[1])

    # --- Independent view: dealer-gamma-exposure regime -------------
    gex_sign = _gex_regime_sign_from_row(row)
    if gex_sign != 0:
        hurst = _local_hurst_rs(r)
        recent_momentum = float(np.mean(r[-horizon * 2:])) * horizon
        # Positive gamma (mean-reverting regime): fade recent momentum.
        # Negative gamma (momentum-amplifying regime): follow it.
        # Hurst confirms/dampens: only lean hard into "follow" if the
        # series is also genuinely trending (H > 0.5).
        if gex_sign == 1:
            gex_drift = -recent_momentum * 0.5
        else:
            gex_drift = recent_momentum * _clamp(0.5 + hurst, 0.5, 1.5)
        independent_drifts.append(gex_drift)
        independent_sigmas.append(float(np.std(r)) * math.sqrt(horizon) + _EPS)

    n_independent = len(independent_drifts)
    if n_independent == 0:
        return 0.0  # no independent confirmation available -- no signal, not a guess

    # --- Supporting views (price-derived; kept for vol/tail realism) -
    drift_garch = float(np.mean(r[-40:])) * horizon
    try:
        gf = garch_forecast(closes, horizon)
        sigma_garch = max(_EPS, gf.h_period_sigma / 100.0)
    except Exception:
        sigma_garch = float(np.std(r)) * math.sqrt(horizon) + _EPS
    drift_boot, sigma_boot = _block_bootstrap_forecast(r, horizon)

    drifts = np.array(independent_drifts + [drift_garch, drift_boot])
    sigmas = np.array(independent_sigmas + [sigma_garch, sigma_boot])
    weights = 1.0 / (sigmas ** 2)
    w_sum = float(np.sum(weights))
    if w_sum <= _EPS:
        return 0.0

    consensus_mean = float(np.sum(weights * drifts) / w_sum)
    pooled_sigma = math.sqrt(1.0 / w_sum)

    k = drifts.size
    q_stat = float(np.sum(weights * (drifts - consensus_mean) ** 2))
    df = k - 1
    if q_stat > _EPS and df > 0:
        i_squared = _clamp((q_stat - df) / q_stat, 0.0, 1.0)
    else:
        i_squared = 0.0
    agreement = 1.0 - i_squared

    t_stat = consensus_mean / (pooled_sigma + _EPS)
    magnitude = min(1.0, abs(t_stat) / 2.0)
    sign = 1.0 if consensus_mean > 0 else (-1.0 if consensus_mean < 0 else 0.0)

    # Explicit, honest discount: with only 1 of 2 possible independent
    # views available, don't let the score reach full strength -- cap
    # it proportional to how much genuine independent confirmation we
    # actually have (2 independent views = full strength).
    independence_factor = min(1.0, n_independent / 2.0)

    score = sign * agreement * magnitude * independence_factor
    return _clamp(score * 2.0, -2.0, 2.0)


def _local_lyapunov_volatility_exponent(returns: np.ndarray, m: int = 3) -> float:
    """Crude local Lyapunov exponent: nearest-neighbour separation
    growth on delay-embedded returns.  Returns the slope, clamped.
    """
    if returns.size < 50:
        return 0.0
    r = returns[-100:]
    n = r.size - (m - 1)
    if n < 20:
        return 0.0
    emb = np.column_stack([r[i: i + n] for i in range(m)])
    # For each point, find its single nearest neighbour (skip i±1 to
    # avoid trivial proximity).
    lambdas = []
    for i in range(n - 5):
        dists = np.linalg.norm(emb - emb[i], axis=1)
        # Mask near-i indices
        for off in (-2, -1, 0, 1, 2):
            j = i + off
            if 0 <= j < n:
                dists[j] = np.inf
        j_star = int(np.argmin(dists))
        if j_star >= n - 5:
            continue
        d0 = float(dists[j_star]) + _EPS
        d1 = float(np.linalg.norm(emb[i + 1] - emb[j_star + 1])) + _EPS
        ld = math.log(d1 / d0)
        if np.isfinite(ld):
            lambdas.append(ld)
    if not lambdas:
        return 0.0
    return _clamp(float(np.mean(lambdas)), -1.0, 1.0)


def _temporal_renormalization_score(per_horizon_drift_to_vol: dict[str, float]) -> float:
    """β(θ) = d θ / d log h  approximated from discrete horizon points.

    Accepts horizon keys in either the trading-style form
    ('1h_hold', '5h_hold', ...) or the forward-block form
    ('forward_1h', 'forward_5h', ...) — auto-normalised below.
    """
    horizon_order = ['1h_hold', '5h_hold', '1d_hold', '5d_hold', '20d_hold']
    _alias = {
        'forward_1h':  '1h_hold',
        'forward_5h':  '5h_hold',
        'forward_1d':  '1d_hold',
        'forward_5d':  '5d_hold',
        'forward_20d': '20d_hold',
    }
    normalised = {}
    for k, v in (per_horizon_drift_to_vol or {}).items():
        canon = _alias.get(k, k)
        normalised[canon] = v
    xs, ys = [], []
    for i, k in enumerate(horizon_order):
        v = normalised.get(k)
        if v is None or not np.isfinite(v):
            continue
        xs.append(float(i))
        ys.append(float(v))
    if len(xs) < 3:
        return 0.0
    try:
        slope = float(np.polyfit(xs, ys, 1)[0])
    except Exception:
        return 0.0
    return _clamp(-abs(slope), -2.0, 2.0)


# =========================================================================
# Top-level builder — compute all 14 metrics for a symbol
# =========================================================================
def compute_predictive_expansion(
    closes: Sequence[float],
    *,
    driver_returns: Sequence[float] | None = None,
    per_horizon_drifts_pct: dict[str, float] | None = None,
    per_horizon_drift_to_vol: dict[str, float] | None = None,
    row: dict | None = None,
    include_reality_breakers: bool = False,
) -> PredictiveExpansionSignals:
    """Compute the Phase 26.60 bundle for a single symbol.

    Parameters
    ----------
    closes
        Daily closes (most-recent last).  Need ≥ 40 for full output.
    driver_returns
        Optional sector/index daily returns aligned by length.  When
        absent, lead_lag_influence and LCC degrade to 0.0.
    per_horizon_drifts_pct
        Dict keyed by horizon ('1h_hold', '5h_hold', ...) with the
        block's `drift_pct` value.  Used by multiscale_consistency.
    per_horizon_drift_to_vol
        Dict keyed by horizon with `drift_pct / sigma_pct`.  Used by
        the reality-breaker TRS overlay.
    row
        Existing snapshot row (for liquidity score lookup).
    include_reality_breakers
        Compute the 4 advanced overlays when True (default False —
        the spec mandates default-OFF for these).
    """
    out = PredictiveExpansionSignals()
    closes_arr = np.asarray(list(closes), dtype=float)
    closes_arr = closes_arr[np.isfinite(closes_arr) & (closes_arr > 0)]
    out.n_obs = int(closes_arr.size)
    if closes_arr.size < _MIN_HISTORY:
        out.notes.append(f'insufficient_history n={closes_arr.size}')
        return out
    rets = _safe_returns(closes_arr)
    if rets.size < _MIN_HISTORY - 1:
        return out

    driver_arr = (
        np.asarray(list(driver_returns), dtype=float)
        if driver_returns is not None and len(driver_returns) > 0
        else None
    )
    drifts = per_horizon_drifts_pct or {}
    drift_to_vol = per_horizon_drift_to_vol or {}

    # Each metric guarded by its own try — never let one bad symbol
    # blow up the whole bundle.
    def _safe(name, fn, *args, default=0.0, **kwargs):
        try:
            v = float(fn(*args, **kwargs))
            if not np.isfinite(v):
                v = float(default)
            setattr(out, name, v)
        except Exception as exc:  # noqa: BLE001
            out.notes.append(f'{name} failed: {exc.__class__.__name__}')
            setattr(out, name, float(default))

    _safe('msm_drift_premium',         _msm_drift_premium, rets)
    _safe('ts_nonlinear_dependence',   _ts_nonlinear_dependence, rets)
    _safe('trend_curvature_pct',       _trend_curvature_pct, closes_arr)
    _safe('lead_lag_influence',        _lead_lag_influence, rets, driver_arr)
    _safe('volofvol_regime_score',     _volofvol_regime_score, rets)
    _safe('multiscale_consistency',    _multiscale_consistency, drifts)
    _safe('entropy_regime_stability',  _entropy_regime_stability, rets)
    _safe('drawdown_memory_score',     _drawdown_memory_score, closes_arr)
    _safe('ml_residual_edge',          _ml_residual_edge, rets)

    # Liquidity-adjusted: derived
    pred_norm = _predictability_norm(rets)
    liq_norm = _liquidity_norm_from_row(row)
    out.predictability_score_norm = pred_norm
    out.liquidity_score_norm = liq_norm
    out.liq_adjusted_signal = _liq_adjusted_signal(pred_norm, liq_norm)

    if include_reality_breakers:
        _safe('local_causal_cone_signal', _local_causal_cone_signal, rets, driver_arr)
        _safe('quantum_path_interference_index', _model_consensus_score, rets, closes_arr,
              driver_returns=driver_arr, row=row)
        _safe('local_lyapunov_volatility_exponent', _local_lyapunov_volatility_exponent, rets)
        _safe('temporal_renormalization_score', _temporal_renormalization_score, drift_to_vol)
        out.reality_breakers_computed = True
    else:
        # Default-OFF: neutral values, no telemetry surface
        out.local_causal_cone_signal = 0.0
        out.quantum_path_interference_index = 0.0
        out.local_lyapunov_volatility_exponent = 0.0
        out.temporal_renormalization_score = 0.0

    out.available = True
    return out


# =========================================================================
# Composite multipliers
# =========================================================================
def _tanh_band(raw: float, gain: float, half_band: float) -> float:
    """Convert a centred raw score into a multiplier in
    [1 - half_band, 1 + half_band] via tanh(raw/gain)."""
    return 1.0 + half_band * math.tanh(raw / gain)


def compute_strategy_v2_rank_multiplier(s: PredictiveExpansionSignals) -> float:
    """strategy_v2_rank_multiplier ∈ [0.6, 1.4].

    Spec raw score:
        0.25·(2·nlDep-1) + 0.20·z(curv) + 0.20·(2·lead-1)
      + 0.25·z(msc)     + 0.10·z(ddMem)
    z(.) uses a soft saturation (tanh) so the mapping is bounded
    without needing population statistics — this is what makes the
    output reproducible for unit tests.
    """
    if not s.available:
        return 1.0
    z_curv = math.tanh(s.trend_curvature_pct / 1.0)         # ±1%/period² is "strong"
    z_msc = math.tanh(s.multiscale_consistency * 2.0)
    z_dd = math.tanh(s.drawdown_memory_score / 1.0)
    raw = (
        0.25 * (2.0 * s.ts_nonlinear_dependence - 1.0)
        + 0.20 * z_curv
        + 0.20 * (2.0 * s.lead_lag_influence - 1.0)
        + 0.25 * z_msc
        + 0.10 * z_dd
    )
    return _clamp(_tanh_band(raw, gain=2.0, half_band=0.4), 0.6, 1.4)


def compute_regime_risk_multiplier(s: PredictiveExpansionSignals) -> float:
    """regime_risk_multiplier ∈ [0.6, 1.4].

    Spec raw score:
        riskScore = 0.6·volofvol + 0.4·(1 - entropy_stability)
        raw       = 0.6·z(msm) - 1.2·(2·riskScore - 1)
    """
    if not s.available:
        return 1.0
    risk_score = (
        0.6 * s.volofvol_regime_score
        + 0.4 * (1.0 - s.entropy_regime_stability)
    )
    z_msm = math.tanh(s.msm_drift_premium / 1.0)            # 1% premium = strong
    raw = 0.6 * z_msm - 1.2 * (2.0 * risk_score - 1.0)
    return _clamp(_tanh_band(raw, gain=2.5, half_band=0.4), 0.6, 1.4)


def compute_liq_kelly_factor(s: PredictiveExpansionSignals) -> float:
    """liq_kelly_factor ∈ [0.7, 1.3].

    Spec:  0.7 + 0.6·liq_adjusted_signal
    """
    if not s.available:
        return 1.0
    return _clamp(0.7 + 0.6 * s.liq_adjusted_signal, 0.7, 1.3)


def compute_ml_rank_multiplier(s: PredictiveExpansionSignals) -> float:
    """ml_rank_multiplier ∈ [0.8, 1.2].

    Spec: bounded mapping of ml_residual_edge via tanh.
    """
    if not s.available:
        return 1.0
    raw = math.tanh(s.ml_residual_edge / 1.0)
    return _clamp(_tanh_band(raw, gain=1.0, half_band=0.2), 0.8, 1.2)


def compute_reality_breaker_multiplier(s: PredictiveExpansionSignals) -> float:
    """reality_breaker_multiplier ∈ [0.5, 1.5].

    Spec raw score:
        0.30·z(LCC) + 0.25·z(QPII) - 0.25·z(LLVE) + 0.20·z(TRS)
    """
    if not s.available:
        return 1.0
    z_lcc = math.tanh(s.local_causal_cone_signal / 1.0)
    z_qpii = math.tanh(s.quantum_path_interference_index / 1.0)
    z_llve = math.tanh(s.local_lyapunov_volatility_exponent / 0.5)
    z_trs = math.tanh(s.temporal_renormalization_score / 0.5)
    raw = 0.30 * z_lcc + 0.25 * z_qpii - 0.25 * z_llve + 0.20 * z_trs
    return _clamp(_tanh_band(raw, gain=2.5, half_band=0.5), 0.5, 1.5)


# =========================================================================
# Horizon-block + per-symbol attach helpers — mirror Lab/Strategy pattern
# =========================================================================
def enrich_horizon_block_predictive(
    *,
    horizon_block: dict,
    signals: PredictiveExpansionSignals,
    include_reality_breakers: bool = False,
) -> dict:
    """Mutate (and return) `horizon_block` with Phase 26.60 fields.

    The HORIZON-LEVEL block carries:
      * The 5 composite multipliers
      * The 10 standard metric VALUES (the same per-horizon for now —
        a future extension can re-fit per horizon)
      * The 4 reality_breaker metric values when explicitly enabled
    """
    if not signals.available:
        # Inject neutral multipliers so downstream code can blindly
        # multiply without "missing key" branches.
        horizon_block.setdefault('strategy_v2_rank_multiplier', 1.0)
        horizon_block.setdefault('regime_risk_multiplier', 1.0)
        horizon_block.setdefault('ml_rank_multiplier', 1.0)
        horizon_block.setdefault('liq_kelly_factor', 1.0)
        horizon_block.setdefault('reality_breaker_multiplier', 1.0)
        return horizon_block

    # Standard 10 metrics
    horizon_block.update({
        'msm_drift_premium':            round(signals.msm_drift_premium, 5),
        'ts_nonlinear_dependence':      round(signals.ts_nonlinear_dependence, 5),
        'trend_curvature_pct':          round(signals.trend_curvature_pct, 5),
        'lead_lag_influence':           round(signals.lead_lag_influence, 5),
        'volofvol_regime_score':        round(signals.volofvol_regime_score, 5),
        'multiscale_consistency':       round(signals.multiscale_consistency, 5),
        'entropy_regime_stability':     round(signals.entropy_regime_stability, 5),
        'drawdown_memory_score':        round(signals.drawdown_memory_score, 5),
        'liq_adjusted_signal':          round(signals.liq_adjusted_signal, 5),
        'ml_residual_edge':             round(signals.ml_residual_edge, 5),
    })

    # Composite multipliers (always present, always clamped).
    # Phase 26.61 — apply user weight overrides (exponent ∈ [0, 2]) from
    # the Metrics Hub.  Defaults are exponent=1 so this is a NO-OP when
    # the user hasn't touched the tuner.  Falls back to identity on any
    # failure so the rest of the pipeline is never impacted.
    sv2 = compute_strategy_v2_rank_multiplier(signals)
    rr = compute_regime_risk_multiplier(signals)
    ml = compute_ml_rank_multiplier(signals)
    liq = compute_liq_kelly_factor(signals)
    try:
        from app.services.metrics_hub_service import (
            apply_multiplier_exponent, load_weights, metric_enabled,
        )
        _weights = load_weights()
        # Per-metric enable mask — zero-out the SIGNAL value before
        # recomputing the composite.  We do this AFTER computing the
        # multiplier so the row payload still surfaces the raw signal
        # value (for diagnostic display), but the multiplier is
        # neutralised when the metric is masked off.
        def _is_masked(name):
            return not metric_enabled(name, _weights)
        # If a structural metric is masked, neutralise the multiplier
        # it feeds rather than the metric value itself (less invasive,
        # and avoids forced recomputation).  This is intentionally
        # conservative — we treat the multiplier mask as a "ranking
        # impact" toggle rather than a "metric blackhole".
        if all(_is_masked(k) for k in (
            'ts_nonlinear_dependence', 'trend_curvature_pct',
            'lead_lag_influence', 'multiscale_consistency',
            'drawdown_memory_score',
        )):
            sv2 = 1.0
        if all(_is_masked(k) for k in (
            'msm_drift_premium', 'volofvol_regime_score',
            'entropy_regime_stability',
        )):
            rr = 1.0
        if _is_masked('ml_residual_edge'):
            ml = 1.0
        if _is_masked('liq_adjusted_signal'):
            liq = 1.0
        # Apply exponents
        sv2 = apply_multiplier_exponent('strategy_v2_rank_multiplier', sv2, _weights)
        rr = apply_multiplier_exponent('regime_risk_multiplier', rr, _weights)
        ml = apply_multiplier_exponent('ml_rank_multiplier', ml, _weights)
        liq = apply_multiplier_exponent('liq_kelly_factor', liq, _weights)
    except Exception:  # noqa: BLE001
        # Weight-override layer must NEVER crash enrichment.
        pass
    horizon_block.update({
        'strategy_v2_rank_multiplier':  round(sv2, 4),
        'regime_risk_multiplier':       round(rr, 4),
        'ml_rank_multiplier':           round(ml, 4),
        'liq_kelly_factor':             round(liq, 4),
    })

    # Reality-breaker block — DEFAULT OFF; only attached when explicitly
    # enabled by the caller.  Frontend reads these only when the
    # Advanced Experimental Mode toggle is ON.
    if include_reality_breakers:
        rb_mult = compute_reality_breaker_multiplier(signals)
        try:
            from app.services.metrics_hub_service import apply_multiplier_exponent as _ame
            rb_mult = _ame('reality_breaker_multiplier', rb_mult)
        except Exception:  # noqa: BLE001
            pass
        horizon_block.update({
            'local_causal_cone_signal':            round(signals.local_causal_cone_signal, 5),
            'quantum_path_interference_index':     round(signals.quantum_path_interference_index, 5),
            'local_lyapunov_volatility_exponent':  round(signals.local_lyapunov_volatility_exponent, 5),
            'temporal_renormalization_score':      round(signals.temporal_renormalization_score, 5),
            'reality_breaker_multiplier':          round(rb_mult, 4),
        })
    else:
        # Neutral RB multiplier so the row's payload is well-formed
        # (frontend treats missing as neutral too — belt + suspenders).
        horizon_block.setdefault('reality_breaker_multiplier', 1.0)

    return horizon_block


def attach_per_symbol_predictive(row: dict, signals: PredictiveExpansionSignals) -> dict:
    """Stamp the per-symbol bundle onto the row under
    `predictive_expansion_signals`."""
    if not signals.available:
        row['predictive_expansion_signals'] = None
        return row
    row['predictive_expansion_signals'] = {
        'n_obs':                                signals.n_obs,
        # Standard 10
        'msm_drift_premium':                    round(signals.msm_drift_premium, 5),
        'ts_nonlinear_dependence':              round(signals.ts_nonlinear_dependence, 5),
        'trend_curvature_pct':                  round(signals.trend_curvature_pct, 5),
        'lead_lag_influence':                   round(signals.lead_lag_influence, 5),
        'volofvol_regime_score':                round(signals.volofvol_regime_score, 5),
        'multiscale_consistency':               round(signals.multiscale_consistency, 5),
        'entropy_regime_stability':             round(signals.entropy_regime_stability, 5),
        'drawdown_memory_score':                round(signals.drawdown_memory_score, 5),
        'liq_adjusted_signal':                  round(signals.liq_adjusted_signal, 5),
        'ml_residual_edge':                     round(signals.ml_residual_edge, 5),
        # Reality breakers (always serialised; frontend hides when off)
        'local_causal_cone_signal':             round(signals.local_causal_cone_signal, 5),
        'quantum_path_interference_index':      round(signals.quantum_path_interference_index, 5),
        'local_lyapunov_volatility_exponent':   round(signals.local_lyapunov_volatility_exponent, 5),
        'temporal_renormalization_score':       round(signals.temporal_renormalization_score, 5),
        # Diagnostics
        'predictability_score_norm':            round(signals.predictability_score_norm, 5),
        'liquidity_score_norm':                 round(signals.liquidity_score_norm, 5),
    }
    return row
