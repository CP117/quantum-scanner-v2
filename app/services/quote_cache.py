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

Phase 26.30 (CPU win): the hot path no longer serializes-and-writes a JSON
shard on every `save_quote()` call. py-spy profiling showed `_write_shard`
consumed ~32% of batch wall time on a cold cache. The new architecture:

  - Shards are read from disk lazily into an in-memory dict on first access
    and become the canonical source of truth for that shard.
  - `save_quote` / `invalidate_quote` mutate the in-memory dict (~O(1)) and
    mark the shard "dirty"; reads see writes immediately.
  - A background daemon thread (`_flusher_loop`) wakes every 250 ms and
    persists any shard that has been dirty for >= 1 s. Bursts of saves
    against the same shard coalesce into a single atomic disk write.
  - Serialization uses `orjson` (5-10x faster than stdlib `json`) and
    drops the cosmetic `indent=2, sort_keys=True` from the hot path (still
    sort-keys for determinism, but no pretty-print).
  - `atexit` flushes all dirty shards on interpreter shutdown so we never
    lose data on a clean exit.

The public API (`save_quote`, `get_cached_quote`, `load_quote_cache`,
`invalidate_quote`, `cache_status`) is unchanged, so the rest of the app
doesn't need to know about the in-memory layer.
"""
from __future__ import annotations
import atexit
import json
import logging
import os
import string
import threading
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

# Dirty bookkeeping (guarded by _DIRTY_LOCK):
#   _DIRTY_SHARDS: shards whose in-memory state has unflushed writes.
#   _DIRTY_SINCE[shard]: monotonic timestamp of the FIRST dirty mark since
#     last flush. Used to decide when the debounce window has elapsed.
_DIRTY_LOCK = Lock()
_DIRTY_SHARDS: set[str] = set()
_DIRTY_SINCE: dict[str, float] = {}

# Flusher configuration. Defensive defaults: 1 second of coalescing buys
# us most of the wins from a hot scoring loop (which may dirty the same
# shard dozens of times per second) without leaving data unflushed long
# enough to be problematic on a clean exit.
_DEBOUNCE_S = 1.0
_FLUSHER_TICK_S = 0.25

_FLUSHER_LOCK = Lock()
_FLUSHER_THREAD: threading.Thread | None = None
_FLUSHER_STOP = threading.Event()
_ATEXIT_REGISTERED = False


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
    """Ensure the in-memory shard is populated from disk, then return the
    underlying dict (the caller MUST already hold _SHARD_LOCKS[shard])."""
    if not _SHARD_LOADED.get(shard):
        _SHARD_MEM[shard] = _read_shard_from_disk(shard)
        _SHARD_LOADED[shard] = True
    # The .get() guard is defensive in case something else cleared the
    # mem dict between the load and the return.
    return _SHARD_MEM.setdefault(shard, {})


def _read_shard(shard: str) -> dict:
    """Backward-compatible read: returns the current in-memory shard
    contents (loading from disk on first access). Returns a *copy* to
    isolate callers from in-place mutations of the in-memory dict."""
    with _SHARD_LOCKS[shard]:
        return dict(_ensure_shard_loaded(shard))


def _write_shard_bytes(shard: str, payload: bytes) -> None:
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
                return
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
    if not final_ok and last_exc:
        _log.warning(
            'shard %s flush failed after %d retries (last error: %s); '
            'will retry on next dirty mark', shard, len(delays_ms), last_exc,
        )


def _write_shard(shard: str, data: dict) -> None:
    """Backward-compatible synchronous write: serialize + atomically replace
    the shard file. Used by the one-time legacy migration and by `_flush_shard`.
    The hot path (`save_quote`/`invalidate_quote`) no longer calls this
    directly; it mutates memory and lets the background flusher persist."""
    _write_shard_bytes(shard, _dumps(data))


# ---------------------------------------------------------------------------
# Background flusher
# ---------------------------------------------------------------------------

def _mark_dirty(shard: str) -> None:
    """Mark a shard for asynchronous flushing. Idempotent within the
    debounce window."""
    now = time.monotonic()
    with _DIRTY_LOCK:
        _DIRTY_SHARDS.add(shard)
        # setdefault preserves the FIRST-dirty timestamp so we flush
        # exactly _DEBOUNCE_S after the burst begins, not after it ends.
        _DIRTY_SINCE.setdefault(shard, now)
    _ensure_flusher_started()


def _snapshot_shard_for_flush(shard: str) -> bytes | None:
    """Serialize the in-memory shard to bytes while holding the per-shard
    lock so we don't observe a torn update. Returns None if there's nothing
    to flush (shard was never loaded)."""
    with _SHARD_LOCKS[shard]:
        data = _SHARD_MEM.get(shard)
        if data is None:
            return None
        # orjson.dumps releases the GIL and is fast; still, do it inside
        # the lock so any concurrent save_quote can't interleave a partial
        # mutation into the serialized output.
        return _dumps(data)


def _flush_shard(shard: str) -> None:
    payload = _snapshot_shard_for_flush(shard)
    if payload is None:
        return
    _write_shard_bytes(shard, payload)


def _flusher_loop() -> None:
    """Daemon loop: every _FLUSHER_TICK_S seconds, flush any shard whose
    dirty-age exceeds _DEBOUNCE_S."""
    while not _FLUSHER_STOP.is_set():
        # Sleep with the stop event so shutdown can interrupt immediately.
        if _FLUSHER_STOP.wait(_FLUSHER_TICK_S):
            break
        try:
            now = time.monotonic()
            to_flush: list[str] = []
            with _DIRTY_LOCK:
                for s in list(_DIRTY_SHARDS):
                    since = _DIRTY_SINCE.get(s, now)
                    if (now - since) >= _DEBOUNCE_S:
                        to_flush.append(s)
                        _DIRTY_SHARDS.discard(s)
                        _DIRTY_SINCE.pop(s, None)
            for s in to_flush:
                try:
                    _flush_shard(s)
                except Exception as exc:  # noqa: BLE001
                    _log.warning('flusher: shard %s flush failed: %s', s, exc)
        except Exception as exc:  # noqa: BLE001
            # The flusher must NEVER die. Log and keep going.
            _log.warning('flusher tick error: %s', exc)


def _ensure_flusher_started() -> None:
    """Lazy-start the background flusher on the first dirty mark."""
    global _FLUSHER_THREAD, _ATEXIT_REGISTERED
    if _FLUSHER_THREAD is not None and _FLUSHER_THREAD.is_alive():
        return
    with _FLUSHER_LOCK:
        if _FLUSHER_THREAD is not None and _FLUSHER_THREAD.is_alive():
            return
        _FLUSHER_STOP.clear()
        t = threading.Thread(
            target=_flusher_loop,
            name='quote-cache-flusher',
            daemon=True,
        )
        t.start()
        _FLUSHER_THREAD = t
        if not _ATEXIT_REGISTERED:
            atexit.register(_atexit_flush)
            _ATEXIT_REGISTERED = True


def _atexit_flush() -> None:
    """Drain all dirty shards on interpreter shutdown so a clean exit
    never loses cached quotes."""
    try:
        _FLUSHER_STOP.set()
    except Exception:
        pass
    try:
        with _DIRTY_LOCK:
            shards = list(_DIRTY_SHARDS)
            _DIRTY_SHARDS.clear()
            _DIRTY_SINCE.clear()
        for s in shards:
            try:
                _flush_shard(s)
            except Exception:
                pass
    except Exception:
        pass


def flush_now() -> int:
    """Synchronously flush every dirty shard. Returns the number of
    shards written. Intended for tests, shutdown hooks, and operators
    who want a definitive on-disk checkpoint."""
    with _DIRTY_LOCK:
        shards = list(_DIRTY_SHARDS)
        _DIRTY_SHARDS.clear()
        _DIRTY_SINCE.clear()
    n = 0
    for s in shards:
        try:
            _flush_shard(s)
            n += 1
        except Exception as exc:  # noqa: BLE001
            _log.warning('flush_now: shard %s failed: %s', s, exc)
    return n


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
            with _SHARD_LOCKS[shard]:
                existing = _ensure_shard_loaded(shard)
                existing.update(entries)
            # Migration is a one-shot operation; persist synchronously so
            # the legacy file is replaced atomically with the shard set.
            _write_shard(shard, _SHARD_MEM.get(shard, {}))
        # Rename legacy so it isn't re-imported on next process start.
        try:
            _LEGACY_FILE.rename(_LEGACY_FILE.with_suffix(".json.migrated"))
        except Exception:
            pass
        _MIGRATION_DONE = True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_quote_cache() -> dict:
    """Return the merged contents of every shard. Mostly used by
    `cache_status()` and end-to-end tests; the hot path uses
    `get_cached_quote()` which only reads one shard."""
    _maybe_migrate_legacy()
    out: dict = {}
    for shard in list(string.ascii_uppercase) + ["_"]:
        with _SHARD_LOCKS[shard]:
            out.update(_ensure_shard_loaded(shard))
    return out


def save_quote(symbol: str, payload: dict) -> None:
    if not symbol:
        return
    _maybe_migrate_legacy()
    shard = _shard_for(symbol)
    sym = symbol.upper()
    with _SHARD_LOCKS[shard]:
        data = _ensure_shard_loaded(shard)
        data[sym] = {
            "symbol": sym,
            "last_price": payload.get("last_price"),
            "previous_close": payload.get("previous_close"),
            "captured_at_utc": payload.get("captured_at_utc") or utcnow_iso(),
            "source": payload.get("source", "yfinance"),
        }
    _mark_dirty(shard)


def invalidate_quote(symbol: str) -> bool:
    """Drop the cached quote for one symbol so the next read forces a live fetch."""
    if not symbol:
        return False
    _maybe_migrate_legacy()
    shard = _shard_for(symbol)
    sym = symbol.upper()
    removed = False
    with _SHARD_LOCKS[shard]:
        data = _ensure_shard_loaded(shard)
        if sym in data:
            del data[sym]
            removed = True
    if removed:
        _mark_dirty(shard)
    return removed


def get_cached_quote(symbol: str) -> dict | None:
    if not symbol:
        return None
    _maybe_migrate_legacy()
    shard = _shard_for(symbol)
    sym = symbol.upper()
    with _SHARD_LOCKS[shard]:
        data = _ensure_shard_loaded(shard)
        hit = data.get(sym)
        # Return a shallow copy so callers can't mutate the canonical
        # in-memory state by accident.
        return dict(hit) if hit is not None else None


def quote_age_seconds(cached: dict | None) -> int:
    if not cached:
        return settings.cache_max_age_seconds + 1
    return age_seconds_from_iso(cached.get("captured_at_utc"))


def cached_quote_is_usable(cached: dict | None) -> bool:
    return quote_age_seconds(cached) <= settings.cache_max_age_seconds


def cache_status() -> dict:
    data = load_quote_cache()
    last = None
    if data:
        last = sorted(data.values(), key=lambda x: x.get("captured_at_utc", ""))[-1]
    # Report shard stats so operators can see the shard layout in /system/status.
    shard_sizes: dict[str, int] = {}
    for shard in list(string.ascii_uppercase) + ["_"]:
        path = _shard_path(shard)
        if path.exists():
            try:
                shard_sizes[shard] = path.stat().st_size
            except Exception:
                pass
    with _DIRTY_LOCK:
        dirty_count = len(_DIRTY_SHARDS)
    return {
        "cache_file_present": _SHARD_DIR.exists(),
        "cache_dir_present": _SHARD_DIR.exists(),
        "cache_entries": len(data),
        "cache_shards_present": len(shard_sizes),
        "cache_shard_total_bytes": sum(shard_sizes.values()),
        "cache_dirty_shards": dirty_count,
        "cache_serializer": "orjson" if _HAS_ORJSON else "stdlib-json",
        "last_cache_write_utc": (last or {}).get("captured_at_utc"),
        "last_cache_symbol": (last or {}).get("symbol"),
        "legacy_migrated": (_LEGACY_FILE.with_suffix(".json.migrated")).exists(),
    }
