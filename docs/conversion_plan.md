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
