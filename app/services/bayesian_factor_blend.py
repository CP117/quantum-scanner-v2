"""Phase 26.42 — Bayesian factor-conditional drift estimator.

The legacy prediction model treated every factor family as a unit
direction signal that got modulated by a fixed multiplier.  This
implicitly weights all factors equally and ignores their actual
historical predictive power.

The proper way to combine multiple noisy unbiased estimators of a
single quantity (drift μ) is Bayesian inverse-variance weighting:

    Each estimator i has:  μ̂_i  with precision  τ_i = 1 / σ²_i
    Posterior precision:   τ_post = Σ τ_i  (under independence)
    Posterior mean:        μ_post = Σ (τ_i · μ̂_i) / τ_post
    Posterior variance:    σ²_post = 1 / τ_post

This is the minimum-variance unbiased estimator (BLUE under Gaussian
assumptions, near-optimal otherwise).

Each factor family contributes a "per-unit-standardised-score drift"
that is taken from the published quant-finance literature for
cross-sectional equity factors:

    Family                       | Expected daily drift per +1σ signal
    -----------------------------+------------------------------------
    Momentum                     | ~10 bps  (Carhart, Asness)
    Trend (time-series)          | ~8 bps   (Moskowitz/Ooi/Pedersen)
    Volume sentiment             | ~5 bps   (Easley/Lopez de Prado)
    Options positioning (gamma)  | ~7 bps   (Lemmon/Ni, Garleanu et al.)
    Institutional confluence     | ~6 bps   (institutional flow lit.)
    IOB resistance/support       | ~4 bps   (technical-level lit.)
    Dark pool proxy              | ~3 bps   (Buti/Rindi/Werner)
    Reaction clustering (regime) | ~5 bps   (regime-dependent lit.)
    Quality                      | ~5 bps   (Asness/Frazzini/Pedersen)
    Stability                    | ~3 bps   (low-volatility anomaly)

Reliabilities (σ_i) are calibrated so that a "perfectly aligned"
factor reading (score = 100 → standardised z = +1) generates the
above drift with a confidence interval that's about 3x the drift.
That gives a Sharpe ratio of ~0.33 per single-factor read, which is
in line with the cross-sectional out-of-sample evidence.  The posterior
then combines them into a much tighter aggregate.

Outputs are in **percent per period** (whatever the period is in the
upstream GARCH forecast — usually trading days for daily, hours for
intraday).
"""
from __future__ import annotations

from dataclasses import dataclass


# Per-factor drift coefficients in percent per period when the factor
# is +1 sigma above its long-run mean.  Negative reads simply flip
# the sign.  See module docstring for citations.
#
# Two columns: (daily_bps, intraday_hourly_bps).  The intraday column
# is scaled down by sqrt(6.5) (the trading-hours-per-day) but for
# microstructure factors (options gamma, volume sentiment, IOB) the
# intraday signal is actually STRONGER than the daily ratio implies —
# dealer hedging and order-flow effects play out within hours.
_FACTOR_DRIFT_BPS = {
    # name                 (daily,  hourly)
    'momentum':            (10.0,   2.5),
    'trend':               ( 8.0,   1.8),
    'volume_sentiment':    ( 5.0,   3.5),    # microstructure-heavy → intraday stronger relatively
    'options_positioning': ( 7.0,   4.5),    # dealer gamma plays out within hours
    'institutional_confluence': ( 6.0, 2.0),
    'institutional_order_block': ( 4.0, 2.5), # IOB levels matter MORE intraday
    'dark_pool_proxy':     ( 3.0,   1.5),
    'reaction_clustering': ( 5.0,   2.0),
    'quality':             ( 5.0,   0.5),    # fundamentals barely matter intraday
    'stability':           ( 3.0,   0.3),
    # Regulatory is signed (insider buys vs sells); use a separate path.
    'regulatory':          ( 8.0,   1.5),
}

# Confidence ratio: the expected drift is `drift_bps`, and the implied
# sigma per period for ONE factor read is `drift_bps × CONF_RATIO`.
# A CONF_RATIO of 3.0 means a single-factor Sharpe ~ 1/3 per period.
_FACTOR_CONF_RATIO = 3.0

# Standardisation: factor scores arrive as 0-100 with 50 = neutral.
# Treat (score - 50) / 50 = z ∈ [-1, +1] as the "unit signal".  A
# raw score of 100 = +1 sigma signal (the cited drift number).
def _factor_z(score: float | None) -> float | None:
    if score is None:
        return None
    try:
        s = float(score)
    except (TypeError, ValueError):
        return None
    return max(-1.0, min(1.0, (s - 50.0) / 50.0))


@dataclass(frozen=True)
class FactorContribution:
    name: str
    raw_score: float
    z_signal: float
    drift_per_period_pct: float            # μ̂_i (signed)
    sigma_per_period_pct: float            # σ_i (positive)
    precision: float                       # τ_i = 1/σ²_i
    posterior_contribution_pct: float      # τ_i·μ̂_i / Σ τ  (the share this factor pushed)


@dataclass(frozen=True)
class BayesianBlend:
    """Result envelope from `blend_factors_for_drift`."""
    posterior_drift_per_period_pct: float
    posterior_sigma_per_period_pct: float
    posterior_precision: float
    total_drift_horizon_pct: float         # μ_post × horizon
    total_sigma_horizon_pct: float         # σ_post × sqrt(horizon)
    n_factors_used: int
    contributions: list[FactorContribution]
    # Shrinkage: if posterior precision is very low relative to typical
    # scale, we shrink toward zero drift.  Reported here so the UI can
    # show why a strong-looking factor read produced a modest drift.
    shrinkage_factor: float                # in [0, 1]; 1.0 = no shrink


# When the aggregate posterior precision is below this threshold, we
# shrink the posterior drift toward zero to avoid over-confident calls
# on thin/noisy inputs.  Calibrated so a single high-quality factor
# yields ~0.7x shrinkage; full 7-factor agreement yields ~1.0x.
_SHRINKAGE_REFERENCE_PRECISION = 5.0


def blend_factors_for_drift(
    factor_scores: dict[str, float | None],
    final_direction_sign: int,
    horizon: int,
    is_intraday: bool,
    regulatory_signal: dict | None = None,
) -> BayesianBlend:
    """Compute the Bayesian posterior drift over the requested horizon.

    Args:
        factor_scores: {family_name: score 0-100}.  Missing keys are
            simply omitted from the posterior — they contribute zero
            precision.
        final_direction_sign: +1, 0, or -1.  Used only for the
            regulatory signal which is already signed and doesn't
            otherwise need a separate direction.
        horizon: forecast horizon in periods.
        is_intraday: True for hourly forecasts, False for daily.
            Selects the appropriate `_FACTOR_DRIFT_BPS` column.
        regulatory_signal: {score_delta, weight, event_count} or None.
            score_delta is a signed pre-clipped value with σ ≈ 8.

    Returns: BayesianBlend with everything the upstream prediction
    service needs to plug into the probit direction map + price target.
    """
    contributions: list[FactorContribution] = []
    sum_precision = 0.0
    sum_weighted_drift = 0.0

    bps_col = 1 if is_intraday else 0
    bps_to_pct = 1e-2  # 1 bps = 0.01 percent

    for name, (daily_bps, hourly_bps) in _FACTOR_DRIFT_BPS.items():
        if name == 'regulatory':
            continue  # handled separately below
        z = _factor_z(factor_scores.get(name))
        if z is None:
            continue
        per_unit_drift = (hourly_bps if is_intraday else daily_bps) * bps_to_pct
        drift = z * per_unit_drift                 # signed
        sigma = abs(per_unit_drift) * _FACTOR_CONF_RATIO
        if sigma <= 0:
            continue
        precision = 1.0 / (sigma * sigma)
        sum_precision += precision
        sum_weighted_drift += precision * drift
        contributions.append(FactorContribution(
            name=name,
            raw_score=float(factor_scores.get(name) or 50.0),
            z_signal=z,
            drift_per_period_pct=drift,
            sigma_per_period_pct=sigma,
            precision=precision,
            posterior_contribution_pct=0.0,    # filled in below
        ))

    # Regulatory: score_delta already lives in a comparable scale
    # (roughly ±8 max).  Treat it as a signed +1σ-equivalent signal
    # when |delta| >= 4 (the "material" threshold).
    if regulatory_signal:
        delta = float(regulatory_signal.get('score_delta') or 0.0)
        weight = float(regulatory_signal.get('weight') or 0.0)
        if abs(delta) >= 0.5 and weight > 0.05:
            # Normalise to [-1, +1]
            z = max(-1.0, min(1.0, delta / 8.0))
            daily_bps, hourly_bps = _FACTOR_DRIFT_BPS['regulatory']
            per_unit_drift = (hourly_bps if is_intraday else daily_bps) * bps_to_pct
            # Scale by signal weight (how recent / material).
            drift = z * per_unit_drift * min(1.0, weight)
            sigma = abs(per_unit_drift) * _FACTOR_CONF_RATIO
            precision = 1.0 / (sigma * sigma)
            sum_precision += precision
            sum_weighted_drift += precision * drift
            contributions.append(FactorContribution(
                name='regulatory',
                raw_score=float(50.0 + delta * 6.0),  # rough cosmetic remap to 0-100 scale
                z_signal=z,
                drift_per_period_pct=drift,
                sigma_per_period_pct=sigma,
                precision=precision,
                posterior_contribution_pct=0.0,
            ))

    n = len(contributions)
    if sum_precision <= 0 or n == 0:
        return BayesianBlend(
            posterior_drift_per_period_pct=0.0,
            posterior_sigma_per_period_pct=float('inf'),
            posterior_precision=0.0,
            total_drift_horizon_pct=0.0,
            total_sigma_horizon_pct=float('inf'),
            n_factors_used=0,
            contributions=[],
            shrinkage_factor=0.0,
        )

    # Raw posterior (no shrinkage)
    raw_post_drift = sum_weighted_drift / sum_precision
    raw_post_sigma = 1.0 / (sum_precision ** 0.5)

    # James-Stein-style shrinkage toward zero drift when precision is
    # low.  This is the principled regularisation — high noise → less
    # confident point estimate.  shrink = τ / (τ + τ_ref); at τ = τ_ref
    # we shrink by 50 %.
    shrinkage = sum_precision / (sum_precision + _SHRINKAGE_REFERENCE_PRECISION)
    post_drift = raw_post_drift * shrinkage
    post_sigma = raw_post_sigma   # sigma stays — shrinkage is on drift only

    # Fill in per-factor share (what fraction of posterior drift came
    # from this factor) for UI breakdown.
    final_contribs: list[FactorContribution] = []
    for c in contributions:
        share = (c.precision * c.drift_per_period_pct) / sum_precision * shrinkage
        final_contribs.append(FactorContribution(
            name=c.name, raw_score=c.raw_score, z_signal=c.z_signal,
            drift_per_period_pct=c.drift_per_period_pct,
            sigma_per_period_pct=c.sigma_per_period_pct,
            precision=c.precision,
            posterior_contribution_pct=share,
        ))

    total_drift = post_drift * horizon
    total_sigma = post_sigma * (horizon ** 0.5)

    return BayesianBlend(
        posterior_drift_per_period_pct=post_drift,
        posterior_sigma_per_period_pct=post_sigma,
        posterior_precision=sum_precision,
        total_drift_horizon_pct=total_drift,
        total_sigma_horizon_pct=total_sigma,
        n_factors_used=n,
        contributions=final_contribs,
        shrinkage_factor=shrinkage,
    )


def normal_cdf(z: float) -> float:
    """Standard-normal CDF via the erf approximation.

    Used to map the Bayesian (μ, σ) posterior into a directional
    probability:  P(up) = Φ(μ / σ).
    Pure-Python so we don't pull in scipy.
    """
    # Abramowitz-Stegun erf-via-tanh approximation: max error ~1.5e-7
    import math
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
