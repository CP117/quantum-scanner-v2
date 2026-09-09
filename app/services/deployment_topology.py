"""Runtime deployment facts used to size process-local cache safely."""
from __future__ import annotations

import os
from typing import Any

_WORKER_ENV_NAMES = ("WEB_CONCURRENCY", "GUNICORN_WORKERS", "UVICORN_WORKERS")


def _worker_count() -> tuple[int, str]:
    for name in _WORKER_ENV_NAMES:
        raw = os.environ.get(name)
        if raw is None:
            continue
        try:
            value = int(raw)
        except ValueError:
            continue
        if value > 0:
            return value, name
    return 1, "default"


def cache_topology() -> dict[str, Any]:
    """Return non-sensitive topology facts for process-local cache operations."""
    workers, source = _worker_count()
    process_manager = "gunicorn" if os.environ.get("GUNICORN_CMD_ARGS") else "uvicorn"
    return {
        "process_scope": "local",
        "process_manager": process_manager,
        "worker_count": workers,
        "worker_count_source": source,
        "multi_worker": workers > 1,
        "shared_cache_backend": False,
        "cache_coordination": "per-process only",
        "cloudflare_tunnel_configured": bool(
            os.environ.get("TUNNEL_ORIGIN_CERT") or os.environ.get("CLOUDFLARED_TUNNEL_TOKEN")
        ),
    }


def cache_memory_budget() -> dict[str, int]:
    """Return the per-worker and aggregate configured cache budget."""
    from app.config import settings

    topology = cache_topology()
    per_worker = settings.memory_cache_max_mb * 1024 * 1024
    return {
        "per_worker_bytes": per_worker,
        "estimated_all_workers_bytes": per_worker * topology["worker_count"],
    }
