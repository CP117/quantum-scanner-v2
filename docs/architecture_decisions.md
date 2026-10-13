# Architecture decisions and blockers

This record captures decisions supported by current repository evidence. It
does not pre-approve a wholesale rewrite or a technology choice.

## Decisions

### ADR-001 — Preserve the running application during Phase 0/1

**Decision:** Keep current FastAPI routes, scanner paths, algorithms, stores,
and launchers unchanged. Add documentation and test-only fixtures/helpers
before extracting or redirecting production behavior.

**Reason:** Current behavior has multiple callers and stateful provider/cache
paths; a parity baseline does not yet exist.

### ADR-002 — Treat fixture data as synthetic and isolated

**Decision:** Initial stock and crypto OHLCV examples use fixed synthetic
values, not copied quotes or credentials. Fixtures are consumed only by tests.
Baseline capture is opt-in and must not overwrite prior captured data.

**Reason:** The first parity increment should be deterministic, sanitized, and
non-destructive without requiring provider access.

### ADR-003 — Keep market, availability, and applicability distinct

**Decision:** Preserve stock/crypto identity and existing
unavailable/not-applicable states in future contracts; do not substitute
legitimate zeroes for missing data.

**Reason:** Current factor services already expose asset-specific or missing
data, including options behavior; flattening it would alter scores.

### ADR-004 — Defer database, queue, and transport selection

**Decision:** Do not add PostgreSQL, Redis queueing, SSE/WebSocket, or new
runtime dependencies in this increment.

**Reason:** Current requirements and launchers establish a single-process
application and existing persistence, but provide no measured workload,
deployment topology, recovery target, or operational ownership sufficient to
select replacement infrastructure.

## Blockers / questions for later phases

- What is the supported instrument universe, including venues, crypto pairs,
  instrument types, provider exclusions, and licensing entitlements?
- Which deployment environments and operating-system constraints must the
  scheduler/workers support?
- What durable database, backup, retention, and legacy-state import policy is
  approved?
- What provider call budgets, market-data rights, and freshness semantics are
  available?
- Which exact baseline workloads and measurement environment are representative?
- Which model outputs are product commitments versus research/diagnostic data?

Until answered, preserve uncertainty and avoid claiming complete coverage,
performance targets, or validated prediction accuracy.
