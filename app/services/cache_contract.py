"""Freshness and provenance requirements for cache-domain migrations."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FreshnessContract:
    domain: str
    freshness_rule: str
    stale_fallback_rule: str
    required_dimensions: tuple[str, ...]
    required_provenance: tuple[str, ...]


CONTRACTS = {
    "quotes": FreshnessContract(
        "quotes", "Provider/session-appropriate short TTL", "Only stale-if-error where existing behavior permits it",
        ("symbol", "provider_id"), ("provider_id", "source_timestamp"),
    ),
    "daily_history": FreshnessContract(
        "daily_history", "Latest validated completed bar", "Serve validated completed bars during refresh",
        ("symbol", "provider_id", "interval", "lookback", "adjustment_mode"),
        ("provider_id", "source_timestamp", "interval", "adjustment_mode"),
    ),
    "options_chains": FreshnessContract(
        "options_chains", "Provider-aware short TTL", "Bounded stale-if-error only if current path allows it",
        ("symbol", "provider_id", "expiration_selection", "filters"),
        ("provider_id", "source_timestamp", "expiration_selection"),
    ),
    "universe_metadata": FreshnessContract(
        "universe_metadata", "Static/slow source TTL", "Use current in-process source only during a refresh",
        ("source", "universe_definition"), ("source_timestamp",),
    ),
    "scanner_presets": FreshnessContract(
        "scanner_presets", "Preset content/configuration fingerprint", "Recompile declarative filters",
        ("preset", "content_fingerprint", "config_fingerprint"), (),
    ),
    "score_components": FreshnessContract(
        "score_components", "Compatible material-input fingerprint", "Never serve incomplete calculations",
        ("symbol", "scoring_version"), ("provider_id", "source_timestamp", "fingerprint"),
    ),
    "composite_scores": FreshnessContract(
        "composite_scores", "Oldest compatible material input", "Never represent a partial score as complete",
        ("symbol", "scoring_version"), ("provider_id", "source_timestamp", "fingerprint"),
    ),
    "bayesian_priors": FreshnessContract(
        "bayesian_priors", "Validated source history and model-config fingerprint", "Never serve incomplete calibration",
        ("symbol", "segment", "model_version", "horizon", "is_intraday"),
        ("model_version", "model_config_fingerprint", "history_fingerprint", "history_validation"),
    ),
    "narratives": FreshnessContract(
        "narratives", "Complete factor inputs and template-config fingerprint", "Regenerate on incomplete inputs",
        ("template_version",), ("template_version", "template_config_fingerprint", "input_validation"),
    ),
}

_SENSITIVE_PROVENANCE_KEYS = {"authorization", "cookie", "headers", "api_key", "password", "token"}


def contract_for(domain: str) -> FreshnessContract | None:
    return CONTRACTS.get(domain)


def missing_dimensions(domain: str, dimensions: dict[str, Any]) -> tuple[str, ...]:
    """Return missing mandatory key dimensions for a domain migration."""
    contract = contract_for(domain)
    if contract is None:
        return ()
    return tuple(key for key in contract.required_dimensions if dimensions.get(key) in (None, ""))


def sanitize_provenance(provenance: dict[str, Any] | None) -> dict[str, Any]:
    """Keep only non-sensitive provenance suitable for process-local metadata."""
    return {
        key: value
        for key, value in (provenance or {}).items()
        if key.lower() not in _SENSITIVE_PROVENANCE_KEYS
    }
