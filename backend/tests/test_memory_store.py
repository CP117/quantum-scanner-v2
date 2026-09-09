"""Focused deterministic coverage for the RAM-first cache contract."""
from __future__ import annotations

import os
import sys
import threading
import time

import pytest

_REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


@pytest.fixture
def cache_settings(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "memory_cache_enabled", True)
    monkeypatch.setattr(settings, "memory_cache_mode", "enabled")
    monkeypatch.setattr(settings, "memory_cache_namespace", "test-v1")
    monkeypatch.setattr(settings, "memory_cache_max_mb", 1)
    monkeypatch.setattr(settings, "memory_cache_max_entries_per_domain", 3)
    monkeypatch.setattr(settings, "cache_ttl_jitter_percent", 0.0)
    monkeypatch.setattr(settings, "memory_cache_singleflight_timeout_seconds", 0.05)
    monkeypatch.setattr(settings, "cache_correctness_sample_rate", 0.0)
    for domain in (
        "quotes", "daily_history", "score_components", "composite_scores",
        "options_chains", "universe_metadata", "narratives", "bayesian_priors",
    ):
        monkeypatch.setattr(settings, f"cache_enable_{domain}", True)
        monkeypatch.setattr(settings, f"cache_mode_{domain}", "enabled")


def test_ttl_namespace_and_symbol_invalidation(cache_settings, monkeypatch):
    from app.config import settings
    from app.services.memory_store import MemoryStore, stable_fingerprint

    clock = FakeClock()
    store = MemoryStore(clock=clock)
    dimensions = {"symbol": " aapl ", "price": 100}
    fingerprint = stable_fingerprint(dimensions)
    store.set("score_components", dimensions, {"score": 10}, ttl_seconds=2, fingerprint=fingerprint)

    assert store.get("score_components", {"symbol": "AAPL", "price": 100}, fingerprint=fingerprint) == {"score": 10}
    assert store.invalidate_symbol("aapl") == 1
    assert store.get("score_components", dimensions, fingerprint=fingerprint) is None

    store.set("score_components", dimensions, {"score": 10}, ttl_seconds=2, fingerprint=fingerprint)
    clock.now = 3
    assert store.get("score_components", dimensions, fingerprint=fingerprint) is None
    settings.memory_cache_namespace = "test-v2"
    assert store.get("score_components", dimensions, fingerprint=fingerprint) is None


def test_provider_fingerprint_and_mutation_are_isolated(cache_settings):
    from app.services.memory_store import MemoryStore, stable_fingerprint

    store = MemoryStore(clock=FakeClock())
    dimensions = {"symbol": "MSFT", "interval": "1d", "window": "90d"}
    fingerprint = stable_fingerprint(dimensions)
    value = {"nested": {"score": 5}}
    store.set("daily_history", dimensions, value, ttl_seconds=60, fingerprint=fingerprint, provider_id="yahoo")
    value["nested"]["score"] = 99

    cached = store.get("daily_history", dimensions, fingerprint=fingerprint, provider_id="yahoo")
    assert cached == {"nested": {"score": 5}}
    cached["nested"]["score"] = 42
    assert store.get("daily_history", dimensions, fingerprint=fingerprint, provider_id="yahoo") == {"nested": {"score": 5}}
    assert store.get("daily_history", dimensions, fingerprint=fingerprint, provider_id="other") is None
    assert store.get("daily_history", dimensions, fingerprint=stable_fingerprint({"changed": True}), provider_id="yahoo") is None


def test_lru_capacity_and_disabled_fallback(cache_settings, monkeypatch):
    from app.config import settings
    from app.services.memory_store import MemoryStore

    store = MemoryStore(clock=FakeClock())
    for symbol in ("A", "B", "C", "D"):
        store.set("quotes", {"symbol": symbol}, {"symbol": symbol}, ttl_seconds=60)
    assert store.get_stats()["domains"]["quotes"]["entries"] == 3

    settings.memory_cache_mode = "disabled"
    store.set("quotes", {"symbol": "E"}, {"symbol": "E"}, ttl_seconds=60)
    assert store.get("quotes", {"symbol": "A"}) is None


def test_shadow_write_populates_without_serving(cache_settings, monkeypatch):
    from app.config import settings
    from app.services.memory_store import MemoryStore

    settings.memory_cache_mode = "shadow_write"
    store = MemoryStore(clock=FakeClock())
    calls = []

    value = store.get_or_compute(
        "quotes", {"symbol": "AAPL"}, lambda: calls.append(1) or {"symbol": "AAPL"},
        ttl_seconds=60,
    )
    assert value == {"symbol": "AAPL"}
    assert calls == [1]
    assert store.get("quotes", {"symbol": "AAPL"}) is None

    settings.memory_cache_mode = "enabled"
    assert store.get("quotes", {"symbol": "AAPL"}) == {"symbol": "AAPL"}


def test_domain_flag_disables_one_cache_without_affecting_others(cache_settings, monkeypatch):
    from app.config import settings
    from app.services.memory_store import MemoryStore

    store = MemoryStore(clock=FakeClock())
    monkeypatch.setattr(settings, "cache_enable_quotes", False)
    store.set_quote("AAPL", {"price": 1}, provider_id="fixture", ttl_seconds=60)
    assert store.get_quote("AAPL", provider_id="fixture") is None

    store.set("narratives", {"symbol": "AAPL"}, {"text": "safe"}, ttl_seconds=60)
    assert store.get("narratives", {"symbol": "AAPL"}) == {"text": "safe"}


def test_global_rollback_and_correctness_sampling(cache_settings):
    from app.config import settings
    from app.services.memory_store import MemoryStore

    store = MemoryStore(clock=FakeClock(), random_uniform=lambda _low, _high: 0.0)
    store.set("quotes", {"symbol": "AAPL"}, {"price": 1}, ttl_seconds=60)
    settings.memory_cache_mode = "disabled"
    assert store.get("quotes", {"symbol": "AAPL"}) is None

    settings.memory_cache_mode = "enabled"
    settings.cache_correctness_sample_rate = 1.0
    assert store.should_sample_correctness("quotes") is True
    store.record_correctness_comparison("quotes", matches=False)
    stats = store.get_stats()["domains"]["quotes"]
    assert stats["correctness_samples"] == 1
    assert stats["correctness_mismatches"] == 1


def test_singleflight_computes_once_and_releases_waiters(cache_settings):
    from app.services.memory_store import MemoryStore

    store = MemoryStore()
    started = threading.Event()
    release = threading.Event()
    calls = []
    results = []

    def loader():
        calls.append(1)
        started.set()
        assert release.wait(1)
        return {"symbol": "AAPL", "score": 1}

    def worker():
        results.append(store.get_or_compute("scores", {"symbol": "AAPL"}, loader, ttl_seconds=60))

    owner = threading.Thread(target=worker)
    owner.start()
    assert started.wait(1)
    waiters = [threading.Thread(target=worker) for _ in range(3)]
    for waiter in waiters:
        waiter.start()
    time.sleep(0.01)
    release.set()
    owner.join(1)
    for waiter in waiters:
        waiter.join(1)

    assert calls == [1]
    assert results == [{"symbol": "AAPL", "score": 1}] * 4
    stats = store.get_stats()["domains"]["scores"]
    assert stats["singleflight_owners"] == 1
    assert stats["singleflight_waits"] == 3


def test_singleflight_timeout_falls_back_without_leaking_lock(cache_settings):
    from app.services.memory_store import MemoryStore

    store = MemoryStore()
    started = threading.Event()
    release = threading.Event()

    def slow_loader():
        started.set()
        assert release.wait(1)
        return {"source": "owner"}

    owner = threading.Thread(
        target=lambda: store.get_or_compute("quotes", {"symbol": "MSFT"}, slow_loader, ttl_seconds=60)
    )
    owner.start()
    assert started.wait(1)
    fallback = store.get_or_compute(
        "quotes", {"symbol": "MSFT"}, lambda: {"source": "fallback"},
        ttl_seconds=60, wait_timeout_seconds=0.01,
    )
    release.set()
    owner.join(1)

    assert fallback == {"source": "fallback"}
    assert store.get_stats()["domains"]["quotes"]["singleflight_timeouts"] == 1
    assert store.get_or_compute(
        "quotes", {"symbol": "MSFT"}, lambda: {"source": "unexpected"}, ttl_seconds=60
    ) == {"source": "owner"}
