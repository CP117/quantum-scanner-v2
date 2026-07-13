"""Phase 26.47 — Advanced predictive math layer for Future Mode.

This module sits BETWEEN the existing GARCH + Bayesian factor blend
and the per-horizon `forward_metrics` block emitted by
`future_mode_service`.  It adds a richer mathematical signal stack so
the Future Mode ranking goes meaningfully beyond plain Gaussian
Kelly:

    Per-symbol (computed once per cycle, scale-invariant)
    -------------------------------------------------------
    hurst_exponent           Mandelbrot R/S — trending vs mean-reverting regime
    realized_skew            Fisher–Pearson sample skewness of log returns
    realized_excess_kurt     Sample excess kurtosis (Fisher form, normal=0)
    jump_intensity_per_day   λ — mean jumps per trading day (Lee–Mykland style)
    jump_mean_return         μ_j — mean jump-return in %
    jump_std_return          σ_j — std of jump-returns in %
    ou_half_life_days        Ornstein–Uhlenbeck mean-reversion timescale
    rv_har_sigma_pct         HAR-RV forecast of next-day σ (daily/weekly/monthly RV components)

    Per-horizon overlay (cheap)
    -------------------------------------------------------
    p_up_cf                  Cornish–Fisher-adjusted P(up) — fat-tail aware
    var95_pct                95 % VaR over the horizon
    cvar95_pct               95 % CVaR (expected shortfall)
    kelly_fraction           Half-Kelly position sizing recommendation
    jump_drift_pct           Expected drift contribution from jumps over horizon
    regime_weight            Hurst-derived directional confidence multiplier
    effective_kelly_rank     Final ranking metric for Future Mode sort order

All functions are pure (no I/O) and operate on simple numpy arrays or
Python lists.  Total per-symbol cost: ~0.3-0.8 ms with numpy.

References:
    Hurst (1951), Mandelbrot & Wallis (1969) — R/S analysis
    Cornish & Fisher (1938) — Cornish–Fisher expansion
    Merton (1976) — Jump-diffusion (used as proxy here)
    Lee & Mykland (2008) — Jump detection in high-frequency data
    Ornstein & Uhlenbeck (1930) — OU mean-reverting process
    Corsi (2009) — HAR-RV: A Simple Approximate Long-Memory Model of RV
    Kelly (1956) — Optimal betting fractions
    Cont (2001) — Stylized facts of financial returns
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Sequence

import numpy as np

# ---------------------------------------------------------------------------
# Constants / safety limits
# ---------------------------------------------------------------------------
_MIN_HISTORY = 30                # minimum closes for ANY advanced signal
_MIN_RS_WINDOW = 8               # smallest window used in R/S Hurst
_MAX_RS_WINDOWS = 4              # cap number of R/S window sizes for speed
_JUMP_THRESHOLD_SIGMA = 3.5      # threshold-Z above which a return is flagged as a jump
_CF_SKEW_CLAMP = 3.0             # safety clamp on Cornish-Fisher skew
_CF_KURT_CLAMP = 8.0             # safety clamp on Cornish-Fisher excess kurtosis
_KELLY_CAP = 1.0                 # max |kelly fraction| (no infinite leverage)
_KELLY_FRACTION_OF_FULL = 0.5    # half-Kelly by default — industry standard
_VAR_ALPHA = 0.05                # 95 % VaR/CVaR


@dataclass
class AdvancedSignals:
    """Per-symbol advanced signal bundle.  Computed once per cycle from
    daily history; reused across all 5 horizons.

    All fields are safe defaults when history is insufficient.  Callers
    detect the "no advanced signals" case via `available == False`.
    """
    available: bool = False
    n_obs: int = 0
    # Regime / long-memory
    hurst_exponent: float = 0.5
    # Higher moments of daily returns
    realized_mean_pct: float = 0.0
    realized_std_pct: float = 0.0
    realized_skew: float = 0.0
    realized_excess_kurt: float = 0.0
    # Jump-diffusion proxy
    jump_intensity_per_day: float = 0.0
    jump_mean_return_pct: float = 0.0
    jump_std_return_pct: float = 0.0
    n_jumps: int = 0
    # OU mean reversion
    ou_half_life_days: float = 0.0   # 0.0 → not mean-reverting / inestimable
    ou_speed: float = 0.0            # θ (kappa); 0 = no mean reversion
    # HAR-RV daily sigma (one-day-ahead) — pct units
    rv_har_sigma_pct: float = 0.0
    # Bookkeeping
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _log_returns(closes: Sequence[float]) -> np.ndarray:
    arr = np.asarray(closes, dtype=float)
    arr = arr[arr > 0]
    if arr.size < 2:
        return np.array([], dtype=float)
    return np.diff(np.log(arr))


def _safe_std(x: np.ndarray) -> float:
    if x.size < 2:
        return 0.0
    s = float(np.std(x, ddof=1))
    return s if math.isfinite(s) and s > 0 else 0.0


# ---------------------------------------------------------------------------
# Hurst exponent — Mandelbrot R/S analysis (windowed)
# ---------------------------------------------------------------------------

def hurst_exponent_rs(returns: np.ndarray) -> float:
    """R/S Hurst exponent estimator.

    Returns a value in (0, 1).  Values near 0.5 indicate a random walk
    (geometric Brownian motion); values > 0.5 indicate persistence /
    trending; values < 0.5 indicate anti-persistence / mean-reversion.

    Robust to short series: falls back to 0.5 when there is too little
    data to fit the log-log regression.
    """
    n = returns.size
    if n < 32:
        return 0.5

    # Pick window sizes geometrically between _MIN_RS_WINDOW and n//2.
    # Cap to _MAX_RS_WINDOWS to keep this cheap.
    max_w = max(_MIN_RS_WINDOW + 1, n // 2)
    if max_w <= _MIN_RS_WINDOW:
        return 0.5
    ratio = (max_w / _MIN_RS_WINDOW) ** (1.0 / (_MAX_RS_WINDOWS - 1))
    windows = sorted(set(
        max(_MIN_RS_WINDOW, int(round(_MIN_RS_WINDOW * (ratio ** i))))
        for i in range(_MAX_RS_WINDOWS)
    ))

    rs_vals: list[tuple[float, float]] = []
    for w in windows:
        if w < _MIN_RS_WINDOW or w > n:
            continue
        # Number of non-overlapping windows we can fit.
        k = n // w
        if k < 1:
            continue
        rs_for_window = []
        for i in range(k):
            seg = returns[i * w:(i + 1) * w]
            mean = float(np.mean(seg))
            dev = seg - mean
            cum = np.cumsum(dev)
            rng = float(np.max(cum) - np.min(cum))
            sd = _safe_std(seg)
            if sd > 0 and rng > 0:
                rs_for_window.append(rng / sd)
        if rs_for_window:
            rs_vals.append((float(w), float(np.mean(rs_for_window))))

    if len(rs_vals) < 2:
        return 0.5

    xs = np.log([w for w, _ in rs_vals])
    ys = np.log([rs for _, rs in rs_vals])
    # OLS slope = Hurst exponent
    try:
        slope, _ = np.polyfit(xs, ys, 1)
    except Exception:  # noqa: BLE001
        return 0.5
    h = float(slope)
    if not math.isfinite(h):
        return 0.5
    # Clamp to a sane band — R/S can leak slightly outside (0,1) for
    # short series.
    return max(0.05, min(0.95, h))


# ---------------------------------------------------------------------------
# Realized moments — sample mean, std, skew, excess kurtosis
# ---------------------------------------------------------------------------

def realized_moments(returns: np.ndarray) -> tuple[float, float, float, float]:
    """Sample (mean, std, skew, excess kurtosis) on *raw* return units
    (log-returns).  Skew/kurt use Fisher–Pearson (k_3, k_4) with sample
    bias correction.

    All four values returned in ratio units (NOT percent — convert at
    the call site if needed).
    """
    n = returns.size
    if n < 4:
        return (0.0, 0.0, 0.0, 0.0)
    mean = float(np.mean(returns))
    std = _safe_std(returns)
    if std <= 0:
        return (mean, 0.0, 0.0, 0.0)
    z = (returns - mean) / std
    # Sample-bias-corrected skew & excess kurt (Fisher-Pearson)
    g1 = float(np.mean(z ** 3))
    g2 = float(np.mean(z ** 4)) - 3.0
    if n >= 3:
        bias_skew = math.sqrt(n * (n - 1)) / (n - 2)
        skew = g1 * bias_skew
    else:
        skew = g1
    if n >= 4:
        bias_kurt = ((n - 1) * ((n + 1) * g2 + 6)) / ((n - 2) * (n - 3))
        kurt = bias_kurt
    else:
        kurt = g2
    if not math.isfinite(skew):
        skew = 0.0
    if not math.isfinite(kurt):
        kurt = 0.0
    return (mean, std, skew, kurt)


# ---------------------------------------------------------------------------
# Cornish–Fisher fat-tail-adjusted p_up
# ---------------------------------------------------------------------------

def _phi(z: float) -> float:
    """Standard normal CDF using erf — vendored copy so we don't depend
    on bayesian_factor_blend internals (avoids a circular import once
    we plug this back into future_mode_service)."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def cornish_fisher_p_up(drift_pct: float, sigma_pct: float,
                        skew: float, excess_kurt: float) -> float:
    """Fat-tail aware P(return > 0) using the Cornish–Fisher expansion.

    Inputs:
        drift_pct      Expected return over the horizon, in %.
        sigma_pct      Std dev over the horizon, in %.
        skew           Sample skewness (Fisher-Pearson).
        excess_kurt    Excess kurtosis (Fisher form; normal=0).

    Returns p_up in [0, 1].  Falls back to plain Gaussian Φ(drift/σ)
    when σ ≤ 0 or the inputs are pathological.

    Implementation notes
    --------------------
    Cornish–Fisher gives an inverse-CDF expansion.  To get a
    fat-tail-adjusted p_up we INVERT the expansion: solve for the
    standard-normal z whose CF-expanded quantile equals 0, then
    p_up = 1 - Φ(z_root).

    We can short-circuit when skew=kurt=0 (reduces to Gaussian).
    """
    if sigma_pct <= 0 or not math.isfinite(sigma_pct):
        return 0.5
    if not math.isfinite(drift_pct):
        return 0.5
    # Clamp the higher moments for stability — the CF expansion is
    # only accurate for moderate departures from normality.
    s = max(-_CF_SKEW_CLAMP, min(_CF_SKEW_CLAMP, float(skew)))
    k = max(-_CF_KURT_CLAMP, min(_CF_KURT_CLAMP, float(excess_kurt)))
    # Plain-Gaussian short-circuit.
    if abs(s) < 1e-6 and abs(k) < 1e-6:
        return _phi(drift_pct / sigma_pct)

    # Cornish-Fisher quantile transform:
    #   x_cf(z) = z + (z^2 - 1)/6 * s + (z^3 - 3z)/24 * k - (2z^3 - 5z)/36 * s^2
    # Want x_cf(z) = -drift/sigma  (the standardized point at which
    # the return hits zero), then p_up = 1 - Φ(z_root).
    target = -drift_pct / sigma_pct

    def x_cf(z: float) -> float:
        return (z
                + (z * z - 1.0) / 6.0 * s
                + (z * z * z - 3.0 * z) / 24.0 * k
                - (2.0 * z * z * z - 5.0 * z) / 36.0 * (s * s))

    # Bisection on a wide bracket — x_cf is monotonic in the [-6, 6]
    # range for our clamped (s, k).  We bracket then refine.
    lo, hi = -6.0, 6.0
    f_lo = x_cf(lo) - target
    f_hi = x_cf(hi) - target
    if f_lo * f_hi > 0:
        # Lost monotonicity at the extreme tails → graceful fallback.
        return _phi(drift_pct / sigma_pct)
    for _ in range(48):  # ~1e-14 convergence — plenty
        mid = 0.5 * (lo + hi)
        f_mid = x_cf(mid) - target
        if f_mid == 0 or (hi - lo) < 1e-9:
            lo = hi = mid
            break
        if f_lo * f_mid < 0:
            hi = mid
            f_hi = f_mid
        else:
            lo = mid
            f_lo = f_mid
    z_root = 0.5 * (lo + hi)
    return max(0.0, min(1.0, 1.0 - _phi(z_root)))


# ---------------------------------------------------------------------------
# Jump-diffusion proxy (Merton-style)
# ---------------------------------------------------------------------------

def jump_diffusion_params(returns: np.ndarray) -> tuple[float, float, float, int]:
    """Estimate (λ, μ_j, σ_j, n_jumps) under a simple threshold-jump
    detector.

    Definition of "jump": a return whose |z-score| (against the
    median-corrected sample std) exceeds `_JUMP_THRESHOLD_SIGMA`.

    Why median: robust to the jumps themselves polluting the variance
    estimate (Lee–Mykland is the gold standard but requires
    high-frequency data; this is the daily-bar analogue).
    """
    n = returns.size
    if n < 30:
        return (0.0, 0.0, 0.0, 0.0)
    # Robust scale via median absolute deviation × 1.4826.
    med = float(np.median(returns))
    mad = float(np.median(np.abs(returns - med))) * 1.4826
    if mad <= 0:
        return (0.0, 0.0, 0.0, 0)
    z = (returns - med) / mad
    jump_mask = np.abs(z) >= _JUMP_THRESHOLD_SIGMA
    n_jumps = int(jump_mask.sum())
    if n_jumps == 0:
        return (0.0, 0.0, 0.0, 0)
    jumps = returns[jump_mask]
    lam = n_jumps / n                  # per-day jump intensity
    mu_j = float(np.mean(jumps))       # mean jump (log-return)
    sigma_j = _safe_std(jumps)         # std of jumps
    return (lam, mu_j, sigma_j, n_jumps)


# ---------------------------------------------------------------------------
# Ornstein–Uhlenbeck half-life of mean reversion
# ---------------------------------------------------------------------------

def ornstein_uhlenbeck_half_life(closes: Sequence[float]) -> tuple[float, float]:
    """Fit a discrete-time AR(1) to log-prices and return
    (half_life_days, theta).

    Discrete OU:   x_t = μ + φ (x_{t-1} - μ) + ε,   0 < φ < 1
    Continuous θ:  θ = -ln(φ)                       (per unit time)
    Half-life:     T_{1/2} = ln(2) / θ

    Returns (0.0, 0.0) when the symbol is NOT mean-reverting
    (φ ≥ 1, infinite-variance random walk).

    We use log-prices (more economically meaningful than levels).
    """
    arr = np.asarray(closes, dtype=float)
    arr = arr[arr > 0]
    if arr.size < 30:
        return (0.0, 0.0)
    x = np.log(arr)
    x_lag = x[:-1]
    x_now = x[1:]
    # OLS:  x_now = a + b * x_lag + ε  →  φ = b, μ = a/(1-b)
    try:
        slope, intercept = np.polyfit(x_lag, x_now, 1)
    except Exception:  # noqa: BLE001
        return (0.0, 0.0)
    phi = float(slope)
    if not math.isfinite(phi) or phi <= 0 or phi >= 1:
        return (0.0, 0.0)
    theta = -math.log(phi)
    if theta <= 0:
        return (0.0, 0.0)
    half_life = math.log(2.0) / theta
    if not math.isfinite(half_life) or half_life <= 0 or half_life > 10_000:
        return (0.0, 0.0)
    return (half_life, theta)


# ---------------------------------------------------------------------------
# HAR-RV — Corsi (2009) one-day-ahead realized-volatility forecast
# ---------------------------------------------------------------------------

def har_rv_sigma_pct(returns: np.ndarray) -> float:
    """One-day-ahead σ (in %) under the Heterogeneous AutoRegressive
    model of Realized Volatility.

    Uses three components of realized variance:
        RV_d   — yesterday's daily RV
        RV_w   — average over the last 5 days
        RV_m   — average over the last 22 days

    Forecast σ²_{t+1} = c + β_d·RV_d + β_w·RV_w + β_m·RV_m.

    We fit (c, β_d, β_w, β_m) by OLS on the in-sample series and
    return the one-step forecast σ in percentage points.
    """
    n = returns.size
    if n < 30:
        return 0.0
    # Daily realized variance (squared log returns are a sufficient
    # proxy when we don't have intraday data).
    rv_d = returns ** 2
    # Build the (RV_d, RV_w, RV_m) feature vector for every t ≥ 22.
    # Target = RV_d[t]; features use lagged windows ending at t-1.
    if n < 23:
        # Not enough history for a full HAR fit — emit the simple
        # sqrt(mean RV) instead so the caller still has a number.
        return float(math.sqrt(float(np.mean(rv_d))) * 100.0)
    starts = np.arange(22, n)
    feats = np.zeros((starts.size, 4), dtype=float)
    feats[:, 0] = 1.0
    # one-day lag
    feats[:, 1] = rv_d[starts - 1]
    # 5-day average lag
    for i, t in enumerate(starts):
        feats[i, 2] = float(np.mean(rv_d[t - 5:t]))
    # 22-day average lag
    for i, t in enumerate(starts):
        feats[i, 3] = float(np.mean(rv_d[t - 22:t]))
    target = rv_d[starts]
    try:
        coef, *_ = np.linalg.lstsq(feats, target, rcond=None)
    except Exception:  # noqa: BLE001
        return float(math.sqrt(float(np.mean(rv_d))) * 100.0)
    # Forecast the next period using the latest feature vector.
    last_feat = np.array([
        1.0,
        rv_d[-1],
        float(np.mean(rv_d[-5:])),
        float(np.mean(rv_d[-22:])),
    ])
    next_var = float(np.dot(coef, last_feat))
    if not math.isfinite(next_var) or next_var <= 0:
        # Fallback to sample-mean RV.
        next_var = float(np.mean(rv_d))
    return float(math.sqrt(max(next_var, 0.0)) * 100.0)


# ---------------------------------------------------------------------------
# Risk metrics — VaR, CVaR, fractional Kelly, regime weight
# ---------------------------------------------------------------------------

def var_pct(drift_pct: float, sigma_pct: float, alpha: float = _VAR_ALPHA,
            skew: float = 0.0, excess_kurt: float = 0.0) -> float:
    """Cornish-Fisher-adjusted Value at Risk (percent units).

    Returns the MAGNITUDE of the worst expected loss at confidence
    `1 - alpha` (positive number = loss).  e.g. VaR_95 = 3.2 % means
    "we expect to lose ≤ 3.2 % with 95 % confidence."
    """
    if sigma_pct <= 0:
        return 0.0
    s = max(-_CF_SKEW_CLAMP, min(_CF_SKEW_CLAMP, float(skew)))
    k = max(-_CF_KURT_CLAMP, min(_CF_KURT_CLAMP, float(excess_kurt)))
    # z_α from inverse-Normal (lower tail).  alpha=0.05 → z ≈ -1.6449.
    z = _inv_phi(alpha)
    z_cf = (z
            + (z * z - 1.0) / 6.0 * s
            + (z * z * z - 3.0 * z) / 24.0 * k
            - (2.0 * z * z * z - 5.0 * z) / 36.0 * (s * s))
    var = -(drift_pct + z_cf * sigma_pct)
    return float(max(0.0, var))


def cvar_pct(drift_pct: float, sigma_pct: float, alpha: float = _VAR_ALPHA) -> float:
    """Gaussian Expected Shortfall (Conditional VaR) in percent units.

    Returns the magnitude of the average loss in the worst `alpha`
    tail.  Gaussian closed form (intentionally not Cornish-Fisher: ES
    under CF is more complex and the simpler Gaussian ES is the
    industry-standard "first-cut" risk surrogate)."""
    if sigma_pct <= 0 or alpha <= 0 or alpha >= 1:
        return 0.0
    z = _inv_phi(alpha)
    phi_z = math.exp(-0.5 * z * z) / math.sqrt(2.0 * math.pi)
    es = -drift_pct + sigma_pct * phi_z / alpha
    return float(max(0.0, es))


def _inv_phi(p: float) -> float:
    """Approximate inverse of the standard-normal CDF.  Beasley-Springer
    /Moro algorithm; ≤ 1e-7 error in the central body.  Vendored to
    avoid scipy dependency."""
    if p <= 0:
        return -8.0
    if p >= 1:
        return 8.0
    # Beasley-Springer/Moro
    a = (-3.969683028665376e+01, 2.209460984245205e+02,
         -2.759285104469687e+02, 1.383577518672690e+02,
         -3.066479806614716e+01, 2.506628277459239e+00)
    b = (-5.447609879822406e+01, 1.615858368580409e+02,
         -1.556989798598866e+02, 6.680131188771972e+01,
         -1.328068155288572e+01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01,
         -2.400758277161838e+00, -2.549732539343734e+00,
         4.374664141464968e+00, 2.938163982698783e+00)
    d = (7.784695709041462e-03, 3.224671290700398e-01,
         2.445134137142996e+00, 3.754408661907416e+00)
    p_low, p_high = 0.02425, 1 - 0.02425
    if p < p_low:
        q = math.sqrt(-2 * math.log(p))
        return ((((c[0]*q + c[1])*q + c[2])*q + c[3])*q + c[4])*q + c[5] \
               / ((((d[0]*q + d[1])*q + d[2])*q + d[3])*q + 1)
    if p > p_high:
        q = math.sqrt(-2 * math.log(1 - p))
        return -((((c[0]*q + c[1])*q + c[2])*q + c[3])*q + c[4])*q + c[5] \
               / ((((d[0]*q + d[1])*q + d[2])*q + d[3])*q + 1)
    q = p - 0.5
    r = q * q
    return (((((a[0]*r + a[1])*r + a[2])*r + a[3])*r + a[4])*r + a[5]) * q \
           / (((((b[0]*r + b[1])*r + b[2])*r + b[3])*r + b[4])*r + 1)


def fractional_kelly(drift_pct: float, sigma_pct: float,
                     fraction: float = _KELLY_FRACTION_OF_FULL) -> float:
    """Fractional-Kelly position sizing.

    Full Kelly: f* = μ / σ²   (per Thorp 1969 for log-utility).
    We return `fraction · f*`, clamped to ±_KELLY_CAP.

    Why fractional? Full Kelly is noise-intolerant; estimation error
    on μ in particular blows up the recommended bet.  Half-Kelly
    (Thorp, 2006) is the industry standard for retail systems.

    Units: returns the *fraction of capital* to allocate.  Positive →
    long; negative → short; |value| ≤ _KELLY_CAP.
    """
    if sigma_pct <= 0 or not math.isfinite(sigma_pct):
        return 0.0
    sigma_ratio = sigma_pct / 100.0
    drift_ratio = drift_pct / 100.0
    full = drift_ratio / max(sigma_ratio * sigma_ratio, 1e-9)
    f = fraction * full
    return float(max(-_KELLY_CAP, min(_KELLY_CAP, f)))


def regime_weight_from_hurst(hurst: float, signal_direction: int) -> float:
    """Multiplier in [0.6, 1.4] that boosts the Kelly rank when the
    market regime AGREES with the predicted direction and dampens it
    when they disagree.

    Trending market (H > 0.55) + bullish signal → boost (1.0 → 1.4).
    Mean-reverting market (H < 0.45) + bullish signal → suppress
    (since drift is fighting the regime).
    """
    if not math.isfinite(hurst) or hurst <= 0:
        return 1.0
    # Distance from random walk; positive when trending, negative when
    # mean-reverting.
    persistence = hurst - 0.5
    # `signal_direction` is +1, 0, or -1.
    if signal_direction == 0:
        # No directional signal → small penalty unless regime is clean.
        return 1.0 - 0.2 * abs(persistence)
    # The regime EITHER agrees with the signal (boost) OR fights it
    # (suppress).  Trending markets with weak signals also get a small
    # boost (because trend persistence increases base hit-rate).
    agree = (signal_direction > 0 and persistence > 0) or \
            (signal_direction < 0 and persistence < 0)
    if agree:
        return 1.0 + 0.8 * abs(persistence)
    # Mean-reverting market fighting our bullish signal — or trending
    # market with a fade signal.
    return 1.0 - 0.4 * abs(persistence)


# ---------------------------------------------------------------------------
# Public API: bundle everything per symbol
# ---------------------------------------------------------------------------

def compute_advanced_signals(closes: Sequence[float]) -> AdvancedSignals:
    """Run the full advanced-math stack on a daily-bar close series.

    Returns an `AdvancedSignals` bundle.  Total runtime ~0.3-0.8 ms
    for typical 6-month histories, dominated by HAR-RV's OLS.

    All sub-estimators fail gracefully — if a particular signal can't
    be computed, the corresponding field defaults but the bundle
    still returns with `available=True` so the caller can use the
    rest.
    """
    out = AdvancedSignals()
    arr = np.asarray(closes, dtype=float)
    arr = arr[arr > 0]
    if arr.size < _MIN_HISTORY:
        out.notes.append(f'insufficient history (n={arr.size})')
        return out
    out.n_obs = int(arr.size)
    rets = _log_returns(arr)
    if rets.size < _MIN_HISTORY - 1:
        out.notes.append(f'insufficient return series (n={rets.size})')
        return out

    out.available = True

    # Realized moments (percent units for std/mean → easier to reason about)
    mean, std, skew, kurt = realized_moments(rets)
    out.realized_mean_pct = float(mean * 100.0)
    out.realized_std_pct = float(std * 100.0)
    out.realized_skew = float(skew)
    out.realized_excess_kurt = float(kurt)

    # Hurst regime
    try:
        out.hurst_exponent = float(hurst_exponent_rs(rets))
    except Exception:  # noqa: BLE001
        out.notes.append('hurst failed')

    # Jump diffusion
    try:
        lam, mu_j, sigma_j, n_j = jump_diffusion_params(rets)
        out.jump_intensity_per_day = float(lam)
        out.jump_mean_return_pct = float(mu_j * 100.0)
        out.jump_std_return_pct = float(sigma_j * 100.0)
        out.n_jumps = int(n_j)
    except Exception:  # noqa: BLE001
        out.notes.append('jump-diffusion failed')

    # OU half-life
    try:
        hl, theta = ornstein_uhlenbeck_half_life(arr)
        out.ou_half_life_days = float(hl)
        out.ou_speed = float(theta)
    except Exception:  # noqa: BLE001
        out.notes.append('ou-half-life failed')

    # HAR-RV
    try:
        out.rv_har_sigma_pct = float(har_rv_sigma_pct(rets))
    except Exception:  # noqa: BLE001
        out.notes.append('har-rv failed')

    return out


# ---------------------------------------------------------------------------
# Per-horizon overlay — combines the per-symbol bundle with a single
# horizon block (already produced by `future_mode_service`).
# ---------------------------------------------------------------------------

def enrich_horizon_block(
    *,
    horizon_block: dict,
    advanced: AdvancedSignals,
    horizon_units: int,
    is_intraday: bool,
) -> dict:
    """Mutate (and return) `horizon_block` with the advanced-math
    overlay.  Safe to call when `advanced.available == False` — the
    block is returned unchanged in that case.

    Fields added (all rounded to keep payload compact):
        p_up_cf, var95_pct, cvar95_pct, kelly_fraction,
        jump_drift_pct, regime_weight, effective_kelly_rank
    """
    if not advanced.available:
        return horizon_block
    drift = float(horizon_block.get('drift_pct', 0.0))
    sigma = float(horizon_block.get('sigma_pct', 0.0))
    posterior_precision = float(horizon_block.get('posterior_precision', 0.0))
    p_up_gauss = float(horizon_block.get('p_up', 0.5))
    direction_gauss = horizon_block.get('direction', 'Neutral')

    # Cornish-Fisher p_up.  Use horizon-scaled moments: skew & excess
    # kurt of daily returns translate roughly as skew/√h and kurt/h
    # under additive iid assumption (this is the classical
    # central-limit-theorem scaling result).  Intraday horizons use
    # the same daily skew/kurt as a conservative proxy.
    h = max(1, int(horizon_units))
    skew_h = advanced.realized_skew / math.sqrt(h)
    kurt_h = advanced.realized_excess_kurt / h
    p_up_cf = cornish_fisher_p_up(drift, sigma, skew_h, kurt_h)

    # VaR / CVaR over the horizon.
    var95 = var_pct(drift, sigma, alpha=_VAR_ALPHA,
                    skew=skew_h, excess_kurt=kurt_h)
    cvar95 = cvar_pct(drift, sigma, alpha=_VAR_ALPHA)

    # Jump-drift contribution: λ · μ_j · h.  For intraday horizons we
    # convert λ from per-day → per-hour and the horizon stays in
    # hours.
    if is_intraday:
        lam_per_unit = advanced.jump_intensity_per_day / 6.5
    else:
        lam_per_unit = advanced.jump_intensity_per_day
    jump_drift = lam_per_unit * advanced.jump_mean_return_pct * h

    # Half-Kelly position recommendation (signed: long if positive,
    # short if negative).  Computed against PLAIN drift since that's
    # the unbiased expected return; CF only adjusts our probability of
    # hitting positive return.
    kelly = fractional_kelly(drift, sigma, fraction=_KELLY_FRACTION_OF_FULL)

    # CF-derived direction.  When skew/kurt are negligible this matches
    # the Gaussian direction; under heavy left-skew it can flip the
    # bullish-by-drift call to bearish-by-quantile.
    if p_up_cf >= 0.55:
        direction_cf = 'Bullish'
        direction_cf_sign = 1
    elif p_up_cf <= 0.45:
        direction_cf = 'Bearish'
        direction_cf_sign = -1
    else:
        direction_cf = 'Neutral'
        direction_cf_sign = 0

    directional_certainty_cf = max(0.0, 2.0 * abs(p_up_cf - 0.5))

    # Sign-agreement between Gaussian drift sign and CF p_up direction.
    # When they DISAGREE the rank is dampened — the CF view is telling
    # us the fat-tail-adjusted bet is unreliable.
    drift_sign = 1 if drift > 0 else (-1 if drift < 0 else 0)
    agreement = 1.0
    if direction_cf_sign != 0 and drift_sign != 0 and direction_cf_sign != drift_sign:
        agreement = 0.5

    # Regime weight from Hurst, using CF direction (the one we
    # actually trade).
    regime_w = regime_weight_from_hurst(advanced.hurst_exponent,
                                        direction_cf_sign or drift_sign)
    regime_label = (
        'trending' if advanced.hurst_exponent >= 0.55
        else 'mean-reverting' if advanced.hurst_exponent <= 0.45
        else 'random-walk'
    )

    # Effective Kelly rank — the new sort key.  Sign = CF direction
    # (so positive=long, negative=short).  Magnitude = expected-return
    # magnitude × precision × certainty × regime × agreement.
    sign_for_rank = direction_cf_sign if direction_cf_sign != 0 else drift_sign
    abs_expected = abs(drift + jump_drift)
    effective_kelly = (
        sign_for_rank
        * abs_expected
        * math.sqrt(max(0.0, posterior_precision))
        * directional_certainty_cf
        * regime_w
        * agreement
    )

    horizon_block.update({
        'p_up_cf': round(p_up_cf, 4),
        'p_up_gauss': round(p_up_gauss, 4),
        'direction_cf': direction_cf,
        'direction_gauss': direction_gauss,
        'directional_certainty_cf': round(directional_certainty_cf, 4),
        'var95_pct': round(var95, 4),
        'cvar95_pct': round(cvar95, 4),
        'kelly_fraction': round(kelly, 4),
        'jump_drift_pct': round(jump_drift, 5),
        'regime_weight': round(regime_w, 4),
        'regime_label': regime_label,
        'cf_drift_agreement': round(agreement, 2),
        'effective_kelly_rank': round(effective_kelly, 6),
        'effective_kelly_rank_abs': round(abs(effective_kelly), 6),
    })
    return horizon_block


def attach_per_symbol_signals(row: dict, advanced: AdvancedSignals) -> dict:
    """Attach a compact `advanced_signals` sub-dict to the row so the
    detail panel can display it.  Mutates and returns `row`."""
    if not advanced.available:
        row['advanced_signals'] = None
        return row
    row['advanced_signals'] = {
        'n_obs': advanced.n_obs,
        'hurst_exponent': round(advanced.hurst_exponent, 4),
        'realized_skew': round(advanced.realized_skew, 4),
        'realized_excess_kurt': round(advanced.realized_excess_kurt, 4),
        'jump_intensity_per_day': round(advanced.jump_intensity_per_day, 5),
        'jump_mean_return_pct': round(advanced.jump_mean_return_pct, 4),
        'jump_std_return_pct': round(advanced.jump_std_return_pct, 4),
        'n_jumps': advanced.n_jumps,
        'ou_half_life_days': round(advanced.ou_half_life_days, 2),
        'ou_speed': round(advanced.ou_speed, 6),
        'rv_har_sigma_pct': round(advanced.rv_har_sigma_pct, 4),
        'regime_label': (
            'trending' if advanced.hurst_exponent >= 0.55
            else 'mean-reverting' if advanced.hurst_exponent <= 0.45
            else 'random-walk'
        ),
    }
    return row
