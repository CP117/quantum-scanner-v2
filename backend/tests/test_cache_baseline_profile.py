"""Tests for the deterministic baseline profiling harness."""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_SPEC = importlib.util.spec_from_file_location(
    "profile_cache_baseline", _REPO_ROOT / "scripts" / "profile_cache_baseline.py"
)
profile_cache_baseline = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
sys.modules[_SPEC.name] = profile_cache_baseline
_SPEC.loader.exec_module(profile_cache_baseline)


def test_measure_records_fixture_provider_metrics():
    provider = profile_cache_baseline.FixtureProvider()
    measurement = profile_cache_baseline.measure(
        "fixture", lambda: provider.quote("AAPL"), iterations=3, provider=provider
    )

    assert measurement.provider_calls == 3
    assert measurement.provider_failures == 0
    assert measurement.wall_seconds >= 0
    assert measurement.top_cpu_functions


def test_concurrent_same_symbol_exposes_duplicate_fetch_baseline():
    provider = profile_cache_baseline.FixtureProvider()
    result = profile_cache_baseline.concurrent_same_symbol(provider, workers=4)

    assert result == {"workers": 4, "provider_calls": 4}
