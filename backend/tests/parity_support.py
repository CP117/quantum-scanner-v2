"""Test-only helpers for deterministic parity fixtures and opt-in baselines."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "parity"
BASELINE_DIR = FIXTURE_DIR / "baselines"
_SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_MARKETS = {"stocks", "crypto"}


def load_market_fixture(market: str) -> dict[str, Any]:
    """Load a fresh copy of the checked-in synthetic OHLCV fixture."""
    if market not in _MARKETS:
        raise ValueError(f"unsupported fixture market: {market}")
    return json.loads((FIXTURE_DIR / f"{market}.json").read_text(encoding="utf-8"))


def canonical_json(value: Any) -> str:
    """Serialize JSON-compatible output with stable key order and no NaN."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def load_baseline(name: str, *, directory: Path | None = None) -> Any:
    """Load an explicitly captured baseline from the test fixture directory."""
    if not _SAFE_NAME.fullmatch(name):
        raise ValueError("baseline name must contain only lowercase letters, digits, '_' or '-'")
    path = (directory or BASELINE_DIR) / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def capture_baseline(
    name: str,
    output: Any,
    *,
    enabled: bool | None = None,
    directory: Path | None = None,
) -> Path | None:
    """Create a new test baseline only when explicitly enabled; never overwrite.

    Callers are responsible for passing sanitized, deterministic test output.
    This helper never invokes a provider or production code path.
    """
    if not _SAFE_NAME.fullmatch(name):
        raise ValueError("baseline name must contain only lowercase letters, digits, '_' or '-'")
    if enabled is None:
        enabled = os.environ.get("QS_CAPTURE_PARITY_BASELINES") == "1"
    if not enabled:
        return None

    target_dir = directory or BASELINE_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{name}.json"
    with path.open("x", encoding="utf-8", newline="\n") as baseline:
        baseline.write(canonical_json(output))
        baseline.write("\n")
    return path
