"""Display/redistribution rights guardrail.

Default is deny: data may be shown publicly only if an explicit entitlement
exists for the provider+kind. This module never alters or bypasses provider
licensing; it only refuses.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .contracts import DataKind


@dataclass(frozen=True)
class Entitlement:
    provider: str
    kind: DataKind
    public_display: bool = False
    redistribution: bool = False
    verified_by: str = ""  # who verified the license terms (must be non-empty to count)


class EntitlementRegistry:
    def __init__(self, entitlements: Mapping[tuple[str, DataKind], Entitlement] | None = None):
        self._e = dict(entitlements or {})

    def register(self, ent: Entitlement) -> None:
        self._e[(ent.provider, ent.kind)] = ent

    def may_display_publicly(self, provider: str, kind: DataKind) -> bool:
        e = self._e.get((provider, kind))
        return bool(e and e.public_display and e.verified_by)

    def may_redistribute(self, provider: str, kind: DataKind) -> bool:
        e = self._e.get((provider, kind))
        return bool(e and e.redistribution and e.verified_by)


def registry_from_config(spec: str) -> EntitlementRegistry:
    """Parse ``provider:kind:verified_by[,...]`` (public display only).

    Empty/invalid entries grant nothing; unknown kinds are ignored (deny).
    """
    reg = EntitlementRegistry()
    for item in (spec or '').split(','):
        parts = [p.strip() for p in item.split(':')]
        if len(parts) != 3 or not all(parts):
            continue
        try:
            kind = DataKind(parts[1])
        except ValueError:
            continue
        reg.register(Entitlement(parts[0], kind, public_display=True, verified_by=parts[2]))
    return reg
