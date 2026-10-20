"""Phase 3 ingestion boundary tests (pure; no provider calls)."""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from app.ingestion.contracts import (  # noqa: E402
    AdjustmentMode, DataKind, DataState, Field, IncompatibleSeriesError, IngestionMeta,
    OHLCVBar, TimestampSemantics, assert_compatible)
from app.ingestion import resilience as r  # noqa: E402
from app.ingestion import semantics as s  # noqa: E402
from app.ingestion.rights import Entitlement, EntitlementRegistry  # noqa: E402


def meta(**kw):
    kw.setdefault('provider', 'p1')
    kw.setdefault('kind', DataKind.BAR)
    return IngestionMeta(**kw)


def test_state_distinctions():
    assert Field.of(0).state is DataState.ZERO and Field.of(0).usable
    assert Field.of(None).state is DataState.UNAVAILABLE and not Field.of(None).usable
    assert Field.unsupported('x').state is DataState.UNSUPPORTED
    st = Field.of(5.0, max_age_s=10, age_s=60)
    assert st.state is DataState.STALE and st.value == 5.0 and not st.usable
    assert Field.of(5.0).state is DataState.OK
    assert Field.of(False).state is DataState.OK


def test_no_silent_blend():
    assert_compatible(meta(), meta())
    with pytest.raises(IncompatibleSeriesError):
        assert_compatible(meta(), meta(provider='p2'))
    with pytest.raises(IncompatibleSeriesError):
        assert_compatible(meta(adjustment=AdjustmentMode.RAW),
                          meta(adjustment=AdjustmentMode.SPLIT_ADJUSTED))


def bar(ts, complete, ing, sem=TimestampSemantics.EXCHANGE_UTC, close=1.0):
    return OHLCVBar(meta(ingested_at=ing, timestamp_semantics=sem), 'A', ts, 1, 1, 1, close,
                    Field.of(0), complete)


def test_finalize_bars_drops_forming_orders_and_dedupes():
    out = s.finalize_bars([bar(2, True, 1), bar(1, True, 1), bar(3, False, 1),
                           bar(2, True, 5, close=9.0),
                           bar(1, True, 9, sem=TimestampSemantics.AMBIGUOUS, close=7.0)])
    assert [b.open_ts for b in out] == [1, 2]
    assert out[1].close == 9.0 and out[0].close == 1.0


def test_bar_completeness_and_late_update():
    assert not s.bar_is_complete(100, 60, 150) and s.bar_is_complete(100, 60, 160)
    assert s.is_late_update(10, 5) and not s.is_late_update(10, 11)
    assert not s.is_late_update(None, 5)


def test_sessions():
    sat = datetime(2024, 6, 1, 15, tzinfo=timezone.utc)
    assert s.equity_session(sat).reason == 'weekend'
    hol = datetime(2024, 7, 4, 15, tzinfo=timezone.utc)
    assert s.equity_session(hol, holidays=[date(2024, 7, 4)]).reason == 'holiday'
    wed = datetime(2024, 6, 5, 15, tzinfo=timezone.utc)
    assert s.equity_session(wed).is_open
    c = s.crypto_session(sat)
    assert c.is_open and c.continuous


def test_batching_bounded():
    assert [len(x) for x in r.batched(list(range(10)), 4)] == [4, 4, 2]
    assert max(len(x) for x in r.batched(list(range(1000)), 10**6)) == r.MAX_BATCH
    assert r.backoff_delay(100) == r.MAX_BACKOFF_S


def test_retry_bounds_and_health():
    now = [0.0]
    clock = lambda: now[0]  # noqa: E731
    budget = r.RateLimitBudget(100, 1.0, clock)
    health = r.ProviderHealth(failure_threshold=10, clock=clock)
    calls, sleeps = [], []

    def boom():
        calls.append(1)
        raise RuntimeError('x')

    with pytest.raises(r.FetchRefused):
        r.fetch_with_retry(boom, budget=budget, health=health, max_retries=99, sleep=sleeps.append)
    assert len(calls) == r.MAX_RETRIES + 1 and len(sleeps) == r.MAX_RETRIES


def test_budget_and_circuit():
    now = [0.0]
    clock = lambda: now[0]  # noqa: E731
    b = r.RateLimitBudget(2, 10.0, clock)
    assert b.try_acquire() and b.try_acquire() and not b.try_acquire()
    now[0] = 11
    assert b.try_acquire()
    h = r.ProviderHealth(2, 5.0, clock)
    h.record_failure(); h.record_failure()
    assert not h.available
    now[0] = 17
    assert h.available
    h.record_success()
    assert h.consecutive_failures == 0


def test_rights_default_deny():
    reg = EntitlementRegistry()
    assert not reg.may_display_publicly('p1', DataKind.QUOTE)
    reg.register(Entitlement('p1', DataKind.QUOTE, public_display=True))  # unverified
    assert not reg.may_display_publicly('p1', DataKind.QUOTE)
    reg.register(Entitlement('p1', DataKind.QUOTE, public_display=True, verified_by='ops'))
    assert reg.may_display_publicly('p1', DataKind.QUOTE)
    assert not reg.may_redistribute('p1', DataKind.QUOTE)
