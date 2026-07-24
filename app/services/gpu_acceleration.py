"""
GPU Acceleration — Phase 28
============================

Provides GPU-accelerated batch scoring for Tier 3 hourly universe scans.
Uses CuPy when a CUDA GPU is available; falls back transparently to NumPy
so the same call-sites work on CPU-only machines.

At startup the module attempts to import CuPy.  If that succeeds AND at least
one CUDA device is reported, ``GPU_AVAILABLE`` is set to ``True`` and all
heavy operations are dispatched to the GPU.

On a CPU-only rig ``GPU_AVAILABLE`` stays ``False`` and the Tier 3 scanner
operates in once-daily mode instead of hourly (see tier_3_background_scanner).

Public API
----------
    ``GPU_AVAILABLE: bool``
    ``batch_compute_scores(rows: list[dict]) -> list[float]``
        Lightweight composite score for each row in *rows*.  Returns a list
        of floats in the same order.  Uses vectorised NumPy/CuPy operations.
    ``batch_normalise_features(matrix: np.ndarray) -> np.ndarray``
        Row-wise z-score normalisation; GPU-accelerated when available.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import numpy as np

log = logging.getLogger('app.gpu_acceleration')

# ---------------------------------------------------------------------------
# GPU detection
# ---------------------------------------------------------------------------
GPU_AVAILABLE: bool = False
_xp: Any = np  # computational module: cupy when GPU is live, else numpy


def _detect_gpu() -> bool:
    """Return True if a usable CuPy + CUDA GPU combo is found at startup."""
    try:
        import cupy as cp  # type: ignore[import]
        n = cp.cuda.runtime.getDeviceCount()
        if n > 0:
            return True
        return False
    except Exception:
        return False


def _init_gpu() -> None:
    """One-time GPU initialisation.  Called from ``initialize()``."""
    global GPU_AVAILABLE, _xp
    # Honour the config override first.
    try:
        from app.config import settings
        if not settings.gpu_enabled:
            log.info('gpu_acceleration: GPU disabled by config (GPU_ENABLED=false)')
            return
    except Exception:  # noqa: BLE001
        pass

    if _detect_gpu():
        import cupy as cp  # type: ignore[import]  # noqa: PLC0415
        _xp = cp
        GPU_AVAILABLE = True
        log.info('gpu_acceleration: CuPy GPU backend ACTIVE')
    else:
        log.info('gpu_acceleration: no CUDA GPU found — using NumPy CPU backend')


# ---------------------------------------------------------------------------
# Feature extraction helpers
# ---------------------------------------------------------------------------

def _extract_features(rows: list[dict]) -> np.ndarray:
    """Extract a fixed numeric feature vector from each scored row.

    Features (all normalised to [0, 1] range where possible):
        0  final_score          (0-100 composite)
        1  change_pct           (capped ±20 %)
        2  volume_ratio         (volume / avg_volume, capped at 10)
        3  options_bias         (0=bear, 0.5=neutral, 1=bull)
        4  institutional_score  (0-100)
        5  trend_volume_delta   (0-100)
        6  momentum             (derived from change_pct sign)
        7  age_seconds          (freshness, inverted)

    Returns an (N, 8) float32 array.
    """
    N = len(rows)
    mat = np.zeros((N, 8), dtype=np.float32)
    for i, row in enumerate(rows):
        fb = row.get('factor_breakdown') or {}
        mkt = fb.get('market') or {}

        mat[i, 0] = min(float(row.get('final_score') or 0), 100.0) / 100.0

        chg = float(row.get('change_pct') or mkt.get('change_pct') or 0)
        mat[i, 1] = (max(-20.0, min(20.0, chg)) + 20.0) / 40.0

        vol = float(mkt.get('volume') or 0)
        avg_vol = float(row.get('averageVolume') or 1)
        mat[i, 2] = min(vol / max(avg_vol, 1), 10.0) / 10.0

        # options bias: map bull/bear to 1/0 via string check
        opts = (mkt.get('options_positioning') or {})
        opts_bias = str(opts.get('bias') or 'neutral').lower()
        if 'bull' in opts_bias:
            mat[i, 3] = 1.0
        elif 'bear' in opts_bias:
            mat[i, 3] = 0.0
        else:
            mat[i, 3] = 0.5

        ic = mkt.get('institutional_confluence') or {}
        mat[i, 4] = min(float(ic.get('score') or 0), 100.0) / 100.0

        tvd = mkt.get('trend_volume_delta') or {}
        mat[i, 5] = min(float(tvd.get('score') or 0), 100.0) / 100.0

        mat[i, 6] = 1.0 if chg > 0 else 0.0

        age = float(row.get('age_seconds') or mkt.get('age_seconds') or 86400)
        mat[i, 7] = max(0.0, 1.0 - min(age, 86400.0) / 86400.0)

    return mat


# ---------------------------------------------------------------------------
# Core GPU-accelerated operations
# ---------------------------------------------------------------------------

def batch_normalise_features(matrix: np.ndarray) -> np.ndarray:
    """Row-wise z-score normalisation.

    Operates on GPU when ``GPU_AVAILABLE`` is True, otherwise CPU.
    Returns an array of the same dtype on CPU (CuPy arrays are moved back
    via ``.get()`` so callers don't need to know which backend is active).
    """
    xp = _xp
    if GPU_AVAILABLE:
        import cupy as cp  # type: ignore[import]
        arr = cp.asarray(matrix)
    else:
        arr = matrix.copy()

    mean = xp.mean(arr, axis=0, keepdims=True)
    std = xp.std(arr, axis=0, keepdims=True)
    std[std == 0] = 1.0
    result = (arr - mean) / std

    if GPU_AVAILABLE:
        return result.get()  # type: ignore[union-attr]  # back to CPU numpy
    return result  # type: ignore[return-value]


def batch_compute_scores(rows: list[dict]) -> list[float]:
    """Compute a lightweight composite score for each row.

    Suitable for Tier 3 minimal-scoring purposes — much faster than the full
    ``score_from_prices`` pipeline.

    Algorithm:
      1. Extract 8 numeric features per row.
      2. Normalise features column-wise (z-score).
      3. Compute weighted dot product per row (weights tuned for Tier 3 ranking).
      4. Clamp to [0, 100] and return.

    If the input list is empty, returns an empty list.
    """
    if not rows:
        return []

    t0 = time.monotonic()
    features = _extract_features(rows)  # (N, 8) float32
    features = batch_normalise_features(features)

    # Weights for [final_score, change_pct, volume_ratio, options, institutional,
    #              tvd, momentum, freshness]
    weights = np.array([0.35, 0.15, 0.10, 0.10, 0.10, 0.10, 0.05, 0.05], dtype=np.float32)
    raw_scores = features.dot(weights)  # (N,)

    # Re-scale from z-score space to [0, 100] using a sigmoid-like mapping.
    sigmoid = 1.0 / (1.0 + np.exp(-raw_scores))  # (N,) in (0, 1)
    scores = (sigmoid * 100.0).tolist()

    elapsed = time.monotonic() - t0
    backend = 'gpu' if GPU_AVAILABLE else 'cpu'
    log.debug(
        'gpu_acceleration: batch_compute_scores rows=%d backend=%s elapsed=%.3fs',
        len(rows), backend, elapsed,
    )
    return scores


# ---------------------------------------------------------------------------
# Module initialisation
# ---------------------------------------------------------------------------
_initialized = False
_init_lock_obj = __import__('threading').Lock()


def initialize() -> None:
    """Detect GPU and configure the compute backend.  Idempotent."""
    global _initialized
    with _init_lock_obj:
        if _initialized:
            return
        _initialized = True
    _init_gpu()
