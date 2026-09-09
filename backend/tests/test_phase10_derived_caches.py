"""Phase 10 cache coverage for Bayesian priors and factor narratives."""
from __future__ import annotations

import os
import sys

import pytest

_REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


@pytest.fixture
def derived_cache(monkeypatch):
    from app.config import settings
    from app.services import memory_store as memory_store_module

    monkeypatch.setattr(settings, "memory_cache_enabled", True)
    monkeypatch.setattr(settings, "memory_cache_mode", "enabled")
    monkeypatch.setattr(settings, "memory_cache_namespace", "phase10-test")
    monkeypatch.setattr(settings, "cache_ttl_jitter_percent", 0.0)
    monkeypatch.setattr(settings, "cache_enable_bayesian_priors", True)
    monkeypatch.setattr(settings, "cache_mode_bayesian_priors", "enabled")
    monkeypatch.setattr(settings, "cache_enable_narratives", True)
    monkeypatch.setattr(settings, "cache_mode_narratives", "enabled")
    store = memory_store_module.MemoryStore()
    monkeypatch.setattr(memory_store_module, "memory_store", store)
    return store


def _complete_families() -> dict:
    return {
        "trend_volume_delta": {"status": "implemented", "score": 61, "bucket": "bullish_neutral"},
        "institutional_confluence": {"status": "implemented", "score": 62},
        "options_positioning": {"status": "implemented", "score": 63},
        "institutional_order_block": {"status": "implemented", "score": 64},
        "dark_pool_proxy": {"status": "implemented", "score": 65},
        "volume_sentiment": {"status": "implemented", "directional_score": 66},
        "reaction_clustering": {"status": "implemented", "score": 67},
    }


def test_bayesian_prior_cache_keys_history_model_scope_and_keeps_provenance(derived_cache, monkeypatch):
    from app.services import bayesian_factor_blend
    from app.services.bayesian_factor_blend import blend_factors_for_drift

    history = [100.0 + index for index in range(20)]
    kwargs = {
        "factor_scores": {"momentum": 60.0, "quality": 55.0},
        "final_direction_sign": 1,
        "horizon": 5,
        "is_intraday": False,
        "symbol": "aapl",
        "segment": "stocks",
        "source_history": history,
        "history_validated": True,
    }
    first = blend_factors_for_drift(**kwargs)
    assert derived_cache.get_stats()["domains"]["bayesian_priors"]["entries"] == 1
    assert blend_factors_for_drift(**kwargs) == first
    assert derived_cache.get_stats()["domains"]["bayesian_priors"]["hits"] == 1

    changed_history = [*history[:-1], 999.0]
    blend_factors_for_drift(**{**kwargs, "source_history": changed_history})
    blend_factors_for_drift(**{**kwargs, "segment": "crypto"})
    # A changed history replaces the symbol/segment slot (rather than serving
    # the old fingerprint); a second segment gets an isolated slot.
    assert derived_cache.get_stats()["domains"]["bayesian_priors"]["entries"] == 2
    assert derived_cache.get_stats()["domains"]["bayesian_priors"]["misses"] == 3
    monkeypatch.setattr(bayesian_factor_blend, "_BAYESIAN_PRIOR_MODEL_VERSION", "bayesian-factor-prior-v2")
    blend_factors_for_drift(**kwargs)
    assert derived_cache.get_stats()["domains"]["bayesian_priors"]["entries"] == 3
    provenance = next(iter(derived_cache._domains["bayesian_priors"].values())).provenance
    assert provenance["model_version"] in {"bayesian-factor-prior-v1", "bayesian-factor-prior-v2"}
    assert provenance["history_validation"] == "complete"


def test_bayesian_prior_cache_rejects_unvalidated_or_incomplete_history(derived_cache):
    from app.services.bayesian_factor_blend import blend_factors_for_drift

    kwargs = {
        "factor_scores": {"momentum": 60.0},
        "final_direction_sign": 1,
        "horizon": 1,
        "is_intraday": False,
        "symbol": "AAPL",
        "segment": "stocks",
    }
    blend_factors_for_drift(**kwargs, source_history=[100.0] * 20, history_validated=False)
    blend_factors_for_drift(**kwargs, source_history=[100.0] * 19, history_validated=True)
    assert "bayesian_priors" not in derived_cache.get_stats()["domains"]


def test_narrative_cache_requires_complete_inputs_and_preserves_payload(derived_cache, monkeypatch):
    from app.services import factor_narratives
    from app.services.factor_narratives import build_factor_narratives

    families = _complete_families()
    first = build_factor_narratives(families)
    assert derived_cache.get_stats()["domains"]["narratives"]["entries"] == 1
    assert build_factor_narratives(families) == first
    assert derived_cache.get_stats()["domains"]["narratives"]["hits"] == 1

    changed = _complete_families()
    changed["trend_volume_delta"]["score"] = 70
    changed_payload = build_factor_narratives(changed)
    assert "TVD=70" in changed_payload["trend_volume_delta"]["cell_text"]
    assert derived_cache.get_stats()["domains"]["narratives"]["entries"] == 1
    monkeypatch.setattr(factor_narratives, "_NARRATIVE_TEMPLATE_VERSION", "factor-narratives-v2")
    assert build_factor_narratives(changed) == changed_payload
    assert derived_cache.get_stats()["domains"]["narratives"]["entries"] == 2

    incomplete = _complete_families()
    incomplete["options_positioning"]["status"] = "options_unavailable"
    payload = build_factor_narratives(incomplete)
    assert payload["options_positioning"]["prediction"] == "N/A — no options market to read."
    assert derived_cache.get_stats()["domains"]["narratives"]["entries"] == 2
