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
