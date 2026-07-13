"""
User-supplied API key storage.

Some providers (Finnhub, Polygon, Twelve Data, etc.) require an API key to
unlock useful rate limits. We don't ship those keys with the product -- the
user brings their own, entering them via the Settings panel in the
dashboard. This module persists the keys to a small JSON file at
`data/user_api_keys.json` and exposes a simple get/set/list API.

Design notes:
  - Keys live OUTSIDE the bundled zip (`build_zip.sh` excludes
    `data/user_api_keys.json`) so users can't accidentally redistribute
    their personal keys.
  - Read path is in-memory cached and lock-protected; writes go through
    an atomic tmpfile + os.replace so a crash mid-write can't corrupt the
    store.
  - The provider modules read the key via `api_keys.get("finnhub")` etc.
    A missing key returns None, which providers treat as "feature
    disabled" instead of raising.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from threading import Lock
from typing import Optional

_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
_KEYS_FILE = _DATA_DIR / "user_api_keys.json"
_LOCK = Lock()
_CACHE: dict[str, str] | None = None

# Whitelist of provider slugs we accept, so a typo in the UI doesn't
# silently create a dangling key entry. Add new providers here as we
# integrate them.
SUPPORTED_PROVIDERS = {
    "finnhub",
    "polygon",
    "twelvedata",
    "alphavantage",
    "fmp",
    "tiingo",
    "marketstack",
}


def _read_from_disk() -> dict[str, str]:
    if not _KEYS_FILE.exists():
        return {}
    try:
        raw = json.loads(_KEYS_FILE.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return {k: str(v) for k, v in raw.items() if v}
    except Exception:
        pass
    return {}


def _write_to_disk(data: dict[str, str]) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _KEYS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, _KEYS_FILE)
    # Lock down permissions so the file is owner-readable only. Best-effort
    # on platforms that support chmod; harmless no-op on Windows.
    try:
        os.chmod(_KEYS_FILE, 0o600)
    except Exception:
        pass


def _ensure_cache() -> dict[str, str]:
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    with _LOCK:
        if _CACHE is None:
            _CACHE = _read_from_disk()
    return _CACHE


def get(provider: str) -> Optional[str]:
    """Return the stored key for `provider`, or None if not configured.
    Also checks environment variables (e.g. `FINNHUB_API_KEY`) as a
    fallback so power users can configure keys without going through the
    UI."""
    if not provider:
        return None
    key = _ensure_cache().get(provider.lower())
    if key:
        return key
    env_name = f"{provider.upper()}_API_KEY"
    return os.environ.get(env_name) or None


def set_key(provider: str, value: str) -> bool:
    """Persist a key. Returns True if stored, False if the provider slug
    is not in `SUPPORTED_PROVIDERS` or the value is empty."""
    global _CACHE
    if not provider or provider.lower() not in SUPPORTED_PROVIDERS:
        return False
    value = (value or "").strip()
    with _LOCK:
        data = _read_from_disk()
        if not value:
            data.pop(provider.lower(), None)
        else:
            data[provider.lower()] = value
        _write_to_disk(data)
        _CACHE = data
    return True


def delete(provider: str) -> bool:
    return set_key(provider, "")


def list_configured() -> dict[str, bool]:
    """Return `{provider: True/False}` for every supported provider,
    indicating whether a key is configured. Does NOT return the raw key
    values (security) -- the UI only needs to know which providers are
    enabled."""
    data = _ensure_cache()
    return {p: bool(data.get(p) or os.environ.get(f"{p.upper()}_API_KEY")) for p in SUPPORTED_PROVIDERS}


def key_preview(provider: str) -> Optional[str]:
    """Return a masked preview of the stored key (`fhn1...3xz`), for the
    Settings panel readout. Never returns the full key."""
    key = get(provider)
    if not key or len(key) < 8:
        return "••••" if key else None
    return f"{key[:4]}…{key[-3:]}"
