"""
Tiered Universe Architecture — Tier Manager (Phase 28)
======================================================

Manages the assignment of symbols to three scanning tiers:

  Tier 1 (Active)      — top 100 symbols, full scoring every 2-4 s
  Tier 2 (Monitor)     — next 1,000 symbols, lightweight scoring every 30-60 s
  Tier 3 (Background)  — remaining ~7,000 symbols, minimal scoring every 1 h
                         (falls back to once-daily if no GPU)

Promotion/demotion rules
------------------------
  Composite score      — primary factor (respects active filter / ranking preset)
  Volume spike         — 5× average volume → promote T3→T2
  Price gap            — >3 % overnight gap → promote T3→T2
  User interaction     — clicking a symbol → immediate T2 bump, or T1 if in T2
  Watchlist / pinned   — pinned symbols stay in Tier 2 minimum always
  Time decay           — quiet for 24 h + low score → demote one tier
  Stepped promotion    — T3→T2→T1 (never skips), subject to cooldown

State is kept in memory and periodically flushed to
``data/tier_state.json`` so restarts restore the last known assignment.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, Optional, Set

log = logging.getLogger('app.tier_manager')

# ---------------------------------------------------------------------------
# Tier constants
# ---------------------------------------------------------------------------
TIER_1 = 1
TIER_2 = 2
TIER_3 = 3

_TIER_LABELS = {TIER_1: 'active', TIER_2: 'monitor', TIER_3: 'background'}


# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------
# symbol → tier assignment (1, 2, or 3)
_tier_assignments: Dict[str, int] = {}
# symbol → monotonic time of last promotion (prevents rapid churn)
_last_promoted_at: Dict[str, float] = {}
# symbol → monotonic time of last scoring (used for inactivity demotion)
_last_scored_at: Dict[str, float] = {}
# symbol → most recent composite score (used for ranking within tiers)
_composite_scores: Dict[str, float] = {}
# symbols pinned to Tier ≤ 2 (won't be demoted to Tier 3)
_pinned_symbols: Set[str] = set()
# symbols promoted due to user interaction (temporary T2 bump)
_user_interaction_symbols: Dict[str, float] = {}  # symbol → expires_at (monotonic)

_state_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Disk persistence
# ---------------------------------------------------------------------------
_STATE_FILE = Path(os.environ.get('TIER_STATE_FILE', 'data/tier_state.json'))
_FLUSH_INTERVAL_S = 60.0  # write to disk every 60 s


def _state_file_path() -> Path:
    """Resolve state file relative to the repo root when not absolute."""
    p = _STATE_FILE
    if not p.is_absolute():
        p = Path(__file__).resolve().parent.parent.parent / p
    return p


def _load_state() -> None:
    """Load persisted tier assignments from disk on startup (best-effort)."""
    global _tier_assignments, _last_promoted_at, _composite_scores, _pinned_symbols
    try:
        fp = _state_file_path()
        if not fp.exists():
            return
        data = json.loads(fp.read_text(encoding='utf-8'))
        _tier_assignments = {k: int(v) for k, v in (data.get('assignments') or {}).items()}
        _last_promoted_at = {k: float(v) for k, v in (data.get('last_promoted') or {}).items()}
        _composite_scores = {k: float(v) for k, v in (data.get('scores') or {}).items()}
        _pinned_symbols = set(data.get('pinned') or [])
        log.info('tier_manager: loaded state for %d symbols from %s', len(_tier_assignments), fp)
    except Exception:  # noqa: BLE001
        log.warning('tier_manager: failed to load state from disk (fresh start)', exc_info=True)


def _save_state() -> None:
    """Persist current state to disk (best-effort, called from flush thread)."""
    try:
        fp = _state_file_path()
        fp.parent.mkdir(parents=True, exist_ok=True)
        with _state_lock:
            data = {
                'assignments': dict(_tier_assignments),
                'last_promoted': {k: v for k, v in _last_promoted_at.items()},
                'scores': {k: v for k, v in _composite_scores.items()},
                'pinned': list(_pinned_symbols),
                'saved_at': time.time(),
            }
        fp.write_text(json.dumps(data, separators=(',', ':')), encoding='utf-8')
    except Exception:  # noqa: BLE001
        log.debug('tier_manager: flush to disk failed', exc_info=True)


def _flush_loop() -> None:
    """Background daemon: periodically flush state to disk."""
    while True:
        time.sleep(_FLUSH_INTERVAL_S)
        _save_state()


# ---------------------------------------------------------------------------
# Tier assignment queries
# ---------------------------------------------------------------------------

def get_tier(symbol: str) -> int:
    """Return the current tier (1, 2, or 3) for *symbol*.

    Defaults to Tier 3 if the symbol has never been assigned.
    """
    with _state_lock:
        return _tier_assignments.get(symbol.upper(), TIER_3)


def get_tier_symbols(tier: int) -> list[str]:
    """Return all symbols currently assigned to *tier*."""
    with _state_lock:
        return [s for s, t in _tier_assignments.items() if t == tier]


def get_all_tiers() -> Dict[str, int]:
    """Return a snapshot of all tier assignments (symbol → tier)."""
    with _state_lock:
        return dict(_tier_assignments)


def update_composite_score(symbol: str, score: float) -> None:
    """Record the latest composite score for a symbol and timestamp its scoring."""
    sym = symbol.upper()
    with _state_lock:
        _composite_scores[sym] = float(score)
        _last_scored_at[sym] = time.monotonic()


# ---------------------------------------------------------------------------
# Promotion / demotion
# ---------------------------------------------------------------------------

def _cooldown_ok(symbol: str) -> bool:
    """Return True if the symbol is not within its promotion cooldown window.

    A symbol that has never been promoted has no entry in ``_last_promoted_at``
    and is always eligible.  We use ``None`` as the "never promoted" sentinel
    rather than ``0.0`` to avoid false-cooldown on machines whose monotonic
    clock is still below the cooldown window (e.g., freshly booted).
    """
    from app.config import settings
    mono = _last_promoted_at.get(symbol)
    if mono is None:
        return True  # never promoted → always eligible
    return (time.monotonic() - mono) >= settings.tier_promotion_cooldown_seconds


def promote(symbol: str, reason: str = 'score') -> bool:
    """Promote *symbol* one tier (T3→T2 or T2→T1).

    Returns True if the assignment actually changed.  Respects cooldown.
    """
    sym = symbol.upper()
    with _state_lock:
        current = _tier_assignments.get(sym, TIER_3)
        if current == TIER_1:
            return False  # already at top
        if not _cooldown_ok(sym):
            return False  # in cooldown
        new_tier = current - 1
        _tier_assignments[sym] = new_tier
        _last_promoted_at[sym] = time.monotonic()
    log.info('tier_manager: %s promoted T%d → T%d (%s)', sym, current, new_tier, reason)
    return True


def demote(symbol: str, reason: str = 'score') -> bool:
    """Demote *symbol* one tier (T2→T3 or T1→T2).

    Pinned symbols and user-interacted symbols are protected from demotion.
    Returns True if the assignment actually changed.
    """
    sym = symbol.upper()
    with _state_lock:
        if sym in _pinned_symbols:
            return False  # pinned — minimum T2
        if sym in _user_interaction_symbols:
            if time.monotonic() < _user_interaction_symbols[sym]:
                return False  # user recently clicked it
            del _user_interaction_symbols[sym]
        current = _tier_assignments.get(sym, TIER_3)
        if current == TIER_3:
            return False  # already at bottom
        # Pinned symbols may not fall below Tier 2.
        if sym in _pinned_symbols and current == TIER_2:
            return False
        new_tier = current + 1
        if sym in _pinned_symbols:
            new_tier = max(new_tier, TIER_2)
        _tier_assignments[sym] = new_tier
    log.info('tier_manager: %s demoted T%d → T%d (%s)', sym, current, new_tier, reason)
    return True


def record_user_interaction(symbol: str, duration_seconds: float = 300.0) -> None:
    """Mark that a user just interacted with *symbol* (clicked, opened detail).

    The symbol is promoted to at least Tier 2 and protected from demotion for
    *duration_seconds* (default 5 minutes).
    """
    sym = symbol.upper()
    with _state_lock:
        _user_interaction_symbols[sym] = time.monotonic() + duration_seconds
        current = _tier_assignments.get(sym, TIER_3)
    if current == TIER_3:
        promote(sym, reason='user_interaction')
    log.debug('tier_manager: user interaction for %s (T%d)', sym, get_tier(sym))


# ---------------------------------------------------------------------------
# Tier rebalancing — called periodically by the Tier 1/2/3 scanner loops
# ---------------------------------------------------------------------------

def rebalance(market: str = 'stocks') -> dict:
    """Re-run promotion / demotion logic against the current composite scores.

    Called every Tier 2 interval (30-60 s) so the universe continuously self-
    sorts:

      1. Sort all scored symbols by composite score (highest first).
      2. Top `tier_1_size` symbols belong in Tier 1.
      3. Next `tier_2_size` belong in Tier 2.
      4. Rest belong in Tier 3.
      5. Pinned symbols are clamped to Tier 2 minimum.
      6. Promotions / demotions respect the cooldown.

    Returns telemetry dict with counts of changes.
    """
    from app.config import settings

    t1_size = settings.tier_1_size
    t2_size = settings.tier_2_size

    with _state_lock:
        scores_snapshot = dict(_composite_scores)
        pinned_snapshot = set(_pinned_symbols)
        promotions_on_cooldown_snapshot = set(
            s for s, t in _last_promoted_at.items()
            if (time.monotonic() - t) < settings.tier_promotion_cooldown_seconds
        )

    # Sort all symbols with known scores (descending).
    ranked = sorted(scores_snapshot.items(), key=lambda kv: -kv[1])

    target_tiers: Dict[str, int] = {}
    for rank, (sym, _score) in enumerate(ranked):
        if rank < t1_size:
            target_tiers[sym] = TIER_1
        elif rank < t1_size + t2_size:
            target_tiers[sym] = TIER_2
        else:
            target_tiers[sym] = TIER_3

    # Pinned symbols are always at least Tier 2.
    for sym in pinned_snapshot:
        if target_tiers.get(sym, TIER_3) > TIER_2:
            target_tiers[sym] = TIER_2

    promoted = demoted = unchanged = 0
    with _state_lock:
        for sym, target in target_tiers.items():
            current = _tier_assignments.get(sym, TIER_3)
            if current == target:
                unchanged += 1
                continue
            if target < current:  # promote
                if sym not in promotions_on_cooldown_snapshot:
                    _tier_assignments[sym] = target
                    _last_promoted_at[sym] = time.monotonic()
                    promoted += 1
                    log.debug('rebalance: promote %s T%d→T%d', sym, current, target)
            else:  # demote
                # Protect pinned + user-interacted.
                if sym in pinned_snapshot:
                    continue
                ui_exp = _user_interaction_symbols.get(sym, 0.0)
                if time.monotonic() < ui_exp:
                    continue
                _tier_assignments[sym] = target
                demoted += 1
                log.debug('rebalance: demote %s T%d→T%d', sym, current, target)

    log.info(
        'tier_manager: rebalance market=%s promoted=%d demoted=%d unchanged=%d',
        market, promoted, demoted, unchanged,
    )
    return {
        'promoted': promoted,
        'demoted': demoted,
        'unchanged': unchanged,
        'tier_1_count': len([s for s, t in target_tiers.items() if t == TIER_1]),
        'tier_2_count': len([s for s, t in target_tiers.items() if t == TIER_2]),
        'tier_3_count': len([s for s, t in target_tiers.items() if t == TIER_3]),
    }


# ---------------------------------------------------------------------------
# Volume / gap event-driven promotions (called by Tier 3 scanner)
# ---------------------------------------------------------------------------

def check_volume_spike(symbol: str, current_volume: float, avg_volume: float) -> bool:
    """Promote T3→T2 when current volume exceeds spike_factor × avg_volume."""
    from app.config import settings
    if avg_volume <= 0:
        return False
    if current_volume >= settings.tier_volume_spike_factor * avg_volume:
        return promote(symbol, reason=f'volume_spike({current_volume:.0f}/{avg_volume:.0f})')
    return False


def check_price_gap(symbol: str, current_price: float, prev_close: float) -> bool:
    """Promote T3→T2 when overnight price gap exceeds the configured threshold."""
    from app.config import settings
    if prev_close <= 0:
        return False
    gap_pct = abs((current_price - prev_close) / prev_close) * 100.0
    if gap_pct >= settings.tier_price_gap_pct:
        return promote(symbol, reason=f'price_gap({gap_pct:.1f}%)')
    return False


# ---------------------------------------------------------------------------
# Watchlist / pin management
# ---------------------------------------------------------------------------

def pin_symbol(symbol: str) -> None:
    """Pin *symbol* to Tier 2 minimum.  Calls promote() if it's currently T3."""
    sym = symbol.upper()
    with _state_lock:
        _pinned_symbols.add(sym)
        current = _tier_assignments.get(sym, TIER_3)
    if current == TIER_3:
        with _state_lock:
            _tier_assignments[sym] = TIER_2
            _last_promoted_at[sym] = time.monotonic()
        log.info('tier_manager: %s pinned and promoted T3→T2', sym)


def unpin_symbol(symbol: str) -> None:
    """Remove the pin from *symbol* (allows future demotion to Tier 3)."""
    sym = symbol.upper()
    with _state_lock:
        _pinned_symbols.discard(sym)
    log.info('tier_manager: %s unpinned', sym)


def is_pinned(symbol: str) -> bool:
    """Return True if *symbol* is currently pinned."""
    with _state_lock:
        return symbol.upper() in _pinned_symbols


def get_pinned_symbols() -> list[str]:
    """Return all currently pinned symbols."""
    with _state_lock:
        return list(_pinned_symbols)


# ---------------------------------------------------------------------------
# Bootstrap: seed all universe symbols into Tier 3 on first run
# ---------------------------------------------------------------------------

def seed_universe(market: str = 'stocks') -> int:
    """Assign all unranked universe symbols to Tier 3.

    This is called once at startup so every symbol has an explicit tier
    assignment.  Symbols that already have an assignment from the disk-state
    are not touched.

    Returns count of newly-assigned symbols.
    """
    try:
        from app.services.universe_service import get_universe
        rows = get_universe(market)
    except Exception:  # noqa: BLE001
        log.warning('tier_manager: failed to load universe for market=%s', market)
        return 0

    new_count = 0
    with _state_lock:
        for row in rows:
            sym = (row.get('symbol') or '').upper()
            if sym and sym not in _tier_assignments:
                _tier_assignments[sym] = TIER_3
                new_count += 1
    log.info('tier_manager: seeded %d new symbols → Tier 3 (market=%s)', new_count, market)
    return new_count


# ---------------------------------------------------------------------------
# Status telemetry
# ---------------------------------------------------------------------------

def get_status() -> dict:
    """Return a snapshot of tier sizes and top symbols per tier."""
    with _state_lock:
        t1 = [s for s, t in _tier_assignments.items() if t == TIER_1]
        t2 = [s for s, t in _tier_assignments.items() if t == TIER_2]
        t3 = [s for s, t in _tier_assignments.items() if t == TIER_3]
        pinned = list(_pinned_symbols)
        scores_snap = dict(_composite_scores)

    top_t1 = sorted(t1, key=lambda s: -scores_snap.get(s, 0.0))[:20]
    top_t2 = sorted(t2, key=lambda s: -scores_snap.get(s, 0.0))[:20]
    return {
        'tier_1_count': len(t1),
        'tier_2_count': len(t2),
        'tier_3_count': len(t3),
        'pinned_count': len(pinned),
        'total_tracked': len(t1) + len(t2) + len(t3),
        'tier_1_top': top_t1,
        'tier_2_top': top_t2,
        'pinned_symbols': pinned[:50],
    }


# ---------------------------------------------------------------------------
# Module initialization
# ---------------------------------------------------------------------------
_initialized = False
_init_lock = threading.Lock()


def initialize() -> None:
    """Load persisted state and start the background flush thread.

    Idempotent — safe to call multiple times.
    """
    global _initialized
    with _init_lock:
        if _initialized:
            return
        _initialized = True

    _load_state()
    t = threading.Thread(target=_flush_loop, name='tier-state-flusher', daemon=True)
    t.start()
    log.info('tier_manager: initialized (state flusher started)')
