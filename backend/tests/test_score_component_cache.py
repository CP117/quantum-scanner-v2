"""Deterministic core-score component cache tests."""
from __future__ import annotations

import os
import sys

import pandas as pd
import pytest

_REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


@pytest.fixture
def score_component_cache(monkeypatch):
    from app.config import settings
    from app.services.memory_store import memory_store

    monkeypatch.setattr(settings, "memory_cache_enabled", True)
    monkeypatch.setattr(settings, "memory_cache_mode", "enabled")
    monkeypatch.setattr(settings, "cache_enable_score_components", True)
    monkeypatch.setattr(settings, "cache_mode_score_components", "enabled")
    monkeypatch.setattr(settings, "cache_enable_composite_scores", True)
    monkeypatch.setattr(settings, "cache_mode_composite_scores", "enabled")
    monkeypatch.setattr(settings, "cache_ttl_jitter_percent", 0.0)
    memory_store.invalidate_domain("score_components")
    memory_store.invalidate_domain("composite_scores")
    yield memory_store
    memory_store.invalidate_domain("score_components")
    memory_store.invalidate_domain("composite_scores")


def test_core_component_cache_reuses_only_matching_material_inputs(score_component_cache, monkeypatch):
    from app.services import scoring_service as scoring

    calls = []

    def build_quality(info):
        return {"score": 70.0, "intraday_inputs": {}, "components": {}, "weights": {}}

    def build_core(px, prev_close, source, age_seconds, provider_note, quality):
        calls.append((px, prev_close))
        return {
            "final_score": 70.0,
            "confidence_audit": {"live_count": 4},
            "score_explanation": None,
            "market": {},
            "ratings": {
                "momentum": {"score": 70.0},
                "quality": {"score": 70.0},
                "trend": {"score": 70.0},
                "stability": {"score": 70.0},
                "exit_risk": {"score": 30.0},
            },
        }

    monkeypatch.setattr(scoring, "build_quality_breakdown", build_quality)
    monkeypatch.setattr(scoring, "build_algorithm_breakdown", build_core)

    first = scoring._core_algorithm_breakdown(
        symbol="aapl", market_kind="stocks", px=100.0, prev_close=99.0,
        source="fixture", age_seconds=1, provider_note="first", fundamentals_info={"volume": 10},
        daily_hist=None,
    )
    second = scoring._core_algorithm_breakdown(
        symbol="AAPL", market_kind="stocks", px=100.0, prev_close=99.0,
        source="fixture", age_seconds=9, provider_note="second", fundamentals_info={"volume": 10},
        daily_hist=None,
    )
    changed = scoring._core_algorithm_breakdown(
        symbol="AAPL", market_kind="stocks", px=101.0, prev_close=99.0,
        source="fixture", age_seconds=9, provider_note="second", fundamentals_info={"volume": 10},
        daily_hist=None,
    )

    assert calls == [(100.0, 99.0), (101.0, 99.0)]
    assert first["market"]["age_seconds"] == 1
    assert second["market"]["age_seconds"] == 9
    assert second["market"]["provider_note"] == "second"
    assert changed["market"]["last_price"] == 101.0


def test_incomplete_core_component_is_not_cached(score_component_cache, monkeypatch):
    from app.services import scoring_service as scoring

    calls = []
    monkeypatch.setattr(scoring, "build_quality_breakdown", lambda info: {})

    def incomplete(*args, **kwargs):
        calls.append(1)
        return {
            "final_score": 0.0,
            "confidence_audit": {"live_count": 0},
            "score_explanation": "missing quote",
            "market": {},
        }

    monkeypatch.setattr(scoring, "build_algorithm_breakdown", incomplete)
    kwargs = {
        "symbol": "AAPL", "market_kind": "stocks", "px": 0.0, "prev_close": 0.0,
        "source": "fixture", "age_seconds": 1, "provider_note": None,
        "fundamentals_info": {}, "daily_hist": None,
    }
    scoring._core_algorithm_breakdown(**kwargs)
    scoring._core_algorithm_breakdown(**kwargs)

    assert calls == [1, 1]


def test_core_cache_fingerprint_includes_history_values(score_component_cache, monkeypatch):
    from app.services import scoring_service as scoring

    calls = []
    monkeypatch.setattr(
        scoring, "build_quality_breakdown",
        lambda info: {"score": 70.0, "intraday_inputs": {}, "components": {}, "weights": {}},
    )

    def complete(*args, **kwargs):
        calls.append(1)
        return {
            "final_score": 70.0, "confidence_audit": {"live_count": 4},
            "score_explanation": None, "market": {},
            "ratings": {name: {"score": 70.0 if name != "exit_risk" else 30.0}
                        for name in ("momentum", "quality", "trend", "stability", "exit_risk")},
        }

    monkeypatch.setattr(scoring, "build_algorithm_breakdown", complete)
    history = pd.DataFrame({"Close": [10.0, 11.0], "Volume": [100, 200]})
    kwargs = {
        "symbol": "AAPL", "market_kind": "stocks", "px": 100.0, "prev_close": 99.0,
        "source": "fixture", "age_seconds": 1, "provider_note": None,
        "fundamentals_info": {"volume": 10}, "daily_hist": history,
    }
    scoring._core_algorithm_breakdown(**kwargs)
    scoring._core_algorithm_breakdown(**kwargs)
    kwargs["daily_hist"] = history.assign(Close=[10.0, 12.0])
    scoring._core_algorithm_breakdown(**kwargs)

    assert calls == [1, 1]


def test_extended_factor_cache_reuses_only_complete_factor_sets(score_component_cache, monkeypatch):
    from app.services import extended_factors as factors
    from app.services import scoring_service as scoring
    from app.services import reaction_clustering_service, volume_sentiment

    calls = []
    monkeypatch.setattr(volume_sentiment, "compute_volume_sentiment", lambda hist: {
        "status": "implemented", "directional_score": 60.0, "conviction_score": 20.0, "bias": "bullish",
    })
    monkeypatch.setattr(reaction_clustering_service, "compute_reaction_map", lambda *args: {
        "status": "implemented", "propel_probability": 0.5, "reject_probability": 0.2,
        "chop_probability": 0.3, "classification": "PROPEL",
    })
    monkeypatch.setattr(scoring, "institutional_confluence_factor", lambda *args: calls.append("icf") or {
        "status": "implemented_from_icm", "score": 60.0, "bias": "bullish",
    })
    monkeypatch.setattr(scoring, "institutional_order_block_factor", lambda *args: calls.append("iob") or {
        "status": "implemented", "score": 60.0, "bias": "bullish", "midpoint": 100.0,
    })
    monkeypatch.setattr(scoring, "dark_pool_proxy_factor", lambda *args: calls.append("dp") or {
        "status": "implemented", "score": 60.0, "bias": "bullish",
    })
    history = pd.DataFrame({
        "Open": [100.0] * 25, "High": [102.0] * 25, "Low": [99.0] * 25,
        "Close": [101.0] * 25, "Volume": [1000] * 25,
    })
    kwargs = {
        "symbol": "AAPL", "last_price": 101.0, "prev_close": 100.0,
        "info": {"open": 100.0, "dayLow": 99.0, "dayHigh": 102.0, "volume": 1000, "averageVolume": 900},
        "daily_hist": history,
    }
    factors.compute_extended_factors(**kwargs)
    factors.compute_extended_factors(**kwargs)

    assert calls == ["icf", "iob", "dp"]


def test_full_composite_cache_requires_current_complete_components(score_component_cache, monkeypatch):
    from app.services import extended_factors, scoring_service as scoring

    monkeypatch.setattr(
        scoring, "build_quality_breakdown",
        lambda info: {"score": 70.0, "intraday_inputs": {}, "components": {}, "weights": {}},
    )
    monkeypatch.setattr(scoring, "build_algorithm_breakdown", lambda *args: {
        "final_score": 70.0, "confidence_audit": {"live_count": 4},
        "score_explanation": None, "market": {}, "direction": "Bullish",
        "ratings": {name: {"score": 70.0 if name != "exit_risk" else 30.0}
                    for name in ("momentum", "quality", "trend", "stability", "exit_risk")},
    })
    monkeypatch.setattr(scoring, "_compute_context_families", lambda *args: ({}, {}, {}))

    factor_score = [60.0]
    incomplete = [False]

    def complete_factors(**kwargs):
        score = factor_score[0]
        confluence_status = "unavailable" if incomplete[0] else "implemented"
        return {
            "trend_volume_delta": {"status": "implemented", "score": score, "bias": "bullish"},
            "institutional_confluence": {"status": confluence_status, "score": score, "bias": "bullish"},
            "options_positioning": {"status": "inferred", "score": score, "bias": "bullish"},
            "institutional_order_block": {"status": "implemented", "score": score, "bias": "bullish"},
            "dark_pool_proxy": {"status": "implemented", "score": score, "bias": "bullish"},
            "volume_sentiment": {"status": "implemented", "directional_score": score, "bias": "bullish"},
            "reaction_map": {
                "status": "implemented", "classification": "PROPEL",
                "propel_probability": 0.5, "reject_probability": 0.2, "chop_probability": 0.3,
            },
        }

    monkeypatch.setattr(extended_factors, "compute_extended_factors", complete_factors)
    kwargs = {
        "row": {"symbol": "AAPL"}, "px": 100.0, "prev_close": 99.0, "source": "fixture",
        "age_seconds": 1, "as_of_utc": "2026-01-01T00:00:00Z", "fundamentals_info": {},
        "reg_index_snapshot": {},
    }
    first = scoring.score_from_prices(**kwargs)
    second = scoring.score_from_prices(**kwargs)
    factor_score[0] = 80.0
    changed = scoring.score_from_prices(**kwargs)
    entries_after_complete = score_component_cache.get_stats()["domains"]["composite_scores"]["entries"]
    incomplete[0] = True
    scoring.score_from_prices(**kwargs)

    stats = score_component_cache.get_stats()["domains"]["composite_scores"]
    assert stats["hits"] == 1
    assert stats["entries"] == entries_after_complete
    assert first["final_score"] == second["final_score"]
    assert changed["final_score"] != first["final_score"]
