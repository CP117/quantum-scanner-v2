"""Deterministic Phase 9 quote-cache write-behind coverage."""
from __future__ import annotations

import json
import os
import sys
import threading

import pytest

_REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


@pytest.fixture
def quote_cache(monkeypatch, tmp_path):
    from app.config import settings
    from app.services import quote_cache as cache
    from app.services.memory_store import memory_store

    monkeypatch.setattr(settings, "memory_cache_enabled", True)
    monkeypatch.setattr(settings, "memory_cache_mode", "enabled")
    monkeypatch.setattr(settings, "cache_enable_quotes", True)
    monkeypatch.setattr(settings, "cache_mode_quotes", "enabled")
    monkeypatch.setattr(settings, "cache_ttl_jitter_percent", 0.0)
    monkeypatch.setattr(settings, "quote_cache_ttl_seconds", 60)
    monkeypatch.setattr(cache, "_SHARD_DIR", tmp_path / "quote_cache")
    monkeypatch.setattr(cache, "_LEGACY_FILE", tmp_path / "quote_cache.json")
    monkeypatch.setattr(cache, "_MIGRATION_DONE", True)
    monkeypatch.setattr(cache, "_DEBOUNCE_S", 1.0)
    memory_store.invalidate_domain("quotes")
    for value in (
        cache._SHARD_MEM, cache._SHARD_LOADED, cache._SHARD_VERSION,
        cache._DIRTY_SINCE, cache._DIRTY_FAILURES, cache._DIRTY_RETRY_AT,
    ):
        value.clear()
    cache._DIRTY_SHARDS.clear()
    cache._PERSISTING_SHARDS.clear()
    yield cache
    memory_store.invalidate_domain("quotes")


def test_ram_hit_skips_shard_recovery_and_disk(quote_cache, monkeypatch):
    quote_cache.save_quote("aapl", {"last_price": 100, "previous_close": 99})

    def should_not_run(_shard):
        raise AssertionError("RAM hit attempted shard recovery")

    monkeypatch.setattr(quote_cache, "_ensure_shard_loaded", should_not_run)
    assert quote_cache.get_cached_quote("AAPL")["last_price"] == 100
    assert not hasattr(quote_cache, "_FLUSHER_THREAD")


def test_coalesces_latest_write_and_leaves_no_dirty_shard(quote_cache, monkeypatch):
    writes = []
    monkeypatch.setattr(
        quote_cache,
        "_write_shard_bytes",
        lambda _shard, payload: writes.append(json.loads(payload)) or True,
    )
    quote_cache.save_quote("AAPL", {"last_price": 100, "previous_close": 99})
    quote_cache.save_quote("AAPL", {"last_price": 101, "previous_close": 99})

    due = quote_cache._DIRTY_SINCE["A"] + quote_cache._DEBOUNCE_S
    assert quote_cache.flush_due(now=due) == 1
    assert len(writes) == 1
    assert writes[0]["AAPL"]["last_price"] == 101
    assert "A" not in quote_cache._DIRTY_SHARDS


def test_failed_write_retries_without_losing_dirty_snapshot(quote_cache, monkeypatch):
    quote_cache.save_quote("AAPL", {"last_price": 100, "previous_close": 99})
    monkeypatch.setattr(quote_cache, "_write_shard_bytes", lambda _shard, _payload: False)

    assert quote_cache.flush_due(force=True) == 0
    assert "A" in quote_cache._DIRTY_SHARDS
    assert quote_cache._DIRTY_FAILURES["A"] == 1
    retry_at = quote_cache._DIRTY_RETRY_AT["A"]

    persisted = []
    monkeypatch.setattr(
        quote_cache,
        "_write_shard_bytes",
        lambda _shard, payload: persisted.append(json.loads(payload)) or True,
    )
    assert quote_cache.flush_due(now=retry_at + 1.0) == 1
    assert persisted[0]["AAPL"]["last_price"] == 100
    assert "A" not in quote_cache._DIRTY_SHARDS


def test_newer_save_survives_older_inflight_snapshot(quote_cache, monkeypatch):
    persisted = []

    def write_old_then_save_new(_shard, payload):
        persisted.append(json.loads(payload))
        if len(persisted) == 1:
            quote_cache.save_quote("AAPL", {"last_price": 102, "previous_close": 99})
        return True

    monkeypatch.setattr(quote_cache, "_write_shard_bytes", write_old_then_save_new)
    quote_cache.save_quote("AAPL", {"last_price": 100, "previous_close": 99})

    assert quote_cache.flush_due(force=True) == 1
    assert persisted[0]["AAPL"]["last_price"] == 100
    assert "A" in quote_cache._DIRTY_SHARDS
    assert quote_cache.flush_due(force=True) == 1
    assert persisted[1]["AAPL"]["last_price"] == 102
    assert "A" not in quote_cache._DIRTY_SHARDS


def test_disk_recovery_reads_before_acquiring_shared_shard_lock(quote_cache, monkeypatch):
    quote_cache._SHARD_DIR.mkdir()
    (quote_cache._SHARD_DIR / "A.json").write_text(
        json.dumps({"AAPL": {"symbol": "AAPL", "last_price": 88}}),
        encoding="utf-8",
    )
    original = quote_cache._read_shard_from_disk

    def assert_unlocked(shard):
        lock = quote_cache._SHARD_LOCKS[shard]
        assert lock.acquire(blocking=False), "disk read occurred under shard lock"
        lock.release()
        return original(shard)

    monkeypatch.setattr(quote_cache, "_read_shard_from_disk", assert_unlocked)
    assert quote_cache.get_cached_quote("AAPL") == {
        "symbol": "AAPL", "last_price": 88,
    }


def test_shutdown_flush_is_bounded_by_shard_limit(quote_cache, monkeypatch):
    writes = []
    monkeypatch.setattr(quote_cache, "_SHUTDOWN_FLUSH_MAX_SHARDS", 1)
    monkeypatch.setattr(quote_cache, "_write_shard_bytes", lambda shard, _payload: writes.append(shard) or True)
    quote_cache.save_quote("AAPL", {"last_price": 100})
    quote_cache.save_quote("MSFT", {"last_price": 200})

    assert quote_cache.flush_now() == 1
    assert writes == ["A"]
    assert "M" in quote_cache._DIRTY_SHARDS


def test_managed_maintenance_loop_owns_quote_flush_ticks(monkeypatch):
    from app.services import maintenance_service
    from app.services import quote_cache

    flushed = threading.Event()
    monkeypatch.setattr(maintenance_service, "QUOTE_CACHE_FLUSH_TICK_SECONDS", 0.01)
    monkeypatch.setattr(quote_cache, "flush_due", lambda _now: flushed.set() or 0)
    maintenance_service.start_maintenance_thread()
    try:
        assert flushed.wait(0.5)
    finally:
        maintenance_service.stop_maintenance_thread()
    assert not maintenance_service._thread.is_alive()
