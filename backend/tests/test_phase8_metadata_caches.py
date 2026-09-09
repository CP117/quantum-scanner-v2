"""Focused coverage for Phase 8 metadata and options cache migrations."""
from __future__ import annotations

import os
import sys
import threading
import time

import pytest

_REPO_ROOT = os.path.join(os.path.dirname(__file__), '..', '..')
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


@pytest.fixture
def phase8_cache_settings(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, 'memory_cache_enabled', True)
    monkeypatch.setattr(settings, 'memory_cache_mode', 'enabled')
    monkeypatch.setattr(settings, 'cache_enable_universe_metadata', True)
    monkeypatch.setattr(settings, 'cache_mode_universe_metadata', 'enabled')
    monkeypatch.setattr(settings, 'cache_enable_options_chains', True)
    monkeypatch.setattr(settings, 'cache_mode_options_chains', 'enabled')
    monkeypatch.setattr(settings, 'cache_ttl_jitter_percent', 0.0)
    monkeypatch.setattr(settings, 'universe_metadata_ttl_seconds', 60)
    monkeypatch.setattr(settings, 'options_chain_cache_ttl_seconds', 60)
    monkeypatch.setattr(settings, 'scanner_preset_cache_max_entries', 8)


def test_universe_metadata_uses_singleflight_for_same_definition(
    phase8_cache_settings, monkeypatch,
):
    from app.services import universe_service
    from app.services.memory_store import MemoryStore

    store = MemoryStore()
    monkeypatch.setattr(universe_service, 'memory_store', store)
    started = threading.Event()
    release = threading.Event()
    calls = []

    def build():
        calls.append(1)
        started.set()
        assert release.wait(1)
        return [{'symbol': 'AAPL'}]

    monkeypatch.setattr(universe_service, 'build_active_stock_universe', build)
    results = []
    threads = [threading.Thread(target=lambda: results.append(universe_service.load_universe('stocks'))) for _ in range(2)]
    threads[0].start()
    assert started.wait(1)
    threads[1].start()
    release.set()
    for thread in threads:
        thread.join(1)

    assert calls == [1]
    assert results == [[{'symbol': 'AAPL'}], [{'symbol': 'AAPL'}]]
    assert universe_service._universe_metadata_dimensions('stocks') != universe_service._universe_metadata_dimensions('crypto')


def test_preset_definitions_and_filter_pipelines_are_content_fingerprinted(
    phase8_cache_settings, monkeypatch,
):
    from app.services import scanner_presets

    scanner_presets.clear_compiled_presets()
    original = scanner_presets.resolve_filters('leaders')
    first = scanner_presets._compile_filter_pipeline(original)
    second = scanner_presets._compile_filter_pipeline(dict(original))
    assert first is second
    assert scanner_presets.apply_filters(
        [{'final_direction': 'Bullish', 'tier': 'A', 'final_score': 60}],
        original,
    )

    monkeypatch.setitem(
        scanner_presets.SCANNER_PRESETS, 'leaders',
        {**scanner_presets.SCANNER_PRESETS['leaders'], 'min_score': 70},
    )
    updated = scanner_presets.resolve_filters('leaders')
    assert updated['min_score'] == 70

    monkeypatch.setitem(scanner_presets.SCANNER_PRESETS, 'unsafe', {'label': lambda: 'no'})
    with pytest.raises(ValueError, match='invalid scanner preset'):
        scanner_presets.resolve_filters('unsafe')


def test_options_keys_include_provider_expiration_and_filter_dimensions():
    from app.services.options_chain_service import _memory_cache_dimensions

    cboe = _memory_cache_dimensions('CBOE', ' aapl ', 3)
    yahoo = _memory_cache_dimensions('yahoo', 'AAPL', 3)
    shorter = _memory_cache_dimensions('cboe', 'AAPL', 1)
    assert cboe['provider_id'] == 'cboe'
    assert cboe['symbol'] == 'AAPL'
    assert cboe['filters'] == {}
    assert cboe != yahoo
    assert cboe != shorter


def test_tier1_options_prefetch_only_refreshes_entries_older_than_target(
    phase8_cache_settings, monkeypatch,
):
    from app.services import options_chain_service as options

    options.clear_cache()
    now = options._now()
    with options._lock:
        options._cache[options._cache_key('AAPL', 4)] = (
            now - 5, {'score': 60, 'provenance': 'real_chain'},
        )

    calls = []

    def fetch(symbol, price, **kwargs):
        calls.append((symbol, kwargs.get('max_cache_age_seconds')))
        return {'score': 60, 'provenance': 'real_chain'}

    monkeypatch.setattr(options, 'get_real_options_positioning', fetch)
    assert options.prefetch_options_chains(
        [('AAPL', 100)], tier1_target_age_seconds=10, timeout_seconds=1,
    ) == {'AAPL': 'cached'}
    assert not calls

    outcomes = options.prefetch_options_chains(
        [('AAPL', 100)], tier1_target_age_seconds=1, timeout_seconds=1,
    )
    assert outcomes == {'AAPL': 'hit'}
    assert calls == [('AAPL', 1)]
