# Phase 3 ingestion slice — validation record

**Status: fixture-validated, NOT live-verified.** Live ingestion rollout is BLOCKED until
`scripts/verify_yahoo_chart_live.py` passes in a network-enabled environment. Public quote display stays denied
(default-deny entitlements); technical provider compatibility does not establish display/redistribution rights,
which must be verified separately. Phase 3 remains in progress. Not ready for full-market throughput (see #14).

Tested head: `bab4cef5e96cc66ba2a515c2f3e8ab27cf7abc7b`; base: `c32d10e8f6ac2ea68ca9a1d95b0c2455af861c4e`.

Everything below was actually run in this PR's environment (no network; Python 3.12). Nothing here is a
performance-improvement claim.

## 1. Branch vs base regression (isolated copies, local servers)
Base = `c32d10e` (master after PR #11, `git archive` copy); branch = working tree copy. Each ran in its own
throw-away directory with its own `CATALOG_DB_PATH`, with a uvicorn server started FROM THAT SAME COPY
(`python -m uvicorn app.main:app --host 127.0.0.1 --port 8001`, `REACT_APP_BACKEND_URL=http://127.0.0.1:8001`,
since several legacy tests default to a remote preview URL or `localhost:8001`), then
`python -m pytest -p no:cacheprovider backend/tests -q -rfE`.

| | base | branch |
|---|---|---|
| passed | 271 | 314 (+43 new Phase 3 tests) |
| failed | 31 | 31 |
| skipped | 1 | 1 |
| failing test IDs | identical set (`diff` empty) | |
| files rewritten under the copy by server+tests | 13 (`data/*`, `app/data/quote_cache/*`) | the same 13 |
| `GET /api/catalog/counts` | 200 | 200 |
| `GET /api/market/catalog` | 404 | 200 |

The 31 failures are pre-existing and environmental (e.g. hard-coded `/app/data/...`, `/app/frontend/app.js`,
universe-size expectations); they are not caused by this branch. A first attempt without a local server (tests
hitting the remote URL, offline) gave 76 failed/23 errors on both trees, also identical — that comparison was
discarded as untargeted. The legacy startup/scan machinery rewrites runtime files under `data/` on both trees
(pre-existing); this is why all runtime tests run in copies.

## 2. Real mounted app
`backend/tests/test_phase3_real_app.py` runs `scripts/probe_real_app.py` in an isolated copy: seeds via the CLI
(outside the app), starts `app.main` with its lifespan (startup + shutdown) through `TestClient`, GETs
`/api/market/catalog`, `/api/catalog/instruments`, `/api/catalog/counts`. Asserts: route served by the real app;
instruments/market-state tables are byte-identical before/after the GETs; the only files changed by
import/startup/shutdown are the four legacy runtime files (`data/known_bad_symbols.json`, `data/regulatory.db`,
`data/watchlists.db`, `data/wedge_watchdog.json`), none owned by this slice.

## 3. Schema ownership
Catalog keeps `PRAGMA user_version` (unchanged, still 2). Market-state tables are versioned by
`market_state_schema` (`app/ingestion/market_state_store.py`, `SCHEMA_VERSION = 1`), each migration in one
`BEGIN IMMEDIATE` transaction with its version row. Only the explicit ingestion path migrates; GETs never do.
Tested: upgrade from a Phase 2 DB (catalog rows and `user_version` preserved, idempotent, survives reopen),
rollback of a failing migration (no tables, version 0, later retry works), refusal of a newer market-state
version and of a newer catalog version (nothing changed).

## 4. Failover x ordering (provider rows are never merged)
Tested sequence: primary ok -> primary down + newer backup stored under `backup` (primary row untouched) ->
older backup rejected -> unknown-ts backup rejected -> equal-time different value `rejected_conflict` (stored
value kept; there is no automatic "correction" overwrite — a corrected same-timestamp value needs a source
revision, which this slice does not model) -> identical equal-time `duplicate` -> primary recovers and advances
while backup row stays. Failover is fetch-level only: a primary that answers with an older quote is an ordering
rejection, not a failover. Both providers returned by the API separately; no "best provider" selection exists.

## 5. yahoo-chart mapping
Live script side effects (inspected before running): read-only; one HTTPS GET per symbol to
`query1.finance.yahoo.com/v8/finance/chart/{symbol}` via `resilient_get` (`max_attempts=1`); no DB, file or
catalog writes; prints a report; exit 0 ok / 1 mismatch / 2 unreachable. Re-run at the tested head
(`python scripts/verify_yahoo_chart_live.py AAPL`): DNS resolution failure, exit 2 -> still PENDING. No live
fixtures were captured; none are claimed. When it can be run, validate symbol/venue label, currency, price and
`regularMarketTime` semantics, and only then add sanitized fixtures if the provider terms permit.

Deterministic: fixtures in `backend/tests/fixtures/ingestion/` follow the documented chart-API response shape;
they were hand-built and sanitized, NOT captured from the live provider. **Live validation is PENDING**:
`python scripts/verify_yahoo_chart_live.py` -> every symbol `UNREACHABLE` (DNS resolution failure in this
sandbox), exit 2. Venue aliases `NMS/NYQ/PCX -> XNAS/XNYS/ARCX` and the field names must be confirmed by an
operator running that script where the provider is reachable and permitted.

## 6. API matrix / leakage (`test_phase3_hardening.py`)
Missing quote (listed, `market_state: null`), zero, unavailable field, stale (value kept, state `stale`),
unsupported asset class, provider failure and "stopped ingestion" (identical responses before/after), denied
display (4 entitlement variants: body contains none of the prices, timestamps, provider name, currency, or
field names; key set of the item is fixed), partial entitlement (only the entitled provider appears), 400/422
errors carry no data, the router has exactly one GET route (no diagnostics/write surface), the ingestion package
and route import no scoring/prediction/provider modules (AST check), and a GET with fetch/write/migrate/socket
connect poisoned still returns 200.

## 7. Freshness policy
Documented and returned in each response as `freshness_policy: "age_only"`: `stale` = age of the provider
`source_ts` exceeds `max_age_seconds` (default 900). It is NOT session/holiday aware, so equities will show
`stale` overnight, on weekends and holidays. Session-aware freshness is deferred.

## 8. Measurements (`python scripts/measure_ingestion.py --n 500`, synthetic, one machine)
- First ingest of 500 instruments: 0.254 s, 500 provider calls, 502 sqlite row changes.
- Identical re-run: 0.062 s, 500 `duplicate`, 1 row change (run log only) -> idempotent, no data writes.
- `GET /api/market/catalog?limit=200`: p50 20.5 ms, p95 21.4 ms, max 40.2 ms (100 requests, in-process).
- Lock behaviour: 4 reader threads + writer doing 3 ingest runs: 585 reads, 0 errors, 0 lock failures; reader
  p95 89 ms / max 106 ms; the writer took 10.5 s vs ~0.25 s alone (per-quote transactions contend with readers
  / GIL). No lock errors, but ingestion throughput degrades under read load — a known limitation.
- Stability over 10 further ingest+GET cycles: RSS 64.5 -> 65.2 MB, fds 12 -> 12; WAL 4.3 MB / DB 0.9 MB.

## Follow-up issues
- #13 legacy startup/scan rewrites tracked runtime files (pre-existing on base).
- #14 ingestion slows under concurrent reads.

## 9. Security / review
`bandit -q -r app/ingestion app/routes/market_state.py scripts/probe_real_app.py`: no remaining findings (one
B608 in the probe script is a fixed table-name list, annotated). CodeQL and independent code review results are
listed in the PR description.

## Remaining limitations
Live provider validation pending; fixtures are synthetic; age-only freshness; single provider; no revision
model for equal-time corrections; ingestion is slow under concurrent reads; entitlement config is a string env
var with no audit trail; Yahoo public-endpoint terms unverified.
