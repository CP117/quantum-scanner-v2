# Current architecture (Phase 0 audit)

## Runtime shape

The repository is a Python/FastAPI application with a static HTML/CSS/JavaScript
frontend. `start.sh` and `start.bat` launch one Uvicorn application process
(`app.main:app`); startup code also starts scanner and maintenance threads/tasks
inside that process. This is a modular monolith, not a set of independently
deployable API, scheduler, and worker processes.

```text
start.sh / start.bat
        |
        v
Uvicorn -> app.main FastAPI + lifespan
        |         |-- routers: /stocks/results, search, detail, snapshot, etc.
        |         |-- startup background scan loop and tier scanners
        |         `-- maintenance / warmup tasks
        v
universe + provider adapters -> quote/history enrichment -> scoring_service
        |                                               |-> result/snapshot stores
        `-----------------------------------------------'         |
                                                                  v
                                                   frontend/app.js polling
```

The implementation has tiered scanner services, but those are in-process
orchestration rather than a durable job queue. `app.main` mounts routers and
starts the snapshot loop and tier scanners during import/startup. The scanner
uses provider data and calls `score_symbol_rows`; results are held in scanner,
snapshot, and tier/cache state and exposed through read routes.

## Request and scoring paths

- `GET /stocks/results` is implemented by `app/routes/results.py` and delegates
  filtering, sorting, and paging to `app/services/result_store.py`.
- `GET /api/scan/snapshot` is implemented by `app/routes/snapshot.py`; it reads
  the server-side snapshot and supports a `market`, row limit, compact shape,
  and only `score` or `predicted_volume_intensity` sort values.
- The scanner's `score_symbol_rows` in `scoring_service.py` acquires quotes for
  stock and `-USD` crypto symbols, runs a cheap score pass, and optionally
  reruns a subset at full depth. Full-depth scoring invokes the extended-factor
  pipeline and may use real options data for selected active-scan symbols.
- The core score is made from momentum, quality, trend, and stability plus an
  exit-risk penalty. Full-depth scoring blends the core score with the average
  of seven extended-family scores, then applies a predictive modifier and
  clamps the result. Exact formulas and limitations are recorded in
  [factor catalog](factor_catalog.md).

## Data, persistence, and providers

The repository contains JSON universe/provider caches and local persistent
state, including SQLite databases and JSON/JSONL files under `data/` and
`app/data/`. These are not a normalized, durable instrument-catalog schema
independent of scan results. `tier_manager.py`, snapshot/result stores, cache
services, and tracker services have separate responsibilities and persistence
mechanisms; no single versioned result/projection store is identified.

Provider integration includes yfinance and crypto-specific provider code
(including CryptoCompare, CoinGecko, and CoinPaprika paths). Provider selection
and availability depend on runtime configuration and active-market behavior.
Provider entitlements, complete universe coverage, call budgets, and data
redistribution rights are not established by the code audit.

## Frontend and test setup

The frontend is served as static files and uses `frontend/app.js`; its
`package.json` defines only a Node proxy start command, not a frontend test or
build script. Python tests are in `backend/tests/` and use pytest. Some are
deterministic unit tests; several scanner feature tests require a running
backend and external market data.

## Architectural gaps relevant to migration

- Universe membership and scan completion are not yet cleanly separated by a
  catalog-backed query model.
- Provider ingestion, scoring, scheduling, and projection updates are coupled
  through current services and process-local scanner execution.
- No canonical instrument ID / venue / provider-mapping contract was found.
- There is no independent durable job queue or independently operated worker
  process in the current launch path.
- Existing tests do not establish full algorithm, prediction, or performance
  parity against sanitized baseline outputs.

These findings describe current code, not a claim that every route or provider
has been exhaustively measured. See [decisions and blockers](architecture_decisions.md).
