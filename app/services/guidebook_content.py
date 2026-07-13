"""Phase 26.50 — Guidebook content registry.

Single source of truth for the downloadable PDF guidebook.  The metric
definitions are imported from `_metric_info.json` (generated from the
frontend's `FF_METRIC_INFO` so the PDF and the click-to-pin popovers
never drift apart).  The longer-form sections (how to read the cell,
blending rules, tips & tricks) live below as Python strings.

The PDF generator (`guidebook_pdf.py`) consumes this module's
`build_guidebook()` to render a fully styled PDF.
"""
from __future__ import annotations

import json
from pathlib import Path

_METRIC_INFO_PATH = Path(__file__).with_name('_metric_info.json')

with _METRIC_INFO_PATH.open() as _fh:
    METRIC_INFO: dict[str, dict] = json.load(_fh)


# ---------------------------------------------------------------------------
# Section grouping — order matters for the printed Table of Contents.
# ---------------------------------------------------------------------------
SECTIONS: list[dict] = [
    {
        'id': 'per_horizon',
        'title': 'Per-Horizon Forecast Block (Fast + GARCH tiers)',
        'intro': (
            "Every row in the leaderboard carries a forecast block for each horizon "
            "(1h, 5h, 1d, 5d, 20d hold).  When the priority lane has fired on a "
            "symbol the row also carries a `forward_metrics_garch` block — a higher-quality "
            "re-fit of the same horizon using GARCH-conditional volatility and the full "
            "advanced-math overlay.  The detail panel surfaces both; the leaderboard "
            "sorts on the better of the two whenever GARCH is available."
        ),
        'metrics': [
            'direction_cf', 'p_up_cf', 'p_up_gauss', 'drift_pct', 'jump_drift_pct',
            'sigma_pct', 'var95_pct', 'cvar95_pct', 'directional_certainty_cf',
            'kelly_fraction', 'effective_kelly_rank', 'regime_label',
        ],
    },
    {
        'id': 'advanced_math',
        'title': 'Advanced-Math Overlay (per-symbol)',
        'intro': (
            "These statistics describe the underlying return distribution and price "
            "dynamics.  They are computed once per symbol from the daily-close series "
            "and feed into every horizon's forecast block."
        ),
        'metrics': [
            'hurst_exponent', 'realized_skew', 'realized_excess_kurt',
            'jump_intensity_per_day', 'jump_mean_return_pct', 'ou_half_life_days',
            'rv_har_sigma_pct',
        ],
    },
    {
        'id': 'lab',
        'title': 'Lab Mode — 10 Experimental Signals',
        'intro': (
            "Lab Mode unlocks ten experimental quantitative readings that do not yet "
            "have enough live-validation runs to be promoted to the production tier.  "
            "They are computed on demand and surface in their own grid inside the "
            "Future Forecast cell.  The aggregate `lab_rank_multiplier` is what gets "
            "blended into the leaderboard ranking when the matching toggle is on."
        ),
        'metrics': [
            'rsv_upside_share', 'egarch_leverage_gamma',
            'garch_m_premium_bps_per_sigma', 'permutation_entropy',
            'approximate_entropy', 'mahalanobis_outlier_z', 'dfa_alpha',
            'ssa_trend_slope_pct_per_day', 'vol_hmm_p_stressed',
            'vol_hmm_p_stay_stressed', 'lab_qi_certainty', 'lab_rank_multiplier',
        ],
    },
    {
        'id': 'strategy',
        'title': 'Strategy Tier — 10 Predictive Algorithms',
        'intro': (
            "The Strategy Tier extracts memory, predictability, and regime structure "
            "from the same close-series the Lab Mode uses.  Where Lab signals lean "
            "on distributional moments and regime probabilities, Strategy signals "
            "focus on time-series memory (variance ratios, AR(1)), spectral content "
            "(Welch periodogram, 1/f slope), and complexity measures (RQA, "
            "Lempel-Ziv).  The aggregate `strategy_rank_multiplier` blends into the "
            "leaderboard when the matching toggle is on."
        ),
        'metrics': [
            'strategy_vr5', 'strategy_vr22', 'strategy_ar1', 'strategy_mi_lag1',
            'strategy_spectral_beta', 'strategy_welch_cycle_days',
            'strategy_rqa_determinism', 'strategy_lz_complexity',
            'strategy_emd_slope_pct', 'strategy_vol_regime_mom',
            'strategy_rank_multiplier',
        ],
    },
]


# ---------------------------------------------------------------------------
# Long-form sections (front matter + how-to + tips).
# ---------------------------------------------------------------------------
INTRO_TEXT = (
    "This guidebook describes every numeric reading you can pin from a Future Forecast "
    "cell in the Market Refinement Dashboard.  It covers the per-horizon forecast "
    "block (fast + GARCH tiers), the symbol-level advanced-math overlay, and the two "
    "experimental tiers — Lab Mode and Strategy Tier — that unlock additional "
    "quantitative readings.  Each metric entry tells you what the number is, how to "
    "read it at a glance, and how it feeds into the leaderboard ranking.\n\n"
    "Use this document as a reference while the dashboard is open.  Every reading in "
    "the click-to-pin popover comes from the same definitions you'll see here, plus "
    "a tone (Bullish / Bearish / Neutral / Caution) and intensity (weak / moderate / "
    "strong / extreme) computed from the current live value."
)

HOW_TO_READ_CELL = [
    ("Reading the Future Forecast cell", [
        "The cell is divided into three (or four) horizontal sections, top to bottom:",
        "1. Direction line — Bullish / Bearish / Neutral plus a colored chip.  This is the "
        "Cornish-Fisher direction from the active horizon block (1h hold by default).",
        "2. Numeric grid — the seven core fields (P(up) CF, drift, σ, VaR, kelly, effective "
        "kelly rank, regime).  Hovering or clicking any value pins the explainer popover "
        "with a live reading + intensity chip.",
        "3. Optional Lab Mode block — only visible when Lab Mode toggle is on.  Shows the 10 "
        "Lab signals and an Overall Lab Prediction banner that aggregates them.",
        "4. Optional Strategy Tier block — only visible when Strategy Tier toggle is on.  "
        "Same layout as Lab Mode but driven by the 10 Strategy signals.",
        "When the priority lane has rebuilt a symbol with the GARCH overlay, the cell "
        "annotates each field with a small `garch` badge.  The leaderboard always prefers "
        "the GARCH block when available — you don't have to do anything to opt in.",
    ]),
    ("Tier system: fast vs GARCH", [
        "The fast tier runs on every row every tick.  It uses a Bayesian-blended drift "
        "estimate and Cornish-Fisher fat-tail-adjusted P(up).",
        "The GARCH tier runs only on the top 25 leaderboard rows (by fast-tier rank) and "
        "any symbol you've explicitly deep-refreshed.  It replaces the volatility estimate "
        "with a GARCH(1,1) one-step-ahead forecast, drops the Cornish-Fisher overlay onto "
        "that, and recomputes drift, VaR/CVaR, kelly and the effective rank.",
        "Intraday horizons (1h, 5h) carry a `garch-mixed` tier label — GARCH is a daily-scale "
        "model, so intraday blocks reuse the fast σ while taking the GARCH-adjusted higher "
        "moments.  This is by design, not a bug.",
        "Manual deep-refresh forces the GARCH tier to be recomputed even if the symbol isn't "
        "in the top-25 priority lane.  Use it when you want maximum-quality numbers on a "
        "specific symbol.",
    ]),
    ("Reading the live colored chip", [
        "Every popover and tier-summary panel leads with two chips:",
        "1. A direction pill (Bullish / Bearish / Neutral / Trending / Stressed regime / "
        "Mean-reverting / etc.) coloured green / red / amber / grey to match the call.",
        "2. An intensity chip (weak / moderate / strong / extreme) coloured to MATCH the "
        "direction — bullish-extreme is bright green, bearish-extreme is bright red.  Amber "
        "intensities only appear on caution-tone reads (e.g. Stressed Vol Regime).  If you "
        "see a red intensity, the metric is calling bearish or flagging real risk.",
    ]),
]

BLENDING_RULES = [
    ("Future Mode toggle", [
        "Future Mode is the master gate.  When OFF, the leaderboard ranks on the classical "
        "0–100 composite score.  When ON, ranking switches to `effective_kelly_rank_abs` of "
        "the active horizon (1h hold default).",
        "Future Mode also unlocks the horizon dropdown, the Bulls/Bears/All filter, the "
        "intensity-band filter, and the Lab/Strategy parent toggles.",
    ]),
    ("Lab Mode + Blend Lab into ranking", [
        "Turning on Lab Mode reveals the Lab section in every detail-panel Future Forecast "
        "cell.  By itself it does NOT change the leaderboard order — readings are "
        "standalone informational.",
        "Turning ON Blend Lab into Ranking multiplies the effective Kelly rank by "
        "`lab_rank_multiplier` (typical range 0.85–1.20) before the leaderboard sorts.  "
        "If you check Blend Lab without Lab Mode being on, the dashboard auto-cascades and "
        "enables Lab Mode + Future Mode for you.  Conversely, unchecking Lab Mode also "
        "unchecks the blend, so the UI never lies about what's actually impacting the order.",
    ]),
    ("Strategy Tier + Blend Strategy into ranking", [
        "Same UX pattern as Lab Mode.  Turning Strategy Tier on alone is informational; "
        "turning the Blend Strategy toggle on multiplies the rank by "
        "`strategy_rank_multiplier`.  Both blends can be active simultaneously — the "
        "ranking simply chains the two multipliers.",
    ]),
    ("Intensity-band filter", [
        "The All / Moderate / Strong / Max buttons filter the leaderboard by the magnitude "
        "of the effective Kelly rank.  Moderate = top 50 %; Strong = top 25 %; Max = "
        "top 10 % (approximate).  Useful for cutting noise on quiet days when most rows "
        "are near zero conviction.",
    ]),
]

TIPS_AND_TRICKS = [
    "Always start with the GARCH block.  If a symbol you care about is showing fast-tier "
    "numbers, click its deep-refresh button (the ⚡ icon in the detail panel) to force "
    "a high-quality re-fit.",
    "Cross-reference Cornish-Fisher P(up) against the Gaussian P(up).  A wide gap means "
    "the tails are doing real work — trust the CF call and shrink your size if you're "
    "going against it.",
    "If the regime label says 'mean-reverting' but the Hurst exponent is above 0.55, "
    "you have conflicting evidence.  Drop a half size or wait one tick.  Tier alignment "
    "(`lab_qi_certainty`) above 0.30 means the fast and GARCH tiers agree — high "
    "conviction signal.",
    "Vol HMM saying 'stressed regime' with `p_stay_stressed` above 0.6 means the storm "
    "isn't passing soon.  Pair-trades and theta strategies tend to work in that regime; "
    "directional momentum strategies suffer.",
    "Strategy VR(5) > 1.10 with AR(1) > 0.05 and EMD slope positive is the classic "
    "trend-day combo.  Conversely, VR(5) < 0.90 with AR(1) < -0.05 is a clean "
    "mean-reversion setup — sell tops / buy bottoms.",
    "When effective Kelly rank is sub-0.005 across the entire top-25, the day is "
    "low-conviction.  Switch to the Max intensity-band filter and only trade the rows "
    "that survive.",
    "Blend toggles are multiplicative.  Lab × Strategy = 1.20 × 1.15 = 1.38 boost.  "
    "If you're using both, expect material reshuffling of the leaderboard versus the "
    "pure Future-Mode ordering.",
    "Hard reset clears the snapshot store, all caches, and the priority lane.  Soft "
    "reset only flushes in-memory caches.  Use soft reset to force a fresh GARCH pass; "
    "use hard reset only when the universe seed or variant file changed.",
]


def build_guidebook() -> dict:
    """Returns a structured payload the PDF generator renders into pages.

    Returned shape:
        {
            'title': 'Market Refinement Dashboard — Metric Guidebook',
            'subtitle': 'Phase 26.50',
            'intro_text': str,
            'how_to_read': list[(heading, list[paragraph])],
            'blending_rules': list[(heading, list[paragraph])],
            'sections': list[{
                'title': str,
                'intro': str,
                'metrics': list[{
                    'key': str,
                    'label': str,
                    'summary': str,
                    'interpretation': str,
                    'impact': str,
                }],
            }],
            'tips': list[str],
        }
    """
    sections_out = []
    for sec in SECTIONS:
        metric_rows = []
        for k in sec['metrics']:
            info = METRIC_INFO.get(k)
            if not info:
                continue
            metric_rows.append({
                'key': k,
                'label': info.get('label', k),
                'summary': info.get('summary', ''),
                'interpretation': info.get('interpretation', ''),
                'impact': info.get('impact', ''),
            })
        sections_out.append({
            'title': sec['title'],
            'intro': sec['intro'],
            'metrics': metric_rows,
        })
    return {
        'title': 'Market Refinement Dashboard',
        'subtitle': 'Metric & Strategy Guidebook — Phase 26.50',
        'intro_text': INTRO_TEXT,
        'how_to_read': HOW_TO_READ_CELL,
        'blending_rules': BLENDING_RULES,
        'sections': sections_out,
        'tips': TIPS_AND_TRICKS,
    }
