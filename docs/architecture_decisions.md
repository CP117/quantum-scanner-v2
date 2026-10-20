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

## ADR: Catalog database (Phase 2, provisional)

**Decision:** The durable instrument catalog uses a dedicated SQLite file
(`CATALOG_DB_PATH`, default `data/catalog.db`), separate from every legacy
store. This is provisional: it matches the repository's existing SQLite
persistence (watchlists) and the single-host deployment, without committing to
the production database (still an open blocker below).

**Properties verified by tests** (`backend/tests/test_catalog_service.py`):
foreign keys are enabled per connection (`PRAGMA foreign_keys=ON`; SQLite does
not persist it); schema is versioned with `PRAGMA user_version` and a newer
version is refused; each sync is a single `BEGIN IMMEDIATE` transaction so a
failure rolls back all data and is recorded as a `failed` run afterwards;
`busy_timeout` (5 s) plus `BEGIN IMMEDIATE` serialise overlapping writers across
threads/processes; the path is resolved per call (explicit argument, else
settings, relative paths anchored at the repo root); each caller uses its own
connection (WAL mode for file DBs); tests use temporary DB paths only.

**Migration boundary:** all SQL lives in `app/services/catalog_service.py`
behind functions that take a connection and use portable SQL apart from
`INSERT OR IGNORE`, `ON CONFLICT` upserts, `PRAGMA` and `LIKE ... ESCAPE`.
Instrument IDs are opaque strings and sync-run IDs are integers, so a move to
PostgreSQL is a schema port plus a one-time copy; the identity strategy does not
change. No legacy file under `data/` or `app/data/` is read or modified by the
catalog. A `.db` file created at runtime must not be committed.
Limits: a single writer at a time; multi-host deployment or high write
concurrency would trigger the PostgreSQL port.

**Status:** Phase 2 remains *pending validation*; see the Phase 2 section of
the conversion plan for open gates.

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
