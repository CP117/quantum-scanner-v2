"""Session, bar-completeness and ordering semantics."""
from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Iterable, Optional

from .contracts import IngestionMeta, MarketSession, DataKind, OHLCVBar, TimestampSemantics

# Minimal holiday list is caller-supplied; we do not guess exchange calendars.
_ET_OFFSET = timedelta(hours=-5)  # standard time; DST handled by caller-supplied hours


def equity_session(now_utc: datetime, holidays: Iterable[date] = (), provider: str = "calendar",
                   open_utc: dtime = dtime(14, 30), close_utc: dtime = dtime(21, 0)) -> MarketSession:
    """Regular-hours US equity session. Open/close in UTC are caller-overridable (DST)."""
    meta = IngestionMeta(provider=provider, kind=DataKind.SESSION,
                         ingested_at=now_utc.timestamp(), source_ts=now_utc.timestamp(),
                         timestamp_semantics=TimestampSemantics.PROVIDER_UTC)
    d = now_utc.date()
    if d in set(holidays):
        return MarketSession(meta, "equity", False, False, "holiday")
    if now_utc.weekday() >= 5:
        return MarketSession(meta, "equity", False, False, "weekend")
    is_open = open_utc <= now_utc.time() < close_utc
    return MarketSession(meta, "equity", is_open, False, "regular" if is_open else "closed")


def crypto_session(now_utc: datetime, provider: str = "calendar") -> MarketSession:
    meta = IngestionMeta(provider=provider, kind=DataKind.SESSION,
                         ingested_at=now_utc.timestamp(), source_ts=now_utc.timestamp(),
                         timestamp_semantics=TimestampSemantics.PROVIDER_UTC)
    return MarketSession(meta, "crypto", True, True, "24x7")


def bar_is_complete(open_ts: float, interval_s: int, now_ts: float) -> bool:
    """A bar is complete only once its full interval has elapsed."""
    return now_ts >= open_ts + interval_s


def finalize_bars(bars: Iterable[OHLCVBar]) -> list[OHLCVBar]:
    """Return only completed bars, ordered by open time, de-duplicated.

    Later ingestion wins for the same open_ts (late revisions); ambiguous
    timestamps never override an unambiguous one.
    """
    best: dict[float, OHLCVBar] = {}
    for b in bars:
        if not b.complete:
            continue
        cur = best.get(b.open_ts)
        if cur is None:
            best[b.open_ts] = b
            continue
        cur_amb = cur.meta.timestamp_semantics == TimestampSemantics.AMBIGUOUS
        new_amb = b.meta.timestamp_semantics == TimestampSemantics.AMBIGUOUS
        if new_amb and not cur_amb:
            continue
        if b.meta.ingested_at >= cur.meta.ingested_at:
            best[b.open_ts] = b
    return [best[k] for k in sorted(best)]


def is_late_update(existing_source_ts: Optional[float], incoming_source_ts: Optional[float]) -> bool:
    """True when incoming data is older than what we already hold (out-of-order)."""
    if existing_source_ts is None or incoming_source_ts is None:
        return False
    return incoming_source_ts < existing_source_ts
