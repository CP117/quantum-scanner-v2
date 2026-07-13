"""Phase 26.60 — Metric registry for the Predictive Expansion Pack.

This module exposes the spec-defined `MetricDefinition` records for
all 14 new metrics + 5 composite multipliers.  The dashboard's
existing popover system reads this via the `/api/registry/phase_2660`
endpoint to render labels, units, descriptions, and ranking-role
chips consistently with the existing chip semantics.

The registry follows the EXACT spec keys.  Do not rename — clients
key off these strings.
"""
from __future__ import annotations

from typing import Literal, TypedDict


MetricCategory = Literal[
    'strategy_v2',
    'lab_v2',
    'regime',
    'liquidity',
    'ml_overlay',
    'reality_breaker',
    'composite',
]

RankingRole = Literal['direct', 'multiplier', 'risk', 'diagnostic']


class MetricDefinition(TypedDict, total=False):
    key: str
    label: str
    shortLabel: str
    group: MetricCategory
    description: str
    units: str
    rangeHint: tuple[float, float]
    higherIsBetterSign: int     # +1 / 0 / -1
    dependsOn: list[str]
    rankingRole: RankingRole
    rankingUsageNotes: str
    experimental: bool


# ===========================================================================
# 10 standard metrics
# ===========================================================================
_STANDARD: list[MetricDefinition] = [
    {
        'key': 'msm_drift_premium',
        'label': 'Markov Drift Premium',
        'shortLabel': 'MSM-DP',
        'group': 'regime',
        'description': "Expected drift of the current hidden Markov regime "
                       "relative to the long-run mean (2-state Gaussian mixture). "
                       "Positive = favourable latent regime; negative = "
                       "crash-prone state.",
        'units': '%/day',
        'rangeHint': (-5.0, 5.0),
        'higherIsBetterSign': 1,
        'dependsOn': ['daily_closes'],
        'rankingRole': 'direct',
        'rankingUsageNotes': 'Additive overlay on drift_pct (medium horizons); '
                             'feeds regime_risk_multiplier.',
        'experimental': False,
    },
    {
        'key': 'ts_nonlinear_dependence',
        'label': 'Nonlinear Dependence Lift',
        'shortLabel': 'NL-DEP',
        'group': 'strategy_v2',
        'description': "Out-of-sample forecast lift of a threshold-AR(2) "
                       "(different persistence when the prior return is up "
                       "vs. down) over a plain linear AR(2), measured by "
                       "walk-forward validation: both models are fit ONLY on "
                       "past data at each step and scored on the next block "
                       "of returns they never saw, then pooled into a single "
                       "R\u00b2 lift. 0 = no exploitable nonlinear structure; 1 = "
                       "strong, held-out-verified state-dependent memory. "
                       "(Reworked 2026-07: the prior version compared "
                       "IN-SAMPLE R\u00b2, which is mathematically guaranteed to "
                       "favor the richer model even on pure noise, since it "
                       "has more free parameters. Testing showed that version "
                       "reported a false 'lift' on 89% of pure-noise series; "
                       "the walk-forward version reports ~0 on the same "
                       "noise.)",
        'units': 'unitless',
        'rangeHint': (0.0, 1.0),
        'higherIsBetterSign': 1,
        'dependsOn': ['daily_closes'],
        'rankingRole': 'multiplier',
        'rankingUsageNotes': 'Feeds strategy_v2_rank_multiplier; amplifies '
                             'conviction in memory-based signals.',
        'experimental': False,
    },
    {
        'key': 'trend_curvature_pct',
        'label': 'Trend Curvature',
        'shortLabel': 'CURV',
        'group': 'strategy_v2',
        'description': "Local quadratic coefficient of smoothed log-price. "
                       "Positive curvature + positive slope = breakout "
                       "continuation; negative curvature + positive slope = "
                       "trend exhaustion.",
        'units': '%/step²',
        'rangeHint': (-2.0, 2.0),
        'higherIsBetterSign': 1,
        'dependsOn': ['daily_closes'],
        'rankingRole': 'multiplier',
        'rankingUsageNotes': 'Signed contributor to strategy_v2_rank_multiplier.',
        'experimental': False,
    },
    {
        'key': 'lead_lag_influence',
        'label': 'Lead-Lag Influence',
        'shortLabel': 'LLI',
        'group': 'strategy_v2',
        'description': "Out-of-sample forecast lift of AR(2) + 2 lagged "
                       "sector/index driver-return terms over AR(2) alone, "
                       "measured the same walk-forward way as "
                       "ts_nonlinear_dependence: both models are fit on past "
                       "data only and scored on unseen future blocks, so the "
                       "driver columns only earn a positive score if they "
                       "genuinely improved held-out forecasts for THIS "
                       "symbol. High = the driver relationship has held up "
                       "out of sample, not just in-sample. (Reworked "
                       "2026-07 for the same reason as ts_nonlinear_dependence "
                       "-- the old in-sample-R\u00b2 version showed a false "
                       "'influence' even between two unrelated random walks. "
                       "This factor now also gates whether "
                       "model_consensus_score (formerly QPII) is allowed to "
                       "use the driver at all: a symbol's driver relationship "
                       "must clear lift > 0.05 here before the consensus "
                       "score will use it as an independent view.)",
        'units': 'unitless',
        'rangeHint': (0.0, 1.0),
        'higherIsBetterSign': 1,
        'dependsOn': ['daily_closes', 'driver_basket'],
        'rankingRole': 'multiplier',
        'rankingUsageNotes': 'Feeds strategy_v2_rank_multiplier.',
        'experimental': False,
    },
    {
        'key': 'volofvol_regime_score',
        'label': 'Vol-of-Vol Regime',
        'shortLabel': 'VOV',
        'group': 'regime',
        'description': "Probability the volatility process itself is in a "
                       "high-vol-of-vol regime. High values indicate "
                       "unstable volatility and elevated forecast fragility.",
        'units': 'probability',
        'rangeHint': (0.0, 1.0),
        'higherIsBetterSign': -1,
        'dependsOn': ['daily_closes'],
        'rankingRole': 'risk',
        'rankingUsageNotes': 'Risk input to regime_risk_multiplier; '
                             'shrinks Kelly when high.',
        'experimental': False,
    },
    {
        'key': 'multiscale_consistency',
        'label': 'Multi-Scale Consistency',
        'shortLabel': 'MSC',
        'group': 'strategy_v2',
        'description': "Directional & regime agreement across 1h, 5h, 1d, 5d, "
                       "20d horizons. Heavier weights on longer horizons.",
        'units': 'signed unit',
        'rangeHint': (-1.0, 1.0),
        'higherIsBetterSign': 1,
        'dependsOn': ['per_horizon_drift'],
        'rankingRole': 'multiplier',
        'rankingUsageNotes': 'Core input to strategy_v2_rank_multiplier; '
                             'high values visibly increase conviction.',
        'experimental': False,
    },
    {
        'key': 'entropy_regime_stability',
        'label': 'Regime Stability (1-H)',
        'shortLabel': 'STAB',
        'group': 'regime',
        'description': "1 - normalised entropy of recent regime labels "
                       "(bull/bear/flat windows). High = persistent regime; "
                       "low = rapidly flipping structure.",
        'units': 'unitless',
        'rangeHint': (0.0, 1.0),
        'higherIsBetterSign': 1,
        'dependsOn': ['daily_closes'],
        'rankingRole': 'multiplier',
        'rankingUsageNotes': 'Feeds regime_risk_multiplier; low stability '
                             'dampens Strategy & Regime blends.',
        'experimental': False,
    },
    {
        'key': 'drawdown_memory_score',
        'label': 'Drawdown Memory',
        'shortLabel': 'DDM',
        'group': 'strategy_v2',
        'description': "Mean forward h-day return after deep drawdowns minus "
                       "mean during calm periods. Positive = bounce behaviour; "
                       "negative = crash-continuation behaviour.",
        'units': '%',
        'rangeHint': (-5.0, 5.0),
        'higherIsBetterSign': 1,
        'dependsOn': ['daily_closes'],
        'rankingRole': 'multiplier',
        'rankingUsageNotes': 'Signed contributor to strategy_v2_rank_multiplier; '
                             'distinguishes bounce setups from continuation breakdowns.',
        'experimental': False,
    },
    {
        'key': 'liq_adjusted_signal',
        'label': 'Liquidity-Adjusted Predictability',
        'shortLabel': 'LIQ-PRED',
        'group': 'liquidity',
        'description': "Predictability score (AR + Hurst proxy + sign-entropy) "
                       "multiplied by normalised liquidity score. High = both "
                       "predictable and tradable.",
        'units': 'unitless',
        'rangeHint': (0.0, 1.0),
        'higherIsBetterSign': 1,
        'dependsOn': ['daily_closes', 'liquidity_score'],
        'rankingRole': 'multiplier',
        'rankingUsageNotes': 'Kelly sizing adjustment (liq_kelly_factor), not '
                             'a direction-flipping signal.',
        'experimental': False,
    },
    {
        'key': 'ml_residual_edge',
        'label': 'ML Residual Edge',
        'shortLabel': 'ML-EDGE',
        'group': 'ml_overlay',
        'description': "z-scored residual between a ridge-regression linear "
                       "baseline and a saturating nonlinear blend. Positive = "
                       "ML sees additional structure beyond explicit formulas; "
                       "negative = ML disagreement or hidden fragility.",
        'units': 'σ',
        'rangeHint': (-3.0, 3.0),
        'higherIsBetterSign': 1,
        'dependsOn': ['daily_closes'],
        'rankingRole': 'multiplier',
        'rankingUsageNotes': 'Drives ml_rank_multiplier (controlled [0.8, 1.2] '
                             'when ML blend is enabled).',
        'experimental': False,
    },
]


# ===========================================================================
# 4 reality-breaker overlays (experimental)
# ===========================================================================
_REALITY_BREAKER: list[MetricDefinition] = [
    {
        'key': 'local_causal_cone_signal',
        'label': 'Local Causal Cone',
        'shortLabel': 'LCC',
        'group': 'reality_breaker',
        'description': "Cone-weighted directional field imposed by the "
                       "lagged causal neighbourhood. Large positive = coherent "
                       "upstream driver pressure; large negative = adverse "
                       "field pressure.",
        'units': 'σ',
        'rangeHint': (-3.0, 3.0),
        'higherIsBetterSign': 1,
        'dependsOn': ['daily_closes', 'driver_basket'],
        'rankingRole': 'direct',
        'rankingUsageNotes': 'Direction confirmation / veto layer on top of '
                             'direction_cf. ONLY active when Advanced '
                             'Experimental Mode is enabled.',
        'experimental': True,
    },
    {
        'key': 'quantum_path_interference_index',
        'label': 'Model Consensus Score',
        'shortLabel': 'CONSENSUS',
        'group': 'reality_breaker',
        'description': "Requires at least ONE view built from data that is "
                       "NOT derived from this symbol's own price history "
                       "before it will output anything: (a) a sector/index "
                       "driver-return regression forecast, gated behind "
                       "lead_lag_influence's own walk-forward validation for "
                       "THIS symbol (a driver relationship that doesn't hold "
                       "up out-of-sample is not used), or (b) the dealer "
                       "gamma-exposure (GEX) regime read from options-market "
                       "positioning, which decides whether to fade or follow "
                       "recent momentum. GARCH and an empirical block "
                       "bootstrap (both price-derived) contribute as "
                       "SUPPORTING views once at least one independent view "
                       "is present, but cannot produce a score alone. All "
                       "views are combined by inverse-variance weighting and "
                       "scored for agreement via Cochran's Q/I\u00b2 "
                       "heterogeneity statistic. If NO independent view is "
                       "available for a symbol (no valid driver, no options "
                       "data), this returns exactly 0.0 -- silence, not a "
                       "guess. Positive = independently-confirmed upward "
                       "horizon-return consensus; negative = downward. "
                       "(Field key retained as quantum_path_interference_index "
                       "for backward compatibility. HISTORY: originally a "
                       "single-model Monte-Carlo 'complex-amplitude "
                       "interference' formula with no real predictive basis "
                       "(retired 2026-07). First rework combined 4 real "
                       "techniques -- GARCH, regime-switching, Hurst tilt, "
                       "block bootstrap -- but walk-forward testing showed "
                       "all 4 were transforms of the same one price series "
                       "and could spuriously 'agree' from shared sampling "
                       "noise alone; that version did not reliably beat a "
                       "naive baseline even with genuine drift present. This "
                       "version fixes that by requiring genuine independent "
                       "data before claiming a consensus.)",
        'units': 'unitless',
        'rangeHint': (-2.0, 2.0),
        'higherIsBetterSign': 1,
        'dependsOn': ['daily_closes', 'driver_basket', 'options_gamma'],
        'rankingRole': 'direct',
        'rankingUsageNotes': 'Horizon-local drift/conviction adjustment. '
                             'CLAMPED + opt-in until live-tested. Frequently '
                             '0.0 for symbols lacking a validated driver or '
                             'options data -- this is intentional, not a bug.',
        'experimental': True,
    },
    {
        'key': 'local_lyapunov_volatility_exponent',
        'label': 'Local Lyapunov Exponent',
        'shortLabel': 'LLVE',
        'group': 'reality_breaker',
        'description': "Local nonlinear trajectory divergence rate on "
                       "delay-embedded returns. High positive = sensitive, "
                       "unstable dynamics.",
        'units': 'log/step',
        'rangeHint': (-1.0, 1.0),
        'higherIsBetterSign': -1,
        'dependsOn': ['daily_closes'],
        'rankingRole': 'risk',
        'rankingUsageNotes': 'Risk-damping overlay (similar to vol-of-vol). '
                             'High values shrink Kelly aggressively when '
                             'Advanced Experimental Mode is enabled.',
        'experimental': True,
    },
    {
        'key': 'temporal_renormalization_score',
        'label': 'Temporal Renormalisation Score',
        'shortLabel': 'TRS',
        'group': 'reality_breaker',
        'description': "Negated |slope| of drift/σ vs log-horizon. Near zero "
                       "= stable cross-scale flow; large negative = scale "
                       "fragility (forecast meaning shifts with horizon).",
        'units': 'unitless',
        'rangeHint': (-2.0, 2.0),
        'higherIsBetterSign': 1,
        'dependsOn': ['per_horizon_drift', 'per_horizon_sigma'],
        'rankingRole': 'diagnostic',
        'rankingUsageNotes': 'Modulates trust in multiscale_consistency and '
                             'cross-horizon blending.',
        'experimental': True,
    },
]


# ===========================================================================
# 5 composite multipliers
# ===========================================================================
_MULTIPLIERS: list[MetricDefinition] = [
    {
        'key': 'strategy_v2_rank_multiplier',
        'label': 'Strategy V2 Multiplier',
        'shortLabel': 'SV2-MULT',
        'group': 'composite',
        'description': "Composite of ts_nonlinear_dependence, trend_curvature_pct, "
                       "lead_lag_influence, multiscale_consistency, and "
                       "drawdown_memory_score. Range clamp [0.6, 1.4].",
        'units': '×',
        'rangeHint': (0.6, 1.4),
        'higherIsBetterSign': 1,
        'dependsOn': ['ts_nonlinear_dependence', 'trend_curvature_pct',
                      'lead_lag_influence', 'multiscale_consistency',
                      'drawdown_memory_score'],
        'rankingRole': 'multiplier',
        'rankingUsageNotes': 'Multiplied into effective_kelly_rank when "Blend '
                             'Strategy V2 into ranking" is enabled.',
        'experimental': False,
    },
    {
        'key': 'regime_risk_multiplier',
        'label': 'Regime Risk Multiplier',
        'shortLabel': 'REG-RISK',
        'group': 'composite',
        'description': "Composite of msm_drift_premium, volofvol_regime_score, "
                       "and entropy_regime_stability. Range clamp [0.6, 1.4].",
        'units': '×',
        'rangeHint': (0.6, 1.4),
        'higherIsBetterSign': 1,
        'dependsOn': ['msm_drift_premium', 'volofvol_regime_score',
                      'entropy_regime_stability'],
        'rankingRole': 'multiplier',
        'rankingUsageNotes': 'Multiplied into effective_kelly_rank when "Blend '
                             'Regime Risk into ranking" is enabled.',
        'experimental': False,
    },
    {
        'key': 'ml_rank_multiplier',
        'label': 'ML Overlay Multiplier',
        'shortLabel': 'ML-MULT',
        'group': 'composite',
        'description': "Bounded mapping of ml_residual_edge into a [0.8, 1.2] "
                       "multiplier. Conservative range — ML provides nudge, "
                       "not a directional override.",
        'units': '×',
        'rangeHint': (0.8, 1.2),
        'higherIsBetterSign': 1,
        'dependsOn': ['ml_residual_edge'],
        'rankingRole': 'multiplier',
        'rankingUsageNotes': 'Multiplied into effective_kelly_rank when "Blend '
                             'ML into ranking" is enabled.',
        'experimental': False,
    },
    {
        'key': 'liq_kelly_factor',
        'label': 'Liquidity Kelly Factor',
        'shortLabel': 'LIQ-KEL',
        'group': 'composite',
        'description': "0.7 + 0.6·liq_adjusted_signal. Sizes down fragile "
                       "edges (low liquidity / low predictability).",
        'units': '×',
        'rangeHint': (0.7, 1.3),
        'higherIsBetterSign': 1,
        'dependsOn': ['liq_adjusted_signal'],
        'rankingRole': 'multiplier',
        'rankingUsageNotes': 'Always-on Kelly sizing factor when Strategy V2 / '
                             'Regime Risk / ML are blended.',
        'experimental': False,
    },
    {
        'key': 'reality_breaker_multiplier',
        'label': 'Reality Breaker Multiplier',
        'shortLabel': 'RB-MULT',
        'group': 'composite',
        'description': "EXPERIMENTAL: 0.30·z(LCC) + 0.25·z(QPII) - 0.25·z(LLVE) "
                       "+ 0.20·z(TRS) → mapped via tanh to [0.5, 1.5]. "
                       "Default OFF; only computed when Advanced Experimental "
                       "Mode is enabled.",
        'units': '×',
        'rangeHint': (0.5, 1.5),
        'higherIsBetterSign': 1,
        'dependsOn': ['local_causal_cone_signal', 'quantum_path_interference_index',
                      'local_lyapunov_volatility_exponent',
                      'temporal_renormalization_score'],
        'rankingRole': 'multiplier',
        'rankingUsageNotes': 'EXPERIMENTAL — opt-in; never flips trade '
                             'direction by itself unless explicit override '
                             'mode is enabled.',
        'experimental': True,
    },
]


def get_registry() -> dict:
    """Return the complete Phase 26.60 registry payload."""
    return {
        'version': '26.60',
        'standard_metrics': list(_STANDARD),
        'reality_breaker_overlays': list(_REALITY_BREAKER),
        'composite_multipliers': list(_MULTIPLIERS),
    }


def get_all_keys() -> list[str]:
    """Flat list of every Phase 26.60 metric / multiplier key."""
    keys = []
    for src in (_STANDARD, _REALITY_BREAKER, _MULTIPLIERS):
        keys.extend(m['key'] for m in src)
    return keys
