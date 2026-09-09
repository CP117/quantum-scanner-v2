"""Offline baseline profiler for cache-migration decisions.

Run with ``python scripts/profile_cache_baseline.py --output profile.json``.
The fixture provider has no credentials or network dependency. Results measure
the real cheap scoring function and lightweight Tier 1/2/3 orchestration
shapes; they are a reproducible comparison point for later cache migrations.
"""
from __future__ import annotations

import argparse
import cProfile
import gc
import json
import os
import pstats
import sys
import threading
import time
import tracemalloc
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


@dataclass
class Measurement:
    name: str
    iterations: int
    wall_seconds: float
    cpu_seconds: float
    tracemalloc_current_bytes: int
    tracemalloc_peak_bytes: int
    gc_collections_delta: list[int]
    provider_calls: int
    provider_failures: int
    top_cpu_functions: list[dict[str, Any]]


class FixtureProvider:
    """Deterministic provider fake with optional repeatable failure."""

    def __init__(self, fail_every: int = 0):
        self.calls = 0
        self.failures = 0
        self.fail_every = fail_every

    def quote(self, symbol: str) -> dict[str, float]:
        self.calls += 1
        if self.fail_every and self.calls % self.fail_every == 0:
            self.failures += 1
            raise RuntimeError("fixture provider failure")
        offset = sum(ord(char) for char in symbol) % 10
        return {
            "last_price": 100.0 + offset,
            "previous_close": 99.0 + offset,
            "open": 99.5 + offset,
            "day_low": 98.5 + offset,
            "day_high": 101.5 + offset,
            "volume": 2_000_000 + offset,
            "averageVolume": 1_000_000,
        }

    def reset_metrics(self) -> None:
        self.calls = 0
        self.failures = 0


def _top_functions(profile: cProfile.Profile, limit: int = 8) -> list[dict[str, Any]]:
    stats = pstats.Stats(profile)
    rows = sorted(stats.stats.items(), key=lambda item: item[1][3], reverse=True)[:limit]
    return [
        {
            "function": f"{filename}:{line}({function})",
            "calls": calls[0],
            "cumulative_seconds": round(calls[3], 6),
        }
        for (filename, line, function), calls in rows
    ]


def measure(
    name: str,
    operation: Callable[[], None],
    *,
    iterations: int,
    provider: FixtureProvider,
) -> Measurement:
    """Measure CPU, wall time, allocations, GC, and fake-provider activity."""
    gc.collect()
    gc_before = gc.get_stats()
    profile = cProfile.Profile()
    tracemalloc.start()
    wall_start = time.perf_counter()
    cpu_start = time.process_time()
    profile.enable()
    for _ in range(iterations):
        operation()
    profile.disable()
    cpu_seconds = time.process_time() - cpu_start
    wall_seconds = time.perf_counter() - wall_start
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    gc_after = gc.get_stats()
    return Measurement(
        name=name,
        iterations=iterations,
        wall_seconds=round(wall_seconds, 6),
        cpu_seconds=round(cpu_seconds, 6),
        tracemalloc_current_bytes=current,
        tracemalloc_peak_bytes=peak,
        gc_collections_delta=[
            gc_after[index]["collections"] - gc_before[index]["collections"]
            for index in range(3)
        ],
        provider_calls=provider.calls,
        provider_failures=provider.failures,
        top_cpu_functions=_top_functions(profile),
    )


def _score_operation(
    provider: FixtureProvider,
    cache: dict[str, dict[str, float]],
    symbol: str,
    *,
    cache_success: bool = True,
) -> Callable[[], None]:
    from app.services.scoring_service import score_from_prices

    row = {"symbol": symbol, "name": f"{symbol} fixture", "exchange": "FIXTURE"}

    def operation() -> None:
        quote = cache.get(symbol)
        if quote is None:
            try:
                quote = provider.quote(symbol)
            except RuntimeError:
                return
            if cache_success:
                cache[symbol] = quote
        score_from_prices(
            row, quote["last_price"], quote["previous_close"], "fixture", 0,
            "2026-09-09T00:00:00+00:00", fundamentals_info=quote, score_depth="cheap",
        )

    return operation


def _tier_orchestration_operation(symbols: list[str]) -> Callable[[], None]:
    """Measure the same row-shaping work performed by Tier scanner cycles."""
    rows = [{"symbol": symbol, "_tier": 1} for symbol in symbols]

    def operation() -> None:
        tier1 = [dict(row) for row in rows]
        tier2 = [dict(row, _tier=2) for row in rows]
        tier3 = [{"symbol": row["symbol"], "_tier": 3} for row in rows]
        assert len(tier1) == len(tier2) == len(tier3)

    return operation


def concurrent_same_symbol(provider: FixtureProvider, workers: int = 8) -> dict[str, int]:
    """Baseline duplicate-fetch count before per-key single-flight exists."""
    start = threading.Barrier(workers)

    def fetch() -> None:
        start.wait()
        provider.quote("AAPL")

    threads = [threading.Thread(target=fetch) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return {"workers": workers, "provider_calls": provider.calls}


def run_baseline(iterations: int) -> dict[str, Any]:
    cold_provider = FixtureProvider()
    cold_symbols = [f"FIX{index:03d}" for index in range(iterations)]
    cold_index = 0

    def cold_operation() -> None:
        nonlocal cold_index
        symbol = cold_symbols[cold_index]
        cold_index += 1
        _score_operation(cold_provider, {}, symbol)()

    cold = measure(
        "cold_cache_tier1_score",
        cold_operation,
        iterations=iterations,
        provider=cold_provider,
    )
    warm_provider = FixtureProvider()
    warm_cache = {symbol: warm_provider.quote(symbol) for symbol in cold_symbols}
    warm_provider.reset_metrics()
    warm_index = 0

    def warm_operation() -> None:
        nonlocal warm_index
        symbol = cold_symbols[warm_index]
        warm_index += 1
        _score_operation(warm_provider, warm_cache, symbol)()

    warm = measure(
        "warm_cache_tier1_score",
        warm_operation,
        iterations=iterations,
        provider=warm_provider,
    )
    failure_provider = FixtureProvider(fail_every=2)
    failures = measure(
        "provider_failure_score",
        _score_operation(failure_provider, {}, "MSFT", cache_success=False),
        iterations=iterations,
        provider=failure_provider,
    )
    orchestration_provider = FixtureProvider()
    orchestration = measure(
        "tier1_tier2_tier3_row_orchestration",
        _tier_orchestration_operation(["AAPL", "MSFT", "NVDA", "TSLA", "AMZN"]),
        iterations=iterations,
        provider=orchestration_provider,
    )
    concurrent_provider = FixtureProvider()
    return {
        "metadata": {
            "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "python": os.sys.version,
            "network": "disabled; deterministic fixture provider",
            "scope": "baseline profiling gate",
        },
        "measurements": [asdict(item) for item in (cold, warm, failures, orchestration)],
        "concurrent_same_symbol": concurrent_same_symbol(concurrent_provider),
        "decision_notes": [
            "Use cache migration only where the warm path avoids material provider, disk, or CPU work.",
            "The concurrent duplicate-fetch count is the baseline for later single-flight validation.",
            "This harness does not benchmark live provider latency, durable disk I/O, or RSS; capture those in staging before enabling a domain.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=50)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error("--iterations must be positive")
    report = run_baseline(args.iterations)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
