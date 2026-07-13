"""Phase 26.42 — GARCH(1,1) volatility forecaster.

The legacy prediction model used the naive `ATR × sqrt(h)` random-walk
formula for the h-period sigma.  That implicitly assumes returns are
i.i.d. Gaussian with constant variance — which is empirically false
for every liquid equity ever traded.  Equity returns exhibit:

    * Volatility clustering — calm periods cluster, vol shocks cluster.
    * Mean-reversion in variance to a long-run level.
    * Persistence (autocorrelation in squared returns of ~0.95+).
    * Asymmetry (downside vol > upside vol — the "leverage effect").

GARCH(1,1) captures the first three of those (we'll layer EGARCH-style
asymmetry on top via the regime-conditional drift in the main model).

Model:
    σ²_t = ω + α · ε²_{t-1} + β · σ²_{t-1}

Stationary unconditional variance:
    V = ω / (1 - α - β),   provided α + β < 1.

Multi-step variance forecast (well-known closed form):
    σ²_{t+h | t} = V + (α + β)^h · (σ²_{t+1 | t} - V)

H-period TOTAL variance forecast (sum of point forecasts):
    Var_{1..h} = Σ_{i=1..h} σ²_{t+i | t}
                = h · V + (σ²_{t+1 | t} - V) · (1 - (α + β)^h) / (1 - (α + β))

This gives the h-step sigma needed for the Bayesian posterior and the
probit direction probability in `advanced_prediction_service.py`.

We fit (ω, α, β) using method-of-moments — NOT maximum likelihood —
because:
  1. ML requires iterative optimization (slow, can fail to converge).
  2. The shortcut (α ≈ 0.10, β ≈ 0.85, fit V from sample) is the
     industry-standard "RiskMetrics-style" starting point and is within
     a few percent of ML estimates for daily equity returns.
  3. This is a real-time scanner; we score 800+ symbols every 2 s.
     A closed-form moment estimator is the right complexity tier.

Returns are computed as log-returns: r_t = ln(P_t / P_{t-1}).  This
preserves additivity over horizons and is the conventional GARCH input.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence


# RiskMetrics-style fixed parameters for daily equity GARCH(1,1).
# These are the well-cited industry defaults; they sit very close to
# the cross-sectional MLE for liquid US large-cap names.
_ALPHA_DEFAULT = 0.10
_BETA_DEFAULT = 0.85
# Persistence = α + β.  RiskMetrics-style sits at 0.95, well-fitted
# equities are typically 0.96-0.99.  Don't let it touch 1.0 (non-
# stationary regime) — clamp on extreme samples.
_PERSISTENCE_MAX = 0.995
_MIN_OBSERVATIONS = 20  # Below this we degrade to ATR-style.

# Annualisation constant for reporting.  252 trading days/year.
_TRADING_DAYS_PER_YEAR = 252


@dataclass(frozen=True)
class GarchForecast:
    """Result envelope from `garch_forecast`."""
    # Inputs
    n_observations: int                    # how many returns we had
    sample_var: float                      # realised variance over sample (pct² units)
    last_shock_sq: float                   # ε²_t (most recent squared return)
    last_var: float                        # σ²_t (most recent conditional variance)
    # Model parameters used
    alpha: float
    beta: float
    omega: float
    unconditional_var: float              # long-run V (pct² units)
    persistence: float                    # α + β
    # Forward forecasts
    one_step_var: float                   # σ²_{t+1|t}
    h_period_var: float                   # Σ σ²_{t+i|t} for i in 1..h (pct² units)
    h_period_sigma: float                 # sqrt(h_period_var)  (pct)
    horizon: int
    # Annualised summary (for the UI)
    annualised_vol_pct: float             # 1-day GARCH sigma * sqrt(252) — comparable to VIX-style numbers
    # Provenance
    source: str                           # 'garch' (full fit) or 'fallback' (insufficient sample)


def _log_returns(closes: Sequence[float]) -> list[float]:
    """Convert a sequence of closing prices to log-return percentages.

    We multiply by 100 so the variance is in `%² per period` units.
    That keeps the numerical scale comfortable for `α + β · 0.5²`-style
    products (raw decimal returns produce 1e-8 scale variances that
    accumulate float error).
    """
    out: list[float] = []
    last = None
    for px in closes:
        try:
            v = float(px)
        except (TypeError, ValueError):
            continue
        if v <= 0:
            continue
        if last is not None and last > 0:
            try:
                out.append(math.log(v / last) * 100.0)
            except (ValueError, ZeroDivisionError):
                pass
        last = v
    return out


def _ewma_var(returns: Sequence[float], decay: float = 0.94) -> float:
    """Exponentially-weighted moving variance — used to seed σ²_t for
    the forward recursion (the conditional variance at the *last*
    observation, before we project forward).

    decay=0.94 is the RiskMetrics standard.  Reflects ~25-day effective
    half-life.
    """
    if not returns:
        return 0.0
    v = returns[0] ** 2  # warm-start with first squared return
    one_minus = 1.0 - decay
    for r in returns[1:]:
        v = decay * v + one_minus * r * r
    return v


def garch_forecast(closes: Sequence[float], horizon: int,
                   alpha: float = _ALPHA_DEFAULT,
                   beta: float = _BETA_DEFAULT) -> GarchForecast:
    """Closed-form GARCH(1,1) forecast for the variance and sigma over
    a forward horizon of `horizon` periods.

    Periods are dimensionless — whatever frequency `closes` is sampled
    at (daily, hourly, 5-min) defines the units of `sigma` in the result.
    Callers are responsible for matching `horizon` to that frequency.

    Math (see module docstring for derivation):
        ω    = V × (1 - α - β)
        σ²_{t+1|t} = ω + α · ε²_t + β · σ²_t
        σ²_{t+h|t} = V + (α + β)^h · (σ²_{t+1|t} - V)
        Var[1..h]  = h·V + (σ²_{t+1|t} - V) · (1 - (α+β)^h) / (1 - (α+β))
    """
    horizon = max(1, int(horizon))
    returns = _log_returns(closes)
    n = len(returns)

    # Cold-start: under the minimum sample size, fall back to a "sample
    # variance scaled by sqrt(horizon)" estimator.  This is what the
    # legacy ATR×sqrt(h) path was doing.  We still surface it via the
    # same envelope so the caller doesn't need a branch.
    if n < _MIN_OBSERVATIONS:
        sample_var = (sum(r * r for r in returns) / n) if n else 4.0  # 2% sigma default
        one_step = sample_var
        h_var = sample_var * horizon
        return GarchForecast(
            n_observations=n,
            sample_var=sample_var,
            last_shock_sq=(returns[-1] ** 2 if returns else 0.0),
            last_var=sample_var,
            alpha=0.0,
            beta=0.0,
            omega=0.0,
            unconditional_var=sample_var,
            persistence=0.0,
            one_step_var=one_step,
            h_period_var=h_var,
            h_period_sigma=math.sqrt(h_var),
            horizon=horizon,
            annualised_vol_pct=math.sqrt(sample_var * _TRADING_DAYS_PER_YEAR),
            source='fallback',
        )

    # Method-of-moments-ish fit.  We use sample variance for V, then
    # plug in the canonical RiskMetrics α, β.  Clamp persistence away
    # from the non-stationary boundary.
    mean_r = sum(returns) / n
    sample_var = sum((r - mean_r) ** 2 for r in returns) / max(1, n - 1)
    persistence = min(_PERSISTENCE_MAX, alpha + beta)
    omega = sample_var * (1.0 - persistence)

    # Seed σ²_t with EWMA over the recent sample (better than just
    # sample_var because it weights recent shocks more, which is what
    # GARCH itself does).
    last_var = _ewma_var(returns, decay=0.94)
    last_shock_sq = returns[-1] ** 2

    # σ²_{t+1|t}
    one_step_var = omega + alpha * last_shock_sq + beta * last_var

    # Closed-form sum over forward forecasts.
    if persistence < 1.0 - 1e-9:
        geometric_sum = (1.0 - persistence ** horizon) / (1.0 - persistence)
    else:
        # Degenerate near-unit-root case (shouldn't hit because we
        # clamp persistence < _PERSISTENCE_MAX).
        geometric_sum = float(horizon)
    h_var = horizon * sample_var + (one_step_var - sample_var) * geometric_sum
    h_var = max(h_var, 1e-9)
    h_sigma = math.sqrt(h_var)

    annualised_vol_pct = math.sqrt(max(one_step_var, 1e-9) * _TRADING_DAYS_PER_YEAR)

    return GarchForecast(
        n_observations=n,
        sample_var=sample_var,
        last_shock_sq=last_shock_sq,
        last_var=last_var,
        alpha=alpha,
        beta=beta,
        omega=omega,
        unconditional_var=sample_var,
        persistence=persistence,
        one_step_var=one_step_var,
        h_period_var=h_var,
        h_period_sigma=h_sigma,
        horizon=horizon,
        annualised_vol_pct=annualised_vol_pct,
        source='garch',
    )
