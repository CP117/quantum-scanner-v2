"""Bounded batching, retry/backoff, rate-limit budgets and provider health."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from collections import deque
from typing import Callable, Iterable, Iterator, Sequence, TypeVar

T = TypeVar("T")
R = TypeVar("R")

MAX_BATCH = 200
MAX_RETRIES = 5
MAX_BACKOFF_S = 30.0


def batched(items: Sequence[T], size: int) -> Iterator[Sequence[T]]:
    size = max(1, min(int(size), MAX_BATCH))
    for i in range(0, len(items), size):
        yield items[i:i + size]


def backoff_delay(attempt: int, base: float = 0.5, cap: float = MAX_BACKOFF_S) -> float:
    """Deterministic exponential backoff, capped (jitter left to caller)."""
    return min(cap, base * (2 ** max(0, attempt)))


class RateLimitBudget:
    """Sliding-window budget shared per provider (thread-safe)."""

    def __init__(self, max_calls: int, window_s: float, clock: Callable[[], float] = time.monotonic):
        self.max_calls, self.window_s, self._clock = max(1, max_calls), window_s, clock
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def try_acquire(self) -> bool:
        with self._lock:
            now = self._clock()
            while self._calls and now - self._calls[0] >= self.window_s:
                self._calls.popleft()
            if len(self._calls) >= self.max_calls:
                return False
            self._calls.append(now)
            return True


class ProviderHealth:
    """Tracks consecutive failures; opens a circuit for a cool-down."""

    def __init__(self, failure_threshold: int = 3, cooldown_s: float = 60.0,
                 clock: Callable[[], float] = time.monotonic):
        self.failure_threshold, self.cooldown_s, self._clock = failure_threshold, cooldown_s, clock
        self.consecutive_failures = 0
        self.last_error: str | None = None
        self._opened_at: float | None = None
        self._lock = threading.Lock()

    def record_success(self) -> None:
        with self._lock:
            self.consecutive_failures, self._opened_at = 0, None

    def record_failure(self, err: str = "") -> None:
        with self._lock:
            self.consecutive_failures += 1
            self.last_error = err
            if self.consecutive_failures >= self.failure_threshold:
                self._opened_at = self._clock()

    @property
    def available(self) -> bool:
        with self._lock:
            if self._opened_at is None:
                return True
            if self._clock() - self._opened_at >= self.cooldown_s:
                return True  # half-open: allow a probe
            return False


class FetchRefused(RuntimeError):
    pass


def fetch_with_retry(fn: Callable[[], R], *, budget: RateLimitBudget, health: ProviderHealth,
                     max_retries: int = 3, sleep: Callable[[float], None] = time.sleep) -> R:
    """Run ``fn`` within budget/health limits; retries are hard-bounded."""
    retries = max(0, min(max_retries, MAX_RETRIES))
    last: Exception | None = None
    for attempt in range(retries + 1):
        if not health.available:
            raise FetchRefused("provider circuit open")
        if not budget.try_acquire():
            raise FetchRefused("rate-limit budget exhausted")
        try:
            result = fn()
            health.record_success()
            return result
        except Exception as exc:  # noqa: BLE001 - provider errors are arbitrary
            last = exc
            health.record_failure(str(exc))
            if attempt < retries:
                sleep(backoff_delay(attempt))
    raise FetchRefused(f"retries exhausted: {last}") from last


@dataclass(frozen=True)
class FailoverResult:
    provider: str
    value: object
    attempts: tuple  # ((provider, "ok" | error text), ...) in order tried
    failed_over: bool


def fetch_with_failover(chain, *, budgets, healths, max_retries: int = 2,
                        sleep: Callable[[float], None] = time.sleep) -> FailoverResult:
    """Try ``chain`` = [(provider_name, fn), ...] strictly in the given order.

    Failover is explicit: the result names the provider that actually served
    the data (never merged with another) and lists every attempt. If all
    providers fail, ``FetchRefused`` is raised.
    """
    attempts = []
    for i, (name, fn) in enumerate(chain):
        try:
            value = fetch_with_retry(fn, budget=budgets[name], health=healths[name],
                                     max_retries=max_retries, sleep=sleep)
        except FetchRefused as exc:
            attempts.append((name, str(exc)))
            continue
        attempts.append((name, "ok"))
        return FailoverResult(name, value, tuple(attempts), i > 0)
    raise FetchRefused("all providers failed: " + "; ".join(f"{n}: {e}" for n, e in attempts))
