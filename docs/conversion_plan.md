# Catalog-first conversion plan

This plan is incremental. Existing algorithms, data files, and production
routes remain authoritative until an explicitly tested replacement exists.

## Phase 0 — Audit and decisions

**This increment:** document current runtime paths, factors, prediction systems,
ranking behavior, parity gaps, and unresolved decisions. The documents are an
audit snapshot, not a declaration that the current implementation satisfies
the target architecture.

## Phase 1 — Baselines and parity protection

**Initial scaffolding in this increment:** add fixed, synthetic stock and
crypto OHLCV fixtures; add test-only canonicalization and opt-in baseline
capture helpers. Capture must be explicitly enabled, never overwrite an
existing baseline, and must not call a live provider or production route.

Next, create baseline cases from controlled inputs for:

1. Core score components and missing/degraded-input behavior.
2. Full-depth family scores, composite, and directional fields.
3. Filter/preset and sort behavior (including null/missing metrics).
4. Stock versus crypto applicability and provider fallback states.
5. Actual prediction paths and horizons, with confidence semantics preserved.

Use exact comparisons for stable serialized fields and explicit numeric
tolerances only where observed floating-point behavior requires them. Do not
change formulas, ranking behavior, or live data paths in the parity phase.

## Subsequent increments

1. **Canonical catalog:** define stable instrument/venue identities and
   idempotent membership sync independently of quotes and scan results.
2. **Ingestion boundary:** normalize provider data with source and ingestion
   timestamps, interval, currency, venue, and quality; establish provider
   entitlement and failure semantics.
3. **Versioned engine boundary:** extract pure/bounded factor and prediction
   contracts behind adapters while preserving existing behavior.
4. **Queue and durable results:** choose persistence and queue only after
   deployment, operational, and workload requirements are known. Add bounded,
   idempotent background jobs and versioned results.
5. **Catalog query API:** query the catalog independently of analysis state;
   add stable pagination, explicit null semantics, and coverage/status fields.
6. **Frontend transition:** incrementally present the catalog and analysis
   freshness without requiring a full scan.
7. **Measured rollout:** shadow compare, migrate a limited cohort, verify
   rollback, and retire legacy paths only after parity and operational gates.

## Safety gates

- No destructive migration or rewrite of existing `data/` or `app/data/`.
- No replacement or tuning of mathematical algorithms in parity changes.
- A market-data fixture must be synthetic/sanitized and provider-independent.
- A new catalog row must remain visible when quotes or analysis are missing.
- Do not set latency, coverage, or accuracy targets until representative
  baselines are actually measured.
- A database, queue, event transport, and deployment topology remain decisions
  for a later increment; see [architecture decisions](architecture_decisions.md).

## Phase 2 status (catalog foundation) — PENDING VALIDATION

Additive only: `app/services/catalog_service.py`, `app/routes/catalog.py`
(read-only `/api/catalog/instruments`, `/api/catalog/counts`),
`app/services/catalog_seed.py` (operator command), `CATALOG_DB_PATH` setting,
and the `backend/tests/test_catalog_*.py` tests. Nothing in the scan/result
pipeline reads or writes the catalog. Identity and rename continuity:
[catalog_identity.md](catalog_identity.md). Database decision:
[architecture decisions](architecture_decisions.md).

Seeding (explicit, never from HTTP or startup):
`python -m app.services.catalog_seed --market stocks` (dry run) and
`... --apply`. Venue comes only from the legacy `exchange` field; crypto rows
are skipped because the legacy universe has no real venue/pair identity.

Validation record (sandbox, no CI yet): catalog + parity-scaffolding tests
56 passed. Full `backend/tests` against a live local server (port 8001, temp
catalog/watchlist DBs): 43 failed / 246 passed / 6 errors; the pre-Phase-2
commit (145409e) under the same conditions: 40 failed / 200 passed / 6 errors.
The failing set is the legacy live-server/network/data-state tests (no
outbound network in the sandbox); the only extra failures in that run were
`test_stock_share_populated` and `test_tier_state_store::test_promote_updates_store`
(both pass/fail depending on server snapshot and on-disk tier state; the latter
passes in isolation) and an overlapping-connection race in the new catalog
test, which was a real bug and is fixed. No repository lint/type/security gate
is configured; ad-hoc ruff/mypy/bandit on the new files show no correctness
issues (remaining: bandit B608 false positives on static SQL fragments, ruff
B008 FastAPI `Depends` idiom). Scaffolding tests are not algorithm parity.

Remaining before Phase 2 can be marked complete: CI run of the full suite in
the intended environment (the six live-server integration modules need a
running server and network), maintainer review of the diff, quote/analysis
coverage counts, automatic rename detection, venue alias table, crypto
venue/pair ingestion, and the production database decision.
