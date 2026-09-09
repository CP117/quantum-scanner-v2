"""Tests for process-local cache topology reporting."""
from __future__ import annotations

import os
import sys

_REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def test_defaults_to_one_uvicorn_worker(monkeypatch):
    from app.services.deployment_topology import cache_topology

    for name in ("WEB_CONCURRENCY", "GUNICORN_WORKERS", "UVICORN_WORKERS", "GUNICORN_CMD_ARGS"):
        monkeypatch.delenv(name, raising=False)

    topology = cache_topology()
    assert topology["process_scope"] == "local"
    assert topology["worker_count"] == 1
    assert topology["worker_count_source"] == "default"
    assert topology["multi_worker"] is False
    assert topology["shared_cache_backend"] is False


def test_uses_valid_worker_environment_and_reports_total_budget(monkeypatch):
    from app.config import settings
    from app.services.deployment_topology import cache_memory_budget, cache_topology

    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    monkeypatch.setenv("GUNICORN_CMD_ARGS", "--workers 4")
    monkeypatch.setattr(settings, "memory_cache_max_mb", 128)

    topology = cache_topology()
    budget = cache_memory_budget()
    assert topology["process_manager"] == "gunicorn"
    assert topology["worker_count"] == 4
    assert topology["multi_worker"] is True
    assert budget == {"per_worker_bytes": 128 * 1024 * 1024, "estimated_all_workers_bytes": 512 * 1024 * 1024}


def test_ignores_invalid_worker_counts(monkeypatch):
    from app.services.deployment_topology import cache_topology

    monkeypatch.setenv("WEB_CONCURRENCY", "zero")
    monkeypatch.setenv("GUNICORN_WORKERS", "0")
    monkeypatch.setenv("UVICORN_WORKERS", "-2")
    assert cache_topology()["worker_count"] == 1
