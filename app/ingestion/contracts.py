"""Normalized ingestion contracts.

Analytics must consume these records, never raw provider payloads.
A value is never represented by a bare ``None``/``0``: every field carries a
``DataState`` so unsupported, unavailable, stale and legitimate-zero differ.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Generic, Optional, TypeVar

T = TypeVar("T")


class DataState(str, Enum):
    OK = "ok"                    # value present and fresh (zero is a valid value)
    ZERO = "zero"                # legitimate zero reported by the provider
    UNSUPPORTED = "unsupported"  # provider/asset class can never supply it
    UNAVAILABLE = "unavailable"  # supported, but missing/failed right now
    STALE = "stale"              # value present but older than allowed


class DataKind(str, Enum):
    QUOTE = "quote"
    BAR = "ohlcv_bar"
    FUNDAMENTALS = "fundamentals"
    OPTIONS_CHAIN = "options_chain"
    SHORT_INTEREST = "short_interest"
    SESSION = "market_session"


class AdjustmentMode(str, Enum):
    RAW = "raw"
    SPLIT_ADJUSTED = "split_adjusted"
    SPLIT_DIVIDEND_ADJUSTED = "split_dividend_adjusted"
    UNKNOWN = "unknown"


class DataQuality(str, Enum):
    GOOD = "good"
    DELAYED = "delayed"
    DEGRADED = "degraded"
    UNKNOWN = "unknown"


class TimestampSemantics(str, Enum):
    EXCHANGE_UTC = "exchange_utc"
    PROVIDER_UTC = "provider_utc"
    AMBIGUOUS = "ambiguous"  # provider did not state tz/meaning; never trusted for ordering


@dataclass(frozen=True)
class IngestionMeta:
    """Metadata preserved on every normalized record (None = not provided)."""
    provider: str
    kind: DataKind
    ingested_at: float = field(default_factory=time.time)
    source_ts: Optional[float] = None
    timestamp_semantics: TimestampSemantics = TimestampSemantics.AMBIGUOUS
    interval: Optional[str] = None
    adjustment: AdjustmentMode = AdjustmentMode.UNKNOWN
    currency: Optional[str] = None
    venue: Optional[str] = None
    quality: DataQuality = DataQuality.UNKNOWN
    source_revision: Optional[str] = None


@dataclass(frozen=True)
class Field(Generic[T]):
    state: DataState
    value: Optional[T] = None
    reason: str = ""

    @property
    def usable(self) -> bool:
        return self.state in (DataState.OK, DataState.ZERO)

    @classmethod
    def of(cls, value: Optional[T], *, max_age_s: Optional[float] = None,
           age_s: Optional[float] = None) -> "Field[T]":
        """Classify a provider value. ``None`` -> UNAVAILABLE; 0 -> ZERO."""
        if value is None:
            return cls(DataState.UNAVAILABLE, None, "missing")
        if max_age_s is not None and age_s is not None and age_s > max_age_s:
            return cls(DataState.STALE, value, f"age {age_s:.0f}s > {max_age_s:.0f}s")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value == 0:
            return cls(DataState.ZERO, value)
        return cls(DataState.OK, value)

    @classmethod
    def unsupported(cls, reason: str = "") -> "Field[T]":
        return cls(DataState.UNSUPPORTED, None, reason)

    @classmethod
    def unavailable(cls, reason: str = "") -> "Field[T]":
        return cls(DataState.UNAVAILABLE, None, reason)


@dataclass(frozen=True)
class Quote:
    meta: IngestionMeta
    symbol: str
    price: Field[float]
    volume: Field[float] = field(default_factory=lambda: Field.unavailable())


@dataclass(frozen=True)
class OHLCVBar:
    meta: IngestionMeta
    symbol: str
    open_ts: float
    open: float
    high: float
    low: float
    close: float
    volume: Field[float]
    complete: bool  # False = forming bar; analytics must not treat as final
    corporate_action_note: Optional[str] = None


@dataclass(frozen=True)
class Fundamentals:
    meta: IngestionMeta
    symbol: str
    fields: dict[str, Field[Any]]


@dataclass(frozen=True)
class OptionContract:
    strike: float
    expiry: str
    right: str  # "call" | "put"
    bid: Field[float]
    ask: Field[float]
    open_interest: Field[float]
    implied_vol: Field[float]


@dataclass(frozen=True)
class OptionsChain:
    meta: IngestionMeta
    symbol: str
    contracts: tuple[OptionContract, ...]


@dataclass(frozen=True)
class ShortInterest:
    meta: IngestionMeta
    symbol: str
    short_interest: Field[float]
    days_to_cover: Field[float]
    short_pct_float: Field[float]


@dataclass(frozen=True)
class MarketSession:
    meta: IngestionMeta
    market: str  # "equity" | "crypto"
    is_open: bool
    continuous: bool
    reason: str = ""  # e.g. "holiday", "weekend", "24x7"


class IncompatibleSeriesError(ValueError):
    """Raised instead of silently blending providers/adjustments/intervals."""


def assert_compatible(a: IngestionMeta, b: IngestionMeta) -> None:
    for attr in ("provider", "adjustment", "interval", "currency"):
        if getattr(a, attr) != getattr(b, attr):
            raise IncompatibleSeriesError(
                f"cannot blend records with different {attr}: "
                f"{getattr(a, attr)!r} vs {getattr(b, attr)!r}")
