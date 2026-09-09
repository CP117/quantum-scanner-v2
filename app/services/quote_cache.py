"""
Quote-cache persistence layer.

Phase 15 (speed wins): the cache is now **sharded** across 27 small JSON files
(one per A-Z first letter, plus an underscore for numeric/other prefixes)
instead of one 1.2 MB monolith.

Why: every `save_quote()` call previously rewrote the entire JSON file,
which (a) took 0.3-0.7 s of disk I/O per save, (b) serialised all writes
through a single global lock, and (c) caused the snapshot loop to stall
behind cache flushes during heavy scoring.

With sharding:
  - Each shard is ~50 KB instead of 1.2 MB.
  - Disk-write latency drops to ~10-25 ms per save (-96%).
  - Per-shard locks let multiple workers save concurrently for symbols
    in different buckets.

Auto-migration: on first read, if the legacy `quote_cache.json` exists, we
load it, rewrite every entry into the appropriate shard, and rename the
old file to `quote_cache.json.migrated`. The first run after deployment
pays a small one-time migration cost; every subsequent run uses sharding
natively.

Phase 26: `_write_shard` now uses a unique tmp filename per call so two
processes (e.g. uvicorn --reload's reloader + worker) cannot collide on the
same tmp path and produce a FileNotFoundError on os.replace.

Phase 9: quotes are RAM-first. The bounded process-local `memory_store`
serves the common path, while sharded JSON remains a recoverable,
write-behind durability layer. Disk recovery and writes always happen outside
the quote locks; the application's managed maintenance worker drains the
bounded, coalesced dirty set.

  - Shards are read from disk lazily into an in-memory dict on first access
    and become the canonical source of truth for that shard.
  - `save_quote` / `invalidate_quote` mutate the in-memory dict (~O(1)) and
    mark the shard "dirty"; reads see writes immediately.
  - The managed maintenance loop persists dirty shards after a debounce
    window. Bursts of saves against the same shard coalesce into one atomic
    disk write; failed writes remain dirty and retry with bounded backoff.
  - Serialization uses `orjson` (5-10x faster than stdlib `json`) and
    drops the cosmetic `indent=2, sort_keys=True` from the hot path (still
    sort-keys for determinism, but no pretty-print).
  - FastAPI shutdown performs a bounded final flush after stopping
    maintenance. `atexit` is deliberately not used: lifecycle ownership must
    remain with the application, rather than creating unmanaged writers.

The public API (`save_quote`, `get_cached_quote`, `load_quote_cache`,
`invalidate_quote`, `cache_status`) is unchanged, so the rest of the app
doesn't need to know about the in-memory layer.
"""
from __future__ import annotations
import json
import logging
import os
import string
import time
import uuid
from pathlib import Path
from threading import Lock
from app.config import settings
from app.utils.time import utcnow_iso, age_seconds_from_iso

try:
    import orjson as _orjson
    _HAS_ORJSON = True
except Exception:  # pragma: no cover - orjson is in requirements.txt
    _orjson = None  # type: ignore[assignment]
    _HAS_ORJSON = False

_log = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_SHARD_DIR = _DATA_DIR / "quote_cache"
_LEGACY_FILE = _DATA_DIR / "quote_cache.json"

# Per-shard locks. Keyed by shard letter (A-Z) or '_' for numeric/other.
_SHARD_LOCKS: dict[str, Lock] = {c: Lock() for c in string.ascii_uppercase}
_SHARD_LOCKS["_"] = Lock()
# A module-level lock that gates the one-time migration so concurrent
# importers don't double-migrate the legacy file.
_MIGRATION_LOCK = Lock()
_MIGRATION_DONE = False

# ---------------------------------------------------------------------------
# Phase 26.30: in-memory shard layer + debounced background flusher.
# ---------------------------------------------------------------------------
# _SHARD_MEM[shard] holds the canonical, possibly-dirty contents of each
# shard.  It is populated lazily on first read of that shard.  Once loaded
# from disk, all further reads and writes go through memory and the disk
# is updated asynchronously by the flusher thread.
_SHARD_MEM: dict[str, dict] = {}
_SHARD_LOADED: dict[str, bool] = {}
_SHARD_VERSION: dict[str, int] = {}

# Dirty bookkeeping (guarded by _DIRTY_LOCK):
#   _DIRTY_SHARDS: shards whose in-memory state has unflushed writes.
#   _DIRTY_SINCE[shard]: monotonic timestamp of the FIRST dirty mark since
#     last flush. Used to decide when the debounce window has elapsed.
_DIRTY_LOCK = Lock()
_DIRTY_SHARDS: set[str] = set()
_DIRTY_SINCE: dict[str, float] = {}
_DIRTY_FAILURES: dict[str, int] = {}
_DIRTY_RETRY_AT: dict[str, float] = {}
_PERSISTING_SHARDS: set[str] = set()

# Flusher configuration. Defensive defaults: 1 second of coalescing buys
# us most of the wins from a hot scoring loop (which may dirty the same
# shard dozens of times per second) without leaving data unflushed long
# enough to be problematic on a clean exit.
_DEBOUNCE_S = max(0.0, float(os.environ.get("QUOTE_CACHE_FLUSH_DEBOUNCE_SECONDS", "1.0")))
_FLUSH_BATCH_SIZE = max(1, int(os.environ.get("QUOTE_CACHE_FLUSH_MAX_SHARDS", "4")))
_FLUSH_RETRY_MAX_S = max(0.0, float(os.environ.get("QUOTE_CACHE_FLUSH_RETRY_MAX_SECONDS", "5.0")))
_SHUTDOWN_FLUSH_MAX_SHARDS = max(1, int(os.environ.get("QUOTE_CACHE_SHUTDOWN_FLUSH_MAX_SHARDS", "8")))
_SHUTDOWN_FLUSH_MAX_S = max(0.0, float(os.environ.get("QUOTE_CACHE_SHUTDOWN_FLUSH_SECONDS", "3.0")))
_MEMORY_PROVIDER_ID = "yfinance"

# cache_status() TTL cache: result is valid for this many seconds so that
# frequent UI polling (e.g. every 2-3 s) doesn't re-scan all 27 shard locks.
_STATUS_CACHE_TTL_S = 10.0
_STATUS_CACHE_LOCK = Lock()
_STATUS_CACHE: dict = {"result": None, "at": 0.0}


def _shard_for(symbol: str) -> str:
    """Return the shard key for a symbol. A-Z map to themselves; everything
    else (digits, hyphens, punctuation, empty) goes to the `_` bucket."""
    if not symbol:
        return "_"
    first = symbol[0].upper()
    if "A" <= first <= "Z":
        return first
    return "_"


def _shard_path(shard: str) -> Path:
    return _SHARD_DIR / f"{shard}.json"


def _loads(raw: bytes) -> dict:
    """Decode a shard payload, tolerating both orjson-compact and the
    legacy stdlib-indented format."""
    if not raw:
        return {}
    if _HAS_ORJSON:
        try:
            return _orjson.loads(raw)
        except Exception:
            pass
    try:
        return json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    except Exception:
        return {}


def _dumps(data: dict) -> bytes:
    """Serialize a shard payload. Uses orjson when available (5-10x faster
    than stdlib json with indent+sort_keys) and falls back to stdlib json
    otherwise so the module still works on any Python install."""
    if _HAS_ORJSON:
        try:
            return _orjson.dumps(data, option=_orjson.OPT_SORT_KEYS)
        except Exception:
            pass
    return json.dumps(data, sort_keys=True).encode("utf-8")


def _read_shard_from_disk(shard: str) -> dict:
    """Disk read (no memoization). Used by the lazy loader and migration."""
    path = _shard_path(shard)
    if not path.exists():
        return {}
    try:
        raw = path.read_bytes()
    except Exception:
        return {}
    return _loads(raw)


def _ensure_shard_loaded(shard: str) -> dict:
    """Recover one shard without ever doing disk I/O under its quote lock.

    A concurrent cold caller may duplicate a small shard read, but only one
    result is installed. This deliberately favors lock-free write admission
    over making a first, cold read wait behind filesystem latency.
    """
    lock = _SHARD_LOCKS[shard]
    with lock:
        if _SHARD_LOADED.get(shard):
            return _SHARD_MEM.setdefault(shard, {})

    recovered = _read_shard_from_disk(shard)

    with lock:
        if not _SHARD_LOADED.get(shard):
            # Preserve any memory state injected by a concurrent recovery or
            # test harness; it is newer than the durable snapshot.
            recovered.update(_SHARD_MEM.get(shard, {}))
            _SHARD_MEM[shard] = recovered
            _SHARD_LOADED[shard] = True
            _SHARD_VERSION.setdefault(shard, 0)
        return _SHARD_MEM.setdefault(shard, {})


def _read_shard(shard: str) -> dict:
    """Backward-compatible read: returns the current in-memory shard
    contents (loading from disk on first access). Returns a *copy* to
    isolate callers from in-place mutations of the in-memory dict."""
    _ensure_shard_loaded(shard)
    with _SHARD_LOCKS[shard]:
        return dict(_SHARD_MEM.get(shard, {}))


def _write_shard_bytes(shard: str, payload: bytes) -> bool:
    """Atomically replace the shard file on disk with `payload`.

    The atomic-write + retry semantics are unchanged from Phase 26 so
    multi-process scenarios still tolerate transient ENOENT/PermissionError
    on os.replace; only the serialization step moved to the caller (the
    flusher) so we serialize once even when the shard is dirtied many
    times during the debounce window.
    """
    _SHARD_DIR.mkdir(parents=True, exist_ok=True)
    path = _shard_path(shard)

    def _attempt() -> bool:
        tmp = path.with_suffix(f".json.{uuid.uuid4().hex[:8]}.tmp")
        try:
            tmp.write_bytes(payload)
            os.replace(tmp, path)
            return True
        except FileNotFoundError:
            _SHARD_DIR.mkdir(parents=True, exist_ok=True)
            return False
        except PermissionError:
            # Windows-only race: destination held open by a reader.
            return False
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except (OSError, PermissionError):
                pass

    delays_ms = (10, 20, 40, 80, 160, 320)
    last_exc: Exception | None = None
    for delay_ms in delays_ms:
        try:
            if _attempt():
                return True
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
        time.sleep(delay_ms / 1000.0)

    # Final best-effort write that swallows errors so the caller (the
    # flusher) doesn't crash the daemon.
    final_ok = False
    try:
        final_ok = _attempt()
    except Exception:  # noqa: BLE001
        pass
    if not final_ok:
        _log.warning(
            'shard %s flush failed after %d retries (last error: %s)',
            shard, len(delays_ms), last_exc or 'replace did not succeed',
        )
    return final_ok


def _write_shard(shard: str, data: dict) -> bool:
    """Backward-compatible synchronous write: serialize + atomically replace
    the shard file. Used by the one-time legacy migration and by `_flush_shard`.
    The hot path (`save_quote`/`invalidate_quote`) no longer calls this
    directly; it mutates memory and lets the background flusher persist."""
    return _write_shard_bytes(shard, _dumps(data))


def _mark_dirty(shard: str) -> None:
    """Mark a shard for managed write-behind persistence."""
    now = time.monotonic()
    with _DIRTY_LOCK:
        _DIRTY_SHARDS.add(shard)
        # setdefault preserves the FIRST-dirty timestamp so we flush
        # exactly _DEBOUNCE_S after the burst begins, not after it ends.
        _DIRTY_SINCE.setdefault(shard, now)


def _snapshot_shard_for_flush(shard: str) -> tuple[bytes, int] | None:
    """Return an internally consistent serialized snapshot and its version."""
    with _SHARD_LOCKS[shard]:
        data = _SHARD_MEM.get(shard)
        if data is None:
            return None
        return _dumps(data), _SHARD_VERSION.get(shard, 0)


def _complete_flush(shard: str, version: int, succeeded: bool, now: float) -> None:
    """Clear only the exact snapshot that reached disk.

    A save that races a slow write increments the shard version. Its dirty
    marker therefore survives completion of the older snapshot and will be
    persisted on a subsequent maintenance tick.
    """
    with _SHARD_LOCKS[shard]:
        current_version = _SHARD_VERSION.get(shard, 0)
    with _DIRTY_LOCK:
        _PERSISTING_SHARDS.discard(shard)
        if succeeded:
            _DIRTY_FAILURES.pop(shard, None)
            _DIRTY_RETRY_AT.pop(shard, None)
            if current_version == version:
                _DIRTY_SHARDS.discard(shard)
                _DIRTY_SINCE.pop(shard, None)
            return
        failures = _DIRTY_FAILURES.get(shard, 0) + 1
        _DIRTY_FAILURES[shard] = failures
        _DIRTY_SHARDS.add(shard)
        _DIRTY_SINCE.setdefault(shard, now)
        _DIRTY_RETRY_AT[shard] = now + min(
            _FLUSH_RETRY_MAX_S, 0.25 * (2 ** min(failures - 1, 6))
        )
    _log.warning(
        "quote-cache persistence failed for shard %s (attempt %d); retaining dirty snapshot",
        shard, failures,
    )


def _flush_shard(shard: str) -> bool:
    """Persist a versioned snapshot without holding either shared cache lock."""
    # Claim only the in-flight marker under the bookkeeping lock. The
    # serializer and every filesystem operation below run after releasing it.
    with _DIRTY_LOCK:
        if shard in _PERSISTING_SHARDS:
            return False
        _PERSISTING_SHARDS.add(shard)
    snapshot = _snapshot_shard_for_flush(shard)
    if snapshot is None:
        _complete_flush(shard, 0, True, time.monotonic())
        return True
    payload, version = snapshot
    try:
        succeeded = _write_shard_bytes(shard, payload)
    except Exception as exc:  # noqa: BLE001
        _log.warning("quote-cache disk write crashed for shard %s: %s", shard, exc)
        succeeded = False
    _complete_flush(shard, version, succeeded, time.monotonic())
    return succeeded


def flush_due(
    now: float | None = None,
    *,
    force: bool = False,
    max_shards: int | None = None,
) -> int:
    """Flush a bounded set of due shards from the managed maintenance loop.

    This intentionally has no background thread: callers own scheduling.
    It returns successful writes, leaving failures dirty for backoff retry.
    """
    now = time.monotonic() if now is None else now
    limit = _FLUSH_BATCH_SIZE if max_shards is None else max(0, max_shards)
    with _DIRTY_LOCK:
        candidates = [
            shard for shard in sorted(_DIRTY_SHARDS)
            if force
            or (
                now - _DIRTY_SINCE.get(shard, now) >= _DEBOUNCE_S
                and now >= _DIRTY_RETRY_AT.get(shard, 0.0)
            )
        ][:limit]
    succeeded = 0
    for shard in candidates:
        try:
            succeeded += int(_flush_shard(shard))
        except Exception as exc:  # noqa: BLE001
            _log.warning("quote-cache flush crashed for shard %s: %s", shard, exc)
            _complete_flush(shard, -1, False, time.monotonic())
    return succeeded


def flush_now() -> int:
    """Perform a bounded best-effort shutdown checkpoint.

    Dirty work left after the deadline is intentionally retained in memory;
    no unbounded shutdown or stale-write overwrite is permitted.
    """
    deadline = time.monotonic() + _SHUTDOWN_FLUSH_MAX_S
    written = 0
    attempted: set[str] = set()
    while (
        time.monotonic() <= deadline
        and len(attempted) < _SHUTDOWN_FLUSH_MAX_SHARDS
    ):
        with _DIRTY_LOCK:
            candidates = sorted(
                _DIRTY_SHARDS - attempted - _PERSISTING_SHARDS
            )
        if not candidates:
            break
        shard = candidates[0]
        attempted.add(shard)
        try:
            written += int(_flush_shard(shard))
        except Exception as exc:  # noqa: BLE001
            _log.warning("quote-cache shutdown flush crashed for shard %s: %s", shard, exc)
            _complete_flush(shard, -1, False, time.monotonic())
    return written


# ---------------------------------------------------------------------------
# Legacy migration (one-shot)
# ---------------------------------------------------------------------------

def _maybe_migrate_legacy() -> None:
    """One-time migration of the legacy single-file cache into shards.
    Idempotent: subsequent calls are no-ops."""
    global _MIGRATION_DONE
    if _MIGRATION_DONE:
        return
    with _MIGRATION_LOCK:
        if _MIGRATION_DONE:
            return
        if not _LEGACY_FILE.exists():
            _MIGRATION_DONE = True
            return
        try:
            legacy = json.loads(_LEGACY_FILE.read_text(encoding="utf-8"))
        except Exception:
            _MIGRATION_DONE = True
            return
        # Group entries by shard, write each shard once.
        by_shard: dict[str, dict] = {}
        for sym, payload in (legacy or {}).items():
            shard = _shard_for(sym)
            by_shard.setdefault(shard, {})[sym.upper()] = payload
        for shard, entries in by_shard.items():
            _ensure_shard_loaded(shard)
            with _SHARD_LOCKS[shard]:
                existing = _SHARD_MEM.setdefault(shard, {})
                existing.update(entries)
                _SHARD_VERSION[shard] = _SHARD_VERSION.get(shard, 0) + 1
            # Migration is a one-shot operation; persist synchronously so
            # the legacy file is replaced atomically with the shard set.
            if not _write_shard(shard, _SHARD_MEM.get(shard, {})):
                _mark_dirty(shard)
        # Rename legacy so it isn't re-imported on next process start.
        try:
            _LEGACY_FILE.rename(_LEGACY_FILE.with_suffix(".json.migrated"))
        except Exception:
            pass
        _MIGRATION_DONE = True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def warm_quote_cache(*, rate_limit_per_second: float = 10.0) -> int:
    """Recover durable shards gradually during managed cold-start warmup.

    This only reads local JSON; it never fetches providers or evicts a
    foreground quote. The caller runs it after startup jitter so multiple
    workers do not contend for the same shard files at once.
    """
    _maybe_migrate_legacy()
    delay = 1.0 / rate_limit_per_second if rate_limit_per_second > 0 else 0.0
    loaded = 0
    for shard in list(string.ascii_uppercase) + ["_"]:
        # Cold-start recovery is deliberately low priority.  Do not keep
        # admitting Tier 3 disk entries while maintenance is relieving RAM
        # pressure for active scanner work.
        try:
            from app.services.tier_cache_policy import low_priority_prefetch_halted
            if low_priority_prefetch_halted():
                break
        except Exception:
            pass
        _ensure_shard_loaded(shard)
        loaded += 1
        if delay:
            time.sleep(delay)
    return loaded


def load_quote_cache() -> dict:
    """Return the merged contents of every shard. Mostly used by
    `cache_status()` and end-to-end tests; the hot path uses
    `get_cached_quote()` which only reads one shard."""
    _maybe_migrate_legacy()
    out: dict = {}
    for shard in list(string.ascii_uppercase) + ["_"]:
        _ensure_shard_loaded(shard)
        with _SHARD_LOCKS[shard]:
            out.update(_SHARD_MEM.get(shard, {}))
    return out


def _quote_priority(symbol: str) -> int:
    """Use tier priority for bounded RAM eviction without coupling imports."""
    try:
        from app.services.memory_store import PRIORITY_TIER_1, PRIORITY_TIER_2, PRIORITY_TIER_3
        from app.services.tier_manager import TIER_1, TIER_2, get_tier
        tier = get_tier(symbol)
        if tier == TIER_1:
            return PRIORITY_TIER_1
        if tier == TIER_2:
            return PRIORITY_TIER_2
        return PRIORITY_TIER_3
    except Exception:
        return 2


def _memory_provider_id(quote: dict | None = None) -> str:
    """Use provider generation in RAM identity while disk remains portable."""
    source = ((quote or {}).get("source") or _MEMORY_PROVIDER_ID).strip().lower()
    try:
        from app.services.provider_session import provider_cache_identity
        return provider_cache_identity(source)
    except Exception:
        return source


def _put_memory_quote(symbol: str, quote: dict) -> None:
    """Best-effort RAM admission; durable write-behind remains independent."""
    try:
        from app.services.memory_store import memory_store
        memory_store.set_quote(
            symbol,
            quote,
            provider_id=_memory_provider_id(quote),
            ttl_seconds=settings.quote_cache_ttl_seconds,
            priority=_quote_priority(symbol),
        )
    except Exception as exc:  # noqa: BLE001
        _log.debug("quote RAM cache admission failed for %s: %s", symbol, exc)


def save_quote(symbol: str, payload: dict) -> None:
    if not symbol:
        return
    _maybe_migrate_legacy()
    shard = _shard_for(symbol)
    sym = symbol.upper()
    _ensure_shard_loaded(shard)
    with _SHARD_LOCKS[shard]:
        entry = {
            "symbol": sym,
            "last_price": payload.get("last_price"),
            "previous_close": payload.get("previous_close"),
            "captured_at_utc": payload.get("captured_at_utc") or utcnow_iso(),
            "source": payload.get("source", "yfinance"),
        }
        _SHARD_MEM.setdefault(shard, {})[sym] = entry
        _SHARD_VERSION[shard] = _SHARD_VERSION.get(shard, 0) + 1
    _put_memory_quote(sym, entry)
    _mark_dirty(shard)


def invalidate_quote(symbol: str) -> bool:
    """Drop the cached quote for one symbol so the next read forces a live fetch."""
    if not symbol:
        return False
    _maybe_migrate_legacy()
    shard = _shard_for(symbol)
    sym = symbol.upper()
    removed = False
    _ensure_shard_loaded(shard)
    with _SHARD_LOCKS[shard]:
        data = _SHARD_MEM.setdefault(shard, {})
        if sym in data:
            del data[sym]
            _SHARD_VERSION[shard] = _SHARD_VERSION.get(shard, 0) + 1
            removed = True
    if removed:
        try:
            from app.services.memory_store import memory_store
            memory_store.invalidate_symbol(sym, domains={"quotes"})
        except Exception:
            pass
        _mark_dirty(shard)
    return removed


def invalidate_provider_quotes(provider_id: str) -> int:
    """Remove durable quotes sourced by a provider after its session resets."""
    provider = (provider_id or '').strip().lower()
    if not provider:
        return 0
    _maybe_migrate_legacy()
    removed = 0
    for shard in list(string.ascii_uppercase) + ["_"]:
        _ensure_shard_loaded(shard)
        with _SHARD_LOCKS[shard]:
            data = _SHARD_MEM.setdefault(shard, {})
            stale = [
                symbol for symbol, quote in data.items()
                if (quote.get("source") or _MEMORY_PROVIDER_ID).strip().lower() == provider
            ]
            for symbol in stale:
                del data[symbol]
                removed += 1
            if stale:
                _SHARD_VERSION[shard] = _SHARD_VERSION.get(shard, 0) + 1
        if stale:
            _mark_dirty(shard)
    if removed:
        try:
            from app.services.memory_store import memory_store
            memory_store.invalidate_provider(provider)
        except Exception:
            pass
    return removed


def get_cached_quote(symbol: str) -> dict | None:
    if not symbol:
        return None
    sym = symbol.upper()
    # This is the quote hot path: return the bounded RAM value without
    # touching shard locks or the filesystem whenever possible.
    try:
        from app.services.memory_store import memory_store
        # The durable quote shard is source-agnostic, but RAM entries retain
        # their provider generation. Probe the small supported-source set so
        # crypto quotes do not fall through to disk on every read.
        for source in ("yfinance", "coingecko", "cryptocompare"):
            hit = memory_store.get_quote(sym, provider_id=_memory_provider_id({"source": source}))
            if hit is not None:
                return dict(hit)
    except Exception as exc:  # noqa: BLE001
        _log.debug("quote RAM cache read failed for %s: %s", sym, exc)

    _maybe_migrate_legacy()
    shard = _shard_for(sym)
    _ensure_shard_loaded(shard)
    with _SHARD_LOCKS[shard]:
        hit = _SHARD_MEM.get(shard, {}).get(sym)
        # Return a shallow copy so callers can't mutate the canonical
        # in-memory state by accident.
        recovered = dict(hit) if hit is not None else None
    if recovered is not None:
        _put_memory_quote(sym, recovered)
    return recovered


def quote_age_seconds(cached: dict | None) -> int:
    if not cached:
        return settings.cache_max_age_seconds + 1
    return age_seconds_from_iso(cached.get("captured_at_utc"))


def cached_quote_is_usable(cached: dict | None) -> bool:
    return quote_age_seconds(cached) <= settings.cache_max_age_seconds


def cache_status() -> dict:
    # Return a cached result if it was computed within the TTL window.  This
    # prevents frequent UI polling (e.g. every 2-3 s) from re-scanning all 27
    # shard locks and calling stat() on every shard file on each request.
    now = time.monotonic()
    with _STATUS_CACHE_LOCK:
        if _STATUS_CACHE["result"] is not None and (now - _STATUS_CACHE["at"]) < _STATUS_CACHE_TTL_S:
            return dict(_STATUS_CACHE["result"])

    # Build stats from the in-memory shard layer (_SHARD_MEM) for loaded shards
    # and from file-system metadata for all shards — no full shard deserialization.
    entry_count = 0
    shard_sizes: dict[str, int] = {}
    last_entry: dict | None = None

    for shard in list(string.ascii_uppercase) + ["_"]:
        # File size: a single stat() call, no read.
        path = _shard_path(shard)
        if path.exists():
            try:
                shard_sizes[shard] = path.stat().st_size
            except Exception:
                pass

        # Entry count / last-entry timestamp: use in-memory layer if loaded.
        lock = _SHARD_LOCKS.get(shard)
        if lock is None:
            continue
        with lock:
            mem = _SHARD_MEM.get(shard)
        if mem is not None:
            entry_count += len(mem)
            if mem:
                try:
                    candidate = max(mem.values(), key=lambda x: x.get("captured_at_utc", ""))
                    if last_entry is None or (
                        candidate.get("captured_at_utc", "") > last_entry.get("captured_at_utc", "")
                    ):
                        last_entry = candidate
                except Exception:
                    pass

    with _DIRTY_LOCK:
        dirty_count = len(_DIRTY_SHARDS)
    result = {
        "cache_file_present": _SHARD_DIR.exists(),
        "cache_dir_present": _SHARD_DIR.exists(),
        "cache_entries": entry_count,
        "cache_shards_present": len(shard_sizes),
        "cache_shard_total_bytes": sum(shard_sizes.values()),
        "cache_dirty_shards": dirty_count,
        "cache_serializer": "orjson" if _HAS_ORJSON else "stdlib-json",
        "last_cache_write_utc": (last_entry or {}).get("captured_at_utc"),
        "last_cache_symbol": (last_entry or {}).get("symbol"),
        "legacy_migrated": (_LEGACY_FILE.with_suffix(".json.migrated")).exists(),
    }
    with _STATUS_CACHE_LOCK:
        _STATUS_CACHE["result"] = result
        _STATUS_CACHE["at"] = time.monotonic()
    return dict(result)
