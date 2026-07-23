"""
Tier Resilience — Phase 28
===========================

Per-tier watchdog, error tracking, and user-reset support.

Each tier has its own:
  * Error counter + rolling 10-minute window
  * Watchdog timestamp (updated by each scanner loop tick)
  * "stalled" flag — set when the watchdog fires or error count exceeds threshold
  * Reset function — clears locks, caches, resets the stalled flag

When a tier stalls the ``get_stall_prompt()`` function returns the user-visible
message so the API layer can surface it to the UI.

Usage
-----
    # In a scanner loop tick:
    tier_resilience.heartbeat(tier=1)

    # After a scoring error:
    tier_resilience.record_error(tier=1, error='provider timeout')

    # Check if the user should be warned:
    prompt = tier_resilience.get_stall_prompt(tier=1)
    if prompt:
        # return prompt to UI

    # User-initiated reset:
    tier_resilience.reset_tier(tier=1)
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Deque, Dict, Optional, Tuple

from app.config import settings

log = logging.getLogger('app.tier_resilience')

# ---------------------------------------------------------------------------
# Per-tier state
# ---------------------------------------------------------------------------

class _TierState:
    """Mutable per-tier health state, accessed under its own lock."""

    def __init__(self, tier: int) -> None:
        self.tier = tier
        self.lock = threading.Lock()
        self.last_heartbeat_at: float = time.monotonic()  # updated by scanner tick
        self.error_timestamps: Deque[float] = deque()     # rolling window of errors
        self.is_stalled: bool = False
        self.stall_detected_at: Optional[float] = None
        self.reset_count: int = 0
        self.last_error: Optional[str] = None

    # ------------------------------------------------------------------
    def heartbeat(self) -> None:
        with self.lock:
            self.last_heartbeat_at = time.monotonic()
            if self.is_stalled:
                # Auto-recover if the tier is ticking again.
                self.is_stalled = False
                self.stall_detected_at = None
                log.info('tier_resilience: Tier %d auto-recovered', self.tier)

    def record_error(self, error: str) -> None:
        now = time.monotonic()
        with self.lock:
            self.error_timestamps.append(now)
            self.last_error = error
            # Prune old entries outside the rolling window.
            cutoff = now - settings.tier_error_window_seconds
            while self.error_timestamps and self.error_timestamps[0] < cutoff:
                self.error_timestamps.popleft()
            # Check threshold.
            if len(self.error_timestamps) >= settings.tier_error_threshold:
                if not self.is_stalled:
                    self.is_stalled = True
                    self.stall_detected_at = now
                    log.warning(
                        'tier_resilience: Tier %d stalled — %d errors in %.0fs (last: %s)',
                        self.tier, len(self.error_timestamps),
                        settings.tier_error_window_seconds, error,
                    )

    def check_watchdog(self) -> None:
        """Called periodically.  Marks the tier stalled if no heartbeat for too long."""
        now = time.monotonic()
        with self.lock:
            silence = now - self.last_heartbeat_at
            if silence > settings.tier_watchdog_stall_seconds and not self.is_stalled:
                self.is_stalled = True
                self.stall_detected_at = now
                log.warning(
                    'tier_resilience: Tier %d stalled — no heartbeat for %.0fs',
                    self.tier, silence,
                )

    def get_stall_prompt(self) -> Optional[str]:
        """Return a user-facing stall message, or None if healthy."""
        with self.lock:
            if not self.is_stalled:
                return None
            age = int(time.monotonic() - (self.stall_detected_at or time.monotonic()))
            return (
                f"Tier {self.tier} scan stalled for {age}s. "
                f"**Wait** or **Reset** (clears locks, restarts tier)?"
            )

    def reset(self) -> None:
        """Reset the tier's stall state and error counters."""
        with self.lock:
            self.is_stalled = False
            self.stall_detected_at = None
            self.error_timestamps.clear()
            self.last_error = None
            self.last_heartbeat_at = time.monotonic()
            self.reset_count += 1
        log.info('tier_resilience: Tier %d reset (total resets: %d)', self.tier, self.reset_count)

    def get_status(self) -> dict:
        with self.lock:
            now = time.monotonic()
            silence = now - self.last_heartbeat_at
            errors_in_window = len(self.error_timestamps)
            return {
                'tier': self.tier,
                'is_stalled': self.is_stalled,
                'stall_age_seconds': int(now - self.stall_detected_at) if self.stall_detected_at else None,
                'heartbeat_age_seconds': round(silence, 1),
                'errors_in_window': errors_in_window,
                'reset_count': self.reset_count,
                'last_error': self.last_error,
            }


# ---------------------------------------------------------------------------
# Global registry
# ---------------------------------------------------------------------------
_states: Dict[int, _TierState] = {
    1: _TierState(1),
    2: _TierState(2),
    3: _TierState(3),
}


def _get_state(tier: int) -> _TierState:
    if tier not in _states:
        _states[tier] = _TierState(tier)
    return _states[tier]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def heartbeat(tier: int) -> None:
    """Signal that tier *tier* is alive and processing."""
    _get_state(tier).heartbeat()


def record_error(tier: int, error: str) -> None:
    """Record an error for tier *tier*.  May trigger stall detection."""
    _get_state(tier).record_error(error)


def is_stalled(tier: int) -> bool:
    """Return True if tier *tier* is currently stalled."""
    return _get_state(tier).is_stalled


def get_stall_prompt(tier: int) -> Optional[str]:
    """Return user-visible stall prompt for tier *tier*, or None if healthy."""
    return _get_state(tier).get_stall_prompt()


def get_all_stall_prompts() -> list[dict]:
    """Return stall prompts for all tiers that are currently stalled."""
    result = []
    for tier_num, state in sorted(_states.items()):
        prompt = state.get_stall_prompt()
        if prompt:
            result.append({'tier': tier_num, 'prompt': prompt})
    return result


def reset_tier(tier: int, clear_caches: bool = True) -> dict:
    """User-initiated tier reset.

    Clears the stall flag, resets error counters, optionally clears caches.
    Returns a status dict.
    """
    _get_state(tier).reset()

    cleared = []
    if clear_caches:
        if tier == 2:
            try:
                from app.services.tier_cache_store import clear_tier2_cache
                clear_tier2_cache()
                cleared.append('tier2_memory_cache')
            except Exception:  # noqa: BLE001
                pass
        elif tier == 3:
            try:
                from app.services.tier_cache_store import clear_tier3_cache
                clear_tier3_cache()
                cleared.append('tier3_disk_cache')
            except Exception:  # noqa: BLE001
                pass

    return {
        'tier': tier,
        'reset': True,
        'cleared': cleared,
        'message': f'Tier {tier} has been reset.  Scanner will restart automatically.',
    }


def get_tier_health() -> dict:
    """Return per-tier health status for the /api/tier-status endpoint."""
    return {str(t): state.get_status() for t, state in sorted(_states.items())}


# ---------------------------------------------------------------------------
# Watchdog loop
# ---------------------------------------------------------------------------

def _watchdog_loop() -> None:
    """Background daemon: poll each tier's heartbeat age every 30 s."""
    while True:
        time.sleep(30.0)
        for state in _states.values():
            try:
                state.check_watchdog()
            except Exception:  # noqa: BLE001
                pass


_wdog_started = False
_wdog_lock = threading.Lock()


def start_watchdog() -> None:
    """Start the watchdog background thread.  Idempotent."""
    global _wdog_started
    with _wdog_lock:
        if _wdog_started:
            return
        _wdog_started = True
    t = threading.Thread(target=_watchdog_loop, name='tier-watchdog', daemon=True)
    t.start()
    log.info('tier_resilience: watchdog thread started')
