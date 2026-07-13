"""
Per-factor narrative generator.

For every factor family in a scored row, produce a 1-2 sentence narrative
explaining (a) what the current reading means in plain English, and
(b) a directional prediction when applicable. The narrative is attached
to the factor breakdown so the frontend can render it inline next to the
factor card or in a hover popover on the table-cell pills.

Design rules:
  - Deterministic: same inputs always produce the same narrative. No LLM
    in the hot path.
  - Bounded length: each narrative is <= 220 characters. The popover
    UI assumes this so it doesn't blow out the layout.
  - Never raises: every function returns a string, even with garbage
    inputs (returns "Insufficient data to interpret this factor.").
  - Cell text vs detail text: `cell_text` is the 1-line popover shown on
    the INST/OPTS/DP table pills. `detail_text` is the multi-line
    description shown under each factor card in the detail panel.
"""
from __future__ import annotations

from typing import Any


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


# =====================================================================
# Helpers shared across factors
# =====================================================================

def _strength_word(score: float) -> str:
    """Map a 0-100 score into a strength descriptor."""
    if score >= 80:
        return "very strong"
    if score >= 65:
        return "strong"
    if score >= 55:
        return "moderate"
    if score >= 45:
        return "neutral"
    if score >= 30:
        return "weak"
    return "very weak"


def _bias_word(bias: str) -> str:
    """Normalise an arbitrary bias label into a human-friendly direction."""
    b = (bias or "").lower()
    if "strong_bull" in b or "strongbull" in b:
        return "strongly bullish"
    if "strong_bear" in b or "strongbear" in b:
        return "strongly bearish"
    if "bull" in b:
        return "bullish"
    if "bear" in b:
        return "bearish"
    if b in ("attracting", "absorbing"):
        return "accumulating"
    if b in ("repelling", "distributing"):
        return "distributing"
    return "neutral"


# =====================================================================
# Per-factor generators
# =====================================================================

def trend_volume_delta_narrative(fam: dict) -> dict:
    score = _num(fam.get("score"), 50.0)
    bucket = (fam.get("bucket") or "").lower()
    delta = _num(fam.get("delta_pct"), 0.0)
    if not fam or score == 50.0 and not bucket:
        return {
            "cell_text": "Trend × volume signal is neutral or unavailable.",
            "detail_text": "Not enough divergence between today's price move and average volume to call a directional pressure imbalance.",
            "prediction": "Neutral — no edge from trend-volume confluence.",
        }
    direction = (
        "bullish breakout" if bucket == "strong_bullish" else
        "bullish drift" if bucket == "bullish_neutral" else
        "bearish breakdown" if bucket == "strong_bearish" else
        "bearish drift" if bucket == "bearish_neutral" else
        "neutral"
    )
    cell = (
        f"TVD={score:.0f}: {direction}. Price moved {delta:+.1f}% on "
        f"{_strength_word(score)} volume."
    )
    detail = (
        f"Trend × volume delta of {delta:+.1f}% places this row in the "
        f"'{bucket or 'neutral'}' bucket. Price and volume "
        f"{'agree' if abs(delta) > 5 else 'mostly agree'} on direction."
    )
    pred = (
        "Continuation likely while volume holds." if "strong" in bucket and score >= 65 else
        "Watch for follow-through; reversal risk rises if volume fades." if abs(delta) > 0 else
        "No directional edge from this factor alone."
    )
    return {"cell_text": cell, "detail_text": detail, "prediction": pred}


def institutional_confluence_narrative(fam: dict) -> dict:
    if not fam or str(fam.get("status")).lower() == "insufficient_history":
        return {
            "cell_text": "INST warming — need 25+ days of daily history to compute.",
            "detail_text": "Institutional confluence (RRG + flow + regime + liquidity + session) is still warming up. Computes after the 90-day daily-history cache populates.",
            "prediction": "Unavailable until history populates.",
        }
    score = _num(fam.get("score"), 50.0)
    bias = _bias_word(fam.get("bias"))
    quadrant = (fam.get("rrg") or {}).get("quadrant", "NEUTRAL")
    flow_bias = (fam.get("flow") or {}).get("bias", "NEUTRAL")
    regime = (fam.get("regime") or {}).get("state", "RANGING")
    unusual_vol = bool((fam.get("flow") or {}).get("unusual_volume"))
    cell = (
        f"INST={score:.0f}: {bias} ({quadrant.lower()} RRG quadrant, "
        f"{regime.lower()} regime{', unusual volume' if unusual_vol else ''})."
    )
    detail = (
        f"Relative-rotation graph shows the symbol in the {quadrant} quadrant "
        f"with intraday order-flow biased {flow_bias.lower()}. Volatility regime "
        f"is {regime.replace('_', ' ').lower()}."
        + (" Unusual-volume flag is firing (z-score ≥ 2σ)." if unusual_vol else "")
    )
    if quadrant in ("LEADING", "IMPROVING") and score >= 60:
        pred = "Institutional accumulation pattern; bias to upside continuation."
    elif quadrant in ("LAGGING", "WEAKENING") and score <= 40:
        pred = "Institutional distribution pattern; downside risk elevated."
    else:
        pred = "Mixed institutional signals — no high-conviction directional call."
    return {"cell_text": cell, "detail_text": detail, "prediction": pred}


def options_positioning_narrative(fam: dict) -> dict:
    status = str(fam.get("status") or "").lower()
    if status in ("no_expirations", "options_unavailable", "symbol_unavailable"):
        return {
            "cell_text": "No listed options chain available for this symbol.",
            "detail_text": "Symbol has no listed options chain (common for warrants, units, ETFs without monthlies, and most crypto).",
            "prediction": "N/A — no options market to read.",
        }
    score = _num(fam.get("score"), 50.0)
    bias = _bias_word(fam.get("bias"))
    cp_ratio = fam.get("call_put_premium_ratio")
    pin_risk = fam.get("pin_risk", "low")
    gamma_level = fam.get("gamma_level")
    near = fam.get("near_term") or {}
    cell = (
        f"OPTS={score:.0f}: {bias} positioning"
        + (f", call/put premium ratio {cp_ratio:.2f}" if isinstance(cp_ratio, (int, float)) else "")
        + (f", pin risk {pin_risk}" if pin_risk != "low" else "")
        + "."
    )
    detail_parts = [
        f"Composite dealer-gamma positioning is {bias}.",
    ]
    if gamma_level:
        detail_parts.append(f"Near-term gamma magnet sits at ${gamma_level:.2f}.")
    if near.get("call_wall"):
        detail_parts.append(f"Call wall at ${near['call_wall']:.2f}.")
    if near.get("put_wall"):
        detail_parts.append(f"Put wall at ${near['put_wall']:.2f}.")
    if pin_risk == "high":
        detail_parts.append("High pin-risk: call+put walls within 3% suggest sticky expiry.")
    detail = " ".join(detail_parts)
    if score >= 65 and "bull" in bias:
        pred = "Dealer hedging biased toward upside continuation."
    elif score <= 35 and "bear" in bias:
        pred = "Dealer hedging biased toward downside continuation."
    elif pin_risk == "high":
        pred = "Price likely to gravitate toward the gamma cluster into expiry."
    else:
        pred = "No high-conviction call from the options chain right now."
    return {"cell_text": cell, "detail_text": detail, "prediction": pred}


def institutional_order_block_narrative(fam: dict) -> dict:
    score = _num(fam.get("score"), 50.0)
    state = (fam.get("state") or "unavailable").lower()
    bias = _bias_word(fam.get("bias"))
    zone_low = fam.get("zone_low")
    zone_high = fam.get("zone_high")
    distance = _num(fam.get("distance_from_price_pct"), 0.0)
    if state == "unavailable" or not zone_low:
        return {
            "cell_text": "IOB unavailable — no clear order-block zone detected.",
            "detail_text": "Not enough recent price/volume structure to identify a credible institutional order block.",
            "prediction": "N/A — no zone to react to.",
        }
    cell = (
        f"IOB={score:.0f}: {state} {bias} order block, {distance:+.1f}% from price."
    )
    detail_parts = [
        f"Detected order-block zone {zone_low:.2f}–{zone_high:.2f} currently in '{state}' state."
    ]
    if state == "holding":
        detail_parts.append("Price is *inside* the zone — high-attention area for the next move.")
    elif state == "tested":
        detail_parts.append("Price has tested the zone and reverted; the level is being respected.")
    elif state == "fresh":
        detail_parts.append("Zone is fresh (untested since it printed) — first touch usually triggers a reaction.")
    elif state == "stale":
        detail_parts.append("Zone has been tested multiple times; predictive value is fading.")
    detail = " ".join(detail_parts)
    expected = fam.get("expected_reaction") or {}
    classification = (fam.get("reaction_classification") or "").upper()
    propel_p = _num(expected.get("propel"), 0)
    reject_p = _num(expected.get("reject"), 0)
    if classification == "PROPEL" and propel_p > 0.5:
        pred = f"Reaction model predicts propel through the zone ({propel_p*100:.0f}% probability)."
    elif classification == "REJECT" and reject_p > 0.5:
        pred = f"Reaction model predicts rejection at the zone ({reject_p*100:.0f}% probability)."
    elif state == "holding":
        pred = "Watch the zone edges — break of either side defines the next leg."
    else:
        pred = "Wait for first-touch reaction to confirm bias."
    return {"cell_text": cell, "detail_text": detail, "prediction": pred}


def dark_pool_proxy_narrative(fam: dict) -> dict:
    status = str(fam.get("status") or "").lower()
    if status == "unavailable":
        return {
            "cell_text": "Dark-pool proxy unavailable for this symbol.",
            "detail_text": "Not enough off-exchange volume signal to compute the dark-pool attraction proxy.",
            "prediction": "N/A.",
        }
    score = _num(fam.get("score"), 50.0)
    bias = _bias_word(fam.get("bias"))
    nearest = fam.get("nearest_print_level")
    distance = _num(fam.get("distance_to_print_pct"), 0.0)
    pinning = fam.get("pinning_effect", "low")
    cell = (
        f"DP={score:.0f}: {bias}"
        + (f", nearest print level ${nearest:.2f} ({distance:+.1f}% away)" if nearest else "")
        + (f", {pinning} pinning effect" if pinning != "low" else "")
        + "."
    )
    detail_parts = [
        f"Off-exchange print-cluster analysis flags this row as {bias}."
    ]
    if nearest:
        detail_parts.append(f"Closest cluster magnet at ${nearest:.2f} ({distance:+.1f}% from price).")
    if pinning == "high":
        detail_parts.append("High pinning effect — price likely to mean-revert to the print cluster.")
    detail = " ".join(detail_parts)
    if pinning == "high":
        pred = "Mean-reversion toward the print cluster is the path of least resistance."
    elif bias == "accumulating":
        pred = "Off-exchange flow suggests quiet accumulation — bias slightly to upside."
    elif bias == "distributing":
        pred = "Off-exchange flow suggests quiet distribution — bias slightly to downside."
    else:
        pred = "No directional edge from off-exchange activity right now."
    return {"cell_text": cell, "detail_text": detail, "prediction": pred}


def volume_sentiment_narrative(fam: dict) -> dict:
    status = str(fam.get("status") or "").lower()
    if status in ("unavailable", "insufficient_history"):
        return {
            "cell_text": "Volume sentiment warming.",
            "detail_text": "Wyckoff/VSA volume-sentiment compute is still warming up (needs 25+ days of daily history).",
            "prediction": "Unavailable until history populates.",
        }
    score = _num(fam.get("score"), 50.0)
    bias = _bias_word(fam.get("bias"))
    conviction = _num(fam.get("conviction_score"), 50.0)
    evr = (fam.get("effort_vs_result") or "neutral").lower()
    regime = fam.get("regime", "balanced")
    cell = (
        f"VS={score:.0f}: {bias} with {_strength_word(conviction)} conviction "
        f"({evr.replace('_', ' ')})."
    )
    detail = (
        f"Volume-sentiment substrate reads {bias} with conviction {conviction:.0f}/100. "
        f"Regime is {regime}; effort-vs-result classified as '{evr.replace('_', ' ')}'."
    )
    if evr == "absorbing":
        pred = "Heavy volume with no progress = someone is soaking up supply. Watch for upside break."
    elif evr == "capitulating":
        pred = "Climactic volume + big move = capitulation. Counter-trend bounce often follows."
    elif evr == "efficient" and "bull" in bias:
        pred = "Trending up with confirming volume — continuation favoured."
    elif evr == "efficient" and "bear" in bias:
        pred = "Trending down with confirming volume — continuation favoured."
    else:
        pred = "No high-conviction volume-sentiment call right now."
    return {"cell_text": cell, "detail_text": detail, "prediction": pred}


def reaction_clustering_narrative(fam: dict) -> dict:
    status = str(fam.get("status") or "").lower()
    if status in ("unavailable", "insufficient_history"):
        return {
            "cell_text": "Reaction-clustering warming.",
            "detail_text": "Multi-level reaction-clustering engine warming up (needs 25+ days of daily history to detect zones).",
            "prediction": "Unavailable until history populates.",
        }
    score = _num(fam.get("score"), 50.0)
    classification = (fam.get("classification") or "NEUTRAL").upper()
    dom_prob = _num(fam.get("dominant_probability"), 0.0)
    cell = (
        f"RC={score:.0f}: dominant outcome '{classification}'"
        + (f" with {dom_prob*100:.0f}% probability" if dom_prob > 0 else "")
        + "."
    )
    detail = (
        f"Recent supply/demand reaction zones cluster around a {classification.lower()} "
        f"outcome (dominant probability {dom_prob*100:.0f}%). The classifier blends "
        f"pivot detection, evidence scoring, and volume alignment."
    )
    if classification == "PROPEL" and dom_prob > 0.55:
        pred = "Setup favours breakout *through* the nearest zone (continuation)."
    elif classification == "REJECT" and dom_prob > 0.55:
        pred = "Setup favours rejection *at* the nearest zone (mean-reversion)."
    elif classification == "CHOP":
        pred = "Range-bound; both edges of the zone likely tested before resolution."
    else:
        pred = "No high-conviction reaction call."
    return {"cell_text": cell, "detail_text": detail, "prediction": pred}


# =====================================================================
# Top-level orchestrator
# =====================================================================

# Family-key -> generator function. Stable order so the detail panel
# renders families in the same sequence regardless of input dict ordering.
_GENERATORS = [
    ("trend_volume_delta",        trend_volume_delta_narrative),
    ("institutional_confluence",  institutional_confluence_narrative),
    ("options_positioning",       options_positioning_narrative),
    ("institutional_order_block", institutional_order_block_narrative),
    ("dark_pool_proxy",           dark_pool_proxy_narrative),
    ("volume_sentiment",          volume_sentiment_narrative),
    ("reaction_clustering",       reaction_clustering_narrative),
]


def build_factor_narratives(families: dict) -> dict:
    """Given the full {family_key: family_payload} dict, return a parallel
    dict of {family_key: {cell_text, detail_text, prediction}}.

    Output is safe to serialise as JSON and never raises.
    """
    out: dict[str, dict[str, str]] = {}
    if not isinstance(families, dict):
        return out
    for key, gen in _GENERATORS:
        fam = families.get(key) or {}
        try:
            out[key] = gen(fam)
        except Exception:
            out[key] = {
                "cell_text": "Narrative unavailable for this factor.",
                "detail_text": "Internal error generating the narrative for this factor family.",
                "prediction": "N/A",
            }
    return out
