"""Phase 26.50 — Strategy Tier: 10 predictive algorithms.

This module is the SECOND experimental signal suite, separate from
Lab Mode and designed specifically around algorithms that quants
actively use to PICK TRADES (as opposed to Lab Mode's mix of
diagnostic + predictive signals).

The 10 algorithms
-----------------
1. **Variance Ratio VR(5)** (Lo & MacKinlay 1988) — 5-day variance
   ratio.  VR > 1.10 = positive autocorrelation → momentum signal
   alive at weekly horizon.  VR < 0.90 = mean reversion is the
   dominant force.
2. **Variance Ratio VR(22)** — same statistic at monthly horizon
   (matches typical equity reporting cycle and macro release rhythm).
3. **AR(1) lag-1 autocorrelation** of returns — Direct lag-1 OLS
   regression.  The fitted coefficient is the model's explicit
   tomorrow-prediction from today's return.
4. **Mutual Information lag-1** — Non-linear analogue of AR(1).
   Captures dependencies the linear correlation misses, especially
   in fat-tailed regimes.
5. **Spectral Slope β** — Power-spectrum slope.  β ≈ 0 = white noise
   (unpredictable); β ≈ 1 = pink noise (typical equities); β > 1.5
   = strong long-memory structure.
6. **Welch Dominant Cycle Period** — Frequency of the largest
   Welch-periodogram peak, expressed as a cycle length in days.
   Useful for timing entries when a strong recurring cycle exists.
7. **Recurrence Determinism %** (RQA) — % of recurrence-plot points
   that lie on diagonal lines of length ≥ 2.  Quantifies how
   "deterministic" the trajectory is.
8. **Lempel-Ziv Complexity** — Algorithmic complexity of the
   sign-discretised return sequence.  Inverse of predictability.
9. **EMD IMF1 Slope** (last 5 days) — Slope of the first
   intrinsic-mode oscillation extracted via a fast EMD proxy
   (detrend + low-pass).  A signed short-term momentum indicator
   that adapts to local cyclical structure.
10. **Vol-regime Momentum Wedge** — log(σ_recent / σ_long) where
    σ_recent uses 5-day RV and σ_long uses 30-day RV.  Positive
    wedge = vol-of-vol is RISING (caution); negative = vol cooling.

Composite ranking multiplier
----------------------------
`strategy_rank_multiplier ∈ [0.6, 1.4]`  combines four DIRECTIONAL
components (#3, #9 → signed contributors) and three PREDICTABILITY
components (#5, #7, #8 → predictability boosters), normalised the
same way as the Lab multiplier.  When the "Blend Strategy into
ranking" frontend toggle is enabled, this multiplies the
`effective_kelly_rank` in the table sort order.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Sequence

import numpy as np


_MIN_HISTORY = 40
_RQA_WINDOW = 64
_RQA_THRESHOLD_SIGMA_MULT = 0.5


@dataclass
class StrategySignals:
    """Per-symbol Strategy Tier bundle.  Safe defaults when history
    is insufficient — caller checks `available`."""
    available: bool = False
    n_obs: int = 0
    # 1-2: Variance ratios at multiple horizons
    variance_ratio_5d: float = 1.0      # 1 = random walk; > 1 = momentum; < 1 = mean-rev
    variance_ratio_22d: float = 1.0
    # 3: AR(1) coefficient
    ar1_coefficient: float = 0.0        # signed; tomorrow-prediction slope
    # 4: Mutual information at lag 1 (bits)
    mutual_information_lag1: float = 0.0
    # 5: Spectral slope β
    spectral_slope_beta: float = 1.0    # 0 = white, 1 = pink, 2 = brown
    # 6: Welch dominant cycle (days)
    welch_dominant_cycle_days: float = 0.0
    # 7: RQA determinism %
    rqa_determinism_pct: float = 0.0    # [0, 1]
    # 8: Lempel-Ziv complexity (normalised)
    lempel_ziv_complexity: float = 0.0  # [0, 1]
    # 9: EMD IMF1 slope (last 5 days, log-return per day)
    emd_imf1_slope_pct_per_day: float = 0.0
    # 10: Vol-regime momentum (log ratio recent vs long)
    vol_regime_momentum: float = 0.0    # 0 = stable; +0.3 = vol rising; -0.3 = vol cooling
    notes: list[str] = field(default_factory=list)


def _log_returns(closes: Sequence[float]) -> np.ndarray:
    arr = np.asarray(closes, dtype=float)
    arr = arr[arr > 0]
    if arr.size < 2:
        return np.array([], dtype=float)
    return np.diff(np.log(arr))


# ---------------------------------------------------------------------------
# 1-2) Variance Ratio test (Lo–MacKinlay 1988)
# ---------------------------------------------------------------------------

def variance_ratio(returns: np.ndarray, q: int) -> float:
    """VR(q) = Var(q-period sum) / [q · Var(1-period)].

    Under H₀ (random walk): VR(q) = 1.0.
    VR(q) > 1 → positive autocorrelation (momentum).
    VR(q) < 1 → negative autocorrelation (mean reversion).
    """
    n = returns.size
    if n < q * 4:
        return 1.0
    var1 = float(np.var(returns, ddof=1))
    if var1 <= 0:
        return 1.0
    # q-period sums (rolling, non-overlapping is statistically cleaner
    # but overlapping has lower variance; we use the standard
    # Lo–MacKinlay overlapping form).
    summed = np.convolve(returns, np.ones(q), mode='valid')
    var_q = float(np.var(summed, ddof=1))
    vr = var_q / (q * var1)
    if not math.isfinite(vr):
        return 1.0
    return float(vr)


# ---------------------------------------------------------------------------
# 3) AR(1) coefficient (lag-1 linear autocorrelation)
# ---------------------------------------------------------------------------

def ar1_coefficient(returns: np.ndarray) -> float:
    """Fitted slope of  r_t = a + φ · r_{t-1} + ε.

    φ is bounded to [-1, 1] by construction.  For equity daily
    returns φ is typically tiny (<0.05) but its SIGN is informative.
    """
    if returns.size < 30:
        return 0.0
    x = returns[:-1]
    y = returns[1:]
    try:
        slope, _ = np.polyfit(x, y, 1)
    except Exception:  # noqa: BLE001
        return 0.0
    if not math.isfinite(slope):
        return 0.0
    return float(max(-1.0, min(1.0, slope)))


# ---------------------------------------------------------------------------
# 4) Mutual Information at lag 1
# ---------------------------------------------------------------------------

def mutual_information_lag1(returns: np.ndarray, bins: int = 10) -> float:
    """Shannon mutual information I(r_t; r_{t-1}) in bits.

    Uses fixed equal-width binning on the standardised returns; this
    is the cheapest and reasonably robust estimator for a series of
    a few hundred observations.

    Returns 0 for independent series; grows logarithmically with
    dependence strength.  Typical equity values: 0.02 - 0.15 bits.
    """
    if returns.size < 30:
        return 0.0
    sigma = float(np.std(returns, ddof=1))
    if sigma <= 0:
        return 0.0
    z = (returns - float(np.mean(returns))) / sigma
    x = z[:-1]
    y = z[1:]
    # Bin edges symmetrical around zero spanning ±4σ.
    edges = np.linspace(-4.0, 4.0, bins + 1)
    pxy, _, _ = np.histogram2d(x, y, bins=[edges, edges])
    pxy = pxy / pxy.sum() if pxy.sum() > 0 else pxy
    px = pxy.sum(axis=1)
    py = pxy.sum(axis=0)
    mi = 0.0
    for i in range(bins):
        for j in range(bins):
            p = pxy[i, j]
            if p > 0 and px[i] > 0 and py[j] > 0:
                mi += p * math.log2(p / (px[i] * py[j]))
    if not math.isfinite(mi):
        return 0.0
    return float(max(0.0, mi))


# ---------------------------------------------------------------------------
# 5) Spectral slope β  (1/f^β noise color)
# ---------------------------------------------------------------------------

def spectral_slope(returns: np.ndarray) -> float:
    """Fit  log(power) = c - β · log(frequency)  via OLS on the lower
    half of the FFT-derived power spectrum.

    Returns β.  Typical values:
        β ≈ 0.0 → white noise (no predictable structure)
        β ≈ 1.0 → pink noise (typical equity returns)
        β ≈ 2.0 → brown noise (very strong long memory)
    """
    n = returns.size
    if n < 32:
        return 1.0
    # Remove mean to avoid DC bias.
    x = returns - float(np.mean(returns))
    # Periodogram (Welch with one segment = simple FFT).
    fft = np.fft.rfft(x)
    power = (fft * fft.conjugate()).real
    freqs = np.fft.rfftfreq(n, d=1.0)
    # Discard DC and the upper half (Nyquist-affected high-freq noise).
    mask = (freqs > 0) & (freqs <= 0.5 * freqs.max())
    if mask.sum() < 6:
        return 1.0
    f = freqs[mask]
    p = power[mask]
    p = np.where(p > 1e-18, p, 1e-18)
    try:
        slope, _ = np.polyfit(np.log(f), np.log(p), 1)
    except Exception:  # noqa: BLE001
        return 1.0
    # Slope in fit is d(log p)/d(log f) — by definition β is the
    # MAGNITUDE of the negative slope (since power = f^-β).
    beta = -float(slope)
    if not math.isfinite(beta):
        return 1.0
    return float(max(-1.0, min(4.0, beta)))


# ---------------------------------------------------------------------------
# 6) Welch dominant cycle period
# ---------------------------------------------------------------------------

def welch_dominant_cycle_days(returns: np.ndarray) -> float:
    """Frequency of the LARGEST peak in the Welch periodogram,
    converted to a cycle length in days.

    Returns 0 when no clear dominant cycle exists (e.g. on series
    with very flat spectrum or insufficient data).
    """
    n = returns.size
    if n < 40:
        return 0.0
    x = returns - float(np.mean(returns))
    fft = np.fft.rfft(x)
    power = (fft * fft.conjugate()).real
    freqs = np.fft.rfftfreq(n, d=1.0)
    # Ignore DC and very-low (cycle > n/2 is unreliable).
    min_freq = 2.0 / n
    mask = freqs >= min_freq
    if mask.sum() == 0:
        return 0.0
    p = power[mask]
    f = freqs[mask]
    # Find the dominant peak; ignore peaks that are within 2σ of the
    # mean spectrum power (noise).
    mean_p = float(np.mean(p))
    sd_p = float(np.std(p))
    if sd_p <= 0:
        return 0.0
    idx = int(np.argmax(p))
    if p[idx] < mean_p + 2.0 * sd_p:
        return 0.0   # no statistically significant peak
    if f[idx] <= 0:
        return 0.0
    return float(1.0 / f[idx])   # period in days


# ---------------------------------------------------------------------------
# 7) Recurrence Quantification (% determinism)
# ---------------------------------------------------------------------------

def rqa_determinism_pct(returns: np.ndarray, window: int = _RQA_WINDOW) -> float:
    """Compute the % of recurrence-plot points that lie on diagonal
    lines of length ≥ 2.  This is the standard RQA "determinism"
    statistic.

    We use the most-recent `window` observations to keep the cost
    bounded; the recurrence threshold ε is set to
    `_RQA_THRESHOLD_SIGMA_MULT · σ_returns`.

    Returns a value in [0, 1].
    """
    n = returns.size
    if n < window:
        if n < 30:
            return 0.0
        window = n
    x = returns[-window:]
    sigma = float(np.std(x, ddof=1))
    if sigma <= 0:
        return 0.0
    eps = _RQA_THRESHOLD_SIGMA_MULT * sigma
    # Recurrence matrix R[i,j] = 1 if |x_i - x_j| <= eps
    diff = np.abs(x[:, None] - x[None, :])
    R = (diff <= eps).astype(np.int8)
    # Strip the main diagonal (LOI = line of identity).
    np.fill_diagonal(R, 0)
    total = int(R.sum())
    if total == 0:
        return 0.0
    # Count points on diagonal lines of length ≥ 2.
    # Diagonal k > 0 corresponds to (i, i+k) pairs.
    on_diagonal = 0
    for k in range(1, window):
        diag = np.diagonal(R, offset=k)
        # Run-length encode: count contiguous 1s of length ≥ 2.
        m = 0
        runs = 0
        for v in diag:
            if v == 1:
                m += 1
            else:
                if m >= 2:
                    runs += m
                m = 0
        if m >= 2:
            runs += m
        on_diagonal += runs * 2  # symmetric R counts both (i,j) and (j,i)
    if total == 0:
        return 0.0
    return float(max(0.0, min(1.0, on_diagonal / total)))


# ---------------------------------------------------------------------------
# 8) Lempel-Ziv complexity (normalised, binary alphabet)
# ---------------------------------------------------------------------------

def lempel_ziv_complexity_norm(returns: np.ndarray) -> float:
    """LZ76-style complexity counter on the SIGN-DISCRETISED return
    sequence (positives → '1', negatives or zero → '0').

    Normalised by n/log₂(n) — the asymptotic upper bound for an iid
    random binary sequence — so the output lives in roughly [0, 1].
    Values near 1.0 indicate maximally random / unpredictable;
    values near 0.0 indicate highly structured / repeating patterns.
    """
    n = returns.size
    if n < 30:
        return 0.0
    # Discretise: '1' for positive, '0' otherwise.
    s = ''.join('1' if r > 0 else '0' for r in returns)
    i = 0
    c = 1
    L = len(s)
    while i < L:
        j = i + 1
        # Find the longest substring not previously seen.
        while j <= L:
            sub = s[i:j]
            if sub not in s[:i]:
                c += 1
                break
            j += 1
        else:
            break
        i = j
    # Normalise.
    upper = L / math.log2(L) if L > 1 else 1.0
    if upper <= 0:
        return 0.0
    norm = c / upper
    if not math.isfinite(norm):
        return 0.0
    return float(max(0.0, min(1.0, norm)))


# ---------------------------------------------------------------------------
# 9) EMD IMF1 slope (last 5 days) — fast proxy
# ---------------------------------------------------------------------------

def emd_imf1_slope_pct_per_day(closes: Sequence[float],
                               smoothing: int = 5,
                               slope_lookback: int = 5) -> float:
    """Fast Empirical-Mode-Decomposition proxy.

    Full EMD (Huang 1998) is iterative.  For our use we only need
    the first intrinsic-mode function (IMF1) — the highest-frequency
    oscillation.  A robust cheap proxy:
        IMF1_t  ≈  log(P_t) − rolling_mean(log(P_t), window=smoothing)

    The slope of IMF1 over the last `slope_lookback` days reflects
    short-term directional momentum AFTER stripping out the smooth
    trend captured by the rolling mean.
    """
    arr = np.asarray(closes, dtype=float)
    arr = arr[arr > 0]
    if arr.size < smoothing + slope_lookback + 1:
        return 0.0
    log_p = np.log(arr)
    if smoothing < 2:
        smoothing = 2
    # Causal rolling mean (no edge-zero-padding).  For each t, the
    # smoothed value averages the last `smoothing` log-prices.  This
    # AVOIDS the convolve(mode='same') edge-bias that produced
    # absurd 60%+ slopes when log(P) ≈ 4.6 was convolved against
    # zero-padded tails.
    n = log_p.size
    smooth = np.zeros(n)
    for i in range(n):
        lo = max(0, i - smoothing + 1)
        smooth[i] = float(np.mean(log_p[lo:i + 1]))
    imf1 = log_p - smooth
    if imf1.size < slope_lookback + 1:
        return 0.0
    y = imf1[-slope_lookback:]
    x = np.arange(slope_lookback, dtype=float)
    try:
        slope, _ = np.polyfit(x, y, 1)
    except Exception:  # noqa: BLE001
        return 0.0
    pct_per_day = math.expm1(float(slope)) * 100.0
    if not math.isfinite(pct_per_day):
        return 0.0
    # Safety clamp at ±10%/day — single-day moves above this are
    # almost always estimation artefacts, not real EMD signal.
    return float(max(-10.0, min(10.0, pct_per_day)))


# ---------------------------------------------------------------------------
# 10) Vol-regime momentum wedge
# ---------------------------------------------------------------------------

def vol_regime_momentum(returns: np.ndarray,
                        recent_window: int = 5,
                        long_window: int = 30) -> float:
    """log(σ_recent / σ_long) using realised volatility windows.

    Positive = realised volatility is RISING relative to its longer-
    term level (stress regime entering).  Negative = volatility is
    cooling.  Typical range: -0.5 to +0.5.
    """
    n = returns.size
    if n < long_window + 1:
        return 0.0
    sigma_recent = float(np.std(returns[-recent_window:], ddof=1))
    sigma_long = float(np.std(returns[-long_window:], ddof=1))
    if sigma_recent <= 0 or sigma_long <= 0:
        return 0.0
    val = math.log(sigma_recent / sigma_long)
    if not math.isfinite(val):
        return 0.0
    return float(max(-1.0, min(1.0, val)))


# ---------------------------------------------------------------------------
# Public API: bundle + horizon overlay
# ---------------------------------------------------------------------------

def compute_strategy_signals(closes: Sequence[float]) -> StrategySignals:
    """Compute all 10 Strategy-Tier signals on a daily-close series."""
    out = StrategySignals()
    arr = np.asarray(closes, dtype=float)
    arr = arr[arr > 0]
    if arr.size < _MIN_HISTORY:
        out.notes.append(f'insufficient history (n={arr.size})')
        return out
    rets = _log_returns(arr)
    if rets.size < _MIN_HISTORY - 1:
        out.notes.append(f'insufficient return series (n={rets.size})')
        return out

    out.available = True
    out.n_obs = int(arr.size)

    for name, fn in (
        ('variance_ratio_5d',                lambda: variance_ratio(rets, 5)),
        ('variance_ratio_22d',               lambda: variance_ratio(rets, 22)),
        ('ar1_coefficient',                  lambda: ar1_coefficient(rets)),
        ('mutual_information_lag1',          lambda: mutual_information_lag1(rets)),
        ('spectral_slope_beta',              lambda: spectral_slope(rets)),
        ('welch_dominant_cycle_days',        lambda: welch_dominant_cycle_days(rets)),
        ('rqa_determinism_pct',              lambda: rqa_determinism_pct(rets)),
        ('lempel_ziv_complexity',            lambda: lempel_ziv_complexity_norm(rets)),
        ('emd_imf1_slope_pct_per_day',       lambda: emd_imf1_slope_pct_per_day(arr.tolist())),
        ('vol_regime_momentum',              lambda: vol_regime_momentum(rets)),
    ):
        try:
            setattr(out, name, float(fn()))
        except Exception as exc:  # noqa: BLE001
            out.notes.append(f'{name} failed: {exc.__class__.__name__}')

    return out


def enrich_horizon_block_strategy(
    *,
    horizon_block: dict,
    strategy: StrategySignals,
) -> dict:
    """Mutate (and return) `horizon_block` with Strategy Tier overlay
    fields.  Safe when `strategy.available == False` — block returned
    unchanged.

    Composite multiplier  `strategy_rank_multiplier ∈ [0.6, 1.4]`
    blends:
      * Directional contributors (signed):
          AR(1), EMD-IMF1 slope, VR(5)−1 (momentum vs reversion),
          VR(22)−1, vol_regime_momentum (negative when vol cools)
      * Predictability boosters (always positive contributors):
          mutual_information_lag1 (saturated at 0.2 bits),
          rqa_determinism_pct, 1 - lempel_ziv_complexity
    """
    if not strategy.available:
        return horizon_block

    # Directional terms (signed, saturate at ±1).
    ar1_term  = max(-1.0, min(1.0, strategy.ar1_coefficient * 20.0))
    emd_term  = max(-1.0, min(1.0, strategy.emd_imf1_slope_pct_per_day / 0.5))
    vr5_term  = max(-1.0, min(1.0, (strategy.variance_ratio_5d - 1.0) * 5.0))
    vr22_term = max(-1.0, min(1.0, (strategy.variance_ratio_22d - 1.0) * 5.0))
    vrm_term  = max(-1.0, min(1.0, -strategy.vol_regime_momentum * 2.0))  # cooling → boost

    # Predictability terms (centred at 0; positive when more predictable).
    mi_term  = max(-1.0, min(1.0, strategy.mutual_information_lag1 / 0.1 - 1.0))
    rqa_term = max(-1.0, min(1.0, strategy.rqa_determinism_pct * 2.0 - 1.0))
    lz_term  = max(-1.0, min(1.0, 1.0 - 2.0 * strategy.lempel_ziv_complexity))
    spec_term = max(-1.0, min(1.0, (strategy.spectral_slope_beta - 1.0) * 1.5))

    blend = (
        ar1_term + emd_term + vr5_term + vr22_term + vrm_term
        + mi_term + rqa_term + lz_term + spec_term
    ) / 9.0
    multiplier = 1.0 + 0.4 * blend   # → [0.6, 1.4]
    # Phase 26.61 — apply user weight override (exponent ∈ [0, 2])
    # from the Metrics Hub.  Default exponent = 1.0 (no-op).
    try:
        from app.services.metrics_hub_service import apply_multiplier_exponent
        multiplier = apply_multiplier_exponent('strategy_rank_multiplier', multiplier)
    except Exception:  # noqa: BLE001
        pass

    horizon_block.update({
        'strategy_vr5':                round(strategy.variance_ratio_5d, 4),
        'strategy_vr22':               round(strategy.variance_ratio_22d, 4),
        'strategy_ar1':                round(strategy.ar1_coefficient, 5),
        'strategy_mi_lag1':            round(strategy.mutual_information_lag1, 5),
        'strategy_spectral_beta':      round(strategy.spectral_slope_beta, 4),
        'strategy_welch_cycle_days':   round(strategy.welch_dominant_cycle_days, 2),
        'strategy_rqa_determinism':    round(strategy.rqa_determinism_pct, 4),
        'strategy_lz_complexity':      round(strategy.lempel_ziv_complexity, 4),
        'strategy_emd_slope_pct':      round(strategy.emd_imf1_slope_pct_per_day, 5),
        'strategy_vol_regime_mom':     round(strategy.vol_regime_momentum, 4),
        'strategy_rank_multiplier':    round(multiplier, 4),
    })
    return horizon_block


def attach_per_symbol_strategy(row: dict, strategy: StrategySignals) -> dict:
    """Stamp the per-symbol Strategy bundle onto the row."""
    if not strategy.available:
        row['strategy_signals'] = None
        return row
    row['strategy_signals'] = {
        'n_obs':                          strategy.n_obs,
        'variance_ratio_5d':              round(strategy.variance_ratio_5d, 4),
        'variance_ratio_22d':             round(strategy.variance_ratio_22d, 4),
        'ar1_coefficient':                round(strategy.ar1_coefficient, 5),
        'mutual_information_lag1':        round(strategy.mutual_information_lag1, 5),
        'spectral_slope_beta':            round(strategy.spectral_slope_beta, 4),
        'welch_dominant_cycle_days':      round(strategy.welch_dominant_cycle_days, 2),
        'rqa_determinism_pct':            round(strategy.rqa_determinism_pct, 4),
        'lempel_ziv_complexity':          round(strategy.lempel_ziv_complexity, 4),
        'emd_imf1_slope_pct_per_day':     round(strategy.emd_imf1_slope_pct_per_day, 5),
        'vol_regime_momentum':            round(strategy.vol_regime_momentum, 4),
    }
    return row
