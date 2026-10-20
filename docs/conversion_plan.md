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

Validation record (sandbox; still PENDING VALIDATION — no CI run yet).

*Branch/base regression comparison (after the concurrency fix).* Method: for
each run a fresh isolated copy of the tree (branch = working tree, base =
`git archive 145409e`) so each has its own `data/` and `app/data/`; catalog and
watchlist DBs in the copy; identical dependencies (`requirements.txt`
versions, one interpreter); a separate uvicorn server per run on port 8001
(the legacy tests hard-code it) started from that copy, gated on "Application
startup complete" + HTTP 200 and verified by `/proc/<pid>/cwd`; JUnit XML
compared by test ID. Two rounds, alternating order (base→branch, branch→base):

| Round | Base | Branch (excluding new catalog tests) | Branch-only differences |
|---|---|---|---|
| A | 200 pass / 46 fail / 1 skip | 200 / 46 / 1 | 0 by test ID |
| B | 200 pass / 46 fail / 1 skip | 200 / 46 / 1 | 0 by test ID |

The 46 failures are identical in both trees: legacy live-server/network/data
tests (no outbound network, no `/app/data` path, empty crypto universe).
Catalog tests (56 collected in the suite): 56 pass in both rounds, also with
the live server running.

Earlier, un-gated runs showed branch-only differences
(`test_stock_share_populated`, `test_manual_refresh_returns_row`,
`test_snapshot_mirror_legacy_fast_path`, and 7 connection-refused failures).
Classification: harness flake — those failures were `ConnectionRefused` while
the server was not yet listening (a later un-gated run produced 11 such
failures on *base* instead), and they vanished in both readiness-gated rounds
in both orders. Note: this was not dismissed because the tests pass alone; it
was reproduced as a function of server readiness, not of the code under test.
`test_tier_state_store::test_promote_updates_store` was an order/state failure
in one un-gated run only and passed in all four gated runs.

*Static checks (no repo-configured lint/type/security gate exists).*
- ruff (E9,F,B): B008 ×4 (FastAPI `Depends` defaults — framework idiom, left);
  B904 fixed. mypy (`--ignore-missing-imports`): no findings in catalog files
  (findings elsewhere are pre-existing). bandit: 2×B608 inspected individually —
  both concatenate only fixed clause literals with `?` placeholders or a
  hard-coded column name; no user input reaches SQL text. Annotated `# nosec B608`
  with that reasoning; B110 (silent except) replaced by a logged warning.
- CodeQL (python): 0 alerts on the previous diff (scope: the changed Python
  files, i.e. catalog service/seed/route and tests); re-run on the final diff: 0 alerts (python).
- Code review tool: retried on the final diff, still unavailable (model-registry error, no review produced);
  maintainer review is still required.

*Mechanisms tested.* Symbol relink (`link_succession`): chain resolution,
history retained, second successor for one old ID rejected, direct/indirect
cycles rejected with state unchanged, persistence across restart. Venue alias
contract (schema v2 `venue_alias`, `add_venue_alias`, `resolve_venue`):
cross-provider resolution to one instrument, provider scoping, no implicit
canonical alias, conflicting re-point rejected, unresolved alias rolls back the
run, v1→v2 migration without data loss.

*Not claimed.* Catalog counts are `cataloged` instruments only. They say
nothing about quote, analysis or prediction coverage, and the catalog holds no
crypto at all (legacy crypto rows lack venue/pair identity and are skipped), so
no whole-market or crypto coverage is claimed. Scaffolding tests are not
algorithm parity.

Deferred / blocking: CI run in the intended environment; maintainer review;
**shipped alias data and its review are a blocking prerequisite for live
multi-provider ingestion**; automatic rename detection; crypto venue/pair
ingestion; quote/analysis coverage counts; production database decision.

## Next bounded slice (proposal, after Phase 2 acceptance)

Ingestion slice: catalog mapping → normalized market data → durable latest
market state → read-only API.
1. Select a small stock cohort already in the catalog; require
   `provider_instrument_mapping` + reviewed `venue_alias` rows for one provider.
2. Normalized quote contract (provider, source/ingest timestamps, currency,
   venue, quality, explicit unavailable/stale/zero distinction); no blending of
   providers.
3. `latest_market_state` table keyed by `instrument_id`, written by a
   standalone ingestion command/worker with monotonic source-timestamp rule
   (older updates rejected) and idempotent upsert.
4. Read-only `/api/market-state` joined to the catalog (LEFT JOIN: instruments
   without a quote stay visible with null, not zero).
5. Independent stop switches: ingestion worker and any analysis worker are
   separate processes/flags; browsing works with both stopped; tests prove it.
Out of scope: scoring, predictions, frontend replacement, crypto.
