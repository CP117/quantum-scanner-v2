"""Explicit ingestion service: provider -> adapter -> normalized Quote -> store.

Only invoked by the operator command (``python -m app.ingestion.cli``) or
tests; never by GET routes or app startup. Analytics are not touched.
"""
from __future__ import annotations

import dataclasses
import time
from collections import Counter
from typing import Callable, Optional, Sequence

from app.services import catalog_service as cs

from . import market_state_store as store
from .contracts import DataKind
from .resilience import (FetchRefused, ProviderHealth, RateLimitBudget, batched, fetch_with_failover)
from .yahoo_chart_adapter import UnusablePayload, adapt_quote


@dataclasses.dataclass(frozen=True)
class Source:
    """One provider in an explicit, ordered failover chain."""
    name: str
    fetch_raw: Callable[[str], dict]
    adapt: Callable = adapt_quote
    budget: Optional[RateLimitBudget] = None
    health: Optional[ProviderHealth] = None


def run_ingestion(conn, sources: Sequence[Source], provider_symbols: Sequence[str], *,
                  batch_size: int = 25, max_retries: int = 2,
                  clock: Callable[[], float] = time.time,
                  sleep: Callable[[float], None] = time.sleep) -> dict:
    """Ingest quotes for ``provider_symbols`` (symbols of ``sources[0]``'s
    namespace; each source's mapping is resolved via the catalog). Failures
    never touch the catalog or previously stored quotes."""
    store.ensure_schema(conn)
    started = clock()
    budgets = {s.name: s.budget or RateLimitBudget(1000, 1.0) for s in sources}
    healths = {s.name: s.health or ProviderHealth() for s in sources}
    results: Counter = Counter()
    detail: dict[str, str] = {}
    primary = sources[0].name
    for batch in batched(list(provider_symbols), batch_size):
        for sym in batch:
            ids = cs.resolve_provider_symbol(conn, primary, sym)
            if len(ids) != 1:
                results['unmapped' if not ids else 'ambiguous'] += 1
                detail[sym] = 'unmapped' if not ids else 'ambiguous'
                continue
            iid = ids[0]
            inst = conn.execute('SELECT venue_id, asset_class FROM instrument WHERE instrument_id=?',
                                (iid,)).fetchone()
            if inst['asset_class'] != 'equity':
                results['unsupported_asset_class'] += 1
                detail[sym] = 'unsupported_asset_class'
                continue
            chain = []
            for s in sources:
                psyms = [r['provider_symbol'] for r in conn.execute(
                    'SELECT provider_symbol FROM provider_instrument_mapping WHERE provider=? AND instrument_id=?',
                    (s.name, iid))]
                if len(psyms) == 1:
                    chain.append((s.name, (lambda s=s, p=psyms[0]: _fetch_and_adapt(conn, s, p, iid, inst, clock))))
            if not chain:
                results['unmapped'] += 1
                detail[sym] = 'unmapped'
                continue
            try:
                fo = fetch_with_failover(chain, budgets=budgets, healths=healths,
                                         max_retries=max_retries, sleep=sleep)
            except FetchRefused as exc:
                results['provider_failed'] += 1
                detail[sym] = str(exc)
                continue
            q = fo.value
            outcome = store.apply_quote(conn, q)
            results[outcome] += 1
            detail[sym] = f'{outcome} via {fo.provider}' + (' (failover)' if fo.failed_over else '')
    summary = {'counts': dict(results), 'detail': detail}
    store.record_run(conn, primary, started, clock(), summary)
    return summary


def _fetch_and_adapt(conn, source: Source, provider_symbol: str, iid: str, inst, clock):
    now = clock()
    quote, venue_label = source.adapt(source.fetch_raw(provider_symbol), now_ts=now)
    venue = cs.resolve_venue(conn, source.name, venue_label)
    if venue is None:
        raise UnusablePayload(f'unresolved venue alias {venue_label!r}')
    if venue != inst['venue_id']:
        raise UnusablePayload(f'venue mismatch: payload {venue} vs catalog {inst["venue_id"]}')
    meta = dataclasses.replace(quote.meta, provider=source.name, venue=venue)
    return dataclasses.replace(quote, meta=meta, instrument_id=iid)
